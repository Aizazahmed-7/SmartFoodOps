# B2 — Semantic discovery walkthrough (2026-09-21 → 2026-09-23)

> Seven slices. Written as the work landed. Branch: `init-part-b`.

## What B2 had to prove

A customer's phrasing reaches the right dish, `/v1/search` keeps answering
when the GenAI plane is dead, and retrieval stays inside the latency budget
Part A set before any of this existed.

Two of those are proven. The third — "a customer's phrasing" — is proven for
**words** and measured-and-failing for **meaning**, because no real embedding
provider has been connected. §Evaluation says exactly how far off it is.

## The slices

| # | Scope | Ends with |
|---|---|---|
| 0 | FTS + trigram indexes over `content` | planner uses `ix_item_chunks_fts`; `'biriani' <% content` finds Biryani |
| 1 | RRF fusion, `Filters`, `predicates()` | 17 tests, no infrastructure |
| 2 | Both legs, `PostgresRetriever` | filtered-ANN recall measured and fixed |
| 3 | `POST /v1/internal/assistant/retrieve` | SystemOnly, ids not cards |
| 4 | Catalog's `HybridSearch` + fallback | **assistant killed → `/v1/search` still 200** |
| 5 | FE search | city actually sent; closed badge |
| 6 | Golden set, p99, this document | 4/5, p99 91 ms |

## Decisions made while building

**Fusion is by RANK, not by weighted score.** A cosine distance and a
`ts_rank` share no scale, no distribution, and no monotonic relationship;
any weighted sum of the two is a number with no meaning, whose weights would
need re-tuning whenever either leg changed. The test that carries this is
`test_agreement_beats_either_leg_alone`: a candidate ranked *second in both*
legs beats one that tops either, because agreement between two blind systems
is the strongest signal available.

**One `predicates()` composes the WHERE tail for both legs.** If the legs
could drift apart on scoping, one would eventually return a paused
restaurant or another city's menu — and the fusion would launder it into the
result set as though both legs had agreed.

**Retrieval returns ids; Catalog returns cards.** The assistant ranks, and
names and prices are read from catalog's own tables at request time. That is
not a layering preference: it is what makes the index's up-to-60s-stale
`price_cents` *structurally* unable to reach a customer through search,
rather than a rule someone has to keep remembering. FR-60, bought rather
than enforced.

**The lexical leg lives in `assistant_db`.** Catalog's `HybridSearch` calls
the assistant; calling back into catalog's `SearchPort` for the lexical leg
would be a cycle. We already store `content` — by construction the exact
text that was embedded — so both legs read one string and can never disagree
about what a chunk says.

**Each leg over-fetches 4×.** Fusion promotes the candidate both legs ranked
middling, and that candidate only exists in the merged set if each leg was
asked deep enough to include it. Fetching exactly `limit` per leg makes the
fusion decorative.

## The two things live testing changed

**1. Filtered ANN silently under-returns.** B1 flagged this and said it could
not measure it. Measured here, 20k chunks with a filter matching 400:

    LIMIT 10, hnsw.iterative_scan = off (the default)  ->  8 rows
    LIMIT 10, hnsw.iterative_scan = strict_order       -> 10 rows

pgvector applies a `WHERE` clause as a post-filter on the ANN walk, so under
a selective filter most of what it visited is discarded and the query
returns fewer rows than asked — no error, no warning. `strict_order` because
RRF fuses by rank: rows arriving out of distance order feed the fusion a
rank the index never meant. `iterative_scan` rather than a large fixed
`ef_search` because it costs time only on the queries that need it.

**The fix had to move.** It was first written as an `ALTER DATABASE` in a
migration, which failed with *permission denied to set parameter* and
crash-looped the service. Until pgvector's library loads, `hnsw.*` are
PLACEHOLDER parameters, and Postgres requires superuser to set a placeholder
it cannot classify — database ownership is not enough. It lives in `initdb`
as superuser now, beside the extension pre-creates. That is the third time
GUC registration order has bitten this project; it is written down where the
next person will hit it.

**2. A config flag was silently changing what search returned.** The
assistant filters `status = 'open'` (FR-63), `PostgresSearch` does not — so
turning `hybrid_search` on made paused restaurants vanish from search, and
turning it off brought them back. The card carries `status` precisely so a
client can badge a closed restaurant and still let a customer browse, which
makes dropping them the regression rather than the fix. FR-63 governs what
the assistant *recommends*; catalog's search contract is a different thing.
`open_only` is now a request parameter, strict by default, and `HybridSearch`
asks for `false`. **A fallback that returns a different set from the primary
is not a fallback — it is a second product behind a flag.**

## Live proof

| Claim | Evidence |
|---|---|
| Lexical leg (FR-62) | planner picks `ix_item_chunks_fts`; `'biriani' <% content` → Chicken Biryani at the 0.35 threshold |
| Hard predicates (FR-62/63) | `spicy AND ≤$10 AND springfield`: price, tag, city and availability each excluded a candidate |
| Retrieval API | ids and scores, `401` unauthenticated, `403` as a customer |
| **Fallback (FR-65)** | assistant stopped → `karahi`/`biriani`/`naan` all **HTTP 200 in ~160 ms**, 6 degradation lines logged; restarted → 0 |
| Fallback parity | restaurant paused → **identical result on both paths** (1 hit, status `paused`) |
| FE | city chips send a city; "jalapeno" returns both `daily deli` branches with the dish surfaced |
| **NFR-22 latency** | 20k chunks, 300 requests: p50 61 ms, p95 75 ms, **p99 91 ms** against a 150 ms budget |
| Evaluation (FR-64) | 4/5, mean recall 0.80 — see below |

Gates at close: **1238 tests, 100 % coverage**, ruff + pyright clean, `tsc` +
`vite build` clean.

## Evaluation, and the one thing B2 does not deliver

`make eval` runs the golden set against `/v1/search` — the customer path, not
the retriever, because fusion, hydration and the fallback can each be wrong
in ways no component test would show. It **refuses to score a fallback**: if
the assistant is unreachable, search still answers perfectly and grading it
would report the lexical path's numbers as the semantic path's.

    FAIL vague-light    0.00  0.00  Chicken Karahi, Chicken Biryani, Mutton Karahi
                                    expected: Raita, Garlic Naan
    ok   vague-spicy    1.00  1.00
    ok   typo-biriani   1.00  1.00
    ok   exact-name     1.00  1.00
    ok   category-drinks 1.00 1.00
    mean                0.80  0.80

Read that honestly. Four cases pass **on the lexical leg**: "spicy" is a
literal tag in the chunk text, "biriani" is the trigram pass, "cold drink"
reaches "Drinks", and an exact name is the floor. The one case that requires
*meaning* rather than *words* fails completely — no dish contains "light",
and the fake embedder is feature hashing, which matches tokens.

So B2's semantic half is **built, wired, measured and unproven**. The
pipeline demonstrably carries whatever the embedder gives it; nothing has
given it semantics yet. Setting `OPENAI_API_KEY` bumps `model_version`,
which the B1 rolling reindex migrates without an empty window — the mechanism
is built and proven, it has simply never had a real model to migrate to.
`vague-light` is the number to watch when it does.

## Known limits

1. **No real provider has ever been called in this project.** Not for
   generation, not for embeddings. B0's walkthrough records a two-provider
   streaming demo as its exit criterion; that claim was reconstructed from
   code and should be treated as unproven until someone watches it happen.
2. **p99 was measured on one developer's box** against a 20k-row synthetic
   corpus, single-client, warm cache. It says the shape is right and the
   index is used; it is not a capacity result. That belongs to B7.
3. **`hnsw.iterative_scan` is set by `initdb`**, which a real deployment does
   not run. Aurora needs the same parameter set by IaC, and nothing here
   would notice if it were missing — recall would simply be quietly worse.
4. **Reranking (FR-66) is not built.** It is a P2 and explicitly wants to be
   measured against no-rerank on this golden set before being enabled, which
   is not possible until the golden set means something.

## What B3 inherits

A retriever behind a port, an internal API that already speaks ids, a golden
set with a rationale on every case, and a harness that will not lie about an
unmeasured rubric. The `groundedness`, `refusals` and `injection` rubrics are
declared and empty — B3 fills them, and FR-70's "every item id in an answer
came from the retrieved set" has a retrieved set to be checked against.
