# 0006 — SSE for customer tracking, WebSocket for riders; ticket auth for SSE

**Status**: Accepted

## Context

Two real-time populations with opposite shapes: riders send a bidirectional stream (GPS up at 1 Hz, offers down) from ~30k connections; customers only receive tracking updates, but at ~400–500k concurrent connections at ceiling. One protocol for both optimizes neither.

## Decision

- **Riders: WebSocket** on `rider-gateway` (binary protobuf ~30B pings, per-session seq dedupe). Auth via `Sec-WebSocket-Protocol: bearer,<jwt>` — never query strings, which leak into logs. The connection is bound to `rider_id`; GPS frames are attributed from connection state, never payload.
- **Customers: SSE** on `tracking-gateway` — unidirectional, LB-friendly, stateless gateways; auto-reconnect is built into `EventSource`. On connect/reconnect: snapshot from the durable DDB `order_tracking` read model, read-repair via workflow query if stale > 60s, then subscribe to Redis sharded pub/sub (`shared:trk:<delivery_id>`, 2s cadence en-route). Pub/sub loss can never strand a customer on stale state.
- **SSE ticket auth**: `EventSource` cannot set headers, so `POST /v1/track/ticket` (JWT-authed, ownership-checked) issues a single-use 60s Redis ticket, consumed with `GETDEL` on connect.
- Both gateways are separate ECS services on EC2 (connection density tuning), split from edge-bff by ALB listener rules. Connections have a 30-min hard max lifetime, then reconnect with fresh credentials; heartbeats every 20s under a 300s ALB idle timeout.

## Consequences

**Positive**
- Each population gets the right transport: cheap massive fan-out for customers, low-overhead bidirectional for riders.
- No JWTs in URLs anywhere; tickets are single-use and expire in 60s.
- Gateways scale independently of the request-shaped edge; SSE fleet is reused for Part B streaming LLM responses with zero changes.

**Negative**
- Two gateway codebases and two auth flows to maintain.
- Ticket flow adds one round-trip before tracking starts.
- SSE is one-way; any future customer→server interactivity needs a plain POST alongside.

**Revisit trigger**: customer features requiring true bidirectional streams, or SSE connection density making WS-with-multiplexing cheaper at the gateway tier.
