# 0040 — Refund notifications run as a Temporal workflow

**Status**: Accepted (2026-09-15) — retires the `order_recipients`
projection and the `ProjectionLag` retry it existed to drive. Partially
reverses [ADR-0025](0025-side-effects-ride-a-task-queue.md), which names
"the recipients join" as an example of a projection riding the log.

## Context

Order events carry `user_id` by contract, so every notification the system
sends is minted straight from its event — except one. Payment events are
keyed by order and carry no `user_id`:

```python
payload={"order_id": …, "status": "AUTHORIZED", "amount_cents": …,
         "currency": …, "payment_intent_id": result.psp_ref}
```

Exactly one payment event notifies anyone: `RefundProcessed` →
"Refund on its way". To serve it, **every** order event upserted an
`order_recipients` row — roughly six writes per order to arm a join used
by the single message the payments topic produces. A refund arriving
before its order's events raised `ProjectionLag`, and the consumer runtime
retried until the projection appeared.

The disproportion was the trigger for revisiting it: a table, a write on
every order event, an exception class and a retry path, for one message.

## Decision

1. The payments handler **starts `RefundNotificationWorkflow`** and
   returns. It no longer writes the notification itself.
2. The workflow runs two activities:
   - `fetch_recipients` asks order `GET /v1/internal/orders/{id}/recipients`
     (SystemOnly, new). `schedule_to_close` bounds the **whole** retry
     chain at `recipients_lookup_timeout_seconds` (1h default), so an order
     outage is ridden out rather than amplified.
   - `write_notification` mints the bell entry, conflict-ignored on the
     deterministic id.
   - Two activities, not one, because the failure modes differ: a
     cross-service call that may retry for an hour, and a local insert that
     either works or means the database is gone.
3. **Workflow id is `ntf::refund::{event_id}`** with `REJECT_DUPLICATE`.
   The event id is unique per emitted fact, so a Kafka redelivery addresses
   the same execution — carrying the exactly-once property the
   deterministic notification id used to carry alone.
4. A **404 from order is non-retryable** (`LookupError`); transport trouble
   and 5xx raise `OrderUnavailable` and retry. An order that does not exist
   never will, and polling it for an hour learns nothing.
5. `order_recipients`, `upsert_recipients`, `get_recipients` and
   `ProjectionLag` are deleted. Order events now do nothing but mint their
   own drafts.
6. **Disarmed without `temporal_address`**: a refund event is consumed and
   skipped rather than failing the loop — the same shape as an unset
   `celery_broker_url` disarming receipts, so a dev stack without Temporal
   still consumes.

## Consequences

### Positive

- Removes a table, a write on every order event, and a retry path that
  existed to paper over cross-topic ordering.
- An **order outage now delays the notification instead of DLQ-ing it**.
  Under the projection, a refund whose order events never arrived parked
  after the backoff horizon; the workflow keeps trying for an hour.
- The two consumer loops remain separate, now for the reason that always
  independently justified them: separate offsets mean a poison event on
  one topic cannot stall the other.

### Negative

- **Notification is now a Temporal client and runs a worker process** — a
  fourth runtime concern beside FastAPI, Kafka consumers and Celery, and
  the third retry substrate in one service. Anyone debugging a missing
  refund notification must know which of the three it is in.
- **The coupling moved, it did not vanish.** The handler still has to reach
  Temporal to hand the work over; if that fails the event parks on the DLQ.
  We traded "notification is down when order is down" for "notification is
  down when Temporal is down" — the same dependency placement already
  accepts (ARCHITECTURE §6: *Temporal is a checkout-availability
  dependency*), now extended to the notification path.
- **One of seven notifications is structurally different from the other
  six** — different substrate, failure modes, observability and replay
  semantics — because of a gap in payment's payload shape rather than
  anything about the notification itself.
- Order gains a public-ish internal endpoint that hands out a customer id
  for any order id. SystemOnly, never edge-routed, and asserted as such by
  a test, but it is a new surface that did not exist.
- `notification_db` no longer rebuilds from the orders topic alone: the
  refund path needs order to be reachable.

### Considered and rejected

- **Put `user_id` in the payment event.** Payment does not know it; it
  would have to be threaded from order and stored on the `payments` row for
  the payment's whole life to render one sentence — a customer identifier
  retained with no reader in the money service, which is what
  `orders.card_token` was before it was removed.
- **Read `receipts.user_id`.** Notification already holds that mapping, and
  a refund requires a captured payment, which normally implies a settled
  order with a receipt. Rejected for the window between `CAPTURE_PAYMENT`
  and `SETTLE_ORDER`, and because it makes refund handling depend on the
  receipts feature.
- **Keep the projection, arm it only on `OrderConfirmed`.** The cheapest
  option — ~6 writes per order down to 1, no new substrate. Rejected in
  favour of removing the table outright.

## Revisit trigger

If a second payment-topic message ever needs to notify, reconsider: two
workflows for two messages is worse than the projection was for one, and
the honest fix at that point is asking payment to carry the recipient.
