# ADR-0017 — Cart lives on the client, not the backend

**Status**: Accepted. Supersedes the Cart service (`:8003`) and the `sfo-cart-carts` DynamoDB table.

## Context

The Cart service was always the thinnest box in the catalog: a DynamoDB `get`/`put` keyed by user, no events, no transactions with anything, no logic beyond re-resolving display prices. The only structural reason it survived the ADR-0015 boundary test was "it owns data."

Examining what that data actually buys: a food-delivery cart is short-lived (minutes to hours), single-session, and single-restaurant. The two guarantees a server-side cart provides — cross-device sync and survival across app-data loss — are marginal for this product. Meanwhile the cost side was real: a service, a DynamoDB table with ~5,000 WCU at ceiling (cart writes peak at several times the order rate, since most carts never convert), a port, dashboards, and deploys.

Crucially, **correctness never depended on the server-side cart.** The placement contract already takes `{restaurant_id, items: [{item_id, qty, modifiers}]}` — IDs and quantities, never prices — and the `PriceOrder` activity computes the authoritative immutable snapshot server-side at placement. The cart stored only a display estimate.

## Decision

**The cart is client state.** The FE persists it locally (localStorage/IndexedDB on web, local storage on mobile): item IDs, quantities, modifiers, the menu version browsed, and nothing price-authoritative.

Two server-side rules make this safe, and both must hold forever:

1. **`POST /v1/orders` accepts IDs and quantities, never prices.** The server resolves everything against current truth at placement (unchanged — this was always the contract).
2. **The display estimate comes from a stateless `POST /v1/quote` endpoint on the Order service**, which runs the same `libs/smartfood-pricing` code as `PriceOrder`. The FE calls it on the cart-review/checkout screen; simple badge math (sum of item prices from the cached menu) is fine elsewhere. This preserves ADR-0015's core property — one pricing implementation — without persisting anything: the price the customer reviews and the price they are charged come from the same functions.

Staleness handling is unchanged in kind: on opening the cart, the FE re-fetches the (CDN-cached, version-addressed) menu and re-renders — flagging changed prices and removed items exactly as the old `GET /v1/cart` re-resolution did, just client-side. Placement remains the authoritative check (`PRICE_CHANGED` diff → re-confirm; `RESTAURANT_CLOSED`; item validation).

## Consequences

**Positive**
- One less service, one less DynamoDB table (~5k WCU at ceiling), one less deploy/alert/dashboard surface; port 8003 retired.
- Cart interactions generate zero backend load — the spikiest write curve in the system disappears from the infrastructure entirely.
- The quote endpoint is stateless and lives where the pricing library already runs; nothing new to operate.

**Accepted losses** (explicit, not accidental)
- No cross-device cart continuation (start on phone, finish on laptop).
- Cart does not survive clearing browser/app data.
- No server-side abandoned-cart signal for analytics or remarketing (FE analytics events can substitute later).

**Revisit triggers** — reintroduce a server-side cart (the old design is documented in git history) when any becomes a product requirement: cross-device sync; abandoned-cart remarketing as a business line; or Part B's ordering assistant needing server-held order drafts it can build across turns.
