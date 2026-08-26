"""The Celery application — receipts ride a task queue (ADR-0025).

Why a SECOND broker when Kafka already exists: Kafka replays are the
FEATURE for projections (analytics rebuilds by re-reading) and the BUG for
side effects (a replayed "send email" is a second email in an inbox). The
task queue's unit of work is a JOB — acked per message, retried per
message with backoff, routed per queue — which is the contract senders
need and a partitioned log deliberately does not offer.

Config decisions, each load-bearing:

- task_acks_late + task_reject_on_worker_lost: the message is acked AFTER
  the task finishes, so a worker killed mid-task re-delivers instead of
  losing the job. That makes execution AT-LEAST-ONCE by construction —
  which is why every task body below is idempotent (deterministic S3 key,
  delivery_log check) rather than hoping for exactly-once.
- worker_prefetch_multiplier=1: fair dispatch. A prefetching worker grabs
  N jobs and starves its siblings while it chews a slow render; prefetch 1
  keeps queue latency ~ slowest single task, not slowest batch.
- task_ignore_result: no result backend. The chain passes render's return
  value to send INSIDE the broker message; nobody polls task results, so
  storing them would be a write-per-task to nowhere.
- Two queues: rendering is CPU (PDF bytes), sending is I/O (HTTP waits).
  Routing them apart is the scaling seam — prod scales renderers with
  cores (prefork + PROMETHEUS_MULTIPROC_DIR) and senders with
  connections (threads or gevent), without touching a task. Dev compose
  runs BOTH on the threads pool: prefork children keep their metrics in
  per-process registries the parent's /metrics never sees.
- Explicit task NAMES ("receipts.render"): the name is the wire contract
  between producer and worker, exactly like EventType — module paths as
  implicit names would break every queued message on a refactor.
"""

import os

from celery import Celery
from celery.signals import worker_ready
from smartfood_otel import serve_metrics, setup_logging

from .config import Settings

_settings = Settings()

celery_app = Celery("notification", broker=_settings.celery_broker_url or None)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_routes={
        "receipts.render": {"queue": "receipts.render"},
        "receipts.send": {"queue": "receipts.send"},
        # The sweep is DB + broker I/O, so it rides the I/O queue.
        "receipts.sweep": {"queue": "receipts.send"},
    },
    beat_schedule={
        "sweep-unsent-receipts": {
            "task": "receipts.sweep",
            "schedule": _settings.receipt_sweep_seconds,
        },
    },
)


@worker_ready.connect
def _on_worker_ready(**_: object) -> None:  # pragma: no cover — live wiring
    """Worker-side observability: structured logs and a /metrics port per
    worker container (WORKER_METRICS_PORT is set by compose; unset in the
    API container and in tests, where this signal never fires anyway)."""
    setup_logging("notification-worker")
    port = os.environ.get("WORKER_METRICS_PORT", "")
    if port:
        serve_metrics(int(port))
