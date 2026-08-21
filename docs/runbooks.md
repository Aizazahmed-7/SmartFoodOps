# Alert runbooks

One section per alert rule (`deploy/compose/observability/alerts.yml`). Every
section answers three questions in order: what does this MEAN, what do I CHECK
first, what FIXES it. The universal scan order behind all of them:
**Readiness panel → 5xx panel → domain panels**, then Jaeger for the failing
hop, then logs by `trace_id`. Dashboard: http://localhost:3000/d/smartfoodops-slo

## ServiceDown

**Means**: Prometheus could not scrape the target — the process is dead,
crash-looping, or unreachable. This is the only alert that does not depend on
the sick service reporting its own sickness.
**Check**: `docker compose ps <svc>` (crash-loop shows recent restart);
`docker compose logs --tail 30 <svc>` for the exit reason. Common local causes:
stale Docker DNS after a daemon restart (bounce the app tier), a migration
mismatch after a branch switch (`alembic can't locate revision` → `make nuke`),
a workspace member missing `pyproject.toml`.
**Fix**: address the exit reason; `docker compose --profile core --profile apps
restart <svc>`. Verify: the `up` line returns to 1 within one scrape (15s).

## HttpErrorRateHigh

**Means**: >5% of a service's responses are 5xx over 10m. It fires on the
VICTIMS — the culprit is usually below them (a 503 on order + edge usually
means a dependency of order).
**Check**: which jobs fire together (the blast-radius boundary points at the
cause); one `error=true` trace in Jaeger — the failing hop is where child
spans stop appearing (diagnosis by absence).
**Fix**: per the failing hop. Verify: ratio decays out of the 10m window.

## PlacementP95Breach

**Means**: fresh placements (`outcome="placed"` only) breach NFR-2's 3s p95.
Replays/refusals are excluded by design — this is real customer wait.
**Check**: placement-outcomes panel for a `pending` shift (worker starvation —
the 2s await losing to schedule-to-start); one slow placement trace — the fat
span or the gap inside `StartUpdateWithStartWorkflow` names the layer
(worker queue vs identity vs catalog vs DB).
**Fix**: worker starvation → scale order-worker; dependency slowness → that
service's runbook. Verify: p95 line bends back under 3s.

## OutboxPublishLagHigh

**Means**: events are flowing but STALE (staged→published p99 > 5s, NFR-6).
Every consumer is reacting to the past.
**Check**: backlog series next to it — lag high + backlog falling = burst
being digested (usually no action); backlog rising = poller/Kafka trouble.
Poller logs in the OWNING service (`outbox drain failed — will retry`).
**Fix**: Kafka health; poller crash-loop; DB contention on the outbox table.

## OutboxBacklogGrowing

**Means**: >100 rows staged-but-unpublished for 10m. The gauge is set at the
START of every pass (success or not), so this fires even when Kafka is fully
down and the lag histogram has gone silent.
**Check**: is ANYTHING publishing (`rate(outbox_published_total[5m])`)?
Zero = the poller or Kafka is down, not slow. Kafka container, then poller logs.
**Fix**: restore Kafka / restart the owning service. The backlog self-drains —
rows are never lost, only late. Verify: gauge falls; consumers catch up.

## EventsParkedOnDLQ

**Means**: a consumer exhausted five retries (or hit undecodable bytes) and
parked the ORIGINAL event on `<topic>.dlq` (ADR-0021). Facts are waiting for
a human; nothing is lost, something is stuck.
**Check**: which `group` label fired; the consumer's logs for `event parked to
DLQ` (carries error type + source offset); the DLQ topic in the Kafka console
(:8085) — `dlq.*` headers hold the forensics.
**Fix**: fix the handler/dependency, then `make dlq-replay
TOPIC=<source-topic>.dlq` to re-feed the parked events. Undecodable bytes
(SerdeError, attempts=0) can never replay — investigate the producer instead.

## SystemTimeoutCancels

**Means**: the saga's forward deadline (default 300s) expired — reserve/
authorize/confirm could not complete while a customer's order was live, and it
was unwound to CANCELLED `system_timeout`. Customers were notified and never
charged; the alert is about the DEPENDENCY that stayed down.
**Check**: which downstream was unreachable — the workflow history in Temporal
UI (:8233, `ord::{order_id}`) shows the retrying activity and its error;
usually pairs with a ServiceDown/5xx alert naming the culprit.
**Fix**: restore the dependency. Orders cancelled this way are correctly
terminal — customers re-order; nothing needs replaying.

## ConsumerLagGrowing

**Means**: a consumer group is >100 events behind its topic for 10m — it is
processing slower than producers produce (kafka-exporter reads the broker's
own books, so this is real lag, not a proxy).
**Check**: the consumers panel — is the group's `handled` rate flat while
outbox `published` climbs? Any `retried`/`dlq` for the group (a struggling
dependency slows every event)? cAdvisor CPU for the consuming service.
**Fix**: dependency slowness → that runbook; genuine throughput → scale the
consumer — BUT replicas beyond the topic's partition count sit idle
(partitions are the parallelism ceiling; dev topics have 1). Verify: lag
trends to zero; it self-drains, nothing is lost.

## CanarySilent

**Means**: the synthetic customer (canary, obs profile) has not completed a
full place→CONFIRMED→cancel loop in 5 minutes. Whatever the per-service
panels say, the PRODUCT is not working end to end.
**Check**: the canary's own logs name the failing step (`docker compose logs
canary`); then treat it as a customer report — the placement runbooks apply.
Usually pairs with another alert naming the culprit; when it fires ALONE,
suspect the seams (gateway routing, seeded data missing after a nuke, auth).
**Fix**: per the failing step. `make seed` restores the demo world the
canary depends on. Verify: canary_runs_total{outcome="ok"} moving again.

## WorkerTargetAbsent

**Means**: no `order-worker` instance is being discovered at all. This fires
where `ServiceDown` structurally cannot: the worker jobs use `dns_sd_configs`,
so a dead container makes Docker DNS return nothing, Prometheus scrapes an
empty target list, and `up` disappears instead of going to 0. Absence is the
symptom for every discovery-based target. No worker means no saga advances —
placements answer `PlacementPending` (the workflow is durable and waits), the
kitchen's accept/reject signals queue, and nothing reaches SETTLED.
**Check**: `docker compose ps order-worker` (crash-loop shows recent restarts);
`make logs SVC=order-worker` for the exit reason — a sandbox import violation
in `workflows.py` and a failed Temporal connect are the two common ones.
Temporal UI :8233 confirms the diagnosis from the other side: workflows in
Running with activity tasks piling up unstarted.
**Fix**: repair the exit reason and restart the worker; the workflows resume
from their event history exactly where they stopped — nothing is lost, and
in-flight orders continue rather than needing replacement. Verify: the target
reappears within one scrape (15s) and `temporal_activity_schedule_to_start_latency`
p95 drains back toward baseline as the backlog clears.

## TracingDisarmed

**Means**: a process is serving traffic with span export switched off. Almost
always config drift, not a bug: `OTLP_ENDPOINT` is empty unless `make up-obs`
injects it, so any other `docker compose up`/`restart` that recreates a
container brings it back untraced. Metrics and logs keep flowing, which is what
makes this invisible without the gauge — you discover it during the incident
that needed the traces.
**Check**: `curl -s localhost:9090/api/v1/query?query=tracing_armed` names the
jobs reporting 0; confirm with
`docker inspect smartfoodops-<svc>-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep OTLP`.
**Fix**: `make up-obs` — it is the only target that sets the endpoint, and it
recreates the app tier with it. Verify: `tracing_armed` is 1 for every job, and
a fresh order produces a trace spanning all services rather than a partial one
(Jaeger → the `edge-bff POST /v1/orders` root should carry ~80 spans).
