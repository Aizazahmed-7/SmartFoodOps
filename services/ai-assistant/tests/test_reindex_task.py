"""The Celery lane around the reindex.

Thin on purpose — everything worth testing lives in `run_reindex`, which
needs no broker. What is left is the wiring that only fails in production:
the queue route, the at-least-once posture, and the task shell actually
reaching the reindex with a real engine behind it.
"""

import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from ai_assistant.adapters.embeddings_fake import FAKE_MODEL, FakeEmbeddings
from ai_assistant.adapters.repo import IndexStateRepo
from ai_assistant.celery_app import celery_app
from ai_assistant.config import Settings
from ai_assistant.consumers import KnowledgeHandler
from ai_assistant.db import item_chunks, metadata
from ai_assistant.drain import KnowledgeDrain, model_version
from ai_assistant.tasks import _run
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

PAYLOAD = {
    "kind": "branch",
    "name": "Biryani House",
    "branch_label": "Downtown",
    "city": "springfield",
    "cuisines": ["pakistani"],
    "status": "open",
    "menu": {
        "categories": [
            {
                "name": "Mains",
                "items": [{"id": "i1", "name": "Karahi", "price_cents": 500, "available": True}],
            }
        ]
    },
}


def test_the_reindex_is_routed_to_its_own_queue():
    """Sharing a queue with a future draft-generation task would let an
    hour-long corpus migration sit in front of work a partner is waiting
    on."""
    assert celery_app.conf.task_routes["assistant.reindex"] == {"queue": "assistant.reindex"}


def test_execution_is_at_least_once_by_configuration():
    """acks_late means a worker killed mid-reindex re-delivers rather than
    losing the job — which is only safe because the reindex resumes from an
    anti-join instead of a cursor."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_the_result_is_kept_so_an_operator_can_read_done():
    """Unlike the receipts pipeline, which ignores results: here `done` is
    how you know whether to run it again."""
    assert celery_app.conf.task_ignore_result is False


async def test_the_task_shell_reaches_a_real_reindex(tmp_path):
    """The one thing unit-testing `run_reindex` cannot show: that the task
    builds a working engine, picks the fake embedder with no key set, and
    hands both to the reindex."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    old = FakeEmbeddings(dimensions=4, model="old-model")
    await KnowledgeHandler(sessions, debounce_s=30.0, clock=lambda: T0).handle(
        {
            "aggregate_type": "restaurant",
            "aggregate_id": "r1",
            "event_type": "ItemAdded",
            "payload": json.dumps(PAYLOAD),
        }
    )
    await KnowledgeDrain(
        sessions, old, interval_s=0.0, batch=10, clock=lambda: T0 + timedelta(seconds=31)
    ).tick()
    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version=model_version(old), now=T0)
        await session.commit()
    await engine.dispose()

    result = await _run(Settings(database_url=url, embedding_dimensions=4, openai_api_key=""))

    assert result["done"] and result["activated"]
    assert result["target_version"] == f"{FAKE_MODEL}:4"

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        versions = (await conn.execute(sa.select(item_chunks.c.model_version))).scalars().all()
    await engine.dispose()
    assert set(versions) == {f"{FAKE_MODEL}:4"}
