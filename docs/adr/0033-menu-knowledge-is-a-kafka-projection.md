# 0033 — Menu knowledge is a Kafka projection, chunked per item and per restaurant

**Status**: Accepted (2026-09-16)

## Context

ADR-0031 already ruled that **embedding projection rides Kafka** — "an idempotent upsert
keyed by content hash is a projection by definition" — and pointed here for the rest. The
rest is three questions the code cannot avoid answering: what a chunk *is*, what text goes
in it, and what happens when the same menu arrives twice.

Catalog hands us an unusually good input, and most of this decision follows from it.
`c1.catalog.changes` is **compacted and keyed by `restaurant_id`**, and every event carries a
**full effective-state snapshot** — profile plus the entire menu tree — because catalog's own
consumers needed the last surviving event per key to stand alone. Two consequences fall out
before we design anything: a replay of the compacted topic reconstructs the whole index with
no backfill script (FR-59), and there are **no tombstones** — a deleted dish arrives as an
*absence* from the next snapshot, never as an event announcing itself.

The brand fan-out (ADR-0028) is the other shaping force. A single base-menu edit stages a
full-state event **per branch**, so one sentence typed by one restaurant owner can arrive as
dozens of events carrying identical dish text. Embedding is the one operation in this
pipeline that costs real money per call.

## Decision

1. **Two chunk granularities: one per item, one per restaurant** (FR-58), in two tables
   (ADR-0032 §5). The restaurant chunk is not decoration — "Thai near me" matches a
   restaurant's identity, and there may be no single dish whose text contains "Thai" at all.

2. **Chunk ids are derived, never minted**: `{restaurant_id}:{item_id}` and
   `{restaurant_id}:_self`. This is the declared `NATURAL_KEY` dedupe mode (DoD-2): under
   at-least-once delivery a redelivered event upserts the same rows instead of growing the
   index. Ordering is strict-per-key and *nothing depends on it* — every payload is a full
   snapshot, so last-write-wins is genuinely last-state-wins.

3. **Chunk text carries durable facts only** — name, description, tags, category, cuisine
   (FR-60). Price, availability and status are **fields, not prose**: they narrow a query in
   SQL and are re-resolved live before a customer sees them. The test of the boundary is
   simple — if a kitchen can change it during service, it is not in the text.

4. **Item text deliberately omits the restaurant's name.** This is the fan-out economy:
   without it, a base dish inherited by twelve branches is one string and one embedding
   serving twelve rows; with it, twelve strings and twelve calls for one sentence. The
   restaurant chunk carries the name, and retrieval fuses both legs anyway.

5. **`content_hash` decides what to EMBED; it never decides what to write.** Rows are
   rewritten unconditionally on every drain — Postgres writes are cheap, provider calls are
   not — so a price edit, a pause or an 86'd dish updates columns and asks the provider for
   nothing, while a full topic replay rewrites every row and embeds **nothing at all**. The
   text is therefore held byte-stable on purpose: whitespace is collapsed, tags and cuisines
   are lowercased and **sorted**, descriptions are truncated where they are hashed. An
   unsorted tag join would silently re-embed a whole restaurant because two tags swapped
   places.

6. **Deletion is reconciliation, not an event.** Each drain writes the complete desired item
   set for a restaurant and deletes every other row for that restaurant and model version.
   There is no tombstone to listen for, and a dish left behind is not a stale row — it is the
   assistant recommending something nobody can order, which reads to a customer as a lie and
   to FR-70 as an ungrounded answer sourced from ingestion.

7. **Brands are never chunked, and neither is a branch without a city.** A brand is a menu
   template, not a place — catalog says so by leaving `city` and `status` null on brand rows
   — and every branch gets its own full-state event regardless, so skipping brands keeps
   template rows out of results *by construction* rather than by a filter someone must
   remember to write (FR-63). A branch with no city is unplaceable, and every query is
   geo-scoped: indexing it would create a row no predicate can reach and no operator can
   explain.

8. **The chunker is a pure, total function** — no clock, no I/O, no randomness, `Mapping` in
   and dataclasses out. Every question this milestone can get subtly wrong is decidable from
   the payload alone, so it is decidable in a unit test with no infrastructure. Malformed
   input degrades rather than raises: an item with no id is skipped, not fatal, because one
   bad row must not cost a restaurant its whole menu on a poison-message path.

9. **Chunk text is untrusted data and is not sanitised here.** It is restaurant-authored, and
   it reaches a prompt eventually. Escaping it at ingest would corrupt the embedding and give
   false assurance; delimiting and labelling it as data at prompt assembly (FR-71) is the
   only place that can be done correctly. This ADR's contribution is to say plainly that the
   column is tainted.

## Consequences

**Positive**

- The expensive operation is bounded by *meaningful* change, not by event volume. Routine
  kitchen churn — the majority of catalog traffic — costs zero embeddings.
- FR-59 is nearly free: truncate the tables, start a fresh consumer group, and the compacted
  topic rebuilds the index, embedding nothing because every hash already matches.
- The correctness-critical logic has no dependencies, so its tests are exhaustive and
  instant, and a reviewer can read the whole rule set in one file.
- Brand-template leakage — the failure mode most likely to embarrass a demo — is structurally
  impossible rather than filtered.

**Negative**

- Storage duplicates aggressively: a base dish inherited by twelve branches is twelve rows.
  That is the price of per-branch predicates (city, availability and deliverability genuinely
  differ), and rebuildability is what makes it safe.
- Volatile columns are up to one debounce window stale (NFR-28). They are pre-filters only,
  and the live re-resolution is authoritative — but a reader who forgets that will quote a
  wrong price, so the distinction has to survive in review.
- `content_hash` is only as good as the text recipe's stability. Any future change to the
  recipe re-embeds the entire corpus — correct, but expensive, and easy to do by accident
  while "just improving" the prose.
- Dropping the restaurant name from item text costs some retrieval signal on queries that
  name a restaurant and a dish together. The restaurant leg is expected to recover it; if the
  eval set (FR-104) says otherwise, §4 is the clause to revisit.

**Revisit trigger**: eval evidence that item chunks need restaurant context after all; a
chunking change driven by recall@k; per-item chunks proving too coarse (a long menu
description wanting its own sub-chunks); or catalog gaining real tombstones, which would make
§6's reconciliation optional rather than load-bearing.

## Amendment (2026-10-09) — `content_hash` is computed, not stored

§5's rule is unchanged: a digest decides what to **embed** and never decides
what to **write**. Only the storage went (migration `0018`).

The drain now compares `content` itself, which is already on the row because
B2's lexical leg reads it. A stored `sha256(content)` was a derived column
that could disagree with its own source, and the consequence of disagreement
was silent: a chunk whose text had changed but whose hash had not would keep
a stale vector indefinitely. Comparing the text removes that failure mode
entirely rather than defending against it.

The digest still exists in memory during a pass, collapsing chunks that
share text within one restaurant (the same drink under two categories is two
chunks and one vector). §8's caveat — that the digest is only as good as the
text recipe's stability — now applies to that de-duplication alone.
