"""ai-assistant — the GenAI plane (ADR-0029).

Request-shaped and latency-sensitive, so it runs as a container beside the
other domain services rather than as a Lambda (ADR-0008's rule). B0 wires
the providers, the router, the budget guard and the diagnostic routes; the
knowledge consumer (B1) and the Celery lanes (B6) attach as lifespan
runners and separate entrypoints later, the way notification's do.

The injection seams are deliberately mid-grained. Tests pass `providers=`
so they exercise the REAL router, guard and service over a scripted fake
provider — the failover rule and the budget arithmetic are the parts most
worth testing, and a coarse `service=` override would skip both.
"""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from smartfood_realtime import StreamConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .adapters.catalog_client import CatalogClient
from .adapters.inventory_client import InventoryClient
from .adapters.order_client import DeliveryClient, FeedbackClient, OrderTimelineClient
from .adapters.retriever import PostgresRetriever
from .api.chat import STREAM_PREFIX as CHAT_STREAM_PREFIX
from .api.chat import router as chat_router
from .api.routes import STREAM_PREFIX, router
from .cards import CardService
from .chat import ChatService
from .config import Settings
from .db import metadata, outbox
from .domain.budget import BudgetGuard, BudgetStore
from .domain.ports import EmbeddingPort, LlmPort
from .domain.render import TemplateCache
from .domain.retrieval import Hydrated, Passage
from .domain.router import ModelRouter, default_policy
from .domain.service import AssistantService
from .drafts import DraftStore
from .drain import model_version
from .explain_service import ExplainService
from .menu_facts import MenuFactsReader
from .polish_cache import PolishedTemplates
from .profiles import ProfileBuilder
from .recommend import Recommender
from .restaurant_facts import RestaurantFactsReader

Runner = Callable[[], Coroutine[Any, Any, None]]
"""A lifespan-managed background loop — analytics' spelling, same reason:
`create_task` wants a coroutine, not any awaitable."""


def _run_migrations(database_url: str) -> None:  # pragma: no cover — Postgres-only path,
    # exercised by the compose stack, not the sqlite unit suite.
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def _embeddings(settings: Settings, http: httpx.AsyncClient | None) -> EmbeddingPort:
    """Real provider when a key is present, deterministic fake when not.

    Embeddings get a FALLBACK where generation gets a refusal (ADR-0030 §7),
    and the asymmetry is deliberate. A missing key must not mean the
    knowledge pipeline silently does not run: that would put `make up-ai`,
    every B1 demo and the whole milestone's live proof behind a vendor
    account. Generation can answer 503 and degrade a panel; an index that
    quietly stays empty degrades nothing visibly and everything actually.
    """
    from .adapters.embeddings_fake import FakeEmbeddings

    if settings.openai_api_key and http is not None:  # pragma: no cover — live wiring
        from .adapters.embeddings_openai import OpenAiEmbeddings

        return OpenAiEmbeddings(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            http=http,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    return FakeEmbeddings(dimensions=settings.embedding_dimensions)


def _answer_cache(settings: Settings, sessions: Any, redis: Any, model_version: str = "") -> Any:
    """Both tiers, or neither.

    An exact tier without Redis is not a degraded cache, it is a lookup that
    always misses plus a write that always fails — so the switch is one
    switch, and `answer_cache=off` means the turn does exactly what it did
    before FR-74 existed.
    """
    if settings.answer_cache == "off" or redis is None:
        return None
    from .adapters.answer_cache import PostgresSemanticCache, RedisExactCache  # noqa: PLC0415
    from .cache import AnswerCache  # noqa: PLC0415

    return AnswerCache(
        sessions,
        exact_tier=RedisExactCache(redis, ttl_s=settings.answer_cache_ttl_s),
        semantic_tier=PostgresSemanticCache(sessions, threshold=settings.answer_cache_distance),
        model_version=model_version,
    )


def _publish(bus: Any) -> Any:
    """The turn's sink onto the hint channel.

    No None-guard: a turn can only start through the route, and the route
    answers 503 when there is no bus. One guard, at the edge, rather than a
    second one here that would only ever be a lie about reachability.
    """

    async def publish(channel: str, data: str) -> None:
        await bus.publish(channel, data)

    return publish


def _graph_builder(app: FastAPI, settings: Settings) -> Any:
    """A factory, because each turn needs its OWN emit sink — the graph is
    otherwise identical and the ports behind it are shared."""

    def build(emit: Any) -> Any:  # pragma: no cover — live wiring
        from .domain.graph import build_turn

        cache = app.state.answer_cache
        return build_turn(
            retrieve=_retrieve_with_text(app, settings.candidate_limit),
            stream=app.state.service.router.stream,
            emit=emit,
            exact=cache.exact if cache else None,
            semantic=cache.semantic if cache else None,
            remember=cache.remember if cache else None,
            fallback=_popular_in(app, settings.candidate_limit),
            goes_with=_goes_with(app, settings.recommend_limit),
            limit=settings.candidate_limit,
        )

    return build


def _popular_in(app: FastAPI, limit: int) -> Any:  # pragma: no cover — live wiring
    """FR-80's fallback: what the city is ordering around now, hydrated into
    the same `Passage` shape retrieval produces.

    The same shape matters — `ground` and the interaction fact both read
    passages, and two code paths producing two shapes is how a citation
    comes to mean one thing on the retrieval path and another here.
    """

    async def popular(city: str) -> list[Passage]:
        return await app.state.recommender.popular(city=city, limit=limit)

    return popular


def _record_answer_shown(app: FastAPI) -> Any:  # pragma: no cover — live wiring
    """An answer's citations, into the same acceptance denominator as the
    panel's list (FR-79)."""

    async def record(user_id: str, city: str, item_ids: Any) -> None:
        await app.state.recommender.record_shown(
            user_id=user_id, city=city, surface="answer", basis="", item_ids=list(item_ids)
        )

    return record


def _goes_with(app: FastAPI, limit: int) -> Any:  # pragma: no cover — live wiring
    """FR-78's accompaniment lookup, in the same `Passage` shape everything
    else on the candidate path uses."""

    async def partners(item_ids: Any, city: str) -> list[Passage]:
        return await app.state.recommender.goes_with(
            item_ids=list(item_ids), city=city, limit=limit
        )

    return partners


def _retrieve_with_text(app: FastAPI, limit: int) -> Any:  # pragma: no cover — live wiring
    """Rank, then hydrate. Both halves are tested on `PostgresRetriever`;
    what is here is only the pairing, reached through `_graph_builder`."""

    async def retrieve(question: str, filters: Any) -> Hydrated:
        found = await app.state.retriever.retrieve(query=question, filters=filters, limit=limit)
        return Hydrated(
            passages=await app.state.retriever.texts_in_order(found.items),
            query_vector=found.query_vector,
        )

    return retrieve


_TASK_FOR_KIND = {
    "menu_item": "assistant.content.draft",
    "promotion": "assistant.content.draft",
    "engagement": "assistant.content.draft",
    "feedback_summary": "assistant.content.summarise",
}
"""Which worker task writes which kind.

A summary reads a live corpus and verifies its quotes against it; a draft
writes from facts frozen on the row. Different work, different task, one
queue — the routing is a mapping here rather than a branch inside one task,
so a kind with no task is a KeyError at enqueue time instead of a job that
sits in `queued` forever.
"""


def _enqueue_draft(draft_id: str, kind: str) -> None:  # pragma: no cover — live wiring
    """Hand one committed draft to the content queue.

    Sent by NAME rather than by importing the task function. The name is
    the wire contract between producer and worker (celery_app.py says so
    about every task here), and it means the API process never imports the
    task module — which pulls in the engine wiring a worker needs and an
    API serving a read does not.

    Injected through app state so a test can assert WHICH ids were
    enqueued: the guarantee that matters is "row committed, then
    enqueued", and that is only observable from the call site.
    """
    from .celery_app import celery_app

    # Which task depends on the kind, resolved by the caller that knows
    # it — the enqueue seam stays one function so the commit-then-enqueue
    # order has one place to be right.
    celery_app.send_task(_TASK_FOR_KIND[kind], args=(draft_id,))


def create_app(
    settings: Settings | None = None,
    *,
    providers: dict[str, LlmPort] | None = None,
    budget_store: BudgetStore | None = None,
    runners: list[Runner] | None = None,
    realtime: Any | None = None,
    graph_builder: Any | None = None,
    poller: Any | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("ai-assistant")
    # Must precede every engine/client construction below: setup_tracing
    # arms the httpx and SQLAlchemy instrumentors, so a client built first
    # is a client whose provider calls never appear as spans.
    setup_tracing("ai-assistant", settings.otlp_endpoint)

    engine_kwargs: dict[str, Any] = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # One client for first-party service-to-service calls (catalog cards,
    # inventory load). Separate from the provider client, which is built
    # only when `providers=` was not injected — internal calls must not
    # depend on whether a test supplied fake LLMs.
    internal_http = httpx.AsyncClient(timeout=5.0)
    own_http: httpx.AsyncClient | None = None
    own_redis: Any | None = None

    if providers is None:  # pragma: no cover — live wiring (compose runs it)
        from .adapters._http import RetryPolicy
        from .adapters.llm_anthropic import AnthropicLlm
        from .adapters.llm_openai import OpenAiLlm

        own_http = httpx.AsyncClient()
        retry = RetryPolicy(attempts=settings.llm_max_attempts, delay_s=settings.llm_retry_delay_s)
        providers = {}
        # An empty key REMOVES a provider from the fleet rather than
        # producing 401s at request time (ADR-0030 §7). Zero registered
        # providers is a legal, deliberate state: every generative path
        # answers 503 and the plane runs retrieval-only.
        if settings.anthropic_api_key:
            providers["anthropic"] = AnthropicLlm(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
                http=own_http,
                api_version=settings.anthropic_version,
                retry=retry,
            )
        if settings.openai_api_key:
            providers["openai"] = OpenAiLlm(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                http=own_http,
                retry=retry,
            )

    own_realtime: Any | None = realtime
    if own_realtime is None and settings.redis_url:  # pragma: no cover — live wiring
        import redis.asyncio as aioredis
        from smartfood_realtime import RedisRealtime

        own_realtime = RedisRealtime(aioredis.from_url(settings.redis_url))

    # One client for both Redis consumers. Built whenever a url is
    # configured rather than only when the budget store needs it: tying the
    # answer cache's existence to whether somebody injected a budget store
    # would be a coupling nothing states and nobody would find.
    if settings.redis_url:  # pragma: no cover — live wiring
        import redis.asyncio as aioredis

        own_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        if budget_store is None:
            from .adapters.budget_redis import RedisBudgetStore

            budget_store = RedisBudgetStore(own_redis)

    # ONE embedder, and `model_version` is derived from it rather than
    # recomputed from settings. Two derivations of the same string from
    # different inputs is how the active version comes to name a vector
    # space nothing writes: `_embeddings` also requires an http client, so a
    # caller that supplies `providers=` with a key set would adopt the real
    # model's version while the drain wrote the fake's — leaving retrieval
    # filtering on a generation with no rows in it, silently.
    embeddings = _embeddings(settings, own_http)
    active_version = model_version(embeddings)

    live_runners = list(runners) if runners is not None else []
    if not live_runners and settings.kafka_consumers == "on":  # pragma: no cover — live
        # Live wiring only: the loop, the bounded retry and the DLQ policy
        # are all tested in smartfood-kafka; what is ours is the handler,
        # and that is unit-tested directly.
        from smartfood_kafka import AvroSerde, EventConsumer, SchemaRegistry, Topic, topic

        from .consumers import (
            GROUP_FEATURES,
            GROUP_KNOWLEDGE,
            GROUP_VIEWS,
            FeatureHandler,
            KnowledgeHandler,
            ViewHandler,
        )
        from .drain import KnowledgeDrain

        knowledge_consumer = EventConsumer(
            topic(settings.cell_id, Topic.CATALOG_CHANGES),
            GROUP_KNOWLEDGE,
            KnowledgeHandler(sessions, debounce_s=settings.knowledge_debounce_seconds),
            AvroSerde(SchemaRegistry(settings.schema_registry_url)),
            bootstrap=settings.kafka_bootstrap,
        )
        drain = KnowledgeDrain(
            sessions,
            embeddings,
            interval_s=settings.knowledge_drain_interval_seconds,
            batch=settings.knowledge_drain_batch,
        )
        # A second group, on the ORDERS topic: what the city is ordering
        # (FR-80) and who orders what (FR-75). Its own loop, so a backlog of
        # order history can never sit in front of the menu updates that keep
        # retrieval correct.
        features_consumer = EventConsumer(
            topic(settings.cell_id, Topic.ORDERS_EVENTS),
            GROUP_FEATURES,
            FeatureHandler(sessions, window=timedelta(minutes=settings.attribution_window_minutes)),
            AvroSerde(SchemaRegistry(settings.schema_registry_url)),
            bootstrap=settings.kafka_bootstrap,
        )
        views_consumer = EventConsumer(
            topic(settings.cell_id, Topic.BROWSE_EVENTS),
            GROUP_VIEWS,
            ViewHandler(sessions),
            AvroSerde(SchemaRegistry(settings.schema_registry_url)),
            bootstrap=settings.kafka_bootstrap,
        )
        live_runners = [
            knowledge_consumer.run,
            drain.run,
            features_consumer.run,
            views_consumer.run,
            ProfileBuilder(
                sessions,
                interval_s=settings.profile_interval_seconds,
                model_version=active_version,
            ).run,
        ]

    own_producer: Any | None = None
    if poller is None and settings.outbox_mode == "poller":  # pragma: no cover — live wiring
        from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic
        from smartfood_outbox import OutboxPoller

        own_producer = EventProducer(
            settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
        )
        poller = OutboxPoller(
            sessions,
            outbox,
            topic=topic(settings.cell_id, Topic.ASSISTANT_EVENTS),
            producer=own_producer,
            cell_id=settings.cell_id,
        )

    policy = default_policy(
        generate_model=settings.model_generate,
        cheap_model=settings.model_cheap,
        generate_fallback=settings.model_generate_fallback,
        cheap_fallback=settings.model_cheap_fallback,
        timeout_s=settings.llm_timeout_s,
    )
    service = AssistantService(
        # The switch rides on the ROUTER, so it covers the chat graph, the
        # explanation polish and anything added later — not just the two
        # internal endpoints `prepare` happens to serve.
        router=ModelRouter(providers, policy, generation=settings.generation),
        guard=BudgetGuard(
            cell_id=settings.cell_id,
            user_budget=settings.user_token_budget,
            user_window_s=settings.user_token_window_s,
            cell_budget=settings.cell_token_budget,
            cell_window_s=settings.cell_token_window_s,
            store=budget_store,
        ),
        generation=settings.generation,
        max_concurrent_turns=settings.max_concurrent_turns,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        tasks = [asyncio.create_task(runner()) for runner in live_runners]
        if poller is not None:
            if own_producer is not None:  # pragma: no cover — live path
                await own_producer.start()
            # The drain joins the same task list, so shutdown cancels it with
            # everything else: a poller left running past the engine's
            # dispose is a pass that fails on a closed pool every second.
            tasks.append(asyncio.create_task(poller.run()))
        yield
        # Turns first: they write to the engine disposed below, and a turn
        # killed by the loop closing leaves its row `streaming` forever —
        # every later reader then snapshots `done=False` and reconnects for
        # good (found by the B3 review).
        await app.state.chat.drain()
        for task in tasks:
            task.cancel()  # cancellation is the consumer's shutdown signal
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        # Before the clients close: a warm task mid-call would otherwise
        # die against a closed pool and be swallowed as an ordinary failure.
        await app.state.explanations.drain()
        await internal_http.aclose()
        if own_producer is not None:  # pragma: no cover — live wiring
            await own_producer.stop()  # flushes whatever the last pass sent
        if own_http is not None:  # pragma: no cover — live wiring
            await own_http.aclose()
        if own_redis is not None:  # pragma: no cover — live wiring
            await own_redis.aclose()
        await engine.dispose()

    app = FastAPI(title="ai-assistant", lifespan=lifespan)
    # Token streams are LIFETIMES, not latencies. Without this the echo
    # stream's full duration lands in http_request_duration_seconds and
    # pins the service p95 at the top bucket — the live incident recorded
    # in smartfood_otel/middleware.py.
    # Both held-connection routes: a stream's LIFETIME is not a latency,
    # and one slow reader would otherwise pin the service p95 at the top
    # bucket (the incident recorded in smartfood_otel/middleware.py).
    app.add_middleware(
        RequestContextMiddleware, stream_prefixes=(STREAM_PREFIX, CHAT_STREAM_PREFIX)
    )
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    app.state.service = service
    app.state.sessions = sessions
    # Built once and holding a sessionmaker, so the route never touches an
    # adapter to open a session — the layer contract forbids api/ importing
    # adapters, and this is the seam that keeps it honest.
    app.state.stream_config = StreamConfig(
        ticket_ttl_s=60,
        heartbeat_s=settings.stream_heartbeat_seconds,
        # A generation lasts seconds, so the safety lifetime exists to reap a
        # wedged stream rather than to rebalance a fleet — far shorter than
        # the tracking lane's 15-30 minutes.
        lifetime_min_s=settings.stream_lifetime_seconds,
        lifetime_max_s=settings.stream_lifetime_seconds * 1.5,
    )
    app.state.realtime = own_realtime
    app.state.chat = ChatService(
        sessions,
        _publish(own_realtime),
        graph_builder or _graph_builder(app, settings),
        history=settings.history_limit,
        shown=_record_answer_shown(app),
    )
    app.state.answer_cache = _answer_cache(settings, sessions, own_redis, active_version)
    app.state.cards = CardService(
        sessions,
        CatalogClient(settings.catalog_base_url, internal_http),
        model_version=active_version,
    )
    app.state.kitchen_load = InventoryClient(settings.inventory_base_url, internal_http)
    app.state.drafts = DraftStore(sessions)
    app.state.menu_facts = MenuFactsReader(sessions, model_version=active_version)
    app.state.restaurant_facts = RestaurantFactsReader(sessions, model_version=active_version)
    app.state.feedback = FeedbackClient(settings.order_base_url, internal_http)
    # Enqueue is injected rather than imported, so the API can be exercised
    # without a broker — and so a test can assert WHICH ids were enqueued,
    # which is the ordering guarantee that matters (row committed first).
    app.state.enqueue_draft = _enqueue_draft
    app.state.explanations = ExplainService(
        timelines=OrderTimelineClient(settings.order_base_url, internal_http),
        deliveries=DeliveryClient(settings.dispatch_base_url, internal_http),
        kitchen_load=app.state.kitchen_load,
        # The template floor with a rewrite layer over it. With no
        # provider configured the router has no route for EXPLAIN, raises
        # NoProviderAvailable on the first (and only) attempt per key, and
        # every explanation goes on being answered from the template —
        # FR-87, arrived at by the machinery rather than by a flag.
        templates=PolishedTemplates(TemplateCache(), router=app.state.service.router),
    )
    app.state.retriever = PostgresRetriever(
        sessions, embeddings, ef_search=settings.hnsw_ef_search, model_version=active_version
    )
    app.state.recommender = Recommender(sessions, model_version=active_version)
    app.state.recommend_limit = settings.recommend_limit
    app.state.stream_lifetime_s = settings.stream_lifetime_seconds
    app.include_router(router)
    app.include_router(chat_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "ai-assistant"}

    return app


app = create_app()
