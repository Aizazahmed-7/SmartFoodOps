# 0020 — Onboarding consistency: sync grant + outbox convergence, not Temporal

**Status**: Accepted 2026-08-10 (raised in design review of the self-serve onboarding flow)

## Context

Self-serve onboarding spans two services: Catalog commits the restaurant, then
Identity grants `restaurant_admin` + `restaurant_id` to the creator
([service-ownership.md](../service-ownership.md)). Review asked: (1) why isn't
this a Temporal workflow, (2) what happens on a *permanent* grant failure after
the restaurant committed, (3) how is idempotency achieved.

## Decision

**Temporal is not used.** The platform litmus test (ARCHITECTURE §division of
labor) reserves Temporal for flows with orchestration content — timers, human
decisions, divergent compensation, money. Onboarding has two steps, no money,
and its only recovery is *retry the grant until it lands* — convergence, not
orchestration. A workflow would put a worker fleet inside a synchronous
admin-time API (or force the API async) for a flow running a handful of times
a day.

**Consistency layers (current, W1):**
1. Restaurant insert + `RestaurantCreated` outbox event (payload includes
   `owner_user_id`) commit in ONE transaction — the durable intent-log a
   workflow would otherwise provide.
2. The grant runs post-commit; transient failures retry (backoff), then
   surface 503 + `Retry-After` — replaying the POST is the repair path.
3. Idempotency by NATURAL KEY, not `Idempotency-Key` header: one restaurant
   per owner is the business rule, so `owner_user_id` is the key — pre-check
   returns the existing restaurant (200), `UNIQUE(owner_user_id)` decides
   races (the loser rolls back, outbox rows included, and adopts the winner).
   A header would dedupe retries of one request but not prevent a second
   restaurant; the natural key does both. (Orders, where many-per-user is
   legal, use the header machinery instead — api-standards §idempotency.)
4. The grant itself is idempotent on Identity (replay → silent success), so
   re-sending is always safe; permanent refusals (4xx) are never retried.

**No rollback, ever — forward recovery is deliberate.** Compensation suits
intents that are *invalidated* (declined payment → unwind the order); a failed
grant is an intent that is merely *delayed* and always eventually completable.
Deleting the restaurant would make retries mint new ids (event/consumer churn),
add a compensating step that can itself fail, and protect nobody: an
unmanaged restaurant is indistinguishable from any just-onboarded one before
its menu exists.

**The repair window is unbounded.** Recovery is keyed only on durable state —
the `owner_user_id` row and the idempotent grant — never on in-flight
requests, timers, or token lifetimes (an expired refresh token just means an
ordinary re-login first). An Identity outage of days changes nothing: the
POST replay repairs whenever it next happens (client contract: treat the 503
as "setup pending", replay on next launch), an ops runbook can call
`/v1/internal/grants` directly, and post-W3 the outbox row — which persists
until publish is confirmed, with the consumer resuming from its own offset —
completes the grant with no action from anyone. (Note the framing: Identity
down means no logins or refreshes platform-wide; onboarding is not the
availability bottleneck in that scenario.)

**Who replays the POST — the client contract (FE requirement, not folklore):**
on onboarding 503, the app retries briefly (`Retry-After`), then persists an
`onboarding_pending` flag + the form data and shows "we'll finish setup
automatically". Every app launch/login with the flag set silently replays the
POST: 200/201 → refresh token → clear flag → dashboard; 503 → keep flag;
409 → support path. Lost local state self-heals too: re-submitting the
partner form replays by owner (body ignored) and returns the existing
restaurant. The POST cannot serve as an ownership *probe* (it would create
for a new user) — a `GET /v1/me/restaurant` convenience endpoint is a noted
future option, largely mooted post-W3: once the consumer applies the grant,
any ordinary login carries `restaurant_id` in the claims and the app routes
from there, demoting the client replay to a latency nicety.

**Permanent failure today** leaves a committed restaurant whose owner lacks
claims — loudly (409 `GRANT_CONFLICT`), bounded (`UNIQUE` blocks a second
attempt at a different restaurant), and manageable (`system_admin` bypasses
scoping). Every *reachable* path is closed by construction: riders are gated
pre-write, existing admins get their restaurant replayed pre-insert, races
adopt the winner, and no user-deletion path exists yet. The handling is
defensive depth for contract drift and future features.

**Closure — convergence via the event, not a workflow (IMPLEMENTED 2026-08-10,
pulled forward from W3 after this review):** when the
outbox drain lands, Identity consumes `catalog.changes` and applies the grant
idempotently on `RestaurantCreated`. The event commits with the restaurant,
delivery is at-least-once, the grant is idempotent → a lost synchronous grant
can no longer produce a permanent orphan. The sync call becomes the fast path
(claims on the next token refresh); genuinely un-appliable grants surface in
the consumer's DLQ as an ops item.

## Consequences

- No Temporal dependency in W1; the order saga (W2) remains Temporal's
  entry point, per plan.
- Until W3, the orphan window exists only for currently-unreachable failure
  modes; ops path documented above.
- The grant-convergence consumer now RUNS (identity `consumers.py`, group
  `identity.grant-convergence`, processed_events dedupe): the orphan window
  is closed by machinery, not by promise. The API contract did not change —
  it is one more idempotent consumer group, exactly as predicted.
