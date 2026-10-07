# B5 — Explanation engine walkthrough (2026-09-28 → 2026-09-29)

B4 could tell a customer what to order. B5 answers the question they ask
when something has gone quiet: **why is my order where it is?**

The entire milestone rests on a refusal. A model asked "why is this order
late?" over a raw order row will answer, fluently, every time — and it will
be inventing. So the model is never asked why. A pure function decides what
is true; a template says it; a model may, afterwards, rewrite the wording
and nothing else.

## What landed

| Slice | Piece | Where |
|---|---|---|
| 5.1 | Milestone timestamps + four new order events | `order/domain/transitions.py`, migration 0012, `smartfood_kafka` |
| 5.2 | Kitchen congestion, published | `inventory/api/routes.py`, `adapters/inventory_client.py` |
| 5.3 | The reason resolver and stage-based ETA | `domain/explain.py`, migration 0013 |
| 5.4 | Template-first rendering | `domain/render.py` |
| 5.5 | The three reads, assembled | `explain_service.py`, `order_client.py`, the endpoint |
| 5.6 | Order detail | `frontend/src/pages/OrderDetail.tsx` |
| — | The model rewrite, guarded | `domain/polish.py`, `polish_cache.py` |

## The decisions worth remembering

**The system could not see a delay.** Two Part A decisions, each right on
its own terms, combined to erase the timeline: `orders.updated_at` is
overwritten by every move, and the topic carried major states only ("no
per-transition spam"). So `ACCEPTED`, `PREPARING`, `READY` and `PICKED_UP`
— the four stages where delay actually happens — were exactly the four
nobody could observe. An engine over that can only restate the status the
customer can already see. ADR-0046 records the amendment; the anti-spam
intent is kept where it actually lived, in notification's `order_drafts`,
which drafts nothing for the four, pinned by a test.

**The guard that makes replays safe makes timestamps true.** Milestones are
stamped inside `transition()`'s guarded UPDATE and nowhere else. `WHERE
status = :expected` means a replayed transition matches zero rows, so a
redelivery arriving minutes later cannot overwrite a moment that already
passed. ADR-0035 paying for itself a second time, for free.

**A budget is only as honest as the clock it starts from.** The first cut
took FR-81's four stages literally. FR-84 then wanted an ETA whose earliest
basis is `accept_timeout_s` — a timer that runs from CONFIRMED. Without
`confirmed_at` the resolver would have timed the restaurant's decision
window from `placed_at`, silently charging the restaurant for the saga's
reserve-and-authorize round trip. On a live order that gap was 0.26s; on
one where payment retries it is minutes. Migration 0013.

**`overdue` is `bool | None`, not `bool`.** The kitchen has no cooking
budget (Part A's FR-55 wants prep time; it does not exist) and there is no
road-time model (OSRM stays deferred). Reporting "not late" for those would
be a claim nothing supports, so the type makes the third case impossible to
ignore. The same rule shapes `Eta`: past a deadline it is `None` rather
than a zero range, because "0 to 0 minutes" reads as an estimate instead of
the overrun it is.

**Unknown is not idle.** Inventory's load read 404s when a kitchen has no
row, and the client collapses that and every transport failure to `None`.
Synthesising `active: 0` would hand the resolver a fact nobody observed,
and the engine would tell a customer the kitchen is quiet on the strength
of an absent record. Only one missing fact is fatal — the timeline, because
a stage nobody observed is not a stage. The other two cost a clause each.

**The deadline quoted is the one the order is running under.** Order
publishes its timer budget with the timeline rather than the assistant
keeping a copy. A duplicated budget drifts silently and the failure is
invisible: an operator widens `no_rider_deadline_s` for a holiday and the
engine goes on quoting the old one, sounding exactly as confident.

**Ownership is checked before anyone else is asked.** Otherwise an order id
alone would let anyone generate load on dispatch and inventory. Not-found
and not-yours return byte-identical 404s.

**FR-87 is a property of the import graph.** `render.py` may not import
`httpx`, a provider, an adapter, or `socket`, and a test enforces it. There
is no configuration in which the template path stops working, because there
is no code path from it to a model.

**The model rewrites the template, not the sentence.** `"ready for
{elapsed}"` goes to the model with the placeholder intact and comes back
with it intact; facts are substituted per request afterwards. That is what
makes the `(reason_code, locale, bucket)` cache key correct — a cached
sentence containing "4 minutes" would be served to a customer at nine
minutes, which is the trap caching generated prose usually falls into.

**A number the model wrote is a number nobody observed.** Every figure in
an explanation arrives through a placeholder, so a candidate containing a
digit the original did not have is rejected outright. The guards caught a
real Gemini response in the live stack: it dropped `{elapsed}` entirely,
turning "your food has been ready for 1 minute and we're still finding a
courier" into "we are currently working to find a courier to pick up your
food" — losing the only fact in the sentence. Rejected, template kept,
`rule=placeholders_changed` in the log.

## What it costs

- **Two extra internal reads per explanation**, run concurrently and only
  after ownership is established. Both are optional; neither can fail the
  answer.
- **One model call per `(reason, locale, bucket)` per process, ever.** A
  key that fails is remembered as failed and never retried, or an order
  page polling every fifteen seconds would pay a provider timeout on every
  poll. In-process rather than Redis, so each replica pays its own and a
  deploy forgets — the honest trade for a population of a couple of dozen
  keys whose fallback is copy that was already good.
- **Nineteen reason codes to keep in step with nineteen templates.** Two
  tests enforce it: every code must be reachable from `resolve()`, and
  every code must have copy.

## What is proven, and how

| Claim | Evidence |
|---|---|
| Milestones survive the real path | A live order: `placed 09:44:36.778 / confirmed .042 / accepted 09:44:38.097`, all eight events staged and published in order |
| A replay cannot re-stamp | The guarded UPDATE, plus a test that replays an applied transition and asserts the stamp did not move |
| A stalled order resolves correctly | A real order held at READY with no courier: `awaiting_courier` with the ETA tracking `600 − elapsed` (570 → 400 → 230), then `cancelled_no_courier` when FR-32's deadline fired |
| Congestion is live | `active` rose to 1 when the reservation landed, held for the in-flight window, returned to 0 at settlement |
| The rewrite cannot invent | Gemini dropped `{elapsed}`; rejected with `rule=placeholders_changed` |
| Templates answer with no model in reach | An import-graph test on `render.py` — no `httpx`, no provider, no adapter, no socket |

**One exit criterion is owed a live demonstration.** "`llm_api_key=""` →
templates still answer" is proven structurally (there is no code path from
the renderer to a provider) and by unit tests, but the deployed service has
not been restarted with its keys removed and hit end to end. The command to
do it:

```
OPENAI_API_KEY= ANTHROPIC_API_KEY= docker compose -f deploy/compose/docker-compose.yml   --profile core --profile apps up -d --force-recreate ai-assistant
```

The endpoint should answer identically, with `source: "template"` for every
key. Restore with a plain `--force-recreate` afterwards.

## The adversarial review

Four reviewers over four dimensions. It was the most damaging review of
Part B so far, and the reason is worth stating plainly: **this milestone's
entire product claim is that it never says anything untrue, and the review
found it saying untrue things about money.**

**The money rule was wrong, and I had just replaced a different wrong rule
with it.** 5.6 deleted the frontend's hardcoded set of cancel reasons
because it omitted `no_rider_available` — and derived the replacement from
`confirmed_at`. Authorization completes one transition EARLIER:
`authorize_payment` moves VALIDATED → PAYMENT_CLEARED on success, and
CONFIRMED is a separate activity afterwards. So every order cancelled at
PAYMENT_CLEARED — a real hold on a real card, which the workflow itself
votes to void — was told *"You won't be charged."* And because milestone
columns are never backfilled, so was every order in the pre-B5 book. My own
test asserted the falsehood.

The fix is migration 0014: `payment_cleared_at`, stamped by the transition
that causes it, because that stamp **is** the authorization. And
`payment_held` became `bool | None`, because a row with no milestones at all
carries no evidence either way — and an explanation that cannot tell must
not pick a side. The worst sentence this engine can produce is telling
someone they were not charged while a hold sits on their card; an absent
clause is a smaller failure than a confident wrong one.

**Three in-flight templates hardcoded the same falsehood.** `{money}` was
introduced so the eight cancellation templates could not drift — and the
three OVERDUE templates that *predict* a cancellation bypassed it and
hardcoded the wrong half. All three are downstream of a successful
authorization. The FR-32 customer, whose meal was cooked and binned, was
the one being told nothing was charged.

**CANCELLING is the unwind in progress**, not a finished one: the status
moves first and the void runs after, so "your hold has been released" was
past tense about money still sitting on the card, indefinitely under a
degraded PSP. There are now three money sentences, not one.

**The resolver read stamps where it should have read status.** A PREPARING
order with no `preparing_at` — every row older than migration 0012 — was
told the restaurant "still hasn't started cooking", and invited to cancel
food that was on the pass. The status is the system of record everywhere
else in `resolve()`; the stamp only decides whether a clock can be put on
it.

**"9 of the 8 orders it can take at once."** `active > capacity` is a legal
state the port documents, and the copy rendered it as a contradiction. The
tests only ever rendered 8-of-8.

**The rewrite guard was weaker than its own docstring claimed.** It said the
digit rule caught "the most likely and most damaging failure"; it caught
the most likely *spelling*. An adversarial pass got through "they usually
accept within another five minutes", "a courier will be there in about half
an hour", an inverted meaning ("has now accepted it"), an invented cause
("because the restaurant refused it"), and an invented support address —
each of which the cache would then have served to every customer in that
state for the life of the process. Four new rules: spelled-out numbers,
lost negation, introduced causality, and disproportionate growth. The test
that claimed "the damage is tone, not truth" was wrong and now says
something narrower and true.

**`locale` was a customer-controlled cache key.** The template lookup
collapses an unknown locale to English, but the rewrite cache keyed on the
string as sent — so every distinct value was a fresh key and a fresh model
call, bypassing the budget guard entirely, unbounded, for copy that was
English either way. Two reviewers found it independently. Locales are now
resolved once, before anything is keyed on them.

**An Order outage rendered as `404 unknown order`** — telling a customer
their own order does not exist, and logging an outage as a client error
that no dashboard would alert on. Now a 503 with `Retry-After`.

**And the frontend regression that matters most:** removing the local
cancellation banner made a core Part A screen depend on an optional GenAI
service. With the assistant down, a cancelled order showed a status tag and
nothing else — no cause, no word about the customer's money. There is now
an offline floor that names the cause from `cancel_reason` and deliberately
says nothing about money, because the evidence for that claim lives in a
backend that is not answering.

Also fixed: `_budget` accepted `True` as a one-second timeout (`bool` is a
subclass of `int`) and negatives and `nan`; `source="fallback"` was being
relabelled `"model"`, erasing the only signal that a template is broken;
`CancelledError` escaped the warm handler and burned a key's one attempt on
every shutdown; warm tasks were never drained, so deploys closed the HTTP
client out from under live provider calls; `CityMap` still drew a
Springfield box while the world moved to Islamabad, so every courier map
rendered an empty grid; the persisted city store needed a `version` bump or
returning users kept the old default; and `Search` held its own city while
its comment claimed it shared Browse's.

One finding was resolved as a test change rather than a code change: the
e2e asserted the exact prose of an explanation, and a model rewrite
("payment was **unfortunately** declined") failed it while the system was
correct. Asserting on a rewritable sentence is asserting on one sample —
the e2e now asserts `data-reason`, the stable contract.

## Known limits

1. **Cooking has no budget, so the engine reports elapsed and claims
   nothing.** "The kitchen has been preparing your order for 45 minutes" is
   a fact; "that is too long" is not one the system can support. Part A's
   FR-55 would change this.
2. **The road has no model.** `IN_TRANSIT` has an age and no deadline, and
   a post-hoc "why was my delivery slow" is reconstructible only to
   `picked_up_at`. OSRM ETAs stay deferred.
3. **Congestion is a point reading.** `active`/`capacity` is uncached and
   true at `as_of`; the engine cannot say whether a kitchen has been busy
   for an hour or got busy a second ago.
4. **One locale ships.** The key has a `locale` and the lookup falls back
   to `en`, so a translation is a dictionary entry — but nothing is
   translated yet, and the `{elapsed}` formatter's plural rule is English.
5. **The rewrite improves tone, not information, and is best-effort.** A
   chatty but factually clean rewrite passes every guard; the damage would
   be voice, not truth, which is the line `polish.py` draws on purpose.
6. **`REFUNDED` renders one sentence.** FR-86's refund story is thinner
   than its cancellation story because Part A's capture-after-delivery
   design means most "refunds" are voids, and the resolver cannot yet tell
   a void from a settled refund.

## What B6 inherits

A resolver that is pure and exhaustively tested, a renderer with no network
in reach, and a guard that lets a model touch customer-facing copy without
letting it touch customer-facing facts. B6 generates copy that a human
approves before publication — a different risk shape, but `polish.py`'s
rule (the guard is the guarantee; the prompt is only a request) is the one
worth carrying over.
