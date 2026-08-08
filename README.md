# SmartFoodOps

SmartFoodOps is QuickServe's greenfield backend for large-scale food ordering and delivery orchestration, serving customers, restaurant admins, delivery riders, and system admins. The system is designed around the order lifecycle rather than CRUD services: one Temporal workflow owns each order's saga (validation, payment authorization, restaurant acceptance, dispatch, settlement, compensation), Kafka carries immutable facts published only via transactional outbox, and every consumer is at-least-once + idempotent. Part A builds this foundation for a single region/cell sized at 2,000 orders/s sustained; Part B (future GenAI assistant — RAG over menus, recommendations, delay explanations) consumes event streams and read models Part A already exposes, with zero Part A changes.

## Status

**Design phase — Part A.** No code yet; the repo currently contains the design document set. Multi-region is explicitly deferred (single cell `c1`, with cheap day-1 hooks that keep multi-cell additive).

## Document map

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The HLD: philosophy & invariants, context/container diagrams, service catalog, edge & auth, order lifecycle & state machine, compensation flows, dispatch & tracking, data/caching/event architecture, AWS deployment view, observability, load-shedding, risks |
| [docs/PRD.md](docs/PRD.md) | Use-cases, functional requirements, NFRs, milestones, requirement-to-deliverable traceability |
| [docs/capacity-plan.md](docs/capacity-plan.md) | Single-cell capacity math (2,500 orders/s provisioned ceiling) with assumptions |
| [docs/architecture-walkthrough.md](docs/architecture-walkthrough.md) | The guided tour: full architecture explained end to end — every database, the event backbone, and all four background-processing machineries (Temporal, Kafka consumers, Celery/RabbitMQ, Lambda) |
| [docs/service-ownership.md](docs/service-ownership.md) | Per-service reference: what each service owns in Postgres, DynamoDB, Redis, and Kafka — plus the deliberate cross-service exceptions |
| [docs/local-dev.md](docs/local-dev.md) | Onboarding guide: `make up && make seed`, compose profiles, slim mode, simulators, chaos suite |
| [docs/repo-structure.md](docs/repo-structure.md) | Monorepo layout (uv workspaces: services/, libs/, deploy/, tools/) |
| [docs/api-standards.md](docs/api-standards.md) | API rules: error envelope + code catalog, DTO strictness, idempotency-key semantics, pagination, rate-limit classes, and the API inventory (a new route = a new row, same PR) |
| [docs/engineering-checklists.md](docs/engineering-checklists.md) | Definition-of-Done checklists per task type, the anti-pattern catalog, and per-developer security rules |
| [docs/adr/](docs/adr/) | Architecture Decision Records — one per key decision, with alternatives and revisit triggers |

## Mandated tech stack

Python + FastAPI · PostgreSQL (Aurora) · DynamoDB (LocalStack locally) · Redis · Celery + RabbitMQ · Kafka + Schema Registry · Temporal · Prometheus + Grafana · Jaeger · OpenTelemetry · Docker Compose (local) · AWS (deployment).
