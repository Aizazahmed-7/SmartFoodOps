# 0035 — Event ids are random, not derived

**Status**: Accepted (2026-09-10) — supersedes the *"Deterministic event identity"*
row of [ADR-0018](0018-v2-review-register.md). Every other row of that register
stands.

## Context

`smartfood_outbox.event_id()` minted `uuid5(NS, "{aggregate_type}:{aggregate_id}:{version}:{event_type}")`,
and the id is the outbox table's PRIMARY KEY. ADR-0018 adopted this to close a
v1 gap, on the stated reasoning that *"a retried transaction or a re-run poller
can never mint a second identity for the same fact"*. `ARCHITECTURE.md` went
further and recorded random uuid4 ids as **banned**;
`engineering-checklists.md` listed them as a review-rejectable anti-pattern.

Reviewed against the code, neither half of that reasoning holds.

**"A re-run poller"** — the poller does not mint ids. `poller.py` selects rows
and publishes `event_id = row.id`, the *stored* value. A re-publish after a
crash-between-publish-and-mark re-sends the id the row was born with, whatever
scheme minted it. Consumer dedupe (identity's `processed_events`, notification's
`ntf_<uuid5(event_id:recipient)>` natural key) reads that stored value. None of
it depends on the id being *derivable* — only on it being *stable*, which a
column trivially is.

**"A retried transaction"** — this would require the same logical fact to be
staged by two separately committed transactions. All eight staging sites were
checked; every one is already guarded by its own aggregate's uniqueness,
*before* the event row is inserted:

| Site | What actually stops the second staging |
|---|---|
| `order/activities.py` OrderPlaced | `insert_order` runs first; `orders.id` PK raises, `IntegrityError` → `_adopt_existing`. A Temporal retry never reaches the event insert. |
| `order/transitions.py` | Guarded status UPDATE; `version` comes back fresh or the transition did not apply |
| `payment` authorize | `insert_payment` first (`payments` PK) → converge on stored truth; plus the `MONEY_SCOPE` idempotency store |
| `payment` lifecycle | `transition_payment` returns `None` when already applied → converge |
| `inventory` reserve | Explicit `get_reservation` pre-check + `reservations` PK, both before the event |
| `inventory` release/consume | `finished.version` from a guarded transition |
| `inventory` stock adjust | Monotonic version bump per `(branch, item)` ledger |
| `catalog` `_stage_one` | `bump_version` UPDATE…RETURNING under row lock — a fresh version per mutation |

So the deterministic id never deduplicated anything. Worse, because the id is
the PK, a genuine double-stage would not have deduplicated either — it would
have raised `IntegrityError` and aborted the caller's whole business
transaction. The scheme was a second net under a first net that always catches.

Meanwhile it charged real costs: a project namespace that could never change,
a `version` argument that had to be unique per fact for *identity* reasons on
top of its own, and a documented invariant maintained across four documents.
`inventory/domain/service.py` carried a composite `aggregate_id` partly to stop
two branches' bumps "minting colliding deterministic event ids".

## Decision

1. `stage_event` mints `str(uuid.uuid4())`. Ids are unique per *emitted row*,
   not per *logical fact*.
2. The public `event_id()` helper and its `_NAMESPACE` are **deleted**, not
   deprecated — leaving a deterministic-id helper in a shared lib invites its
   reuse for the reasoning this ADR retires.
3. `stage_event` still takes and stores `aggregate_version`. Projector
   ordering ("apply only if newer") is a separate mechanism from identity and
   is untouched here.
4. Consumer dedupe is unchanged and stays keyed on the received `event_id`.
   What it dedupes is **re-delivery of one row**, which is what it always
   actually did.
5. Idempotency of emission remains where it already was: the aggregate's own
   uniqueness, asserted in the same transaction, before the event insert.
   `stage_event`'s docstring now says so, so the next reader does not
   re-derive the wrong reason from the old one.
6. Direct-to-Kafka publishers keep their own schemes and are **out of scope**:
   `catalog/adapters/browse.py` (`uuid5(request_id)`, so a retried browse
   cannot double-count demand) and `dispatch/adapters/events.py`
   (`uuid5(kind:subject:marker)`). Those are not outbox rows, and their
   determinism does real work.

## Consequences

### Positive

- Removes a fixed namespace that could never be changed and a cross-service
  invariant that four documents had to keep in step.
- Unblocks the version removal: with identity no longer derived from
  `version`, nothing needs that column for id purposes.
- A double-stage now produces two rows instead of aborting a business
  transaction. Given the guards above it should never happen — but the
  failure mode if it does is a duplicate event that consumer dedupe was
  already built to absorb, rather than a transaction rollback on a money path.

### Negative

- A bug that genuinely double-emits now yields two distinct ids that no
  dedupe layer collapses. The eight guards above are what stands between us
  and that, and they are now the *only* thing. A new staging site that skips
  its aggregate-uniqueness guard is a correctness bug this ADR removes a
  backstop for; `stage_event`'s docstring is where that requirement is stated.
- Poller batch order within an identical `occurred_at` is now an arbitrary
  uuid4 ordering rather than an arbitrary hash ordering. Both are arbitrary
  and both are *stable across passes* (the ids are stored, not recomputed),
  so ordering repeatability is unchanged — but the sort key is no longer
  reproducible from the fact itself.

### Owed, not done

- **PRD FR-50 (P0)** specifies the envelope's `event_id` as *"deterministic
  UUIDv5 of `aggregate:{id}:{version}:{type}`"*. The requirement's substance
  (governed Avro envelope, `BACKWARD_TRANSITIVE`, a dedupe key present) still
  holds; the derivation clause does not. Left unedited pending a product
  call — a P0 requirement is not ours to amend.
- The Avro `event_id` field `doc` changed. The type is still `string` so this
  is backward-compatible, but Schema Registry will register a new schema
  version on next publish.

## Revisit trigger

A consumer that needs to recognise the same logical fact arriving from two
independent sources — a rebuild from backup alongside live CDC, or a
cross-cell replay — would need derivable identity again. Reinstate it as a
*second* column (`fact_key`) rather than by re-deriving the primary key, so
that emission idempotency and consumer dedupe stay separable.
