# 0008 — Serverless verdicts: Lambda for bursty loss-tolerant edges only

**Status**: Accepted

## Context

The mandate was to evaluate Lambda per-API rather than adopt or reject serverless wholesale. Ordering is sustained-hot at peak (design ceiling 5–10k orders/s; cell provisioned at 2,500/s), latency-sensitive, and PG-connected; other workloads are genuinely bursty and loss-tolerant.

## Decision

**Rule: request-shaped + bursty + loss-tolerant → Lambda; sustained-hot, stream-shaped, or connection-shaped → containers. Fargate before EKS.**

| Verdict | Workloads |
|---|---|
| **Lambda** (+ API Gateway where fronting) | Notification senders (canonical fit), menu-cache version bumps, webhook receivers, admin reports, presigned uploads, Firehose transforms, DDB Streams forwarder, Part B triggers |
| **Explicitly NOT Lambda** | Placement saga's synchronous path (sustained rate makes per-invoke pricing and PG pooling lose even with RDS Proxy; p99 cannot eat cold starts), hot Kafka consumers, WS/SSE termination (API GW WebSocket pricing prohibitive at this volume — ALB terminates both), Temporal workers |

Domain services run on ECS Fargate; rider/tracking gateways on ECS-on-EC2 (connection-density tuning).

## Consequences

**Positive**
- Bursty edges (notification spikes after an `OrderConfirmed` fan-out) scale to zero and to spike without capacity planning.
- The money path keeps warm containers, pooled PG connections (RDS Proxy), and predictable p99.
- The rule is mechanical — new workloads classify themselves without re-litigating serverless.

**Negative**
- Two compute models: two deployment pipelines, two observability setups (OTel on both, but wired differently), Lambda cold starts on rarely-hit admin paths.
- Some Lambda fits (e.g., forwarders) need dev-mode substitutes locally (`DISPATCH_FORWARDER=poller`, ADR-0012 pattern) because LocalStack's triggers are unreliable.

**Revisit trigger**: a "NOT Lambda" workload's utilization dropping to genuinely bursty (or vice versa — a Lambda edge going sustained-hot), or cost-per-order dashboards showing a compute verdict losing against the alternative.
