# 0004 — HTTP/JSON for internal calls, gRPC deferred

**Status**: Accepted

## Context

Internal service-to-service calls (including hot-path saga activities like Inventory and Payment) need a protocol. gRPC offers binary efficiency and streaming, but in a Python/FastAPI monorepo it brings protoc codegen, `grpcio` asyncio quirks, load-balancer complications with long-lived HTTP/2 connections, and worse debuggability — while our latency budget is dominated by DB round-trips and Temporal scheduling, not JSON parsing.

## Decision

**All internal calls are HTTP/JSON in phase 1 — gRPC dropped.** REST outside, HTTP/JSON inside. Contracts stay typed via shared Pydantic model packages plus OpenAPI-generated clients. Kafka topics (Avro, Schema Registry) are the async interfaces; this ADR covers only synchronous calls. Discovery via ECS Service Connect (`http://order.sfo.local:8000`), URLs env-injected; identical DNS names in compose.

## Consequences

**Positive**
- One protocol everywhere: `curl`-able endpoints, human-readable payloads in Jaeger, no codegen step in the dev loop, ALB/Service Connect load balancing just works.
- FastAPI + Pydantic gives request/response validation and OpenAPI schemas for free; typed clients are generated, not hand-rolled.
- New engineers debug the saga's activity calls with the same tools as public endpoints.

**Negative**
- JSON serialization costs more CPU and bytes than protobuf on hot paths — accepted because profiling, not folklore, must show it matters.
- No streaming RPC primitive; anything stream-shaped uses Kafka, WS, or SSE (which is where it belongs anyway).
- A later gRPC migration touches clients and servers of the affected pair — contained by the OpenAPI-client seam.

**Revisit trigger**: profiling shows serialization > 10% of p99 on a hot internal path → move that pair (Inventory and Payment, the hottest synchronous activities, are the first candidates) to gRPC; the hardening path is per-edge, not fleet-wide. Note that ADR-0015 removed the pricing hop entirely rather than optimizing it — deleting a call always beats making it faster.
