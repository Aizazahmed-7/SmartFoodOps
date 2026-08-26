"""Candidate scoring — pure arithmetic over what the index reported.

FR-28 names four inputs (pickup ETA, food-wait, utilization, detour); the
buildable-today pair is distance (the ETA proxy until OSRM, FR-33/P1) and
acceptance rate (riders who ghost offers sink). Weights are constants
tonight; "hot-reloadable per cell" is the named upgrade, and the FUNCTION
SHAPE — a total order over candidates — is the part that persists.

No clock, no I/O: `radius_km_for` takes the attempt number, `rank` takes
measurements. Same discipline as hours.py and the pricing engine.
"""

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0

# The acceptance nudge: a chronic decliner must yield to a slightly
# farther rider who actually takes jobs — but distance stays dominant
# (food cools by meters, not by manners).
_ACCEPTANCE_WEIGHT = 0.5


@dataclass(frozen=True)
class Candidate:
    rider_id: str
    distance_m: float
    offers_made: int = 0
    offers_accepted: int = 0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle meters — the ETA proxy (streets come with OSRM)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def acceptance_rate(offers_made: int, offers_accepted: int) -> float:
    """A NEW rider scores as a perfect accepter on purpose: the cold-start
    penalty would otherwise lock fresh riders out of the very offers that
    would build their history."""
    if offers_made <= 0:
        return 1.0
    return min(1.0, offers_accepted / offers_made)


def score(candidate: Candidate) -> float:
    """Lower is better: effective distance, stretched for decliners."""
    rate = acceptance_rate(candidate.offers_made, candidate.offers_accepted)
    return candidate.distance_m * (1.0 + _ACCEPTANCE_WEIGHT * (1.0 - rate))


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Deterministic total order: score, then id (two riders on identical
    scores must sort the same way on every node — ties broken by luck
    would make the cascade's replay non-reproducible)."""
    return sorted(candidates, key=lambda c: (score(c), c.rider_id))


def radius_km_for(attempt: int, *, base_km: float, widened_km: float, widen_after: int) -> float:
    """FR-29's widening: the first `widen_after` attempts search the base
    radius; every attempt after that searches wide."""
    return base_km if attempt <= widen_after else widened_km
