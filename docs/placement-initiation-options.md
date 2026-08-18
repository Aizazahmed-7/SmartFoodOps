# Where placement begins — five ways to wire Temporal into `POST /v1/orders`

> **Outcome (2026-08-18): the team chose C, in the hybrid form described in
> §7 — the checks stay in HTTP, the workflow creates the order, the sweeper is
> gone.** [ADR-0023](adr/0023-placement-runs-inside-the-order-workflow.md) is
> the decision record. This document keeps the full comparison, because the
> cons listed under C are now *our* costs and the revisit triggers are what we
> watch. Everything below describes the option space as it was scored; §7
> records what was decided and why the hybrid changed the balance.

**Purpose**: a decision document for the recurring review question *"why isn't
the whole order flow inside the workflow?"* It lays out the full option space,
scores each on the same axes, and records the trade that was taken.

**Audience**: reviewers, and anyone about to re-open the debate. Nothing here
is folklore: every claim points at code
([`domain/service.py`](../services/order/order/domain/service.py),
[`activities.py`](../services/order/order/activities.py),
[`adapters/temporal_client.py`](../services/order/order/adapters/temporal_client.py),
[`workflows.py`](../services/order/order/workflows.py)). The sweeper this
document discusses lived in `services/order/order/sweeper.py` until
ADR-0023 deleted it — read it in git history, not on disk.

Related: [ADR-0001](adr/0001-temporal-owns-the-order-saga.md) (Temporal owns
the saga), [ADR-0020](adr/0020-onboarding-consistency-outbox-convergence-not-temporal.md)
(the litmus test for *when* Temporal earns its place),
[flows.md](flows.md) (the as-built sequence diagrams),
[api-standards §4](api-standards.md) (idempotency semantics).

---

## 1. The question, stated precisely

Everybody agrees Temporal owns the order **saga**: reserve stock, authorize the
card, wait for the kitchen, run delivery, capture, compensate on failure. That
is settled ([ADR-0001](adr/0001-temporal-owns-the-order-saga.md)) and none of
the options below change it.

The open question is narrower, and it is really three questions wearing one
coat:

1. **Which write is the durable intent?** After which line of code is the
   customer's order guaranteed to eventually happen even if every process dies
   one millisecond later — the `orders` row, or the workflow-started record in
   Temporal's own database?
2. **Who is allowed to be down at checkout?** Whatever sits inside the
   synchronous request path becomes a checkout-availability dependency. When
   this was written that set was {Postgres, Identity, Catalog}, with Temporal
   deliberately outside it; the decision in §7 moved Temporal into it.
3. **What can the customer be told synchronously?** Some refusals are
   *fixable in the moment* — the price changed, an item just got 86'd, the
   restaurant just closed. Those are worth far more as a 409 on the POST than
   as a push notification two seconds later.

Everything below is a different answer to those three.

---

## 2. The five options

Ordered by how much of the flow the HTTP request holds open, from most to least:

| | Option | One-line shape |
|---|---|---|
| **A** | **Full hold** | POST starts the workflow and waits for it to reach CONFIRMED, then answers |
| **C** | **Workflow-first, detach after the order row exists** | POST starts the workflow, waits only for the "order created" step, answers 202, workflow continues — **ADOPTED, in the hybrid form of §7** |
| **E** | **Workflow-first, detach immediately** | POST starts the workflow and answers 202 at once — the order row is written by the first activity |
| **D** | **Hybrid partial hold** | HTTP does price → insert → reserve → authorize itself, then starts a workflow for the kitchen/delivery tail |
| **B** | **Commit-then-start + sweeper** | HTTP commits order + outbox + idempotency in one transaction, then starts the workflow — **the former design, replaced 2026-08-18** |

---

## 3. At a glance

| Axis | A — full hold | **C — detach after insert (ADOPTED)** | E — detach immediately | D — hybrid | B — former |
|---|---|---|---|---|---|
| Durable intent lands in | Temporal | Temporal | Temporal | Postgres | **Postgres** |
| Temporal in checkout path | yes, whole saga | yes, start + 1 activity round-trip | yes, start RPC only | no (tail only) | **no** |
| Worker fleet in checkout path | yes | **yes** | no | no | **no** |
| Synchronous `PRICE_CHANGED` / `ITEM_UNAVAILABLE` | yes | yes, via workflow round-trip | **no** | yes | **yes, in-process** |
| Synchronous card decline (402) | yes | no | no | **yes** | no |
| Client's 202 means | "confirmed" | "row exists" | "we accepted the request" | "paid" | **"committed, saga will run"** |
| Read-your-writes on `GET /v1/orders/{id}` | yes | yes | **no — 404 window** | yes | **yes** |
| Timeout ambiguity for the client | severe | real | none | real (PSP-bound) | **none** |
| Gap needing a healer | none | none | none | reserve→auth leak | **commit→start (sweeper)** |
| Idempotency machinery | still needed | still needed, relocated | still needed, relocated | still needed | **as built** |
| p95 checkout budget (NFR-2: <3s) | blown | +schedule-to-start | best | PSP-bound | **met today** |
| Extra Temporal actions/order | +3–5 | +3–5 | +2–3 | −20 (shorter workflow) | **baseline (~40)** |
| Code churn to adopt | large | large | large | large | **none** |

Two rows deserve emphasis because they are the whole argument:

- **"Worker fleet in checkout path"** — Option C does not merely add Temporal
  as a dependency, it adds *a task-queue poller with a deploy cycle* to the
  path a paying customer waits on. A rolling worker restart becomes checkout
  latency.
- **"Client's 202 means"** — in B the 202 is a statement about *committed
  database state*. In C and E it is a statement about *a promise held
  elsewhere*. That difference is what the sweeper buys back, and it is why
  the sweeper is cheap: it heals a gap between two durable stores, not a
  hole in the truth.

---

## 4. Option by option

### A — Full hold: HTTP waits for the saga to confirm

The POST starts `OrderWorkflow` and awaits its progress through reserve →
authorize → confirm before answering.

| Pros | Cons |
|---|---|
| Simplest possible client contract: one request, one final answer, no polling, no push channel | The request now spans stock reservation **and** a PSP call — `tok_timeout` alone is 30s, and NFR-2's p95 < 3s is unreachable |
| Every business refusal is synchronous, with the exact code and details | Availability inversion at maximum: Temporal, the worker fleet, Inventory, Payment and the PSP all become checkout-blocking |
| No sweeper, no gap, no reconciliation logic anywhere | A proxy/browser timeout mid-flight leaves a **paid** order whose customer saw an error — the worst possible ambiguity |
| Trivially observable: one span covers the whole thing | Long-held connections and worker threads collapse under load exactly when load is the problem |

**Verdict**: rejected outright. Listed only because it is the intuitive
starting point ("just put it all in the workflow"), and because the objections
to it are the objections to C in weaker form.

---

### C — Workflow-first, detach once the order row exists ⟵ *ADOPTED (ADR-0023)*

The POST hands the raw request to `OrderWorkflow` (via start, or
`update-with-start`). The workflow's first activity prices the cart and inserts
the `orders` row. The API waits for **just that step**, returns
`202 {order_id, status: PLACED}`, and detaches. Reserve → authorize → kitchen →
delivery continue in the workflow.

This is the "durable request" pattern, and it is a legitimate design — it is
what a Temporal-native shop with a managed cluster would likely write.

| Pros | Cons |
|---|---|
| **One owner for the whole lifecycle.** The placement sequence stops living in a route and lives in the same function as the rest of the saga | **Availability inversion.** Temporal *and* at least one healthy worker must be reachable to place an order. Today a total Temporal outage degrades orders to "placed, saga heals later"; here it is a hard checkout outage |
| **No commit→start gap, so no sweeper** — `sweeper.py`, its partial index and its tests all disappear | **Schedule-to-start latency enters the customer's p95.** Worker backlog, a rolling deploy, or a cold task queue is now checkout latency, not background latency |
| **Durable retries for the pre-commit dependencies.** Today a transient Identity or Catalog blip during placement is a 503 the customer sees; as activities they would be retried for free | **Timeout ambiguity returns.** If the await exceeds the proxy budget the client gets 504 — but the workflow is alive and will produce a real order. The client cannot distinguish "failed" from "slow", which is precisely the ambiguity B eliminates |
| **Uniform compensation.** Anything that fails after the insert is unwound by the same machinery that unwinds everything else | **The idempotency machinery does not go away — it moves and multiplies.** The workflow id must be keyed on something the API knows *before* an order id exists (`ord::{user}:{idem_key}`), so a client-chosen string becomes a Temporal primary key. And the insert activity must itself be idempotent, because an activity that times out after committing *will* be retried — so `order_id` must be derived deterministically, not `uuid4()` |
| **Full forensics from step zero**: the Temporal UI shows pricing and insert attempts, not just post-placement history | **Business errors cross a protocol boundary.** `PRICE_CHANGED` / `ITEM_UNAVAILABLE` / `RESTAURANT_CLOSED` are today `raise PricingError` → error handler → 409 in one process. Here they must be carried out of a workflow as values and re-mapped to HTTP — a second error-translation layer that must stay in sync with [api-standards](api-standards.md) |
| Placement gains long/asynchronous steps for free later (fraud scoring, manual review) without restructuring | **Every placement test needs a Temporal test environment.** Today placement is a fast sqlite + `TestClient` suite; there it is time-skipping-env territory, and the 100% bar gets slower and more brittle to hold |
| | **Cost**: +3–5 Temporal actions per order against the [ADR-0009](adr/0009-temporal-cloud-first.md) tripwire, applied to *every* order including the ones that fail pricing and would previously never have touched Temporal |

**Where it becomes the right answer**: a Temporal Cloud shop with a multi-AZ
worker fleet, where "Temporal is down" is treated the same as "Postgres is
down", and where the team is fluent enough that the extra test surface is not
a tax. That is a real, defensible world — it is just not this one yet.

---

### E — Workflow-first, detach immediately

Same as C, but the API does not wait for anything: it mints the `order_id`,
starts the workflow, and returns 202. The first activity inserts the row.

| Pros | Cons |
|---|---|
| Fastest possible response — one Temporal start RPC and done | **No synchronous business errors at all.** A stale price or an 86'd item becomes a *cancellation notification* seconds later, instead of a fixable 409 with a one-tap re-confirm. This is a direct product downgrade |
| Worker latency is fully off the checkout path (unlike C) | **Read-your-writes hole**: the FE has an `order_id` that `GET /v1/orders/{id}` will 404 on until the activity commits. Every read path needs a "not yet" state |
| Cleanest conceptual story: "the workflow *is* the order" | Temporal is still a checkout dependency for the start RPC |
| Fewest Temporal actions of the workflow-first family | Debugging a bad cart means reading workflow history instead of an HTTP response |

**Verdict**: strictly worse than C for our product, because the synchronous 409
is the single most valuable thing the placement endpoint does for UX.

---

### D — Hybrid: hold HTTP through authorize, workflow for the tail

The request does price → insert order → reserve stock → authorize payment
inline, answers once the money is held, and starts a workflow only for the
accept window, delivery, capture and settle.

| Pros | Cons |
|---|---|
| The customer's 202 carries the most meaningful truth available: **the card cleared** | **p95 is now PSP-bound.** `tok_timeout` is a 30s hang by design — this option imports a third party's tail latency into checkout |
| Every refusal that matters is synchronous: price, availability, capacity, **and decline (402)** | **A mini-saga reappears in the route.** Auth fails after reserve → the route must release the reservation. That is compensation logic without durability: a pod crash between reserve and auth leaks a hold, recovered only by the reaper's 1800s TTL |
| The workflow shrinks to the part that is genuinely long-running (human + delivery), cutting Temporal actions per order substantially | **Two saga engines**, one durable and one not, each owning half the failure story — the exact "where is this order and what happens next?" forensic problem [ADR-0001](adr/0001-temporal-owns-the-order-saga.md) exists to prevent |
| No sweeper for the placement gap | The gap does not vanish, it moves: a crash after authorize but before workflow start now leaves an order that is **paid** with no saga — strictly worse than today's unpaid PLACED row, and the sweeper for it would need to resume mid-saga |
| | Retries of the inline reserve/auth calls are hand-rolled, not Temporal's |

**Verdict**: attractive on the UX axis, and the honest reason to reject it is
the crash window: it converts the cheapest possible orphan (an unstarted PLACED
row) into the most expensive one (an authorized payment with no orchestrator).

---

### B — Commit-then-start plus the sweeper *(the former design)*

```
reserve idempotency key
  → resolve address (Identity)  → snapshot menu (Catalog)  → price (in-process)
  → ONE transaction: orders row + line snapshots + OrderPlaced outbox + idempotency completion
  → COMMIT                      ← the durable intent lands here
  → saga.start(order_id)        ← best-effort, failure is swallowed and logged
  → 202 {order_id, status: PLACED}
```

| Pros | Cons |
|---|---|
| **The durable intent is a row in our own database.** Everything downstream — saga start, sweeper heal, outbox publish — is recovery *from* that row, never a race to create it | **The commit→start gap is real.** A crash in that window leaves a PLACED order whose saga never started, invisible until the sweeper runs |
| **Temporal is not a checkout dependency.** `place()` wraps `saga.start` in try/except and still returns 202 — a Temporal outage degrades orders to "placed, will start when Temporal returns", never "cannot order" | **Heal latency**: `min_age_seconds=60`, `interval_seconds=30` — a crashed placement waits up to ~90s for its saga. Fine for a rare crash, poor as a routine path |
| **The 202 is unambiguous.** It is issued only after commit, so "we accepted your order" is a statement about committed state. No 504 ever contradicts a stored answer | **The placement sequence lives outside the workflow**, so two files know the order's opening moves — `service.py` prices and inserts, `workflows.py` picks it up from the snapshot |
| **Every fixable refusal is synchronous and in-process**: `PRICE_CHANGED`, `ITEM_UNAVAILABLE`, `RESTAURANT_CLOSED`, address 404 — raised, mapped, returned, key released for a clean retry | **Pre-commit dependency blips surface to the customer.** A transient Identity/Catalog failure is a 503; activities would have retried it durably |
| **Read-your-writes holds**: the id in the 202 is immediately readable by `GET /v1/orders/{id}` and appears in history | The sweeper is machinery: a loop, a partial index (`ix_orders_sweeper`), a `bool`-returning start port, and its tests |
| **Idempotency is exactly one mechanism**, in one place: `(scope, idem_key)` with a body hash, completing inside the same transaction as the order, replaying the stored 202 byte-for-byte | Adding a genuinely long step *before* commit would not fit — it would force a move toward C |
| **Placement tests are fast and hermetic** — sqlite + `TestClient`, no Temporal test server needed to prove the endpoint's contract | |
| **Safety does not depend on the sweeper being clever.** `ord::{order_id}` + `REJECT_DUPLICATE` means sweeping a live order is a no-op *by construction*, and `start()` returns `True` only for a genuine heal | |

---

## 5. What actually happens when something dies

The comparison that matters is not the happy path — every option looks fine
there. It is this table.

| Failure | A | **C (now ours)** | E | D | B (former) |
|---|---|---|---|---|---|
| API pod dies mid-request, before any durable write | client retries with same key, nothing leaked | same | same | same | **same** |
| API pod dies in the gap between the durable write and the next step | n/a | n/a | n/a | **paid order, no saga — expensive orphan** | **PLACED row, no saga — sweeper heals in ≤90s** |
| Temporal namespace unreachable at checkout | **checkout down** | **checkout down** | **checkout down** | orders take payment, tail deferred | **202 as normal, sweeper starts sagas when it returns** |
| Worker fleet rolling-restarting | checkout stalls | **checkout latency spike** | unaffected | unaffected | **unaffected** |
| Identity/Catalog transient 500 during placement | activity retries, invisible | **activity retries, invisible** | activity retries | 503 to client | **503 to client, key released, clean retry** |
| PSP hangs 30s (`tok_timeout`) | client waits 30s | invisible | invisible | **client waits 30s** | **invisible — the saga handles it** |
| Client retries with the **same** idempotency key | replay | workflow id collides → attach | attach | replay | **stored 202 replayed + `Idempotent-Replay: true`** |
| Client retries with a **new** key, same cart | second order | second workflow | second workflow | second order | **second order — legitimate by definition; the FE mints keys per cart-body-hash to prevent accidents** |
| Proxy 504 fires mid-request | order may exist | **order may exist — ambiguous** | n/a | order may be paid | **impossible: the 202 comes after commit, and a lost response replays** |

The pattern is consistent: **B moves every ambiguity off the customer and onto
a background loop.** The others trade the sweeper away and take ambiguity back
in its place.

---

## 6. What the customer experiences

Worth separating, because the review often argues UX and the answer is that UX
is mostly *not* determined by this choice.

| | A | C | E | D | B |
|---|---|---|---|---|---|
| Stale price / 86'd item | fixable 409 | fixable 409 | **notification after the fact** | fixable 409 | **fixable 409** |
| Card declined | synchronous 402 | push/poll | push/poll | **synchronous 402** | push/poll |
| Time to first screen | worst | medium | best | medium | **best** |
| Refresh mid-checkout | risk of double order | attaches to workflow | attaches | risk | **replays the key, same order** |

The decline case is C/E/B's shared weakness, and it is already solved at the
product layer rather than the transport layer: after the 202 the FE lands on
the order screen and polls every 3s while the status is non-terminal
([`frontend/src/pages/Orders.tsx`](../frontend/src/pages/Orders.tsx)), rendering
success or the cancellation reason from order status. That
design is what makes "the 202 does not mean paid" acceptable — and it works
identically under B, C and E, so it is **not** a reason to prefer one.

The refresh-safety row, by contrast, *is* decided here: it comes from the
idempotency key being minted per cart-body-hash and persisted in localStorage
(S8), which is orthogonal to Temporal but only meaningful because the server
replays a **stored** response.

---

## 7. Decision — C, hybridised with B's front half

**Implemented 2026-08-18** ([ADR-0023](adr/0023-placement-runs-inside-the-order-workflow.md)):
the workflow creates the order, and the sweeper is deleted. The variant that
won is not pure C: **every synchronous check stays in the HTTP request.**

That single change is what moved the decision, because it retires C's two
worst columns:

| C's cost, as scored above | What the hybrid does about it |
|---|---|
| Business errors cross a protocol boundary, needing a second error-mapping layer | Gone entirely — pricing, availability, closure and address-404 are raised and mapped in-process exactly as before |
| Every placement test needs a Temporal environment | Mostly gone — the route and domain still test against a fake `SagaPort`; only the workflow suite touches Temporal |

The costs that remain are real and were accepted with eyes open: **Temporal is
now on the checkout path** (its outage is a 503, not a delayed saga), worker
schedule-to-start latency sits inside the p95 budget, the pending path
suspends read-your-writes for a moment, `409 IDEMPOTENCY_IN_PROGRESS` becomes
a routine client state, and the card token now travels through workflow
history.

Two design details do the load-bearing work, and neither was in the original
sketch of C:

1. **Order ids are derived from the idempotency key** (`uuid5(NS,
   "{scope}:{idem_key}")`). Without this, the stale-key takeover — reachable
   now that the write happens in a worker — mints a second order id and a
   second workflow for one intent.
2. **The key is never released once the start RPC has been attempted**, so a
   retry waits or takes over onto the same id rather than forking.

**Revisit triggers** — any one of these argues for going back to committing
first and re-introducing a healer:

1. **Placement 5xx attributable to Temporal eats the error budget.** This is
   the cost we bought; if it turns out to be expensive, B is the way back.
2. **Placement p95 breaches NFR-2 (< 3s) because of schedule-to-start** —
   worker capacity is the first fix, reverting is the second.
3. **`PlacementPending` stops being rare.** Like the old sweeper-heal line, it
   logs at `warning` on purpose. Routine pending answers mean the worker fleet
   is not keeping up with checkout, and the pending screen has quietly become
   the normal experience.

**Going back**, if it comes to that, is the reverse of what was done and the
seams are still in place: `SagaPort.place` returns to `start`, `create_order`
becomes the route's transaction again (the code is one activity), the sweeper
and its index come back from git history plus a new migration, and the derived
order id can stay — it is a good idea independent of who writes the row.

---

## 8. Evidence from a production system

We studied a large production Go/Cadence platform (`web-controller`) for
exactly this question. Findings relevant here:

- It runs **101 workflows**, and **zero of them are started from an HTTP
  request handler**. Every start happens in a Kafka consumer, after the
  originating transaction committed — the commit-first shape of B.
- Its consumers commit the offset even when the workflow start fails, so a
  failed start is *silently lost* — weaker than either of our designs.
- Its synchronous APIs answer from database state and let orchestration follow.

This remains the strongest argument for B, and it did not win: at that shop's
scale the workflows are started by *events*, where nobody is waiting, so
keeping durable execution off the request path costs them nothing. Our
placement has a customer waiting either way. Worth re-reading if trigger 1
ever fires.
