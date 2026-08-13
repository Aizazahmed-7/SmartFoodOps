# API Standards

**Status**: Adopted via ADR-0018 (v2 review register). These rules bind every HTTP endpoint — public REST through edge-bff and internal HTTP/JSON between services (ADR-0004). Rows and rules marked *(planned Wn)* refer to the ratified 3-week build plan; everything else describes behavior the current code either implements or must implement in its next touch.

Related: [ADR-0018](adr/0018-v2-review-register.md) · [ADR-0004](adr/0004-http-json-internal-grpc-deferred.md) · [ADR-0005](adr/0005-jwt-verified-once-at-edge.md) · [ADR-0010](adr/0010-mock-psp-behind-payment-gateway-port.md) · [ADR-0014](adr/0014-load-shedding-ladder.md) · [engineering-checklists.md](engineering-checklists.md)

---

## 1. Shape basics

- Paths are `/v1/{resource}`; nouns plural; non-CRUD actions are POST sub-resources (`POST /v1/orders/{id}/cancel`). No verbs in paths otherwise, no query-param tunneling of actions. IDs in paths are our cell-prefixed ULIDs, never numeric PKs.
- Status codes (the only ones we use): 200 read/action · 201 create (with `Location`) · 202 accepted-async (order placement) · 204 delete/no-body · 304 with ETag · 400 malformed · 401 unauthenticated · 403 authenticated-but-wrong-role only · **404 for both not-found and not-yours** (ownership is a WHERE clause; never leak existence) · 409 state conflict · 422 validation · 429 rate/admission (ADR-0014) · 500 unhandled · 503 dependency down (with `Retry-After`).
- `/v1` until a breaking change is unavoidable; additive changes (new optional response fields, new endpoints) never bump the version.

---

## 2. Error envelope & code catalog

Every non-2xx response, no exceptions:

```json
{"error": {"code": "ORDER_NOT_CANCELLABLE",
           "message": "human-readable, no internals",
           "request_id": "…"}}
```

- `request_id` is the value edge-bff stamps as `X-Request-ID` — the same id that appears in every log line for the request, so a support ticket maps to a trace in one lookup.
- 422 responses may add `"details": [{"field": "items[2].qty", "issue": "must be >= 1"}]`.
- `message` never contains SQL, stack traces, internal hostnames, or another user's data.
- `code` values come from the catalog below. **Adding a code = a PR to this table AND to its compile-time mirror `smartfood_api.ErrorCode`** — `ApiError` only accepts enum members, so an uncatalogued or typo'd code is a type error, not a broken client flow. (Event names have the same arrangement: `smartfood_kafka.EventType` / `Topic` mirror ARCHITECTURE §11.)

**Pagination convention**: list endpoints take `page` (0-based, fixed page size ≤20) and return `{"…": [...], "page": N, "has_more": bool}` — `has_more` via the limit+1 read, never a `COUNT(*)`. Offset pagination is acceptable while page depth is shallow; any feed that grows without bound (orders) uses keyset/cursor pagination from day one. Unpaginated lists are only allowed when creation is capped (e.g. addresses ≤20) or the resource is a bounded document (a menu).

Seed catalog (grows only via PR):

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_FAILED` | 422 | DTO validation failed (details list populated) |
| `IDEMPOTENCY_KEY_REUSE` | 422 | Same `Idempotency-Key`, different body (§4) |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | Concurrent duplicate while first attempt runs; `Retry-After: 1` (§4) |
| `AUTH_INVALID_CREDENTIALS` | 401 | Login failed (never says which field was wrong) |
| `AUTH_TOKEN_EXPIRED` | 401 | Access token expired — refresh |
| `AUTH_REFRESH_REUSED` | 401 | Refresh-token reuse detected — family revoked |
| `FORBIDDEN_ROLE` | 403 | Authenticated, wrong role for the route |
| `NOT_FOUND` | 404 | Doesn't exist *or* isn't yours |
| `GRANT_CONFLICT` | 409 | Role/scoping grant refused: user already scoped elsewhere or role ineligible (onboarding) |
| `CATEGORY_NOT_EMPTY` | 409 | Deleting a menu category that still contains items — move or delete the items first |
| `ORDER_NOT_CANCELLABLE` | 409 | Cancel after the cancellable window (e.g. post-pickup) |
| `ORDER_ALREADY_DECIDED` | 409 | Restaurant decision repeated with a different verdict |
| `ORDER_STATE_CONFLICT` | 409 | Lifecycle action doesn't fit the current state (capture w/o auth, /ready before /preparing) |
| `ITEM_UNAVAILABLE` | 409 | Menu item 86'd or stock reservation failed |
| `RESTAURANT_AT_CAPACITY` | 409 | Capacity gate rejected placement |
| `RESTAURANT_CLOSED` | 409 | Restaurant paused/closed — not taking orders right now |
| `PRICE_CHANGED` | 409 | Quote/placement price mismatch (diff in `details`) |
| `PAYMENT_DECLINED` | 402 | Mock-PSP authorization declined (ADR-0010) |
| `RATE_LIMITED` | 429 | Per-class bucket exhausted (§6) |
| `ADMISSION_SHED` | 429 | Edge admission control shed the request before any write (ADR-0014) |
| `DEPENDENCY_UNAVAILABLE` | 503 | Downstream down; `Retry-After` set |
| `INTERNAL_ERROR` | 500 | Unhandled failure — envelope never leaks internals; the log line carries the real error |

COD- and real-PSP-specific codes (`COD_*`, `PHONE_NOT_VERIFIED`, `PAYMENT_ACTION_REQUIRED`) arrive with their ADR-0018 triggers (D4 / D3), not before.

> Implemented: `libs/smartfood-api` provides `ApiError(code, message, status)` + `install_error_handlers(app)`; identity and edge-bff emit this envelope for every non-2xx (validation errors included, with `details`). Domain layers stay HTTP-free — they raise domain exceptions; the API layer maps them to catalog codes.

---

## 3. DTO rules

Every request/response body is a Pydantic model, and:

- `model_config = ConfigDict(extra="forbid")` — unknown fields are rejected, not ignored.
- Every numeric field is bounded: `qty: int = Field(ge=1, le=50)`. Unbounded ints in a request DTO fail review.
- Strings capped (`max_length=…`), lists capped (`max_length=100`). An uncapped list is an invitation to a 100k-item order.
- **Money is never accepted from clients.** Clients send item IDs + quantities; prices and totals come from the `smartfood-pricing` snapshot computed server-side (ADR-0015, ADR-0010). Any endpoint accepting a client-asserted amount is an automatic review reject. Money is integer minor units end-to-end — a `float` near money fails review.

---

## 4. Idempotency-Key semantics (the smartfood-idempotency design)

*Implementation lands W2 with order placement; the semantics are fixed now.*

All POSTs that create or mutate orders/money require `Idempotency-Key` (client-generated UUID, ≤64 chars). GET/DELETE never take the header. Semantics, provided by `libs/smartfood-idempotency` (PG table is the truth, the single global Redis is the fast path, TTL 24 h):

1. **Reserve first.** Before the use-case runs: `INSERT … (user_id, key, body_hash, status='IN_PROGRESS') ON CONFLICT DO NOTHING`. The reservation, not the response, is what makes the double-tap race safe.
2. **Replay.** Retry with same key + same `sha256(body)` → the stored response is returned verbatim with `Idempotent-Replay: true`.
3. **Reuse detection.** Same key + different body → 422 `IDEMPOTENCY_KEY_REUSE`.
4. **The race.** A concurrent duplicate that loses the insert reads the row: `IN_PROGRESS` → 409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After: 1` (client retries the same key, gets the replay); `COMPLETE` → replay.
5. **Crash mid-execution.** The reservation expires with the TTL and the client's retry re-executes — safe only because the underlying operations are idempotent themselves (workflow start is `REJECT_DUPLICATE`, money operations carry their own `{order_id}:{op}` keys).

---

## 5. Pagination

- **Cursor is the default.** Response shape: `{"items": […], "next_cursor": "…" | null}`. The cursor is urlsafe-base64 of `[sort_value, id]`, opaque to clients, and tolerates row deletion.
- `limit` default 20, max 100 — the cap is enforced in the DTO, not prose.
- Offset pagination only with a written justification in the PR (small, bounded, admin-only lists); default answer is no.

---

## 6. Rate-limit classes

Four named classes with budgets; buckets live in the single global Redis (`ratelimit:*`, TTL ≤60 s), with a per-pod local limiter as the Redis-down fallback (ADR-0014):

| Class | Budget | Applies to |
|---|---|---|
| `AUTH` | 10/min/IP | register, login, refresh |
| `READ` | 120/min/user (or IP when anonymous) | browse, menus, order reads, tracking reads |
| `WRITE` | 60/min/user | profile/address mutations, restaurant ops |
| `PLACEMENT` | 5/min/user + the global admission bucket | order placement, cancel |

The class will be declared per route as a `rate_class` field on the edge-bff routing `Rule` *(planned — the field does not exist in `edge_bff/routing.py` yet; until it lands, the inventory table below is the declaration of record)*. Every new route names its class in its inventory row; `PLACEMENT` routes additionally pass admission control before any state is written.

---

## 7. API inventory

**The rule: adding a route = adding a row here, in the same PR.** (Same-PR docs discipline, [repo-structure.md](repo-structure.md) §5.)

Live today:

| Route | Edge mode | Idem-key | Rate class | Upstream | Notes |
|---|---|---|---|---|---|
| POST `/v1/auth/register` | public | – | AUTH | identity | 202 accepted |
| POST `/v1/auth/login` | public | – | AUTH | identity | token pair |
| POST `/v1/auth/refresh` | public | – | AUTH | identity | rotates refresh-token family |
| GET `/v1/auth/me` | auth | – | READ | identity | |
| PATCH `/v1/auth/me` | auth | – | WRITE | identity | |
| GET `/v1/me/addresses` | auth | – | READ | identity | |
| POST `/v1/me/addresses` | auth | – | WRITE | identity | 201; not money — key not required |
| DELETE `/v1/me/addresses/{id}` | auth | – | WRITE | identity | 204; becomes soft-delete with order snapshots (ADR-0018) |
| GET `/.well-known/jwks.json` | *not routed* | – | – | identity | edge-bff fetches directly for JWT verification; never exposed through the gateway |

Route-table prefixes already wired in `edge_bff/routing.py` but whose upstream endpoints are not yet built: `/v1/restaurants`, `/v1/menus` (catalog, public_read), `/v1/inventory` (auth), `/v1/orders`, `/v1/quote` (order, auth).

Planned (build-plan weeks; each row is finalized when the endpoint lands):

| Route | Edge mode | Idem-key | Rate class | Upstream | Week |
|---|---|---|---|---|---|
| GET `/v1/restaurants` | public_read | – | READ | catalog | *(planned W1)* |
| GET `/v1/restaurants/{rid}` | public_read | – | READ | catalog | *(planned W1)* |
| GET `/v1/menus/{rid}` | public_read | – | READ | catalog | *(planned W1)* — ETag = menu version |
| POST `/v1/orders` | auth (customer or restaurant_admin) | **required** (§4 in full: replay w/ `Idempotent-Replay: true`, reuse 422, in-progress 409; deterministic refusals release the key) | PLACEMENT | order | 202 `{order_id, status}`; one tx = order + line/address/pricing snapshots + OrderPlaced outbox + idempotency completion; saga start after commit |
| GET `/v1/orders/{id}` | auth (customer or restaurant_admin) | – | READ | order | snapshots, not live menu; not-yours = 404 |
| GET `/v1/orders` | auth (customer or restaurant_admin) | – | READ | order | keyset cursor (`[placed_at, order_id]` b64), limit ≤100 default 20 |
| POST `/v1/orders/{id}/cancel` | auth (customer or restaurant_admin) | – (naturally idempotent by STATE: 202 submitted / 200 already-cancelled / 409 `ORDER_NOT_CANCELLABLE` — re-POSTing any of them is safe, so no key) | WRITE | order | signals `cancel_requested`; the workflow referees customer-vs-courier via the set-guarded CANCELLING move; refused from PICKED_UP on (FR-21) |
| POST `/v1/quote` | auth (customer or restaurant_admin — owners order dinner too) | – (stateless read) | READ | order | pricing lib in-process; self-heals version drift (response carries current `menu_version`); 409 `ITEM_UNAVAILABLE`/`RESTAURANT_CLOSED`, 422 selection violations, 503 when catalog is down |
| GET `/v1/restaurant/orders?status=` | auth (restaurant role; claim IS the scope) | – | READ | order | per-status FIFO keyset feed (oldest first); batched line items; `status` required |
| POST `/v1/restaurant/orders/{id}/accept` · `/reject` | auth (restaurant role) | – (naturally idempotent: 202 signal submitted / 200 same-verdict-from-DB / 409 `ORDER_ALREADY_DECIDED` vs `ORDER_STATE_CONFLICT`) | WRITE | order | decision → `restaurant_decision` signal; the workflow referees (first verdict wins) |
| POST `/v1/restaurant/orders/{id}/preparing` · `/ready` | auth (restaurant role) | – (guarded `transition()` replay = 200) | WRITE | order | direct guarded transitions; `/ready` also signals `food_ready` to dlv::{order_id} AFTER commit, re-signalling on replay (crash-heal) |
| GET `/v1/inventory/restaurants/{rid}/stock` | auth (restaurant role) | – | READ | inventory | scope mismatch → 404; `{items: [{item_id, available, version}]}` |
| PUT `/v1/inventory/restaurants/{rid}/stock/{item_id}` | auth (restaurant role) | – (PUT = absolute set, naturally idempotent) | WRITE | inventory | `{available 0..100000}`; upsert; foreign item → 404 |
| PUT `/v1/inventory/restaurants/{rid}/capacity` | auth (restaurant role) | – (idempotent PUT) | WRITE | inventory | `{capacity 1..1000}`; lowering below `active` is legal — new orders stop, running ones drain |
| POST `/v1/internal/payments/{order_id}/authorize` · `/capture` · `/void` · `/refund` | system-only, never edge-routed | money keys `{order_id}:{op}` via smartfood-idempotency (read-before-execute; PSP shares the same key) | n/a | payment | 402 `PAYMENT_DECLINED` stored+replayed; capture/refund amounts from the stored auth; 409 `ORDER_STATE_CONFLICT` on state mismatch |
| POST `/v1/internal/reservations` | system-only, never edge-routed | – (reservation PK = order_id is the key) | n/a | inventory | all-or-nothing: capacity slot + every line, one tx; 201 created / 200 replay; 409 `ITEM_UNAVAILABLE` (per-line details) / `RESTAURANT_AT_CAPACITY` |
| POST `/v1/internal/reservations/{order_id}/release` | system-only | – (guarded transition) | n/a | inventory | `{reason: cancelled\|expired}`; restores stock + slot; not-active → no-op |
| POST `/v1/internal/reservations/{order_id}/commit` | system-only | – (guarded transition) | n/a | inventory | settlement: stock stays sold, slot frees; not-active → no-op |

---

*Where a v2-handbook practice referenced gRPC, Stripe, or the three-way Redis split, this document is its adaptation to our ratified stack (HTTP/JSON internal, mock PSP, single Redis). The trigger-gated originals live in ADR-0018.*
