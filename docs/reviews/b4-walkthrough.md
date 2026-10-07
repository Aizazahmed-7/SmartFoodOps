# B4 — Recommendations walkthrough (2026-09-22 → 2026-09-28)

B3 left a turn that could answer a question. B4's job was to answer the
question nobody typed: what should I order, for this person, in this city,
around now, for this much money.

Everything here rests on one thing B3 did not have — **facts about what
people actually ordered**. `order_facts` carries totals and no item ids at
all, so recommendation acceptance and taste profiles were literally
unbuildable before FR-96.

## What landed

| Slice | Piece | Where |
|---|---|---|
| 4.1 | `order_item_facts` — one row per (order, dish) | `analytics/`, migration 0006 |
| 4.2 | City + time-of-day popularity, cold start | `domain/popularity.py`, `order_items`, `assistant.features.v1` |
| 4.3 | Taste profiles, built offline | `domain/taste.py`, `profiles.py`, `assistant.views.v1` |
| 4.4 | Budget as a hard predicate, co-order combos | `domain/combos.py` |
| 4.5 | Order-assistance answers | `domain/assistance.py`, `goes_with()` |
| 4.6 | Recommendation acceptance, derived | `adapters/attribution.py` |
| 4.7 | Panel surfaces, evals, this document | `AssistantPanel.tsx`, `tools/eval/` |

## The decisions worth remembering

**Facts, never counters.** Every aggregate in B4 — popularity, taste,
acceptance — is computed at READ time by grouping rows. Analytics' own
schema states the reason and B4 obeys it everywhere: `count = count + 1`
applied twice is a lie, and no natural key saves an increment. Proven by
resetting a consumer group to the topic's earliest offset and replaying:
the numbers did not move.

**The assistant projects orders itself**, rather than calling analytics.
Analytics owns the METRICS (FR-95/97/98); the assistant owns the FEATURES it
answers with. A synchronous hop would put a second service on the answer
path of every recommendation, and the assistant already holds the
city→restaurant mapping that makes the query correctly geo-scoped. One
topic, three consumer groups, three read models.

**Popularity ranks people, not portions.** Distinct orders, not summed
quantity — otherwise one catering order of forty naans outvotes a dish
twenty people chose. Scoped by RESTAURANT rather than by item, because the
index holds one chunk per (restaurant, item) and joining on items would let
a widely-franchised base dish win on arithmetic.

**Taste is content-based over declared tags**, not a learned user embedding.
"You often order vegetarian Pakistani food" is a sentence with evidence
behind it; a latent vector is not, and neither is anything a customer could
argue with. Below two orders there is no profile at all — a "personalised"
list built on one order differs from the baseline with nothing behind the
difference, which is worse than the baseline because it looks like it knows
something.

**A budget is measured against `min_total_cents`, not `price_cents`**
(ADR-0015's pricing engine refuses a line whose required group is unpicked,
so a dish whose cheapest Size is +300 can never be bought at its base
price). The floor is computed once where the modifier groups still exist and
is a fact from then on.

**Acceptance is derived from the order stream, never reported by a client.**
A surface that grades its own recommendations produces a number that
improves whenever the client changes, and nobody downstream can tell that
from the product getting better.

## What the live runs proved

**FR-96, convergence under replay.** Published a real-shaped `OrderPlaced`
with the same dish on two lines, then reset `analytics.facts.items` to the
topic's earliest offset and replayed everything:

```
menu_item_id | qty | line_total_cents
itm_karahi   |   3 |             3800   ← unchanged after full replay
itm_raita    |   1 |              350
```

**FR-75, the acceptance criterion as a comparison.** Same city, same moment,
two customers — one with a vegetarian history, against a city whose majority
orders meat:

```
stranger (basis: popular)      vegetarian customer (basis: taste)
  Mutton Karahi                  Garlic Naan      ← vegetarian
  Chicken Karahi                 Raita            ← vegetarian
  Garlic Naan                    Chicken Biryani
  Raita                          Seekh Kebab
                                 Mutton Karahi    ← the city's #1, their last
```

**FR-76, a hard predicate.**

| budget | dishes | combos |
|---|---|---|
| none | up to $5.20 | 3, up to $19.40 |
| $6.00 | up to $5.20 | 2 — the $19.40 pair gone |
| $2.00 | all ≤ $1.50 | **none fit** |

At $2.00 the combos vanish rather than being trimmed or shown over budget.

**FR-78, declared-only.** Mutton Karahi's description says "green chillies";
its declared tags say only `halal`:

> "**The restaurant has not listed a spice level** for Mutton Karahi. It is
> prepared with ginger, tomatoes, and green chillies."

**FR-79, derivation and convergence.** Showed five dishes, ordered one,
then replayed the entire orders topic from its earliest offset:

```
recommendation_acceptances: ord_d062257f80f0 → {itm_a56b9e0f…}
outbox: 1 RecommendationShown, 1 RecommendationAccepted
after full replay → still 1 and 1
```

## The eval suite

The golden set moved from Springfield to Islamabad mid-milestone, because it
was measuring a world nobody demos any more — 6 dishes against the 41 the
seed now builds.

```
retrieval     fail    4/5   mean 0.80   (0.60 on the old set)
groundedness  pass    4/4
refusals      pass    5/5
injection     pass    4/4
assistance    pass    4/4   ← new, FR-78
```

The move exposed two bugs **in the eval, not the system**:

1. **It measured the wrong thing.** `/v1/search` returns restaurants
   carrying their matched items, and the rubric flattened them in RESTAURANT
   order — so it ranked kitchens while every case named dishes. On a
   one-restaurant corpus those coincided; on a real city, RRF sums an item's
   score into its restaurant and a kitchen with four weak matches outranks
   one with a single strong match.
2. **Cases asserted an indefensible judgement.** `vague-spicy` demanded two
   specific dishes in the top 3 when five declare `spicy` — it failed for
   returning a different, equally spicy dish. Vague queries now name an
   ACCEPTABLE SET graded on precision, and the report labels which metric
   graded each case, because a column headed `recall` showing a precision is
   a report that lies quietly.

## The adversarial review

Four reviewers over four dimensions found **25 defects**, all fixed before
this milestone closed. Three were confirmed by running the code rather than
by reading it, and those three are the ones worth carrying forward.

**A taste profile counted LINES, not orders.** `build()` incremented per
row, so one order containing a curry and a naan reported `orders = 2` and
cleared `MIN_ORDERS` — shipping precisely what that constant's own docstring
says must never ship: "a difference with nothing behind it, which is worse
than the baseline because it looks like it knows something." Both tests that
should have caught it used single-dish orders.

**The sqlite hour band was dead for half the day.** `strftime('%H')` returns
`'07'` and the code compared against `str(h)` = `'7'`, so popularity matched
nothing between midnight and 10am. Production is Postgres, so it was never
an outage — but the dialect split exists *precisely* so the band would be
tested, and every fixture used noon, the one hour where an unpadded string
happens to be two digits.

**Combos scored a structural 0% acceptance**, because `record_shown` ran
before `combine` and never saw a combo — while the comment directly above it
claimed the opposite. A comment asserting behaviour the code does not have
is worse than no comment, because a reader trusts it.

The rest, by theme:

- **Hydration threw away every filter above it.** `texts_by_item` matched on
  `model_version` and `item_id` alone, so the taste path carefully excluded
  sold-out dishes and closed kitchens and then re-resolved them anyway — and
  since one `item_id` is served by every branch of a brand (ADR-0028), a
  dish could resolve to a branch in a different city. The same gap split
  co-ordered pairs across two branches, producing combos no cart could hold.
  Pairs are now grouped by restaurant and hydration is pinned to it.
- **One dish could fill every recommendation slot** — `menu_attributes`
  yields one row per (restaurant, item), and a six-branch brand contributed
  six identical candidates.
- **FR-80's promise was not kept on the endpoint that makes it.** A city
  with no orders in the hour band returned nothing. There is now a floor
  beneath popularity: any orderable dish, cheapest first.
- **The measurement could not be trusted or seen.** Showings were recorded
  several times per panel session (the effect keyed on a claims OBJECT that
  changes on every token refresh), and for lists that were never rendered;
  the outbox was stamped with the *order's* clock, which would have fired
  NFR-6's publish-lag alert on a healthy outbox; and nothing anywhere
  counted showings or acceptances, so none of it would have been visible.
- **The frontend** silently did nothing when "Add both" met a cart from
  another restaurant, kept the panel open across an account switch, said
  "nothing fits that budget" directly above a combo that did, left the
  previous city's dishes clickable during a refetch, and showed a base price
  on a card whose budget was evaluated against its floor.

**One finding resolved as a decision rather than a fix.** The budget covers
dish subtotals, not delivery fee and tax — a $20 budget can return a $19.90
pairing that quotes at $23.53. The customer is choosing dishes, not
approving a total, and a budget that silently reserved a fee would show them
fewer dishes than they can afford. The response now says
`budget_applies_to: "dish_subtotal"` rather than leaving a caller to assume.

## Known limits

1. **`vague-light` genuinely fails, and now it means something.** For
   "something light" the top three are a burger, a fizzy drink and a fried
   cutlet, against a deliberately generous nine-dish acceptable set. The
   diagnosis is specific: the scores are exactly `1/(60+rank)`, pure RRF
   positions with ZERO agreement between legs. Nothing on the menu contains
   the word "light", so the lexical leg contributes nothing and fusion
   degenerates to the vector leg's raw ordering — which, from Gemini at 512
   truncated dimensions, does not place "light" near salads. B2 named this
   as "the number to watch when a real embedder arrives"; it arrived, and
   the six-dish-corpus excuse is gone.
2. **`assistant.events` still has no consumer.** Both the interaction facts
   (B3) and the recommendation events are published and nothing reads them —
   that is B7's work, and until then the acceptance rate exists only as rows
   and a Prometheus counter.
3. **A guest order attributes nothing.** Acceptance needs a `user_id`, so
   anonymous ordering is invisible to the rate — the denominator only counts
   signed-in customers.
4. **The GET that returns recommendations has a side effect** (it records a
   showing). That is deliberate — the alternative is a client reporting its
   own impressions, which FR-79 rules out — but it means a client that
   polls inflates the denominator, and nothing currently stops one.
5. **`attribution_window_minutes` is a product judgement, not a derived
   number.** Two hours. The acceptance rate is meaningless without knowing
   which window produced it, and nothing publishes the window alongside the
   rate.
6. **Taste profiles are as fresh as the last builder pass** (15 minutes).
   `built_at` says how old an opinion is; a profile older than the interval
   means the builder has stopped, not that the customer stopped eating.
7. **Combos are pairs only.** The co-order signal for a triple is an order
   of magnitude sparser, and a "combo" nobody has ever bought is a
   suggestion with no evidence. Widening it is a data question, not a code
   one.
8. **Analytics' `order_item_facts` cannot be backfilled past topic
   retention.** The migration says resetting the group replays every order
   that ever existed — true only while those events are still on the topic.
   Seven orders in this environment predate it and have no item facts.

## What B5 inherits

An item-level fact table on both sides of the fence, three consumer groups
reading the order stream, and a golden set that measures the world people
actually use. B5's explanation engine amends Part A — `orders` gains
milestone timestamps stamped by `transition()` — and it is the first Part B
milestone that writes to a Part A table rather than reading from one.
