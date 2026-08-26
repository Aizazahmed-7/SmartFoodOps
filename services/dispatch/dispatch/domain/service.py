"""DispatchService — the decisions, over the guarded writes.

Orchestration only: every mutation below is one of the store's conditional
writes (ADR-0011), every lost race is a VALUE the caller branches on, and
the service never holds state of its own — restart it freely.

The stores are sync boto3; this service lives on the event loop, so every
store call crosses via asyncio.to_thread (clients are thread-safe, calls
are single-digit ms). The GEO index, the offer-frame bus, the courier
client and the event emitter are all injected ports — unit tests hand in
fakes and moto."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from smartfood_otel import get_logger

from ..adapters.events import DispatchEvents
from ..adapters.rider_store import ASSIGNED, OFFERING, DeliveryStore, RiderStore
from .scoring import Candidate, radius_km_for, rank

# The courier-relay vocabulary and its transport failure live in the
# DOMAIN (the api layer may import domain, never adapters — the layer
# contract); the HTTP adapter imports them from here.
CourierEvent = Literal["accepted", "picked_up", "delivered"]


class OrderUnavailable(Exception):
    """Order service unreachable — the API surface answers 503 + Retry-After."""


log = get_logger("dispatch.service")


class OfferBus(Protocol):
    """Push seam toward the rider-gateway (Redis pub/sub). Offers are
    DIRECTED WORK ITEMS, not truth broadcasts — the payload rides the
    frame (the hints-only rule governs state fan-out, not commands), and
    the REST floor (`/v1/rider/me`) carries the same facts for any rider
    whose socket is down."""

    async def publish(self, channel: str, data: str) -> None: ...


def rider_channel(rider_id: str) -> str:
    """The one place the rider push channel is spelled (gateway subscribes
    to exactly this — see rider-gateway's relay)."""
    return f"sfo:rider:{rider_id}"


class CourierEvents(Protocol):
    """The order-service relay (adapters/order_client.py satisfies it) —
    structural so tests fake it without HTTP."""

    async def send(self, order_id: str, *, event: CourierEvent, rider_id: str) -> str: ...


class GeoIndex(Protocol):
    async def update(self, rider_id: str, lat: float, lon: float) -> None: ...
    async def remove(self, rider_id: str) -> None: ...
    async def latest(self, rider_id: str) -> tuple[float, float] | None: ...
    async def search(
        self, lat: float, lon: float, *, radius_km: float, exclude: set[str]
    ) -> list[tuple[str, float]]: ...


class DispatchService:
    def __init__(
        self,
        *,
        riders: RiderStore,
        deliveries: DeliveryStore,
        geo: GeoIndex,
        bus: OfferBus | None,
        courier_events: CourierEvents,
        events: DispatchEvents,
        rider_cap: int,
        search_radius_km: float,
        widened_radius_km: float,
        widen_after_misses: int,
        offer_first_timeout_s: float,
        offer_next_timeout_s: float,
    ):
        self._riders = riders
        self._deliveries = deliveries
        self._geo = geo
        self._bus = bus
        self._courier = courier_events
        self._events = events
        self._cap = rider_cap
        self._radius = search_radius_km
        self._widened = widened_radius_km
        self._widen_after = widen_after_misses
        self._first_timeout = offer_first_timeout_s
        self._next_timeout = offer_next_timeout_s

    # ── presence (the rider surface) ───────────────────────────────

    async def go_online(self, rider_id: str, *, lat: float, lon: float) -> None:
        await asyncio.to_thread(self._riders.set_online, rider_id)
        await self._geo.update(rider_id, lat, lon)
        await self._events.rider_online(rider_id, session_marker=datetime.now(UTC).isoformat())

    async def go_offline(self, rider_id: str) -> None:
        """Presence off + pin removed. An active delivery is deliberately
        NOT dropped: going offline mid-job is a liveness problem (the
        workflow's pickup timer or heartbeat expiry revokes), not a
        voluntary handback — FR-32's escalation shapes stay intact."""
        await asyncio.to_thread(self._riders.set_offline, rider_id)
        await self._geo.remove(rider_id)
        await self._events.rider_offline(rider_id, session_marker=datetime.now(UTC).isoformat())

    async def me(self, rider_id: str) -> dict[str, Any]:
        """The REST floor: everything the rider app needs, pollable —
        presence, the live offer (if the lock holds one), the active job."""
        state = await asyncio.to_thread(self._riders.get, rider_id) or {}
        offer: dict[str, Any] | None = None
        lock = state.get("offer_lock")
        if lock is not None:
            row = await asyncio.to_thread(self._deliveries.get, lock["order_id"])
            if (
                row is not None
                and row.get("state") == OFFERING
                and row.get("offer_id") == lock["offer_id"]
            ):
                offer = self._offer_view(row)
        active: dict[str, Any] | None = None
        for order_id in state.get("active_deliveries", []):
            row = await asyncio.to_thread(self._deliveries.get, order_id)
            if row is not None and row.get("state") in (ASSIGNED, "PICKED_UP"):
                active = self._job_view(row)
                break
        return {"status": state.get("status", "offline"), "offer": offer, "delivery": active}

    # ── the cascade (the workflow surface) ─────────────────────────

    async def find_and_offer(
        self,
        order_id: str,
        *,
        user_id: str,
        restaurant_name: str,
        pickup: tuple[float, float],
        dropoff: tuple[float, float],
        attempt: int,
        exclude: set[str],
    ) -> dict[str, Any]:
        """One cascade step: search → rank → first reservable candidate
        gets the lock, the deliveries row, and the push frame. The
        conditional write is the arbiter — a candidate who got locked by
        a concurrent cascade simply refuses, and the NEXT candidate is
        tried (no retry storms, no waiting)."""
        radius = radius_km_for(
            attempt,
            base_km=self._radius,
            widened_km=self._widened,
            widen_after=self._widen_after,
        )
        nearby = await self._geo.search(pickup[0], pickup[1], radius_km=radius, exclude=exclude)
        candidates: list[Candidate] = []
        for rider_id, distance_m in nearby[:8]:  # stats-fetch bound
            state = await asyncio.to_thread(self._riders.get, rider_id) or {}
            candidates.append(
                Candidate(
                    rider_id=rider_id,
                    distance_m=distance_m,
                    offers_made=int(state.get("offers_made", 0)),
                    offers_accepted=int(state.get("offers_accepted", 0)),
                )
            )
        timeout = self._first_timeout if attempt == 1 else self._next_timeout
        for candidate in rank(candidates):
            offer_id = f"off_{uuid.uuid4().hex[:20]}"
            reserved = await asyncio.to_thread(
                self._riders.reserve,
                candidate.rider_id,
                offer_id=offer_id,
                order_id=order_id,
                cap=self._cap,
            )
            if not reserved:
                continue
            await asyncio.to_thread(
                self._deliveries.put_offering,
                order_id,
                rider_id=candidate.rider_id,
                offer_id=offer_id,
                user_id=user_id,
                restaurant_name=restaurant_name,
                pickup=pickup,
                dropoff=dropoff,
                attempt=attempt,
            )
            await self._push(
                candidate.rider_id,
                {
                    "type": "offer",
                    "offer_id": offer_id,
                    "order_id": order_id,
                    "restaurant_name": restaurant_name,
                    "pickup": {"lat": pickup[0], "lon": pickup[1]},
                    "dropoff": {"lat": dropoff[0], "lon": dropoff[1]},
                    "expires_in_s": timeout,
                },
            )
            log.info(
                "offer made",
                order_id=order_id,
                rider_id=candidate.rider_id,
                offer_id=offer_id,
                attempt=attempt,
                radius_km=radius,
            )
            return {
                "outcome": "offered",
                "offer_id": offer_id,
                "rider_id": candidate.rider_id,
                "timeout_s": timeout,
            }
        return {"outcome": "no_candidates"}

    async def expire_offer(self, order_id: str, *, offer_id: str, rider_id: str) -> dict[str, Any]:
        """The 15s timer fired. Release IF this offer still holds the
        lock; a refusal means the accept converted it first — read the
        row and tell the workflow the truth it missed (the lost-signal
        self-heal from the plan's race matrix)."""
        released = await asyncio.to_thread(self._riders.release_offer, rider_id, offer_id=offer_id)
        if released:
            await self._push(rider_id, {"type": "offer_revoked", "offer_id": offer_id})
            return {"outcome": "revoked"}
        row = await asyncio.to_thread(self._deliveries.get, order_id)
        if row is not None and row.get("state") in (ASSIGNED, "PICKED_UP", "DELIVERED"):
            return {"outcome": "already_assigned", "rider_id": str(row["rider_id"])}
        # The lock is simply gone (a prior expiry raced us) — same answer.
        return {"outcome": "revoked"}

    async def unassign_stalled(self, order_id: str, *, rider_id: str) -> dict[str, Any]:
        """The pickup-deadline revoke (FR-30/32): conditional on the
        expected rider still owning an un-picked-up job. A completed
        pickup wins the race and the workflow proceeds to delivery."""
        reverted = await asyncio.to_thread(self._deliveries.unassign, order_id, rider_id=rider_id)
        if not reverted:
            return {"outcome": "already_picked_up"}
        await asyncio.to_thread(self._riders.finish_delivery, rider_id, order_id=order_id)
        await self._push(rider_id, {"type": "assignment_revoked", "order_id": order_id})
        log.warning("assignment revoked — no pickup in time", order_id=order_id, rider=rider_id)
        return {"outcome": "revoked"}

    async def cancel(self, order_id: str) -> dict[str, Any]:
        """The order died while dispatch was working it (customer cancel,
        or the no-rider deadline). Free whatever is held, tell the rider."""
        row = await asyncio.to_thread(self._deliveries.get, order_id)
        cancelled = await asyncio.to_thread(self._deliveries.cancel, order_id)
        if not cancelled:
            state = (row or {}).get("state", "absent")
            return {"outcome": "kept", "state": str(state)}  # picked up / delivered / absent
        if row is not None and "rider_id" in row:
            rider_id = str(row["rider_id"])
            await asyncio.to_thread(
                self._riders.release_offer, rider_id, offer_id=str(row.get("offer_id", ""))
            )
            await asyncio.to_thread(self._riders.finish_delivery, rider_id, order_id=order_id)
            await self._push(rider_id, {"type": "assignment_revoked", "order_id": order_id})
        return {"outcome": "cancelled"}

    # ── the rider's taps ───────────────────────────────────────────

    async def accept_offer(self, rider_id: str, *, offer_id: str, order_id: str) -> str:
        """Lock → assignment (one conditional write each side), then the
        workflow hears about it. Returns a value: assigned | expired.

        REPLAY-TOLERANT (the transitions.py idiom): a retry of an accept
        that already applied — including one whose only failure was the
        courier notify (a 503 the rider app retried) — finds the row
        ASSIGNED to this rider under this offer and answers success again,
        re-raising the idempotent signal it may have lost."""
        converted = await asyncio.to_thread(
            self._riders.accept, rider_id, offer_id=offer_id, order_id=order_id
        )
        if not converted:
            row = await asyncio.to_thread(self._deliveries.get, order_id)
            if (
                row is not None
                and str(row.get("rider_id")) == rider_id
                and str(row.get("offer_id")) == offer_id
                and row.get("state") in (ASSIGNED, "PICKED_UP", "DELIVERED")
            ):
                await self._courier.send(order_id, event="accepted", rider_id=rider_id)
                return "assigned"
            return "expired"
        await asyncio.to_thread(self._deliveries.assign, order_id, offer_id=offer_id)
        await self._events.rider_assigned(order_id, rider_id=rider_id, offer_id=offer_id)
        await self._courier.send(order_id, event="accepted", rider_id=rider_id)
        log.info("offer accepted", order_id=order_id, rider_id=rider_id)
        return "assigned"

    async def picked_up(self, rider_id: str, *, order_id: str) -> str:
        moved = await asyncio.to_thread(
            self._deliveries.mark_picked_up, order_id, rider_id=rider_id
        )
        if not moved and not await self._is_replay(order_id, rider_id, ("PICKED_UP", "DELIVERED")):
            return "conflict"
        await self._courier.send(order_id, event="picked_up", rider_id=rider_id)
        return "ok"

    async def delivered(self, rider_id: str, *, order_id: str) -> str:
        moved = await asyncio.to_thread(
            self._deliveries.mark_delivered, order_id, rider_id=rider_id
        )
        if not moved and not await self._is_replay(order_id, rider_id, ("DELIVERED",)):
            return "conflict"
        await asyncio.to_thread(self._riders.finish_delivery, rider_id, order_id=order_id)
        await self._events.delivery_completed(order_id, rider_id=rider_id)
        await self._courier.send(order_id, event="delivered", rider_id=rider_id)
        log.info("delivered", order_id=order_id, rider_id=rider_id)
        return "ok"

    async def _is_replay(self, order_id: str, rider_id: str, states: tuple[str, ...]) -> bool:
        """A refused transition whose row is already AT (or past) the
        target under the SAME rider is this rider's own retry — answer
        success and re-raise the idempotent signal, never a 409 for an
        action that worked. (Signals collapse; finish_delivery replays as
        a no-op False; the events emitter dedupes by deterministic id.)"""
        row = await asyncio.to_thread(self._deliveries.get, order_id)
        return (
            row is not None and str(row.get("rider_id")) == rider_id and row.get("state") in states
        )

    # ── the customer's map dot ─────────────────────────────────────

    async def courier_position(self, order_id: str, *, caller_sub: str) -> dict[str, Any] | None:
        """Ownership in the lookup: only the order's customer may follow
        its courier — anyone else (and any unknown order) gets the same
        None → 404. Position comes from the 30s latest-loc key; a stale
        fix answers state without coordinates (the FE keeps the last dot)."""
        row = await asyncio.to_thread(self._deliveries.get, order_id)
        if row is None or str(row.get("user_id")) != caller_sub:
            return None
        position = await self._geo.latest(str(row["rider_id"])) if "rider_id" in row else None
        return {
            "state": str(row.get("state")),
            "lat": position[0] if position else None,
            "lon": position[1] if position else None,
            "pickup": {"lat": row.get("pickup_lat"), "lon": row.get("pickup_lon")},
            "dropoff": {"lat": row.get("dropoff_lat"), "lon": row.get("dropoff_lon")},
        }

    # ── plumbing ───────────────────────────────────────────────────

    async def _push(self, rider_id: str, frame: dict[str, Any]) -> None:
        """Best-effort by contract: the REST floor carries the same facts,
        so a dead bus costs push latency, never correctness."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(rider_channel(rider_id), json.dumps(frame))
        except Exception as exc:
            log.warning("offer push dropped — the rider's poll floor holds", error=str(exc))

    def _offer_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "offer_id": str(row["offer_id"]),
            "order_id": str(row["order_id"]),
            "restaurant_name": str(row.get("restaurant_name", "")),
            "pickup": {"lat": row.get("pickup_lat"), "lon": row.get("pickup_lon")},
            "dropoff": {"lat": row.get("dropoff_lat"), "lon": row.get("dropoff_lon")},
        }

    def _job_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**self._offer_view(row), "state": str(row["state"])}
