"""DispatchService over moto stores + fake ports: the cascade's brain,
every outcome value, every race resolution."""

import json

import pytest
from dispatch.adapters.events import DispatchEvents
from dispatch.domain.service import DispatchService, rider_channel

PICKUP = (39.7912, -89.6644)
DROPOFF = (39.8025, -89.6478)


class FakeGeo:
    """A scripted index: positions in, candidates out — no Redis."""

    def __init__(self):
        self.positions: dict[str, tuple[float, float]] = {}
        self.removed: list[str] = []

    async def update(self, rider_id, lat, lon):
        self.positions[rider_id] = (lat, lon)

    async def remove(self, rider_id):
        self.positions.pop(rider_id, None)
        self.removed.append(rider_id)

    async def latest(self, rider_id):
        return self.positions.get(rider_id)

    async def search(self, lat, lon, *, radius_km, exclude):
        from dispatch.domain.scoring import haversine_m

        found = []
        for rider_id, (rlat, rlon) in self.positions.items():
            if rider_id in exclude:
                continue
            distance = haversine_m(lat, lon, rlat, rlon)
            if distance <= radius_km * 1000:
                found.append((rider_id, distance))
        return sorted(found, key=lambda pair: pair[1])


class FakeBus:
    def __init__(self, boom: bool = False):
        self.frames: list[tuple[str, dict]] = []
        self._boom = boom

    async def publish(self, channel, data):
        if self._boom:
            raise RuntimeError("bus down")
        self.frames.append((channel, json.loads(data)))


class FakeCourier:
    def __init__(self, outcome="ok"):
        self.sent: list[tuple[str, str, str]] = []
        self._outcome = outcome

    async def send(self, order_id, *, event, rider_id):
        self.sent.append((order_id, event, rider_id))
        return self._outcome


class RecordingEvents(DispatchEvents):
    def __init__(self):
        super().__init__(None, topic="t", cell_id="c1")
        self.emitted: list[tuple[str, str]] = []

    async def _emit(self, event_type, *, aggregate_type, aggregate_id, marker, payload):
        self.emitted.append((str(event_type), aggregate_id))


@pytest.fixture()
def world(riders, deliveries):
    geo = FakeGeo()
    bus = FakeBus()
    courier = FakeCourier()
    events = RecordingEvents()
    service = DispatchService(
        riders=riders,
        deliveries=deliveries,
        geo=geo,
        bus=bus,
        courier_events=courier,
        events=events,
        rider_cap=1,
        search_radius_km=3.0,
        widened_radius_km=6.0,
        widen_after_misses=3,
        offer_first_timeout_s=15.0,
        offer_next_timeout_s=12.0,
    )
    return service, geo, bus, courier, events


async def _online(service, rider_id, lat=39.7920, lon=-89.6640):
    await service.go_online(rider_id, lat=lat, lon=lon)


async def _offer(service, order_id="ord_1", attempt=1, exclude=()):
    return await service.find_and_offer(
        order_id,
        user_id="usr_1",
        restaurant_name="Biryani House",
        pickup=PICKUP,
        dropoff=DROPOFF,
        attempt=attempt,
        exclude=set(exclude),
    )


# ── presence + the REST floor ──────────────────────────────────────


async def test_online_offline_maintain_pin_and_events(world):
    service, geo, _, _, events = world
    await _online(service, "r1")
    assert "r1" in geo.positions
    me = await service.me("r1")
    assert me == {"status": "online", "offer": None, "delivery": None}
    await service.go_offline("r1")
    assert geo.removed == ["r1"]
    assert [e[0] for e in events.emitted] == ["RiderOnline", "RiderOffline"]


async def test_me_for_a_never_seen_rider_is_calmly_offline(world):
    service, *_ = world
    assert await service.me("ghost") == {"status": "offline", "offer": None, "delivery": None}


# ── the cascade step ───────────────────────────────────────────────


async def test_offer_goes_to_the_nearest_and_pushes_a_frame(world):
    service, _, bus, _, _ = world
    await _online(service, "r_far", lat=39.8050, lon=-89.6350)  # ~2.6 km out
    await _online(service, "r_near")  # ~100 m out
    result = await _offer(service)
    assert result["outcome"] == "offered" and result["rider_id"] == "r_near"
    assert result["timeout_s"] == 15.0  # first attempt uses the long window
    ((channel, frame),) = bus.frames
    assert channel == rider_channel("r_near")
    assert frame["type"] == "offer" and frame["order_id"] == "ord_1"
    assert frame["restaurant_name"] == "Biryani House"
    me = await service.me("r_near")
    assert me["offer"]["order_id"] == "ord_1"  # the REST floor sees it too


async def test_cascade_skips_excluded_and_locked_riders(world):
    service, *_ = world
    await _online(service, "r1")
    await _online(service, "r2", lat=39.7930, lon=-89.6630)
    first = await _offer(service, "ord_1")
    # r1 (nearest) got locked by ord_1 — ord_2 must land on r2.
    second = await _offer(service, "ord_2")
    assert {first["rider_id"], second["rider_id"]} == {"r1", "r2"}
    # Everyone excluded → no candidates.
    third = await _offer(service, "ord_3", exclude=("r1", "r2"))
    assert third == {"outcome": "no_candidates"}


async def test_widened_attempts_reach_farther_and_use_short_timeout(world):
    service, *_ = world
    await _online(service, "r_far", lat=39.820, lon=-89.615)  # ~5.3 km — outside 3, inside 6
    assert (await _offer(service, attempt=1))["outcome"] == "no_candidates"
    late = await _offer(service, attempt=4)
    assert late["outcome"] == "offered" and late["timeout_s"] == 12.0


async def test_offer_survives_a_dead_push_bus(riders, deliveries):
    geo = FakeGeo()
    service = DispatchService(
        riders=riders,
        deliveries=deliveries,
        geo=geo,
        bus=FakeBus(boom=True),
        courier_events=FakeCourier(),
        events=RecordingEvents(),
        rider_cap=1,
        search_radius_km=3.0,
        widened_radius_km=6.0,
        widen_after_misses=3,
        offer_first_timeout_s=15.0,
        offer_next_timeout_s=12.0,
    )
    await _online(service, "r1")
    result = await _offer(service)
    assert result["outcome"] == "offered"  # push is latency, not correctness
    assert (await service.me("r1"))["offer"] is not None  # the poll floor holds


async def test_unarmed_bus_is_a_quiet_noop(riders, deliveries):
    geo = FakeGeo()
    service = DispatchService(
        riders=riders,
        deliveries=deliveries,
        geo=geo,
        bus=None,
        courier_events=FakeCourier(),
        events=RecordingEvents(),
        rider_cap=1,
        search_radius_km=3.0,
        widened_radius_km=6.0,
        widen_after_misses=3,
        offer_first_timeout_s=15.0,
        offer_next_timeout_s=12.0,
    )
    await _online(service, "r1")
    assert (await _offer(service))["outcome"] == "offered"


# ── accept / expire and their race ─────────────────────────────────


async def test_accept_assigns_and_tells_the_workflow(world):
    service, _, _, courier, events = world
    await _online(service, "r1")
    offer = await _offer(service)
    outcome = await service.accept_offer("r1", offer_id=offer["offer_id"], order_id="ord_1")
    assert outcome == "assigned"
    assert courier.sent == [("ord_1", "accepted", "r1")]
    assert ("RiderAssigned", "ord_1") in events.emitted
    me = await service.me("r1")
    assert me["offer"] is None and me["delivery"]["state"] == "ASSIGNED"


async def test_expire_revokes_and_notifies_the_rider(world):
    service, _, bus, _, _ = world
    await _online(service, "r1")
    offer = await _offer(service)
    result = await service.expire_offer("ord_1", offer_id=offer["offer_id"], rider_id="r1")
    assert result == {"outcome": "revoked"}
    assert bus.frames[-1][1] == {"type": "offer_revoked", "offer_id": offer["offer_id"]}
    # The lock is free again — the same rider can be courted for another order.
    assert (await _offer(service, "ord_2"))["outcome"] == "offered"


async def test_expire_after_accept_reports_the_assignment(world):
    """The lost-signal self-heal: the timer's revoke discovers the accept
    already won and hands the workflow the rider it didn't hear about."""
    service, *_ = world
    await _online(service, "r1")
    offer = await _offer(service)
    await service.accept_offer("r1", offer_id=offer["offer_id"], order_id="ord_1")
    result = await service.expire_offer("ord_1", offer_id=offer["offer_id"], rider_id="r1")
    assert result == {"outcome": "already_assigned", "rider_id": "r1"}


async def test_late_accept_answers_expired(world):
    service, *_ = world
    await _online(service, "r1")
    offer = await _offer(service)
    await service.expire_offer("ord_1", offer_id=offer["offer_id"], rider_id="r1")
    outcome = await service.accept_offer("r1", offer_id=offer["offer_id"], order_id="ord_1")
    assert outcome == "expired"


# ── the drive ──────────────────────────────────────────────────────


async def _assigned(service, rider="r1", order="ord_1"):
    await _online(service, rider)
    offer = await _offer(service, order)
    await service.accept_offer(rider, offer_id=offer["offer_id"], order_id=order)
    return offer


async def test_pickup_then_deliver_frees_the_slot(world):
    service, _, _, courier, events = world
    await _assigned(service)
    assert await service.picked_up("r1", order_id="ord_1") == "ok"
    assert await service.delivered("r1", order_id="ord_1") == "ok"
    assert [s[1] for s in courier.sent] == ["accepted", "picked_up", "delivered"]
    assert ("RiderDeliveryCompleted", "ord_1") in events.emitted
    # Slot free: a new order can court r1 again.
    assert (await _offer(service, "ord_2"))["outcome"] == "offered"


async def test_wrong_rider_taps_answer_conflict(world):
    service, *_ = world
    await _assigned(service)
    assert await service.picked_up("r9", order_id="ord_1") == "conflict"
    assert await service.delivered("r1", order_id="ord_1") == "conflict"  # not picked up


async def test_unassign_stalled_frees_rider_and_reverts(world):
    service, _, bus, _, _ = world
    await _assigned(service)
    result = await service.unassign_stalled("ord_1", rider_id="r1")
    assert result == {"outcome": "revoked"}
    assert bus.frames[-1][1]["type"] == "assignment_revoked"
    assert (await _offer(service, "ord_2"))["outcome"] == "offered"  # slot freed


async def test_unassign_loses_to_a_completed_pickup(world):
    service, *_ = world
    await _assigned(service)
    await service.picked_up("r1", order_id="ord_1")
    assert await service.unassign_stalled("ord_1", rider_id="r1") == {
        "outcome": "already_picked_up"
    }


async def test_cancel_frees_an_assigned_rider_and_keeps_picked_up(world):
    service, _, bus, _, _ = world
    await _assigned(service)
    assert (await service.cancel("ord_1"))["outcome"] == "cancelled"
    assert bus.frames[-1][1]["type"] == "assignment_revoked"
    await _assigned(service, order="ord_2")
    await service.picked_up("r1", order_id="ord_2")
    assert await service.cancel("ord_2") == {"outcome": "kept", "state": "PICKED_UP"}
    assert await service.cancel("ord_ghost") == {"outcome": "kept", "state": "absent"}


# ── the customer's dot ─────────────────────────────────────────────


async def test_courier_position_is_ownership_scoped(world):
    service, geo, *_ = world
    await _assigned(service)
    view = await service.courier_position("ord_1", caller_sub="usr_1")
    assert view["state"] == "ASSIGNED"
    assert view["lat"] == pytest.approx(39.7920)
    assert await service.courier_position("ord_1", caller_sub="usr_intruder") is None
    assert await service.courier_position("ord_ghost", caller_sub="usr_1") is None


async def test_courier_position_with_a_stale_fix_keeps_state(world):
    service, geo, *_ = world
    await _assigned(service)
    geo.positions.pop("r1")  # the 30s latest-loc TTL lapsed
    view = await service.courier_position("ord_1", caller_sub="usr_1")
    assert view["state"] == "ASSIGNED" and view["lat"] is None


async def test_taps_are_replay_tolerant(world):
    """A rider whose tap 503'd on the notify leg retries: the DDB move
    already applied, so the retry must answer success (and re-raise the
    idempotent signal) — never 409 a rider for succeeding."""
    service, _, _, courier, _ = world
    offer = await _assigned(service)
    assert await service.accept_offer("r1", offer_id=offer["offer_id"], order_id="ord_1") == (
        "assigned"  # accept replay
    )
    await service.picked_up("r1", order_id="ord_1")
    assert await service.picked_up("r1", order_id="ord_1") == "ok"  # pickup replay
    await service.delivered("r1", order_id="ord_1")
    assert await service.delivered("r1", order_id="ord_1") == "ok"  # deliver replay
    # The intruder still conflicts — replay tolerance is rider-scoped.
    assert await service.picked_up("r9", order_id="ord_1") == "conflict"
    assert [s[1] for s in courier.sent].count("accepted") == 2  # the re-raised signal


async def test_double_expire_is_calmly_revoked(world):
    """Two expiry paths race (a retried activity): the second finds no
    lock and an OFFERING row — same answer, no drama."""
    service, *_ = world
    await _online(service, "r1")
    offer = await _offer(service)
    await service.expire_offer("ord_1", offer_id=offer["offer_id"], rider_id="r1")
    again = await service.expire_offer("ord_1", offer_id=offer["offer_id"], rider_id="r1")
    assert again == {"outcome": "revoked"}
