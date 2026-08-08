# Engineering Checklists

**Status**: Adopted via ADR-0018 (v2 review register). Two things live here: the Definition-of-Done checklists (copy the relevant one into your PR description and check it off) and the anti-pattern catalog (the mistakes we already know about — review blocks all of them today; the tagged ones get CI teeth on the noted build-plan week). If your task type isn't listed, that's a gap — PR this file.

Related: [api-standards.md](api-standards.md) · [repo-structure.md](repo-structure.md) · [ADR-0018](adr/0018-v2-review-register.md)

---

## 1. Definition of Done — by task type

### DoD-1 New HTTP endpoint

- [ ] Row added to the [api-standards.md](api-standards.md) §7 inventory **in the same PR**
- [ ] DTOs: `extra="forbid"`, every numeric field bounded, strings/lists capped (api-standards §3)
- [ ] No client-asserted amounts anywhere in the request shape
- [ ] Rate class named in the inventory row (`AUTH`/`READ`/`WRITE`/`PLACEMENT`)
- [ ] Idempotency decision recorded in the row: key required / not required, and why
- [ ] Error codes from the catalog only; a new code = a catalog PR (api-standards §2)
- [ ] Authz is ownership-in-the-query — `WHERE id=:id AND owner=:auth_user`; the not-yours-→-404 path has a test
- [ ] Cursor pagination if it returns a list (api-standards §5)
- [ ] Unit tests (domain, fakes) + integration test (happy / authz-404 / conflict) green
- [ ] DTO/OpenAPI snapshot updated — the diff is the reviewed contract change

### DoD-2 New Kafka consumer

- [ ] `smartfood-kafka` framework subclass only — no hand-rolled `confluent-kafka` loop
- [ ] Dedupe mode declared (`PG_TX` / `VERSION_GUARD` / `NATURAL_KEY`, ARCHITECTURE §11) with a one-line justification; a consumer with no declared mode fails review
- [ ] Ordering declared (strict-per-key vs tolerant) with a one-line justification
- [ ] Handler is idempotent and side-effect-free outside the provided tx — consumers project and notify; sagas act
- [ ] Duplicate-delivery test + poison-message test *(scaffolded by the W3 test helpers; asserts are yours)*
- [ ] Consumer group named `{service}.{purpose}.v1`; re-consume = bump `v`, never reset live offsets
- [ ] Lag + DLQ alerting inherited from the framework — verified visible in Grafana once obs lands

### DoD-3 Workflow / activity change

- [ ] Determinism rules hold: no `datetime.now()`/`random`/`uuid4`/I/O/env reads inside workflow code — SDK equivalents only
- [ ] Workflow and signal args carry **IDs and small value objects only** — never PII, tokens, or blobs (Temporal history is a database)
- [ ] `workflow.patched()` guard if logic could execute differently on an open history
- [ ] Replay suite green; action budget respected — ≤12 activities / ≤3 timers / ≤4 signals happy-path (warn W2, hard gate Phase 3)
- [ ] Retry class chosen from the shared policies (`MONEY` / `VALIDATION` / `NOTIFY`); a custom policy = an ADR
- [ ] Idempotency key stated for every side effect (`{order_id}:{op}` for money)
- [ ] Failure path tested with injected activity errors (mock-PSP knobs for payment code — the "N timeouts ⇒ ≤1 authorization" invariant is a test, not a hope)

### DoD-4 Migration (new table / column / index)

- [ ] Expand/contract law: expand-only in this release; drops/renames/re-types split across releases with reads switched in between
- [ ] Autogenerate output reviewed line-by-line — never trusted blind
- [ ] `CREATE INDEX CONCURRENTLY` in its **own** migration, non-transactional
- [ ] `EXPLAIN` output from a seeded dataset in the PR if a query pattern changed
- [ ] Migration touching `outbox` or any CDC-captured table flagged in the PR description for explicit review (ADR-0016 slot/publication impact)
- [ ] Retention/TTL stated — nothing lives forever without a written reason
- [ ] Only this service's own database is touched (one DB per service; cross-schema coupling is forbidden)

### DoD-5 Shared lib change (`libs/smartfood-*`)

- [ ] Every consumer adapted **in the same PR** — the single lockfile makes lib changes atomic; a lib change that leaves a consumer broken-but-uncompiled doesn't exist here
- [ ] Lib's own tests updated; at least one consuming service's integration test exercises the new behavior
- [ ] No service-specific logic smuggled into the lib — a helper used by one service lives in that service
- [ ] Contract-bearing changes (envelope fields, header names, key formats) update the affected docs in the same PR
- [ ] Behavior verified under hot reload (`make dev` watches `libs/`) — no import-time side effects that survive reload

---

## 2. Anti-pattern catalog

Blockers, all of them. Tag = how it's enforced: `[review]` today, `[CI-lint (planned Wn)]` when the automated check lands per the build plan, `[CI-lint (live)]` already automated.

**Money & state**

1. `float` anywhere near money — integer minor units end-to-end (ADR-0010) `[CI-lint (planned W2)]`
2. Client-asserted amounts in any DTO — clients send IDs + quantities `[review]`
3. Raw `UPDATE orders SET status=…` outside the guarded-transition helper (compare-and-swap on expected status + version bump) `[CI-lint (planned W2) — grep]`
4. External I/O — HTTP, Redis, PSP, Temporal — inside an open DB transaction `[review]`
5. Network calls "that don't count" (Schema Registry lookups included) inside a transaction — outbox emit is pure CPU; schema IDs come from a boot-time map `[review]`
6. `sessions.begin()` opened in API routes or adapters — the domain layer owns the transaction boundary, especially around security writes (refresh-token rotation, credential updates); repos never commit `[review]`
7. Random `uuid4()` event IDs — `event_id` is UUIDv5 of `aggregate:{id}:{version}:{type}`, and emit fires only when the guarded transition actually applied (ADR-0018) `[review]`
8. A second lock next to the DynamoDB conditional-write assignment lock — the conditional write *is* the lock (ADR-0011) `[review]`
9. `except Exception` swallowing in handlers/routes — domain errors map centrally `[review]`

**Data**

10. Restaurant-, city-, or status-keyed DynamoDB PKs or GSIs — uniform-cardinality keys only (ADR-0007) `[review]`
11. DynamoDB `Scan` in any request path `[review]`
12. `SELECT *` outside repository modules `[review]`
13. List query without `LIMIT` `[review]`
14. `FOR UPDATE` without a justifying comment + `lock_timeout` `[review]`
15. Offset pagination without written justification (api-standards §5) `[review]`
16. Blind-trusted Alembic autogenerate; drop/rename in the same release as the code change `[review]`
17. Cross-service DB access or imports — events or HTTP only ([repo-structure.md](repo-structure.md) §2) `[CI-lint (planned W2) — import-linter]`
18. Redis `SET` without `EX`/TTL (the one exempt family is documented flags) `[CI-lint (planned W3)]`
19. `KEYS`/`SCAN` in application code `[review]`

**Events & workflows**

20. Direct Kafka produce from a service — publication is outbox-only (ADR-0002) `[CI-lint (planned W3) — import ban]`
21. Consumer without a declared dedupe mode, or offsets committed before the durable effect `[review — framework owns commits]`
22. Side-effectful mutations from consumers (PSP calls, HTTP writes) — consumers project and notify; sagas act `[review]`
23. Commands disguised as events (`DoXRequested` consumed by exactly one service — that's an HTTP call or an activity) `[review]`
24. Nondeterminism in workflow code (`datetime.now()`, `random`, I/O, env reads) `[CI-lint (planned W2) — replay suite]`
25. PII, tokens, or blobs in workflow/signal args or in `*.events` payloads — IDs cross boundaries; data stays home `[review]`
26. Ad-hoc retry loops / `tenacity` — Temporal owns saga retries, `smartfood-kafka` owns consumer retries `[review]`

**Ops & hygiene**

27. Sync/blocking calls in async paths (`requests`, `time.sleep`, sync DB drivers) `[CI-lint (live) — ruff ASYNC rules]`
28. PII or secrets in log lines (passwords, tokens, `Authorization`, card data, full addresses at INFO) `[review; log-processor test planned W3]`
29. `os.environ` reads outside `config.py` — typed `Settings` or it doesn't exist `[CI-lint (planned W2) — grep]`
30. Alert added without a runbook page `[CI-lint (planned W3)]`
31. Hard-coded seed IDs in tests — import the constants from the seed package `[review]`
32. "Temporary" scripts inside `services/` — `tools/` or it doesn't merge `[review]`

---

## 3. Security rules for every developer

- **Ownership is a WHERE clause, not an `if` after fetch.** `WHERE order_id=:id AND customer_id=:auth_user` → 0 rows → 404. Existence is never leaked to non-owners; a separate 403 exists only for role mismatch.
- **`Authorization` is read at the edge, nowhere else.** edge-bff verifies the JWT once and stamps `X-Auth-*`; services trust those headers and never parse tokens (ADR-0005). A service reading `Authorization` directly fails review.
- **Committed a secret? Rotate + purge + audit — no exceptions.** Rotate the credential immediately (before the fix PR), purge it from git history, and leave an incident note stating what leaked, for how long, and what was rotated. "It was only the dev key" is not an exemption; dev keys sign real local tokens.
- **New dependency = one-line justification in the PR** (what it does, why the stdlib/existing deps don't) via `uv add` only — never hand-edited into a `pyproject.toml`. License/advisory checking joins CI later; the justification line starts now.
- SQL is always parameterized — f-string SQL fails review.
- Secrets reach code only as env vars named in a `Settings` class; never in compose files committed with real values, never in test fixtures.

---

*Adapted per ADR-0018 from the v2 handbook's DoD/anti-pattern chapters: gRPC-, Stripe-, COD-, and K8s-specific entries are omitted or held behind their triggers; everything here matches our ratified stack.*
