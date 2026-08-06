# Repository Structure

**Status**: docs-only phase — this describes the layout the code will land in (plan §11). Read alongside [local-dev.md](local-dev.md).

---

## 1. The tree

```
smartfoodops/
├─ pyproject.toml            # uv workspace root: member list, shared lint/type config
├─ uv.lock                   # THE lockfile — one, for every service and lib
├─ Makefile                  # up / dev SVC=x / seed / test / chaos / … (see local-dev.md)
├─ .vscode/
│  └─ launch.json            # one debugpy attach configuration per service
├─ services/                 # one FastAPI app per service — 11 total
│  ├─ edge-bff/              # :8000  routing, JWT verify, identity headers, admission
│  ├─ identity/              # :8001  auth, JWTs, roles, refresh-token families
│  ├─ catalog/               # :8002  restaurants, menus, promos, menu versions
│  │                         # :8003 retired — cart is client state (ADR-0017)
│  │                         # :8004 retired — pricing is a library (ADR-0015)
│  ├─ inventory/             # :8005  stock counters, reservations
│  ├─ order/                 # :8006  order state machine, outbox, OrderWorkflow
│  │  ├─ app/                #   FastAPI app: routers, domain, repositories
│  │  ├─ workflows/          #   Temporal workflow + activity definitions
│  │  ├─ migrations/         #   Alembic, versioned per service
│  │  ├─ tests/              #   unit/ + integration/
│  │  ├─ pyproject.toml      #   workspace member; deps on libs by name
│  │  └─ Dockerfile          #   thin layer over the shared base image (proposed)
│  ├─ payment/               # :8007  mock-PSP adapter behind PaymentGateway port, ledger
│  ├─ dispatch/              # :8008  matching, offer protocol, assignment locks
│  ├─ notification/          # :8009  decide + log; Celery senders
│  ├─ rider-gateway/         # :8010  rider WebSocket ingest (GPS, offers)
│  ├─ tracking-gateway/      # :8011  customer SSE fan-out
│  └─ analytics/             # :8012  windowed aggregation, lake sink
├─ libs/                     # shared workspace packages (the ONLY cross-service code path)
│  ├─ smartfood-kafka/       # Avro serde, typed producer/consumer, retry/DLQ, processed_events
│  ├─ smartfood-outbox/      # outbox writer + dual-mode publisher (OUTBOX_MODE=poller|debezium)
│  ├─ smartfood-idempotency/ # idempotency-key table + Redis fast-path helpers
│  ├─ smartfood-auth/        # AuthContext middleware consuming X-Auth-* headers
│  ├─ smartfood-pricing/     # price/discount/fee/tax engine — ADR-0015. Consumed in-process by
│  │                         #   order/workflows (PriceOrder → authoritative snapshot) and by
│  │                         #   the /v1/quote endpoint (display estimate). Pure functions; rules from Catalog.
│  └─ smartfood-otel/        # tracing setup, Temporal TracingInterceptor, log correlation
├─ deploy/
│  ├─ compose/               # docker-compose.yml (profiles), nginx/, initdb/, grafana/, prometheus/
│  └─ cdk/                   # CDK app for AWS; tables.py = single DDB source of truth
├─ tools/                    # dev tooling, also workspace packages
│  ├─ rider-sim/             # N fake riders over WS, GPS polyline replay
│  ├─ order-gen/             # order load generator through the BFF
│  ├─ seed/                  # deterministic seeding + init_tables.py (LocalStack DDB)
│  └─ demo/                  # place-order.sh and friends
└─ docs/                     # PRD, ARCHITECTURE, adr/, capacity-plan, this file
```

Every service follows the `order/` shape shown above: `app/`, `workflows/` (only in services that own Temporal code — order, dispatch, payment compensation activities), `migrations/` (only PG-backed services), `tests/`, own `pyproject.toml`, own `Dockerfile`.

---

## 2. The three structural rules

These are enforced in CI and code review; they are the repo-level expression of the architecture's invariants.

1. **Services never import each other — only `libs/`.** There is no `from services.payment import …` anywhere. Cross-service interaction happens exclusively over the declared interfaces: HTTP/JSON calls, Temporal activities, Kafka topics. Directory imports would create compile-time coupling the architecture explicitly rejects, and would silently break the "any service runs alone in slim mode" property. Enforced by an import-linter contract in CI (proposed) plus review.

2. **`libs/` are uv workspace packages, and the only shared code.** Each lib is a proper package (own `pyproject.toml`) that services depend on *by name*, exactly as they would an external dependency — except one `uv.lock` guarantees every service resolves the identical version at all times. The correctness-critical machinery (outbox, idempotency, Kafka serde/retry/DLQ, auth context, tracing) lives here *because* it is mandatory: services get exactly-once-by-construction behavior by importing it, not by re-implementing it. A new consumer that hand-rolls Kafka handling instead of using `smartfood-kafka` fails review by rule.

3. **`deploy/cdk/tables.py` is the single DynamoDB source of truth.** Table definitions are plain Python dicts. Real CDK constructs consume them for AWS; `tools/seed/init_tables.py` feeds the same dicts to `boto3.create_table` against LocalStack. There is no second definition to drift, and no cdklocal/CloudFormation emulation. Corollary rule from the design review: any new table or GSI keyed on restaurant, city, or status is rejected — uniform-cardinality keys only (plan §6).

---

## 3. Directory-by-directory

**`pyproject.toml` + `uv.lock` (root).** The uv workspace root: lists every member under `services/`, `libs/`, and `tools/`, and hosts the shared tool configuration (§5). The single `uv.lock` is the point — see §4.

**`Makefile`.** The developer interface. Nobody memorizes compose profile incantations; `make up`, `make dev SVC=x`, `make seed`, `make chaos` etc. are documented in [local-dev.md](local-dev.md) and are the same entry points CI uses, so "works in CI" and "works on my machine" are the same claim.

**`services/`.** Eleven deployables, one directory each, all the same shape. A service directory is self-contained: its API app, its Temporal workflow/activity code, its migrations, its tests, its container build. Nothing outside `libs/` may be imported from a sibling. Ports are fixed (8000–8012, debugpy +1000) and match the compose file and local-dev docs.

**`libs/`.** Six shared packages (initial set — plan §11). These encode the plan's invariants as code: `smartfood-outbox` closes the dual-write gap, `smartfood-idempotency` makes retries safe, `smartfood-kafka` makes consumers effectively-once per sink, `smartfood-auth` keeps JWT parsing out of services, `smartfood-otel` keeps traces stitched across the async hop. Editing a lib hot-reloads every locally running consumer (the reload watcher covers `libs/`), and the single lockfile means a lib change is immediately consistent workspace-wide — no version-bump dance.

**`deploy/`.** Everything about running the system, nothing about business logic. `compose/` holds the profile-structured `docker-compose.yml` plus config for nginx (ALB path-rule emulation), Postgres `initdb/` (one DB per service), Grafana dashboards and Prometheus scrape config. `cdk/` is the AWS deployment app (Phase 3) and, from day one, `tables.py`. Compose and CDK deliberately live side by side: local and AWS are the same topology expressed twice, sharing single-source definitions wherever drift would hurt (DDB tables; nginx path rules mirroring ALB listener rules).

**`tools/.`** Developer-facing executables, packaged as workspace members so `uv run rider-sim` just works. `rider-sim` and `order-gen` are the fake phones and fake customers that make dispatch developable locally; `seed/` owns deterministic data (seeded-RNG ULIDs, constants exported for tests) and LocalStack table init; `demo/` holds the guided-tour scripts from the quickstart. Nothing in `tools/` ships to production.

**`docs/`.** The design-document set: `PRD.md`, `ARCHITECTURE.md`, `adr/` (one short ADR per irreversible decision), `capacity-plan.md`, `local-dev.md`, this file. Docs land before code (current phase) and are updated in the same PR as any change that invalidates them.

**`.vscode/`.** Checked in on purpose: `launch.json` carries a debugpy attach configuration per service (port = app port + 1000) so "set a breakpoint in payment" is F5, not a wiki page.

---

## 4. Why a uv workspace (monorepo) — the rationale

The system is 13 services whose correctness depends on five shared libraries behaving identically everywhere. That shape decides the tooling:

- **One `uv.lock` across all services and libs** means a change to `smartfood-outbox` is *immediately consistent* for every consumer — there is no window where order runs v1 of the envelope and payment runs v2. For libraries that implement idempotency and event contracts, version skew is a correctness bug, not an inconvenience.
- **Per-service isolation without per-service repos**: `uv sync --package order` materializes exactly one service's environment in ~2 s. Slim-mode `make dev SVC=x` and per-service container builds both rely on this — you get monorepo consistency at polyrepo build granularity.
- **Atomic cross-cutting changes**: an envelope field, a new `X-Auth-*` header, or an outbox schema change lands as one PR touching lib + all consumers + docs, reviewed and CI-tested together.
- **One toolchain to learn**: `uv` is the only globally installed Python tool; everything else (`pytest`, `ruff`, `alembic`, simulators) runs via `uv run` against the locked environment.

The trade-off (a large lockfile and shared CI) is acceptable at 13 services + 5 libs; the repo does not need Bazel-class tooling at this size.

---

## 5. Conventions

| Convention | Rule |
|---|---|
| **Migrations** | Per service: `services/<svc>/migrations/` (Alembic), applied only to that service's own database (one DB per service, locally created via `deploy/compose/initdb/`). No migration ever touches another service's schema — schema coupling is forbidden along with import coupling. |
| **Tests layout** | `services/<svc>/tests/unit/` (no infra, `make test`) and `tests/integration/` (against compose, `make test-int`). Libs carry their own `tests/`. Chaos scenarios live with the suite runner under `tools/` (proposed) and run via `make chaos`. Seed-ID constants are imported from the seed package — never hard-coded. |
| **Lint/type config** | Single shared config at the workspace root — `ruff` (lint+format) and `mypy` strict on `libs/`, standard on `services/` (proposed tools). Services do not override rules; one repo, one style. The custom CI lints from the plan run alongside: Redis `SET`-without-`EX` rejection, alert-without-runbook rejection, Avro `BACKWARD_TRANSITIVE` compatibility gate. |
| **Naming** | Service directories kebab-case matching the service catalog (`edge-bff`, `rider-gateway`); lib packages `smartfood-*`; Kafka topics per plan §8; Temporal workflow IDs `ord::{order_id}`. |
| **Workflow code** | Temporal workflow + activity definitions live in the owning service's `workflows/` dir and are registered by that service's worker entrypoint. Workflow code follows determinism rules (no I/O, no time/random outside SDK APIs) — enforced by review + the replay tests in CI. |
| **Containers** | One `Dockerfile` per service, layered on a shared Python base image (proposed); dev containers mount `app/` + `libs/` for hot reload, prod images copy the synced environment. |
| **Docs discipline** | A PR that changes a port, topic, table, env knob, or make target updates `docs/` in the same PR. |

---

*Items marked (proposed) are structure details not fixed by the design plan; the plan (§11) remains authoritative.*
