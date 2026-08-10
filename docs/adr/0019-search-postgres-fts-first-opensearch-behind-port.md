# 0019 — Search: Postgres FTS + trigram first, OpenSearch behind a port later

**Status**: Accepted 2026-08-10

## Context

Discovery must support production-grade search: typo-tolerant ("fuzzy") matching on
**restaurant names and menu-item names**, combined with structured filters (city,
cuisine, paging). The stated end-state preference is an Elasticsearch-class engine.

Two constraints shape the timing:

1. **ADR-0002 forbids dual writes.** A search index is a second datastore; feeding it
   synchronously from the request path (write PG, then write the index) reintroduces
   the exact gap the outbox closes — a crash between the two writes leaves search
   lying about the menu. The sanctioned feed is a consumer on `c1.catalog.changes`,
   and that pipeline lands in Week 3 with Kafka.
2. **Local footprint.** OpenSearch costs ~1.5 GB RAM in compose — real money on the
   16 GB laptop budget ([local-dev.md](../local-dev.md) §RAM) — and a second
   consistency surface to operate, before any traffic justifies it.

## Decision

Search is a **domain port** (`SearchPort`) inside Catalog with swappable adapters:

- **Now (W1)**: a Postgres adapter in `catalog_db`. `tsvector` full-text
  (`websearch_to_tsquery`) plus **`pg_trgm` trigram similarity** for typo tolerance,
  GIN-indexed on restaurant and item names. Search columns are maintained **in the
  same transaction** as the rows (generated columns / triggers) — index consistency
  is free, no pipeline exists to fail. Serves `GET /v1/search` with fuzzy matching,
  city/cuisine filters, relevance ranking, and paging from day 1.
- **Later (trigger-gated)**: an OpenSearch adapter fed by a `catalog.changes`
  consumer (never dual-written), with a Kafka-replay backfill runbook. The API
  contract and port are unchanged; adoption is additive.

**Triggers for the swap** (any one):
- catalog exceeds ~1M menu items or search p99 > 150 ms at load-test scale;
- relevance work PG can't express economically — synonyms, multi-language analyzers,
  learned ranking, "did you mean";
- search traffic needs to scale independently of the transactional catalog store.

## Consequences

- Production fuzzy search ships in Week 1 with **zero new infrastructure**; trigram
  handles the "biriani → Biryani" class of error honestly.
- PG ranking (`ts_rank` + `similarity`) is adequate but not tunable like a real
  engine; that gap is the explicit trigger above, not a surprise.
- The W3 CDC consumer (blob pre-renderer) and the future index feed share the same
  topic and idempotency discipline — OpenSearch arrives as one more consumer group.
- `GET /v1/search` result shape (restaurants matched by name, restaurants surfaced
  via matching items, with the matched items attached) is the contract both
  adapters must satisfy; tests run against the port, not the adapter.
