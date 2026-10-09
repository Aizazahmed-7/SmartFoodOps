# 0032 — Vectors live in the shared Postgres behind a `VectorStore` port; the pgvector image is base-matched and pinned

**Status**: Accepted (2026-09-16)

## Context

B1 is the first milestone that needs a vector. Three ADRs already point here for the answer
and none of them gave it: ADR-0029 §4 cites this record for Catalog's hybrid `SearchPort`
adapter, ADR-0030 §3 puts `EmbeddingPort` and `VectorStore` "under the same rule", and
ADR-0031's I/O table lists it beside 0029 and 0030. `ai_assistant/domain/ports.py` says in
as many words that `VectorStore` is deliberately undeclared until this decision lands. So
this ADR owes two answers: **where vectors are stored**, and — because B0 found the problem
live rather than in a doc — **which Postgres image the dev stack runs to get them**.

**The store.** The corpus is menu text: one chunk per item plus one per restaurant
(FR-58), geo-partitioned per city because ADR-0029 already made the assistant cell-scoped
and its index per-city. It is bounded by the size of the catalog, not by order traffic, and
it is queried under NFR-22's p99 < 150 ms — the same budget ADR-0019 set for lexical
search, because `/v1/search` may now route through it. A dedicated vector database (Qdrant,
Weaviate, Milvus) buys ANN features this corpus does not need yet, and costs a second
datastore to run, back up, secure and reason about — on a dev box that already OOMs at
7.7 GB, and in a prod estate ADR-0016 deliberately narrowed to *one* Aurora cluster with one
logical database per service. Hybrid retrieval (FR-62) also wants the lexical leg and the
vector leg to see the same hard predicates — city, open, price band, brand-template
exclusion — which is one SQL query when both legs live in Postgres and a distributed join
when they do not.

**The image.** `postgres:15` is a moving tag, and it moved: it is now Debian trixie
(glibc **2.41**), while `pgvector/pgvector:pg15` is bookworm (glibc **2.36**). Postgres
reads that as a **collation-version downgrade** and refuses — every existing database warns
and `template1` errors, which blocks `CREATE DATABASE` outright, so `initdb/01-databases.sh`
exits 3 and no service database can be created at all:

```
ERROR:  template database "template1" has a collation version mismatch
make: *** [up-ai] Error 3
```

The root cause is not pgvector. It is that a stateful volume was pinned to a tag that tracks
a distribution — on *both* sides. `pgvector/pgvector:pg15` defaults to bookworm, but the
project publishes a **`-trixie` variant in lockstep with every release**, and B0 tested only
the default. Verified live while this ADR was being written: `0.8.6-pg15-trixie` is
PostgreSQL 15.19 on Debian trixie, glibc **2.41-12+deb13u3** — byte-identical to the build
that initialised `pg-data`.

## Decision

1. **Vectors live in `assistant_db`, in the same Postgres cluster as every other service
   database, via the `vector` extension.** No dedicated vector store, no second Postgres
   container. `ai-assistant` owns the table; nothing else reads it (ADR-0029 §3).

2. **The dev stack runs `pgvector/pgvector:0.8.6-pg15-trixie` — base-matched and pinned to a
   tag that names its distribution. No volume rebuild, no re-seed.** Same glibc line as the
   image that initialised `pg-data`, so there is no collation event to resolve. Proven
   against the live volume, not argued: the container starts healthy, the initdb convergence
   script that exited 3 on the bookworm tag now exits 0, every database — `template1`
   included — still reports `datcollversion = 2.41`, and `pg_am` carries `hnsw`. **The
   `-trixie` suffix is load-bearing**: dropping back to `:pg15` silently re-enters the
   downgrade. A stateful volume may not follow a moving base on either side of the swap.

3. **`CREATE EXTENSION vector` is pre-created by `initdb/01-databases.sh` as superuser,
   exactly like `pg_trgm` for Catalog (ADR-0019).** The B1 migration then runs
   `CREATE EXTENSION IF NOT EXISTS vector` and no-ops as `assistant_svc`, which holds no
   superuser rights and is not being given any.

4. **A volume on a different glibc line is rebuilt (`make nuke`), not migrated.** §2 means
   nobody on the current stack needs this — but a volume predating the trixie move, or a
   future base change, lands back in the same place. Dev data is disposable by construction
   (`make seed` is deterministic and idempotent against real APIs), so the fresh volume is
   the route. `REFRESH COLLATION VERSION` + `REINDEX` stays documented in
   `docs/local-dev.md` §3 as the escape hatch for a volume someone must keep, with its
   warning attached: on a *downgrade*, text indexes built under the higher version are
   suspect until rebuilt, and that failure mode is silent wrong ordering rather than a loud
   exit 3.

5. **Two chunk tables — `item_chunks` and `restaurant_chunks` — one HNSW index each; city
   and `model_version` are query predicates, not separate indexes.** The tables split
   because the two vector spaces are not comparable: a query embedding sits systematically
   closer to one text shape than the other, so a single `ORDER BY embedding <=> q` across
   both would rank a mediocre restaurant above an excellent dish for reasons unrelated to
   the question. FR-62 fuses the legs anyway, so the split costs no extra query and buys
   `NOT NULL` that means what it says. **Durable filters ride as columns** (`tags`,
   `cuisines`, `category`, `city`, `brand_id`) with GIN behind the array ones; **volatile
   filters ride as columns too but only as PRE-FILTERS** (`price_cents`, `available`) —
   FR-60 bars them from the embedded *text*, not from the row, and they narrow a query
   cheaply while the live snapshot re-resolution stays authoritative for anything a customer
   sees. HNSW over IVFFlat because it needs no training step against a corpus
   that is still being built and has no recall cliff as that corpus grows. **Amended while
   implementing (B1 slice 1)**: this clause first said *per-city partial indexes*, and
   writing them exposed why that cannot stand — a partial index per city is DDL keyed on
   DATA, so the set could only be completed by an ingestion path issuing `CREATE INDEX` for
   a city it had never seen. Schema writes from a data path is a worse failure mode than a
   filtered scan. The geo-scoping FR-63 requires rides the query with
   `ix_menu_chunks_scope (model_version, city, kind)` behind it, and **declarative LIST
   partitioning by city is the named escalation** — per-partition HNSW comes free there and
   adding a city stays an ops action. `model_version` sits in the predicate because vectors from different models *or different dimensions* are not
   comparable — today `text-embedding-3-small` truncated to **512** dimensions, so
   `vector(512)`. A model or dimension change is a `model_version` bump and a rolling
   reindex (FR-61), never an in-place edit, and queries filter on the active version so the
   two spaces never mix in one result set.

6. **Retrieval sits behind `VectorStore`, declared in B1 with the chunking it serves**, and
   is reached from outside the service only over `ai-assistant`'s internal HTTP API.
   Catalog's hybrid `SearchPort` adapter (ADR-0029 §4) calls that API flag-gated and
   timeout-bounded and **falls back to `PostgresSearch`** — Catalog gains no `vector`
   extension, no `menu_chunks` read, and no new failure it cannot degrade out of. How the
   two legs are fused is B2's problem, not this record's.

7. **Prod is Aurora's managed `vector` extension on the existing `sfo-aurora-main` cluster**,
   not a new datastore. **Unverified against the live cluster**: the minor version must ship
   a pgvector new enough for HNSW — confirm before B1's schema migration is written, because
   an IVFFlat-only cluster changes §5, not the rest of this decision.

## Consequences

**Positive**

- One datastore, one backup story, one connection-pool posture (ADR-0016's PgBouncer in
  transaction mode) — the vector index inherits all of it for free.
- Hard predicates and the vector search are the same query, so FR-63's scoping (deliverable,
  open, no brand templates) cannot drift between the lexical and vector legs.
- The collation trap is closed without a rebuild: no volume in the estate has a glibc line
  its image disagrees with, nobody re-seeds, and local demo state survives the swap.
- ADR-0029 §5's split trigger stays available and stays cheap — retrieval is already behind
  a port and an HTTP boundary, so graduating it to its own service remains a deployment
  change.

**Negative**

- The escape route in §4 is now untested on this box, because §2 made it unnecessary. The
  first person who needs it will be the one discovering whether the documented commands are
  right — a known, accepted gap, not an oversight.
- The index competes for the same cluster's buffer cache and CPU as the money path's reads.
  NFR-22's p99 is the guard, and it is measured, not assumed.
- `assistant_db` now depends on a superuser pre-create in `initdb`, so a brand-new database
  added later without that line fails at migration time rather than at boot.
- Pinning the image means pgvector upgrades are a deliberate act. That is the point, but it
  is also one more thing that goes stale quietly.

**Revisit trigger**: filtered recall or latency that one HNSW index cannot hold, which is
when §5's LIST partitioning by city earns its complexity; retrieval p99 approaching
NFR-22's 150 ms at realistic corpus size, or
recall@k on the golden set (FR-104) that HNSW tuning cannot recover; index build or memory
cost that disturbs the cluster's other databases; ADR-0029 §5's split trigger firing; or an
Aurora minor version that cannot carry the pgvector features §5 depends on.

## Amendment (2026-10-09) — `model_version` is gone from the schema

§3's predicate design is unchanged in intent but no longer literal:
`model_version` was dropped from both chunk tables (migration `0018`). With
the embedding model fixed by `Settings`, every row carried the same value —
a constant leading both primary keys and all four indexes, contributing no
selectivity, behind a query predicate that was always true.

The reasoning it encoded is still correct and still binding: **vectors from
different models, or different dimensions, must never mix in one result
set.** That is now enforced by replacement rather than by a predicate — see
the amendment on ADR-0031. A dimension change remains a schema change, since
`vector(1536)` and `vector(512)` are different column types.

Everything else here stands: the shared-Postgres choice, the `VectorStore`
port, the pinned base-matched image, per-city HNSW, and `REFRESH COLLATION
VERSION` + `REINDEX` as the documented route (which is Postgres's own
`REINDEX`, unrelated to the retired embedding reindex).
