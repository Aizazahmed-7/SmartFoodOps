# 0005 — JWT verified once at edge; identity headers + network trust internally

**Status**: Accepted

## Context

Every request needs authenticated identity, but verifying JWTs in all 13 services means 13 JWKS caches, 13 clock-skew configs, and 13 places to get verification subtly wrong. One hardened verifier beats ten mediocre ones — provided downstream services can trust what it forwards.

## Decision

Identity issues RS256 JWTs (15-min access; claims `sub`, `role`, scoping `restaurant_id`/`rider_id`, `cell`, `jti`) with 30-day opaque rotating refresh tokens (family reuse detection). **Verification happens exactly once, at edge-bff** (JWKS cached in-process 10 min, two live keys for rotation). The edge strips all inbound `X-Auth-*` headers, then stamps `X-Auth-Sub`, `X-Auth-Role`, `X-Auth-Restaurant-Id`, `X-Auth-Rider-Id`. Services consume them via the shared `smartfood-auth` middleware (`AuthContext` dependency) and never parse JWTs.

Headers are trustworthy because **domain services have no public routes** — reachable only from edge/gateways/peers in private subnets. Service-to-service and Temporal activities use internal-network trust: `X-Internal-Caller` for audit, original actor identity propagated (stored in workflow input, restamped by activities); system work runs as `role: system`, `sub: svc:order-worker`.

Ownership enforcement stays in the owning service, in the query (`WHERE id=:id AND restaurant_id=:ctx.restaurant_id`; 0 rows → 404, no existence leaks).

## Consequences

**Positive**
- One audited verifier; services get a typed `AuthContext` with zero crypto.
- Header strip-then-stamp defeats spoofing at the only ingress; the network topology is the enforcement, and it is load-bearing by design.
- Swapping the trust mechanism later is invisible to services — the middleware hides it.

**Negative**
- A misconfigured security group exposing a domain service breaks the model silently — infrastructure review is part of the security boundary.
- No per-hop cryptographic identity in phase 1; internal compromise can forge headers. Hardening path documented and deferred: edge-minted short-lived internal JWTs, then mTLS.

**Revisit trigger**: compliance requirement for zero-trust internals, any multi-tenant/VPC-sharing change, or the first internal-network security finding → activate the internal-JWT/mTLS hardening path.
