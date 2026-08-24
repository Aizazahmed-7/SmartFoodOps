"""Domain-level money artifacts: rows, balanced ledger pairs, events,
idempotency convergence — everything HTTP can't see."""

import pytest
import sqlalchemy as sa
from payment.adapters.repo import PaymentRepo
from payment.db import idempotency_keys, ledger, metadata, outbox, payments
from payment.domain.ports import GatewayResult, PspStateConflict, PspUnavailable
from payment.domain.service import (
    PaymentService,
    PaymentStateConflict,
)
from smartfood_idempotency import IdempotencyStore
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _service(gateway):
    """Bare PaymentService over in-memory sqlite. The scripted `gateway`
    is conftest's fixture: importlib test mode only stops test MODULES
    from importing one another — conftest FIXTURES are the sanctioned
    sharing channel, so the fake travels by injection instead of by a
    per-file copy."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = PaymentService(sessions, gateway, IdempotencyStore(sessions, idempotency_keys))
    return service, sessions


async def _authorize(service, order_id="ord_1", amount=3446):
    return await service.authorize(
        order_id, amount_cents=amount, currency="USD", card_token="tok_ok"
    )


async def test_authorize_approved_writes_row_event_and_completion(gateway):
    service, sessions = await _service(gateway)
    outcome = await _authorize(service)
    assert (outcome.status_code, outcome.replayed) == (200, False)
    assert outcome.body["status"] == "AUTHORIZED"
    assert outcome.body["payment_intent_id"] == "psp_abc"

    async with sessions() as s:
        row = (await s.execute(sa.select(payments))).one()
        event = (await s.execute(sa.select(outbox))).one()
        idem = (await s.execute(sa.select(idempotency_keys))).one()
        entries = (await s.execute(sa.select(ledger))).all()
    assert (row.status, row.amount_cents, row.payment_intent_id) == ("AUTHORIZED", 3446, "psp_abc")
    assert event.id == event_id("payment", "ord_1", 0, "PaymentAuthorized")
    assert idem.status == "COMPLETE" and idem.idem_key == "ord_1:auth"
    assert entries == []  # a hold is NOT a money movement — no ledger rows


async def test_authorize_replay_returns_stored_no_second_psp_call(gateway):
    service, _ = await _service(gateway)
    first = await _authorize(service)
    replay = await _authorize(service)
    assert replay.replayed and replay.body == first.body
    assert len(gateway.authorize_calls) == 1  # the PSP saw exactly one attempt


async def test_decline_is_stored_and_replayed_as_402(gateway):
    service, sessions = await _service(gateway)
    gateway.script = [GatewayResult(False, "psp_x")]
    first = await _authorize(service)
    assert first.status_code == 402
    assert first.body["error"]["code"] == "PAYMENT_DECLINED"
    replay = await _authorize(service)
    assert replay.replayed and replay.status_code == 402 and replay.body == first.body
    async with sessions() as s:
        row = (await s.execute(sa.select(payments))).one()
        events = (await s.execute(sa.select(outbox))).all()
    assert row.status == "DECLINED"
    assert events == []  # declines emit no PaymentAuthorized


async def test_psp_unavailable_releases_key_then_retry_converges(gateway):
    """FR-22 end to end: timeout → 503; the retry reuses the SAME gateway
    key, the (fake) PSP approves, exactly one payment exists."""
    service, sessions = await _service(gateway)
    gateway.script = [PspUnavailable("down"), GatewayResult(True, "psp_abc")]
    with pytest.raises(PspUnavailable):
        await _authorize(service)
    outcome = await _authorize(service)  # re-executes: key was released
    assert outcome.status_code == 200 and not outcome.replayed
    assert [c[0] for c in gateway.authorize_calls] == ["ord_1:auth", "ord_1:auth"]
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(payments))).scalar_one()
    assert count == 1


async def test_existing_row_convergence_on_reexecution(monkeypatch, gateway):
    """Expired-key re-execution long after a committed attempt: the INSERT
    collides and we converge on the stored truth instead of crashing."""
    service, sessions = await _service(gateway)
    await _authorize(service)
    # wipe the idempotency row to simulate 24h expiry + takeover
    async with sessions() as s:
        await s.execute(idempotency_keys.delete())
        await s.commit()
    outcome = await _authorize(service)
    assert outcome.status_code == 200
    assert outcome.body["status"] == "AUTHORIZED"
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(payments))).scalar_one()
    assert count == 1  # still exactly one payment


async def test_capture_moves_money_with_a_balanced_pair(gateway):
    service, sessions = await _service(gateway)
    await _authorize(service)
    outcome = await service.capture("ord_1")
    assert outcome.body == {"order_id": "ord_1", "status": "CAPTURED"}
    async with sessions() as s:
        row = (await s.execute(sa.select(payments))).one()
        entries = (await s.execute(sa.select(ledger))).all()
        event_types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert row.status == "CAPTURED" and row.version == 1
    assert len(entries) == 2
    assert sum(e.debit_cents for e in entries) == sum(e.credit_cents for e in entries) == 3446
    by_account = {e.account: e for e in entries}
    assert by_account["customer"].debit_cents == 3446
    assert by_account["platform_cash"].credit_cents == 3446
    assert all(e.op_key == "ord_1:capture" for e in entries)
    assert event_types == ["PaymentAuthorized", "PaymentCaptured"]
    # amount came from the stored row — the caller never sent one
    assert gateway.lifecycle_calls == [("capture", "ord_1:capture", "psp_abc")]


async def test_capture_replay_posts_no_second_ledger_pair(gateway):
    service, sessions = await _service(gateway)
    await _authorize(service)
    await service.capture("ord_1")
    replay = await service.capture("ord_1")
    assert replay.replayed
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(ledger))).scalar_one()
    assert count == 2  # one pair, forever


async def test_capture_retry_beyond_the_replay_ttl_replays_from_state(gateway):
    """A capture retry arriving after the idempotency window (>24h; a very
    slow Temporal retry) finds an at-target row: that is its OWN success —
    200 CAPTURED, no gateway call, no second ledger pair — never a 409
    that would fail a workflow whose money is already in the till."""
    service, sessions = await _service(gateway)
    await _authorize(service)
    await service.capture("ord_1")
    async with sessions() as s:
        await s.execute(idempotency_keys.delete())  # simulate TTL takeover
        await s.commit()
    late = await service.capture("ord_1")
    assert late.status_code == 200
    assert late.body["status"] == "CAPTURED"
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(ledger))).scalar_one()
    assert count == 2  # still one pair
    assert [c[0] for c in gateway.lifecycle_calls].count("capture") == 1  # PSP untouched


async def test_void_retry_beyond_the_replay_ttl_replays_from_state(gateway):
    """Same state-replay rule for the compensations (uniform idiom)."""
    service, sessions = await _service(gateway)
    await _authorize(service)
    await service.void("ord_1")
    async with sessions() as s:
        await s.execute(idempotency_keys.delete())
        await s.commit()
    late = await service.void("ord_1")
    assert late.status_code == 200
    assert late.body["status"] == "VOIDED"


async def test_refund_writes_the_reversing_pair(gateway):
    service, sessions = await _service(gateway)
    await _authorize(service)
    await service.capture("ord_1")
    outcome = await service.refund("ord_1")
    assert outcome.body["status"] == "REFUNDED"
    async with sessions() as s:
        entries = (await s.execute(sa.select(ledger).order_by(ledger.c.created_at))).all()
        event_types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    refund_entries = [e for e in entries if e.op_key == "ord_1:refund"]
    by_account = {e.account: e for e in refund_entries}
    assert by_account["platform_cash"].debit_cents == 3446  # mirror image
    assert by_account["customer"].credit_cents == 3446
    assert "RefundProcessed" in event_types
    # the books balance globally: every debit has its credit
    assert sum(e.debit_cents for e in entries) == sum(e.credit_cents for e in entries)


async def test_void_releases_hold_no_ledger_no_event(gateway):
    service, sessions = await _service(gateway)
    await _authorize(service)
    outcome = await service.void("ord_1")
    assert outcome.body["status"] == "VOIDED"
    async with sessions() as s:
        ledger_count = (
            await s.execute(sa.select(sa.func.count()).select_from(ledger))
        ).scalar_one()
        event_types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert ledger_count == 0
    assert event_types == ["PaymentAuthorized"]  # no void event (plan)


async def test_lifecycle_state_conflicts_release_the_key(gateway):
    service, _ = await _service(gateway)
    with pytest.raises(PaymentStateConflict):
        await service.capture("ord_ghost")  # no auth at all
    await _authorize(service, "ord_ghost")  # key was released — this works
    outcome = await service.capture("ord_ghost")
    assert outcome.status_code == 200

    # void after capture: wrong state
    with pytest.raises(PaymentStateConflict):
        await service.void("ord_ghost")


async def test_lifecycle_psp_down_releases_key_then_retry_succeeds(gateway):
    service, sessions = await _service(gateway)
    await _authorize(service)
    gateway.fail_lifecycle = PspUnavailable("down")
    with pytest.raises(PspUnavailable):
        await service.capture("ord_1")
    gateway.fail_lifecycle = None
    outcome = await service.capture("ord_1")  # key released — re-executes
    assert outcome.status_code == 200
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(ledger))).scalar_one()
    assert count == 2  # exactly one pair despite the failed attempt


async def test_concurrent_money_op_raises_in_progress(gateway):
    from payment.domain.service import MONEY_SCOPE, MoneyOpInProgress, _op_hash
    from smartfood_idempotency import IdempotencyStore

    service, sessions = await _service(gateway)
    await _authorize(service)
    # another worker holds the capture key right now (same op hash)
    store = IdempotencyStore(sessions, idempotency_keys)
    await store.reserve(MONEY_SCOPE, "ord_1:capture", _op_hash("capture", "ord_1"))
    with pytest.raises(MoneyOpInProgress):
        await service.capture("ord_1")


async def test_declined_row_convergence_describes_the_402(gateway):
    """Expired-key re-execution against a DECLINED row replays the decline."""
    service, sessions = await _service(gateway)
    gateway.script = [GatewayResult(False, "psp_x")]
    await _authorize(service)
    async with sessions() as s:  # simulate 24h key expiry + takeover
        await s.execute(idempotency_keys.delete())
        await s.commit()
    outcome = await _authorize(service)
    assert outcome.status_code == 402
    assert outcome.body["error"]["code"] == "PAYMENT_DECLINED"


async def test_psp_state_conflict_maps_to_payment_state_conflict(gateway):
    service, _ = await _service(gateway)
    await _authorize(service)
    gateway.fail_lifecycle = PspStateConflict("psp says no")
    with pytest.raises(PaymentStateConflict):
        await service.capture("ord_1")


async def test_transition_race_converges_on_winner(monkeypatch, gateway):
    """Between our state read and the guarded UPDATE, another instance
    captured: our 0-row update converges on the winner's state."""
    service, sessions = await _service(gateway)
    await _authorize(service)

    real_transition = PaymentRepo.transition_payment
    fired = {"n": 0}

    async def racing_transition(self, order_id, *, expected, target, now):
        if fired["n"] == 0:
            fired["n"] += 1
            async with sessions() as other:  # the winner commits first
                await real_transition(
                    PaymentRepo(other), order_id, expected=expected, target="CAPTURED", now=now
                )
                await other.commit()
        return await real_transition(self, order_id, expected=expected, target=target, now=now)

    monkeypatch.setattr(PaymentRepo, "transition_payment", racing_transition)
    outcome = await service.capture("ord_1")
    assert outcome.body["status"] == "CAPTURED"  # converged, no crash
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(ledger))).scalar_one()
    assert count == 0  # the loser did NOT double-post the pair


# ── the PSP forgot the ref (found live: in-memory mock-psp restart) ──


async def test_void_converges_when_psp_has_no_record_of_the_auth(gateway):
    """A hold the PSP does not know cannot capture money: void's goal is
    vacuously true, so it converges to VOIDED — row tells the truth after,
    no ledger movement (a released hold moved nothing, even a vacuous one)."""
    from payment.domain.ports import PspUnknownRef

    service, sessions = await _service(gateway)
    await _authorize(service)
    gateway.fail_lifecycle = PspUnknownRef("unknown psp_ref")
    outcome = await service.void("ord_1")
    assert outcome.body["status"] == "VOIDED"
    async with sessions() as s:
        row = (await s.execute(sa.select(payments))).one()
        entries = (await s.execute(sa.select(ledger))).all()
    assert row.status == "VOIDED" and entries == []


async def test_capture_and_refund_stay_loud_when_psp_has_no_record(gateway):
    """Money movement against a forgotten ref = the books disagree about
    MONEY: non-retryable conflict, row untouched, a human reconciles."""
    from payment.domain.ports import PspUnknownRef

    service, sessions = await _service(gateway)
    await _authorize(service)
    gateway.fail_lifecycle = PspUnknownRef("unknown psp_ref")
    with pytest.raises(PaymentStateConflict):
        await service.capture("ord_1")
    async with sessions() as s:
        row = (await s.execute(sa.select(payments))).one()
    assert row.status == "AUTHORIZED"  # nothing pretended to move money
