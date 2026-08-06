# ADR-0015 — Pricing is a shared library, not a service

**Status**: Accepted

## Context

The original decomposition made Pricing its own ECS Fargate microservice, called over HTTP by the `PriceOrder` saga activity. Reviewing it against the tests we use for a service boundary, it fails all four:

- **Owns data?** No. Its own catalog entry read "reads Catalog promotions" — it owns nothing.
- **Scales independently?** No. Called exactly once per order placement, by exactly one caller (OrderWorkflow). Its load curve is Order's load curve.
- **Separate team or deploy cadence?** No, not with one team.
- **Distinct runtime profile?** No. Pure CPU-bound computation, no I/O of its own.

It is a function wearing a service costume, and the costume costs a network hop on the p99-constrained placement path, a failure mode where Pricing being down blocks all order placement, plus a deployment unit, scaling policy, dashboard, and alert to maintain.

There is also a caching argument that runs the other way from intuition. As a service, `PriceOrder` computes over *this specific cart* — inherently uncacheable, so every order pays the round trip. As a library, the only thing crossing the network is the promo and tax **rule set**, which changes a few times a day and caches well. Same correctness, fewer hops, fewer failure modes.

## Decision

**Pricing ships as `libs/smartfood-pricing`, consumed in-process by two callers:**

1. The **`PriceOrder` Temporal activity** in the Order workers — produces the authoritative immutable snapshot `{subtotal, discounts, fees, tax, total}` that all money math derives from.
2. **The stateless `POST /v1/quote` endpoint on the Order service** — produces the display estimate the customer reviews before checkout (the client-side cart calls it; ADR-0017).

Promo and tax rules are read from Catalog and Redis-cached; the library computes locally. `PriceOrder` remains a **distinct Temporal activity with an unchanged contract** — only its implementation changes from an HTTP call to a function call, which is what makes later extraction cheap.

Sharing one implementation between the display path and the authoritative path is a correctness win in its own right: the "price changed at checkout" diff becomes a genuine data difference rather than a bug in one of two implementations that drifted.

**Extraction trigger** — promote to a service when *any* of these becomes true:

- A second team owns pricing rules.
- A non-Python consumer needs pricing.
- Pricing grows a genuinely different scaling profile (ML-driven surge, per-customer personalization).
- Compliance requires the tax/fee engine to be independently deployed and audited.

## Consequences

**Positive**
- One less network hop on the critical placement path, helping the p99 PLACED→CONFIRMED < 6s SLO.
- One less deployment unit, scaling policy, dashboard, alert, and on-call surface.
- Removes a failure mode: pricing can no longer be "down" independently of its callers.
- Display and authoritative pricing cannot silently diverge.

**Negative**
- Pricing changes require redeploying the Order service (workers + quote endpoint) rather than one dedicated service. Acceptable because promos and tax tables are *data* in Catalog, not code — routine rule changes need no deploy at all; only changes to the calculation engine do.
- Two embedders means version skew is possible mid-deploy. Bounded by the uv workspace's single lockfile and by the snapshot being written once, at placement, by whichever version handled that order.
- Reverses a boundary if the extraction trigger fires. Cost is bounded precisely because the activity contract does not change.

**Revisit trigger**: any item in the extraction-trigger list above. Related: the Cart service was later removed entirely for similar thinness (ADR-0017 — cart is client state).
