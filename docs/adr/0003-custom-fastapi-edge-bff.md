# 0003 — Custom FastAPI edge-bff instead of an API gateway product

**Status**: Accepted

## Context

The edge needs JWT verification, identity-header stamping, rate limiting/admission, per-client response shaping, and read aggregation. Gateway products (Kong, Envoy, Traefik, API Gateway) excel at proxy plumbing but push our logic into Lua/WASM plugins or vendor DSLs — foreign territory for a Python-first team — while AWS already covers the plumbing.

## Decision

A single custom **FastAPI service (`edge-bff`)** behind CloudFront + ALB. No Kong/Envoy/Traefik in phase 1. TLS, HTTP/2, path routing, health-checked load balancing, WAF, and DDoS protection come from ALB + CloudFront; everything else at the edge is ordinary FastAPI middleware.

Boundaries are explicit: the edge owns JWT verify + coarse role-per-route gating, `X-Auth-*` strip/stamp, Redis admission buckets, `X-Request-ID`/OTel root span, routing, read aggregation, per-route timeouts, retries on idempotent GETs only, and circuit breakers. Services own business authorization, validation, persistence, idempotency, and saga participation. The edge **never writes domain state** (writes pass through unshaped) and is stateless — Redis + JWKS cache only, no DB.

WS/SSE gateways are deliberately **not** routed through edge-bff (long-lived connections would pin edge workers); the ALB splits by listener rules: `/ws/rider/*` → rider-gateway, `/sse/track/*` → tracking-gateway, default → edge-bff.

## Consequences

**Positive**
- Edge logic lives in the same language, repo, test harness, and shared libraries (`smartfood-auth`, OTel) as everything else; no plugin toolchain.
- Response shaping and read aggregation (e.g., order detail = Order read model + latest rider location) are first-class code, not gateway config.
- Local nginx on `:8080` emulates the ALB path rules exactly — zero code differs between compose and ECS.

**Negative**
- We own edge availability and performance; it must be the best-load-tested service in the fleet.
- No off-the-shelf gateway features (API keys, quotas, dev portal) when a partner program arrives.

**Revisit trigger**: a partner/public API program or multi-protocol needs → insert Envoy between ALB and services without changing service contracts.
