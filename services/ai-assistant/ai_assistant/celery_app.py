"""The AI plane's Celery application — batch work rides a task queue
(ADR-0025, ADR-0031).

B1 puts one task here: the rolling reindex. It belongs on a queue rather
than in the service process for the reasons ADR-0031 already gave — it is
long, rare, and wants its own retry schedule and its own DLQ — and it is
deliberately NOT on the drain's loop, which has a freshness budget to keep
(NFR-28) and must not spend an hour re-embedding a corpus. Content drafts
and feedback summaries join this app in B6.

Config mirrors notification's, and for the same reasons — acks_late so a
worker killed mid-task re-delivers, prefetch 1 so a slow task cannot starve
its siblings, explicit task names because the name is the wire contract.

**One deliberate divergence.** notification's tasks are sync all the way
down, and its module says why: a private event loop per task buys nothing
when the task is one small unit of a high-volume stream. That premise does
not hold here. A reindex runs when someone changes an embedding model —
rarely, under supervision — and each invocation is hundreds of rows and
several seconds of provider I/O. Paying for one `asyncio.run` per
invocation to reuse the already-tested async store and embedding adapters
is a straight win; maintaining a second, sync copy of both to avoid it
would not be.
"""

import os

from celery import Celery
from celery.signals import worker_ready
from smartfood_otel import serve_metrics, setup_logging

from .config import Settings

_settings = Settings()

celery_app = Celery("ai-assistant", broker=_settings.celery_broker_url or None)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=False,  # the operator reads `done` to know when to stop
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_routes={
        "assistant.reindex": {"queue": "assistant.reindex"},
        # Its own queue, not the reindex's: a reindex is hours of batch
        # work and a draft is a restaurant admin waiting at a console.
        # Sharing one queue would put every draft behind a corpus
        # migration, which is the scaling seam ADR-0031 asks for and the
        # reason routing is explicit here rather than defaulted.
        "assistant.content.draft": {"queue": "assistant.content"},
        "assistant.content.summarise": {"queue": "assistant.content"},
    },
)


@worker_ready.connect
def _on_worker_ready(**_: object) -> None:  # pragma: no cover — live wiring
    setup_logging("ai-assistant-worker")
    port = os.environ.get("WORKER_METRICS_PORT", "")
    if port:
        serve_metrics(int(port))
