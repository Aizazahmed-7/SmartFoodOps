# 0025 — Side effects ride a task queue; projections ride the log

**Status**: Accepted

## Context

FR-41 requires the notification service to actually SEND — starting with a payment receipt (rendered PDF, emailed) when an order settles. Until now every consumer in the fleet has been a **projection**: the inbox, the analytics facts, the recipients join — all of them absorb at-least-once redelivery by upserting state, and all of them *benefit* from Kafka's replayability (rebuild = re-read the topic). Sending an email inverts that property: a replayed `send` is a **second email in a human's inbox**. The pipeline also has two dissimilar halves — rendering a PDF is CPU-bound, talking to a mail provider is I/O-bound with its own failure taxonomy (transient 5xx vs permanent 4xx) — and each half wants its own retry schedule, backoff, and scaling curve, none of which a partitioned log offers per-message. The obvious question was whether to press Kafka into this role (it is already deployed) or to introduce the second kind of middleware the stack has so far deliberately avoided.

## Decision

**Receipts run as a Celery chain over RabbitMQ** (`notification/celery_app.py`, ADR-scoped to side-effect jobs — projections stay on `EventConsumer`):

1. **Trigger**: the existing Kafka consumer, on `OrderSettled`, files a **claim check** — a `receipts` row holding everything the document needs, copied from the full-state payload, `ON CONFLICT DO NOTHING` on `order_id` — in the *same transaction* as its other inbox writes. The broker message carries only the `order_id`; bytes never ride the broker.
2. **Enqueue is a post-commit, best-effort nudge** (the bell-hint contract): failure is counted, never raised. Reliability comes from the row itself — a **beat sweeper** re-enqueues any receipt past a grace window that has no `delivery_log` entry. Lost nudge ⇒ one sweep interval of delay, never a lost receipt, and no broker transaction or second outbox needed.
3. **Two queues, two workers**: `receipts.render` (CPU: claim check → PDF → S3 at the deterministic key `receipts/{order_id}.pdf`, returns the key) chained into `receipts.send` (I/O: `delivery_log` check → provider → record). The chain passes the S3 *key*, so storage scales independently of the broker.
4. **At-least-once embraced, not fought**: `task_acks_late` + `task_reject_on_worker_lost` mean a killed worker re-runs the task; render absorbs replays by overwriting the same key, send absorbs them via `delivery_log (order_id, channel)` — checked before, recorded after. The residual crash window can duplicate one email; the inverse ordering (claim-before-send) would convert the same crash into a receipt that never arrives. For a money document, duplicate beats missing.
5. **Provider errors are a two-way contract** (`Sender` port): `MailerUnavailable` (5xx/network) is in `autoretry_for` with exponential backoff + jitter; `MailerRejected` (4xx) **parks** the receipt (`failed_at`, excluded from the sweeper) and fails loudly — the DLQ philosophy applied to email, with `failed_at = NULL` as the replay lever.

## Consequences

**Positive**
- The rule is now legible fleet-wide: **replay-safe state ⇒ Kafka consumer; human-visible side effect ⇒ task queue.** Dispatch's offer timers and future SMS senders have a home.
- Each half scales on its own axis (renderers with cores, senders with connections) and retries on its own schedule, per message — the thing per-partition offsets cannot express.
- The claim-check + sweeper pair makes the pipeline self-healing without distributed transactions: every failure mode degrades to "the sweeper finds it" or "the row is parked and visible".
- `Sender` and the S3 store are ports: SES/SQS (per ADR-0008's serverless posture) swap in without touching a task.

**Negative**
- A second broker to run, monitor, and explain (RabbitMQ joins the core profile; ~150 MB RAM in dev, one more failure domain in prod).
- Exactly-once is explicitly NOT claimed: a crash in the send-record window can duplicate an email; a poisoned receipt needs a human to clear `failed_at`.
- Celery is a sync world — the worker carries a parallel sync stack (psycopg engine, sync httpx, boto3) beside the service's async one, built lazily per prefork child for fork safety.
