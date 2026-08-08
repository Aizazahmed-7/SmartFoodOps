# 0011 — Dispatch assignment lock via DynamoDB conditional writes

**Status**: Accepted

## Context

Double-assigning a rider is dispatch's cardinal failure, and it happens exactly under the conditions dispatch runs in: concurrent offer cascades, rider churn, timeouts racing accepts. The guard needs to be a single atomic authority, not a distributed consensus or an advisory Redis lock that can expire mid-decision.

## Decision

**The DDB conditional write on `rider_state` is the single lock authority.** Reserving a rider for an offer is `attribute_not_exists(offer_lock) AND size(active_deliveries) < cap` — that write *is* the double-assignment guard. Accept converts lock→assignment in one conditional write plus a guarded `OFFERING→ASSIGNED` transition; late or duplicate accepts no-op. Revocation (reassignment path) is a conditional delete guarded by `rider_id = :expected AND state = ASSIGNED` — a rider who already scanned pickup wins the race, and post-pickup failures become ops incidents, never silent reassignment.

The lock table stays **region-local — never a DynamoDB Global Table**: LWW cross-region replication would silently break mutual exclusion (two regions could both win). This constraint is recorded here and in the multi-region deferral (ADR-0013).

Redis GEO is only the candidate index; if it is cold or down, Dispatch falls back to DDB `rider_state` — the lock authority never moves.

## Consequences

**Positive**
- Mutual exclusion rides on DDB's per-item linearizable conditional writes — no lock service, no lease clocks, no consensus to operate.
- Every step (reserve, accept, revoke) is one atomic guarded write, so retries and races resolve deterministically; idempotent by construction.
- Cheap at load: ~3.2k conditional offer-writes/s at cell ceiling is well inside on-demand + pre-warmed floors.

**Negative**
- Correctness is pinned to one table's availability; a DDB incident stalls new assignments (fail-closed — existing assignments unaffected).
- Cap and lock semantics live in condition expressions — reviewable but easy to get subtly wrong; changes require the mass-disconnect chaos drill.

**Revisit trigger**: multi-region dispatch (lock must remain cell-local per ADR-0013), or offer-write throttling at scale → widen capacity floors before ever weakening conditions.

## Canonical lock expression (ADR-0018)

Quoted so reviews diff the code against a canonical form rather than prose. The reserve step is exactly this single-operation check-and-set:

```text
ConditionExpression: attribute_not_exists(offer_lock) AND size(active_deliveries) < :cap
```

One conditional `UpdateItem` sets `offer_lock` and checks capacity **in the same operation** — never a read followed by a write, never two operations.

**No-second-lock rule**: no other lock, lease, semaphore, or Redis key may guard assignment anywhere in the system. A second lock reintroduces exactly the split-brain window this design removes; anything that appears to need one is redesigned to route through this conditional write instead.
