# SmartFoodOps (QuickServe)

Greenfield food ordering & delivery platform. Domain microservices, event-driven,
Temporal-owned order saga. Past design phase; W1/W2/W3 largely built and live-proven.

**Repo path has a space in it** — `/Users/emumba/Smart Food/SmartFoodOps`. Always quote it.

## Working agreement
- **The user commits and pushes. Never run `git commit`/`git push`** — suggest a message instead.
- Step-by-step with explanation; no large autonomous code drops.
- Every milestone gets an adversarial review before it's "done" — scale it to risk
  (full multi-agent fan-out for money/concurrency/saga paths; `/code-review` for CRUD/FE/docs).
  Review the diff, not the whole codebase.

## Quality gates (all enforced, not advisory)
- `make cov` — unit tests + **100% coverage gate** (`fail_under=100`). This is the bar for every branch/edge.
  Domain harnesses cover HTTP-unreachable paths. `concurrency=["thread","greenlet"]` in pyproject is load-bearing.
- `make lint` — ruff + pyright, **both gating**. pyright strict tier on `libs/*/smartfood_*` + `services/*/*/domain`.
- FE: `tsc` + `vite build` clean; Playwright smoke exists.
- Layer-contracts source-scan test (`libs/smartfood-api/tests/test_layer_contracts.py`):
  no cross-service imports, no fastapi in `domain/`, no adapters imported from `api/`.

## Commands
- `make up` core infra · `make up-apps` all services · `make up-m4` current full working set (dispatch milestone)
- `make up-lean`/`up-m2`/`up-m3` = progressively larger subsets · `make up-ui` consoles · `make up-obs` Prometheus/Grafana/Jaeger
- `make dev SVC=order` run one service natively w/ reload · `make logs SVC=payment` · `make psql DB=order_db`
- `make seed` (deterministic, idempotent, real APIs) · `make demo` (→ SETTLED) · `make riders` sim fleet
- `make chaos`/`chaos-off` PSP failure knobs · `make nuke` fresh start · `make migrate SVC=identity`
- `make up-cdc` + `make cdc-register` Debezium lane · `make dlq-replay TOPIC=...`

## Layout
- `services/` — edge-bff, identity, catalog, inventory, order, payment, notification, analytics, dispatch, rider-gateway
- `libs/` — smartfood-{api,auth,idempotency,kafka,otel,outbox,pricing,realtime} (shared, py.typed, strict)
- `tools/` — mock-psp, mock-mailer, seed, demo, rider-sim, canary
- `docs/adr/` — **40 ADRs, authoritative for architecture decisions.** `docs/reviews/*-walkthrough.md` = milestone build records.
- `docs/local-dev.md` — **full port map + troubleshooting; read it before touching ports/compose.**

## Non-obvious invariants (violating these breaks things silently)
- **Kafka publication is transactional-outbox ONLY** (ADR-0002). Services stage outbox rows in the business tx;
  a poller (dev) or Debezium (prod) drains them. Never publish to Kafka directly from request handlers.
- **One status writer per aggregate.** Orders: `domain/transitions.py` `transition()` is the only thing that
  writes `orders.status` (guarded UPDATE, event joins same tx). Enforced by a source-scan test. Same pattern for
  `record_rider`, inventory reservations, payment lifecycle.
- **Wire vocabularies are symbols, not strings** — `smartfood_kafka.EventType`/`Topic`, `smartfood_api.ErrorCode`.
  Adding an error code means the table in `api-standards.md` AND the enum.
- **JWT verified once at the edge** (ADR-0005); services trust `X-Auth-*` headers. Calling a service raw = 401.
- **A user holds a SET of roles** (ADR-0034, `user_roles`); `require_role` is a set intersection and
  `AuthContext.roles` is a non-empty frozenset. This retired the recurring purchaser-gate bug: a
  promoted owner genuinely keeps `customer`, so no endpoint has to name both roles any more.
  The claim is `roles`, the header is `X-Auth-Roles` — **and any trusted `X-Auth-*` header must be in
  `STRIP_HEADERS`**, or a client picks its own identity.
- **`refresh_tokens` is one row per LOGIN, rotated in place** (ADR-0034). No `family_id`, no theft
  detection (given up deliberately), and **no reaper on `expires_at` and no logout endpoint yet** —
  growth is per-login but still unbounded. A logout should `DELETE` the row, never flag it.
- **Idempotency**: `complete()` joins the CALLER's tx so the stored response commits with the business write;
  deterministic refusals `release()` the key (no TTL squatting).
- **Outbox event ids are random uuid4** (ADR-0035) and dedupe nothing. What makes a replayed emit safe is
  that every staging site writes its aggregate row FIRST in the same tx and loses to that row's own PK or
  guarded transition — a new `stage_event` call site without that guard is a correctness bug.
- **Menu cache is cache-aside, 5-min TTL** (ADR-0027) — the versioned blob+pointer scheme is retired. Checkout
  prices from the snapshot endpoint, which bypasses cache by design.
- **No table carries a `version` column** (ADR-0039). Every guarded write keys on the state it protects
  (`status = :expected`, `available >= :qty`, `active < capacity`, a composite PK) — if you add a version
  back, make it a real CAS in the WHERE clause, never a counter in a SET list.
- **Catalog's multi-query reads open a REPEATABLE READ snapshot FIRST** (ADR-0037, `repo.begin_snapshot()`).
  Postgres cannot change the level after the first statement, so a query added above that call silently
  downgrades the read — the ordering tests are the only guard.
- **Placement consents to `expected_total_cents`, not a menu version** (ADR-0036). It is consent, never an
  asserted price: the server reprices and 409s `PRICE_CHANGED` on mismatch. The FE sends the total off the
  quote object it is rendering, so shown-price vs placed-price cannot diverge.
- **Brands are rows in `restaurants`** (ADR-0028, `kind=brand|branch`); the claim carries the BRAND id
  (wire name `X-Auth-Restaurant-Id` unchanged). Base menu edits fan out to every branch in one all-or-nothing tx.

## Machine-specific (this dev box only, not in the repo elsewhere)
- **Host ports remapped around local squatters**: Redis **6380** (in-net `redis:6379`), Schema Registry **8086**
  (in-net `schema-registry:8081`), Kafka **19092** from host / `kafka:9092` in-net — never mix the two.
- App service ports: edge 8000, identity 8001, catalog 8002, inventory 8005, order 8006, payment 8007,
  notification 8008, analytics 8009, rider-gateway 8010, dispatch 8012. debugpy = app port + 1000.
  8003/8004 deliberately unused (cart is client-side, pricing is a library).
- **Docker Desktop VM is 7.7 GB; the full stack needs ~8–9 GB → recurring OOM (exit 137/143).**
  Use the smallest `up-*` subset that covers your work. Real fix: user should raise VM memory to ~12 GB.
- `moto` ≤5.2.3 applies conditional `UpdateItem` unlocked → the dispatch 8-thread drill flakes under `make cov`.
  Fixed in `services/dispatch/tests/conftest.py` (serializes `update_item` with a lock). Store code is correct;
  the mock had the fidelity gap. Do not remove that conftest lock.

## Confirmed constraints (don't re-litigate)
Multi-region deferred, cell-ready (ADR-0013). gRPC dropped for phase 1, HTTP/JSON internal (0004).
DynamoDB for dispatch truth (0026). Mock PSP behind PaymentGateway port (0010). No customer refund path
(capture-after-delivery makes it structurally unreachable; S4 refund machinery stays for ops). Pricing is a
library, not a service (0015). Cart is client-side (0017).
