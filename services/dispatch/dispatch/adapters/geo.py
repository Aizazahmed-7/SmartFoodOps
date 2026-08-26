"""The candidate index — Redis GEO plus the liveness keys (FR-27's shape).

Three keys per cell, written by whoever ingests GPS (the rider-gateway at
1 Hz; the REST status endpoint at go-online) and read here:

  sfo:geo:{cell}              GEO set        rider positions
  sfo:loc:{cell}:{rider}      "lat,lon"      TTL 30s — the freshest fix
  sfo:hb:{cell}:{rider}       "1"            TTL 90s — proof of life

The SAME spellings live in rider-gateway's ingest (services may not import
each other — the layer contract); a drift between the two is caught by the
live stack immediately, and the comment on each side points at the other.

This is an INDEX, not the truth (ADR-0011): the lock authority is DDB.
The index being wrong costs a wasted reserve attempt (the conditional
write refuses) or a missed candidate (the sweep of the next attempt finds
them) — never a double assignment. Search filters on the heartbeat key:
a rider whose phone died 90s ago is a ghost the cascade must not court.
"""

from typing import Any


def geo_key(cell: str) -> str:
    return f"sfo:geo:{cell}"


def loc_key(cell: str, rider_id: str) -> str:
    return f"sfo:loc:{cell}:{rider_id}"


def hb_key(cell: str, rider_id: str) -> str:
    return f"sfo:hb:{cell}:{rider_id}"


LOC_TTL_S = 30
HB_TTL_S = 90


class RiderGeo:
    def __init__(self, redis: Any, *, cell: str):
        self._r = redis
        self._cell = cell

    async def update(self, rider_id: str, lat: float, lon: float) -> None:
        """One position fix: index + freshest-loc + liveness, one pipeline."""
        pipe = self._r.pipeline(transaction=False)
        pipe.geoadd(geo_key(self._cell), (lon, lat, rider_id))  # redis speaks lon-first
        pipe.set(loc_key(self._cell, rider_id), f"{lat},{lon}", ex=LOC_TTL_S)
        pipe.set(hb_key(self._cell, rider_id), "1", ex=HB_TTL_S)
        await pipe.execute()

    async def remove(self, rider_id: str) -> None:
        """Going offline removes the pin — an offline rider must not even
        appear as a candidate for the conditional write to refuse."""
        pipe = self._r.pipeline(transaction=False)
        pipe.zrem(geo_key(self._cell), rider_id)
        pipe.delete(loc_key(self._cell, rider_id))
        pipe.delete(hb_key(self._cell, rider_id))
        await pipe.execute()

    async def latest(self, rider_id: str) -> tuple[float, float] | None:
        raw = await self._r.get(loc_key(self._cell, rider_id))
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        lat_s, _, lon_s = text.partition(",")
        return float(lat_s), float(lon_s)

    async def search(
        self, lat: float, lon: float, *, radius_km: float, exclude: set[str]
    ) -> list[tuple[str, float]]:
        """(rider_id, distance_m) inside the radius, heartbeat-alive,
        nearest first. Excluded riders (already offered and missed this
        cascade) never reappear as candidates."""
        found = await self._r.geosearch(
            geo_key(self._cell),
            longitude=lon,
            latitude=lat,
            radius=radius_km,
            unit="km",
            withdist=True,
            sort="ASC",
        )
        candidates: list[tuple[str, float]] = []
        for member, dist_km in found:
            rider_id = member.decode() if isinstance(member, bytes) else str(member)
            if rider_id in exclude:
                continue
            candidates.append((rider_id, float(dist_km) * 1000.0))
        if not candidates:
            return []
        pipe = self._r.pipeline(transaction=False)
        for rider_id, _ in candidates:
            pipe.exists(hb_key(self._cell, rider_id))
        alive = await pipe.execute()
        return [pair for pair, beat in zip(candidates, alive, strict=True) if beat]
