# 0007 — DynamoDB partition-key rules: uniform-cardinality only

**Status**: Accepted

## Context

A viral restaurant is the platform's most plausible hot spot: one promoted restaurant can concentrate a city's traffic onto whatever key it appears in. DynamoDB throttles per partition, so a restaurant-, city-, or status-shaped key turns a marketing success into an outage. DDB is our store for exactly the high-volume key-access tables (carts, order history, tracking, deliveries, rider state, locations, notification log).

## Decision

**Every DynamoDB PK and GSI key must be uniform-cardinality. Restaurant-keyed, city-keyed, and status-keyed PKs and GSIs are banned everywhere** — enforced by design review and a key-design launch checklist. Approved shapes: `carts` `PK=USER#id` (TTL 7d), `order_history` `PK=customer, SK=ts#order`, `order_tracking` (order-keyed, TTL post-terminal), `deliveries` (+GSI on **rider** — uniform), `rider_state`, `rider_locations` (day-bucketed, TTL 30d), `notification_log` (TTL 90d).

Access patterns that *want* a restaurant key get a different store: the restaurant order feed is served from Aurora PG via index `(restaurant_id, status, placed_at)` — deliberately not DDB. Kafka keys follow the same rule (order-id, never restaurant-id). Tables run on-demand with provisioned floors pre-warmed before meal peaks.

## Consequences

**Positive**
- No key in the system concentrates a viral restaurant's load onto one partition; the failure mode is removed structurally rather than mitigated operationally.
- Checklist review makes key design a launch gate, not an incident retrospective.

**Negative**
- Some queries become less direct: restaurant- or status-scoped views must come from PG indexes or Kafka-fed read models instead of a convenient GSI.
- Discipline cost: every new table/GSI needs cardinality review, and PG carries query load DDB could have naively taken.

**Revisit trigger**: an access pattern that genuinely cannot be served by PG or a projector at budget — then use write-sharded keys (`RESTAURANT#id#shardN`) with scatter-gather reads, as a reviewed exception, never a plain restaurant key.

**Addendum (ADR-0018)**: `Scan` is **banned in any request path**, under the same enforcement as the key rules (design review + CI grep). A `Scan` that looks necessary is a missing read model or a wrong key shape in disguise; scans are tolerable only in offline tooling — backfills, audits, migrations.
