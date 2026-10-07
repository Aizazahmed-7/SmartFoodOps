# 0038 — The outbox and the event envelope carry no `aggregate_version`

**Status**: Accepted (2026-09-11) — removes the `aggregate_version` column
from the shared `outbox_table()` contract and the field from the
`DomainEvent` Avro envelope. Follows [ADR-0035](0035-random-event-ids.md),
which removed the only thing that consumed it.

## Context

Every outbox row carried `aggregate_version`, every producer supplied it
through `stage_event(version=...)`, and the poller copied it onto the wire.
`docs/architecture-walkthrough.md` described its purpose as *"projectors
apply only if newer — a late `OrderConfirmed` after `OrderCancelled`
no-ops"*.

No projector does that. There is **not one version comparison on any
consumer path** in the repository. The analytics projector — the only
consumer with a lifecycle projection — upserts with
`on_conflict_do_update(index_elements=["order_id"], set_=…)` and no WHERE,
and its own column comment says where the ordering actually comes from:

> The LATEST lifecycle state seen. Per-order ordering is guaranteed by the
> topic key (= order_id → one partition), so last-write-wins here is
> genuinely last-event-wins.

Out-of-order convergence is handled per column instead — `if
payload.get("rider_id")`, `if payload.get("brand_id")` — so a late event
cannot blank a later stamp regardless of any version.

Its other job was feeding the deterministic event id, which ADR-0035
retired. That left a column written by four services and read by nobody.

## Decision

1. `outbox_table()` drops `aggregate_version`; `stage_event()` drops its
   `version` parameter. Five services' outbox tables drop the column in
   lockstep (catalog, order, payment, inventory, ai-assistant) because they
   all derive from the one factory.
2. The `DomainEvent` Avro envelope drops the field too.
3. Per-aggregate ordering is unchanged and was never this column's doing:
   the poller drains in `(occurred_at, id)` order and Kafka keys by
   `aggregate_id`, so one aggregate's events share a partition and keep
   their order.
4. `orders.aggregate_version` and `order_facts.aggregate_version` are **not**
   touched here — the order payload still carries a version and analytics
   still stores it. Those come out with the remaining version columns.

## Why dropping the Avro field is safe in both directions

Normally removing a field from a schema is the dangerous direction: an old
consumer whose *reader* schema still requires it cannot resolve a new
message. That does not apply here. `SchemaSerde.decode` reads the schema id
embedded in the message, fetches the **writer's** schema from the registry,
and hands it to `schemaless_reader` with **no reader schema**:

```python
(schema_id,) = struct.unpack(">I", data[1:5])
schema = await self._registry.schema_by_id(schema_id)
return schemaless_reader(io.BytesIO(data[5:]), self._parsed[schema_id])
```

So decoding is schema-on-read. A message written before this change still
decodes with its own schema and still has the key; one written after simply
lacks it. No resolution step exists to fail, and no consumer read the key —
verified by grep across every service.

## Consequences

### Positive

- Removes a column four services wrote and none read, and a wire field that
  cost a `long` on every message.
- Removes the last reason `stage_event` needed to know anything about
  versions, which is what the remaining column drops were waiting on.

### Negative

- A consumer that later *wants* version-guarded projection must reintroduce
  it, and reintroducing an Avro field requires a default (removal did not).
  The payload is the cheaper place — order's already carries one.
- `docs/architecture-walkthrough.md`'s "projectors apply only if newer"
  claim was false before this ADR and is corrected by it. Anyone who trusted
  it may believe consumers have an ordering guard they never had; the real
  guarantee is the Kafka partition key.

## Revisit trigger

A projector that must tolerate genuinely out-of-order delivery for one
aggregate — a topic re-key, a cross-cell merge, or a consumer reading two
partitions for one entity — would need a version again. Put it in the
payload and guard the UPDATE with it; do not restore the envelope field,
because the envelope is shared by every topic and most of them will never
need it.
