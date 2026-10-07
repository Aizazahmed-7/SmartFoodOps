# 0045 — The answer cache, and what it is fenced against

**Status**: Accepted (2026-09-22)

## Context

NFR-24 is blunt about where the ceiling is: at 2,500 orders/s, 5% engagement
× 3 turns is ~375 generations/s, and **provider quota, not CPU, is the
binding constraint**. Cache tiers are named there as load-bearing rather
than as an optimisation. FR-74 asks for the concrete shape: exact-match then
semantic, both fenced by `menu_version` and geo bucket, hit ratio visible on
`assistant_cache_total{tier,result}`.

The danger in caching an assistant's answers is not staleness in the
abstract. It is that a stale answer is *indistinguishable from a fresh one*
— fluent, confident, and about a dish that was taken off the menu this
morning. A cache here is a correctness surface, not a latency knob.

Two things make it tractable at all. Answers carry no prices and no
availability (FR-60: the client re-resolves both live from Catalog's
cache-bypassing snapshot), so the only thing a hit can get wrong is prose
about which dishes exist. And the assistant's corpus is already a
projection we control, so we can know exactly when it moved.

## Decision

### 1. Two tiers, at different points in the turn

    guard ─────────(refuse)───────────► END
      │
    exact_cache ───(hit)──────────────► END      Redis, skips everything
      │
    retrieve ──────(no match)─────────► END
      │
    semantic_cache (hit)──────────────► END      pgvector, skips generation
      │
    generate ──► ground ──────────────► END

Exact match runs **first** because it needs nothing — no embedding, no
query — so a hit collapses the whole turn to one round trip.

Semantic match runs **after retrieval**, which is the non-obvious half. Its
input is the query vector, and the query vector is retrieval's own output.
In front of retrieval it would have to embed the question itself, so every
miss would embed twice — and a cache that doubles the embedding bill on
every miss is not a cache. Placed after, it costs one indexed ANN lookup and
still skips the expensive part. Measured live: 2432ms cold, 3.7ms exact,
433ms semantic.

### 2. One fence: `(model_version, city, epoch)`, captured ONCE

*Amended after the B3 review.* The fence is resolved before retrieval and
carried through the turn to the write-back. It used to be resolved again at
write time, which falsified §3 below: a generation takes seconds and the
drain ticks every five, so an answer computed under epoch N was routinely
stored under N+1 — the one epoch it is certainly wrong for — and then served
to everyone who asked. A write under a SUPERSEDED epoch is harmless because
nothing will ever read it; a write under a future one is the bug.

`city` is part of the row's PRIMARY KEY and not only a filter (migration
0010). With the key as `(question, model_version)`, two cities asking the
same question overwrote each other and both then missed, so the more popular
a question was across cities the closer its hit rate got to zero.

An answer is cached only when `cacheable()` says so, and that function is
now actually called — the rule had been inlined in the graph, which is how
the ungroundedness case came to be missing from it. **An answer with a
fabricated citation is never pinned**: ADR-0043 §2 accepts degrading one
sentence, not replaying it from a tier that bypasses retrieval and grounding
for the next hour.

Every part earns its place. `model_version` because a question vector
written by one embedder is meaningless to another, and the rolling reindex
means two coexist (FR-61). `city` because retrieval is geo-scoped (FR-63) —
FR-74's "geo bucket", and the coarsest bucket that is still correct; a finer
one would shred the hit rate for no additional truth. `epoch` is FR-74's
"menu_version", below.

**The fence is IN the key, never checked after the read.** A key that can be
read and then rejected is a key that will one day be read and not rejected.
This way a stale entry is unreachable rather than merely refused.

### 3. `menu_version` is a per-city epoch the drain bumps

The assistant's corpus is a Kafka projection, not a versioned blob (ADR-0027
retired the versioned-blob scheme for the same reason). So the version is a
counter per city in `knowledge_epochs`, incremented **in the same
transaction as the chunk write that changed the city**. A menu change and
its cache invalidation are one commit, so there is no window in which a
customer can be served an answer about a dish that no longer exists.

It bumps when anything RETRIEVAL can see changed — text, `available`,
`status`, or the city itself. `content_hash` deliberately excludes
availability and status (FR-60), which is right for deciding what to
re-embed and wrong for deciding whether the cache is stale: both are hard
filters in the retrieval SQL, so an 86'd dish or a paused kitchen changed
what a query returned while leaving every hash identical. A branch that
moves city bumps BOTH cities, or the one it left keeps recommending it.

It does not bump when only a price changed, or when nothing changed. A
compacted topic re-delivers and Catalog republishes a restaurant whenever
anything about it moves, so bumping on every drain pass would cold-start the
whole city's cache on traffic that changed no menu text. A **price** change
moves nothing: price is a column and not prose (FR-60), the client resolves
it live, and the kitchen editing prices all afternoon is the common case.

A counter and not a timestamp: two drain passes inside one clock tick would
produce the same "version", and the one entry written between them would
outlive the change that should have killed it.

### 4. A turn with history is never cached, in either direction

"What about something spicier?" means nothing without the turn before it.
Caching it under its own text would hand one conversation's context to the
next person who types those words. **No fence catches this** — nothing about
the corpus is wrong; the context is — so history disqualifies a turn from
both lookup and write-back.

Refusals and no-matches are not cached either: a refusal is already fixed
text and costs no provider call (ADR-0043), so caching saves nothing, and
pinning "nothing matched" risks a stale negative in a city that just gained
a restaurant.

### 5. Both tiers fail open, and the TTL covers what the fence cannot

A cache that can break a turn is a liability, so every error degrades to a
miss and a write that raises is swallowed — the answer is already correct
and already streaming by then.

**Both** tiers carry an age bound, not just Redis — `answer_cache.created_at`
was written and never read, so in a city whose menus were quiet nothing ever
aged out and a prompt fix would never have reached it. The TTL covers a
different thing from the fence. The fence catches corpus changes. The TTL bounds how long a staleness
we did **not** think to fence against survives: a prompt edit, a model swap,
a grounding rule tightened. An hour is short enough that nobody ships a fix
and waits.

### 6. A cache hit is an answer, and says so

`cache_tier` rides on the interaction fact (ADR-0044) beside `duration_ms`,
and `outcome` stays `answered`. Marking a hit as "stopped" would fold it in
with refusals and no-matches, so "questions answered" would fall every time
the cache got better; and without the tier, FR-95's "average AI response
time" averages a 4ms hit against a 2s generation and stops describing
anything.

## Consequences

**Positive**

- The binding constraint moves. A repeated question costs one Redis GET
  instead of an embedding plus four queries plus a generation.
- Invalidation is free and total: nothing is swept or deleted, entries under
  an old epoch simply stop being addressable and age out.
- The whole policy — what may be cached, under what name — is pure functions
  in `domain/answers.py`, testable with no Redis, no Postgres and no key.

**Negative**

- The exact tier costs a small Postgres read before it can build its key
  (the epoch and the active model version are both resolved live, because
  both move while the process is up). A hit is therefore one indexed read
  plus one Redis GET, not a bare Redis GET.
- `answer_cache` rows accumulate under superseded epochs and nothing
  currently reaps them. The fence makes them harmless but not free: the ANN
  index grows with every menu change. A reaper is owed before this sees
  real traffic.
- The similarity threshold is a correctness knob wearing a latency knob's
  clothes. Too loose and the semantic tier answers a question nobody asked.
  0.12 cosine distance is "a rephrasing", tuned against the golden set, and
  it needs re-tuning whenever the embedding model changes.
- Two more places a conversation's text lives, and NFR-32's 90-day purge
  covers neither automatically — the Redis entries expire, but
  `answer_cache.question` is retained text with its own retention story.

**Revisit trigger**: `answer_cache` growth outpacing the drain (the reaper
above becomes urgent); a semantic hit rate that stays near zero, which would
mean the threshold or the embedding is wrong rather than the idea; or
per-user personalisation (B4's taste profiles), which would make an answer
cacheable for one customer and wrong for another and force the fence to
grow a user dimension — or the tier to be switched off for personalised
turns.
