# B1 — Knowledge pipeline walkthrough (2026-09-16 → 2026-09-17)

> Six slices plus one unplanned repair, each reviewed before the next
> started. Written as the work landed. Branch: `init-part-b`.

## What B1 had to prove

A menu edit reaches a vector index inside a freshness budget, the index can
be rebuilt from the log alone, and neither of those costs a provider call it
does not have to. Retrieval *quality* is B2's problem and is deliberately
not claimed here.

## The slices

| # | Scope | Ends with |
|---|---|---|
| 0 | pgvector substrate (ADR-0032) | `make up-ai` on the new image, existing volume intact |
| 1 | `item_chunks` + `restaurant_chunks`, `VectorStore` port | schema live, HNSW usable by `<=>` |
| 2 | Pure chunking + ADR-0033 | 25 tests, no infrastructure |
| 3 | `assistant.knowledge.v1` + the debounce queue | 16 events → 1 queued pass |
| 3.5 | *(unplanned)* compaction repair in `smartfood-kafka` | topic actually compacted |
| 4 | The drain: embed, borrow, reconcile | menu edit → index in 31 s |
| 5 | Rolling reindex (FR-61) | model bump with no empty window |
| 6 | Republish, rebuild demo, evals, walkthroughs | this document |

## Decisions made while building

**Two chunk tables, not one with a `kind` discriminator** (ADR-0032 §5,
amended). The first cut was a single `menu_chunks` with `item_id`,
`category`, `tags` and `price_cents` null on restaurant rows. The user
pushed back on the nullable columns, and the strongest argument turned out
not to be tidiness: *the two vector spaces are not comparable*. An item
chunk embeds "Chicken Biryani | spicy, halal | Mains"; a restaurant chunk
embeds "Biryani House | pakistani, bbq". A query embedding sits
systematically closer to one text SHAPE than the other, so a single
`ORDER BY embedding <=> q` across both ranks a mediocre restaurant above an
excellent dish for reasons unrelated to the question. FR-62 fuses the legs
anyway, so the split costs no extra query and buys `NOT NULL` that means
what it says.

**Durable facts are text; volatile facts are columns.** Price, availability
and status are excluded from the embedded text (FR-60) and carried as
filter columns — narrowing here, truth at the live snapshot. The test of the
boundary: *if a kitchen can change it during service, it is not in the text.*

**Item text omits the restaurant's name**, which is what makes the ADR-0028
fan-out affordable: a base dish inherited by twelve branches is one string
and one embedding serving twelve rows. Proven live in slice 6's rebuild —
the third branch reported `borrowed=3`.

**Per-city partial indexes were abandoned while implementing** (ADR-0032 §5,
amended a second time). A partial index per city is DDL keyed on DATA, so
the set could only be completed by an ingestion path issuing `CREATE INDEX`
for a city it had never seen. Schema writes from a data path is a worse
failure mode than a filtered scan. Declarative LIST partitioning by city is
the named escalation.

**The debounce window is FIXED, not sliding.** On conflict the pending row
keeps the EARLIER deadline. A trailing debounce restarts on every event, so
an owner editing twenty dishes over five minutes would never be indexed at
all — while the queue reported itself healthy the whole time.

**Completion is guarded on the staged payload's fingerprint.** The drain
reads a row, then spends seconds embedding with no transaction open. An
unguarded delete would silently discard an edit that landed in that gap, and
nothing anywhere would report the index as stale. ADR-0039's rule applied
literally: key the write on the state it protects.

**`knowledge_pending` is a table, not a broker.** Asked directly: why not
RabbitMQ, Kafka or Celery? Because this is not a queue — it is a coalescing
keyed store with a deadline, and the operation that makes it work is upsert
by primary key. Brokers are append-only; none of them can *replace the
pending item for key K*. Celery and RabbitMQ give the delay and not the
coalescing, so either would still need this table plus a broker hop and a
second place to be inconsistent. Kafka Streams has exactly this primitive
(suppressed windowed aggregation) and is a JVM product. The cost, stated:
polling instead of push, and queue-in-a-database is a real anti-pattern at
high write rates — menu edits are human-authored, so the volume is orders of
magnitude below where that bites.

## The unplanned repair (slice 3.5)

The consumer joined its group and consumed nothing. `c1.catalog.changes` had
equal earliest and latest offsets — the log was empty — and no topic-level
config at all, so it was running on the broker default: `cleanup.policy=delete`,
seven-day retention.

`ensure_compacted_topic` set the policy only inside `create_topics` and
swallowed `TopicAlreadyExistsError` as "the normal case after first boot".
Kafka auto-creates a topic on first produce or subscribe, so whichever client
touched it first won and the config was never reconciled.

The first fix inherited the same blind spot, and the live test showed it:
**aiokafka does not raise for an existing topic on this version** —
`TOPIC_ALREADY_EXISTS` arrives as `error_code=36` *inside* the
`CreateTopicsResponse`. The original `except` clause had never fired at all.
The helper now inspects the response, reconciles an existing topic's policy,
and carries forward operator-set overrides (`alter_configs` is not
incremental, so a correction that dropped them would be a regression).

This mattered well beyond B1. Catalog stamps every event with the complete
menu *specifically* so the last surviving record per key stands alone —
identity's grant convergence and FR-59's rebuild both assume something
survives. On a delete topic past the retention window, nothing does.

Compaction cannot resurrect what retention already deleted, so slice 6 added
`POST /v1/internal/catalog/republish` — a one-off repair for that gap, and
thereafter the ordinary way to rebuild a downstream projection.

## Live proof

| Claim | Evidence |
|---|---|
| FR-57 debounce | 16 catalog events (8 branch + 8 brand) → **1** queued pass, window `00:00:30` |
| FR-58 freshness (NFR-28) | menu edit 12:33:17 → indexed 12:33:48, **31 s** against a 60 s budget |
| FR-58 economy | price change → `embedded=0`, `price_cents` moved; full-catalog replay → `embedded=0` on every restaurant |
| Fan-out borrow | rebuild's third branch: `items=3 embedded=1 borrowed=3` |
| **FR-59 rebuild** | truncate both tables + delete the consumer group → index reconstructs from the compacted topic alone. Fingerprint before `42613c91bffb780eb7f5f960c5609676` (17 chunks); after: **identical** |
| FR-61 rolling reindex | partial run left both generations present with the pointer unmoved; completion activated the new space and retired 7 rows |
| FR-63 | brand payloads never reach the queue — 8 brand events, 0 rows |
| Hybrid predicates | `spicy AND ≤$10 AND springfield` + cosine: price, tag, city and availability each excluded a candidate |

Gates at close: **1170 tests, 100 % coverage**, ruff + pyright clean.

## Adversarial review (2026-09-19)

`/code-review` at high effort over the whole B1 diff — the scale the working
agreement sets for a projection pipeline with no money, saga or lock in it.
Five findings, all fixed before the commit:

1. **The slice 3.5 fix was a boot regression.** `_was_created` re-raised
   every CreateTopics error but 36, so on a cluster where topics are
   IaC-provisioned and the principal lacks CREATE/ALTER_CONFIGS — the normal
   least-privilege posture — `TOPIC_AUTHORIZATION_FAILED` would propagate out
   of catalog's lifespan and crash-loop the container. Authorization codes
   now return "not ours to say" and the reconcile is best-effort: the topic
   is writable either way, and the loss is a policy check worth a loud log
   line rather than an outage.
2. **The active version could name a space nothing writes.**
   `_model_version(settings)` keyed on the API key alone while `_embeddings`
   also required an http client, so a caller passing `providers=` with a key
   set would adopt the real model's version while the drain wrote the fake's
   — retrieval filtering on a generation with zero rows, silently. Now one
   embedder instance is built and the version is derived *from it*. Sharing
   the `FAKE_MODEL` constant had not fixed this, because the duplicated
   thing was the **condition**, not the literal.
3. **`restaurant_chunks` had no `(restaurant_id, model_version)` index**
   while `item_chunks` did, so every `hashes_for` seq-scanned it — a full
   scan per restaurant per drain pass at catalog scale.
4. **The reindex conflict path could pair new content with a stale vector.**
   `copy_forward` updated `embedding` but not `content`, so a drain write
   landing mid-migration left a row whose vector did not describe its text,
   with `content_hash` still matching. Now `ON CONFLICT DO NOTHING` — the
   drain's row is the fresher of the two.
5. **`tools/eval` sat outside the 100% gate** (missing from `--cov=`), which
   is a particular irony for the file whose job is refusing to report
   unmeasured rubrics as passing.

## Known limits, stated rather than discovered later

1. **The reindex visibility window.** The production sequence is: change the
   model → deploy → the drain writes the new version while retrieval still
   reads the old → reindex → cutover. During that window a restaurant edited
   by its owner lands only in the target generation, so retrieval serves its
   *old* content until the flip. Bounded by the reindex duration; the fix
   (dual-write both generations while migrating) is meaningfully more
   complex and is not built.
2. **The fake embedder is lexical, not semantic.** It is feature hashing:
   texts that share words come out close, texts that share meaning do not.
   Every B1 claim above is about plumbing and identity, none about
   relevance. `model_version` carries `fake-hashing-v1` into every row and
   every query predicate, so a fake corpus is queryably fake.
3. **`REFRESH COLLATION VERSION` is documented and untested** on this box,
   because ADR-0032 §2 made it unnecessary (ADR-0032's own negatives say so).
4. **Retrieval recall under a selective filter is unmeasured.** pgvector
   applies the hard predicates as a post-filter on the ANN scan, which can
   under-return. An attempt to measure it during slice 1 proved nothing —
   `hnsw.ef_search` was set in a session where the extension's GUCs were not
   loaded, so the "raised ef_search" comparison was meaningless. It belongs
   to B2, tuned against the golden set rather than guessed at.
5. **The eval rubrics are all `n/a`.** The harness ships; the cases arrive
   with the milestones that can answer them.

## What B2 inherits

`VectorStore` with the retrieval methods still undeclared (the same
discipline B0 applied to the port itself), `content` stored for the lexical
leg, `knowledge_index_state` naming the space to query, and the hard
predicates already columns. The first thing B2 should do is decide
`hnsw.ef_search` / `hnsw.iterative_scan` against real embeddings and a
golden set — that is the measurement B1 could not honestly make.
