"""Building the model wiring a Celery task needs.

The API builds its providers in `main.py`'s lifespan, which a worker never
runs. Rather than duplicating that wiring in every task, it lives here once
— and returns None when nothing is configured, so a worker with no keys
parks its jobs with a readable reason instead of crashing on import.
"""

from typing import Any

from .config import Settings


def shed_reason(settings: Settings) -> str | None:
    """Why this worker must not call a model at all right now, or None.

    The degradation ladder's generation switch (NFR-29 step 2a) used to stop
    the CHAT path and nothing else: the content studio built its own
    generator straight from settings, so throwing the kill switch left the
    worker happily billing the provider for menu copy and review summaries.
    A switch that only half-works is worse than none, because the operator
    who threw it believes the spend has stopped.

    Checked BEFORE the job runs, so the shed costs nothing. The job parks
    with this as its reason — visible to the restaurant, replayable the
    moment the switch goes back on, and distinguishable from "no provider is
    configured", which is a deployment mistake rather than a decision.
    """
    if settings.generation == "off":
        return "generation is switched off — this will run when it is back on"
    return None


def build_generator(settings: Settings) -> Any | None:
    """A content generator over the real router, or None.

    None is the honest answer for a deployment with no provider: FR-87's
    template floor has no equivalent here, because invented copy is the
    whole product and there is nothing truthful to fall back to. The task
    parks the job with that as its reason, which is visible to the
    restaurant — better than a worker crash-looping on a missing key.
    """
    if shed_reason(settings) or not settings.openai_api_key:
        return None
    return _build(settings)  # pragma: no cover — live wiring


def build_router(settings: Settings) -> Any | None:
    """The model router a worker task needs, or None with no provider —
    or with generation switched off, which `shed_reason` explains."""
    if shed_reason(settings) or not settings.openai_api_key:
        return None
    return _router(settings)  # pragma: no cover — live wiring


def _router(settings: Settings) -> Any:  # pragma: no cover — live wiring
    import httpx

    from .adapters.llm_openai import OpenAiLlm
    from .domain.router import ModelRouter, default_policy

    provider = OpenAiLlm(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        http=httpx.AsyncClient(timeout=settings.llm_timeout_s),
    )
    return ModelRouter(
        {"openai": provider},
        default_policy(
            cheap_model=settings.model_cheap,
            generate_model=settings.model_generate,
            generate_fallback=settings.model_generate_fallback,
            cheap_fallback=settings.model_cheap_fallback,
            timeout_s=settings.llm_timeout_s,
        ),
        generation=settings.generation,
    )


def _build(settings: Settings) -> Any:  # pragma: no cover — live wiring
    import httpx

    from .adapters.content_generator import ContentGenerator
    from .adapters.llm_openai import OpenAiLlm
    from .domain.router import ModelRouter, default_policy

    http = httpx.AsyncClient(timeout=settings.llm_timeout_s)
    provider = OpenAiLlm(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        http=http,
    )
    policy = default_policy(
        cheap_model=settings.model_cheap,
        generate_model=settings.model_generate,
        generate_fallback=settings.model_generate_fallback,
        cheap_fallback=settings.model_cheap_fallback,
        timeout_s=settings.llm_timeout_s,
    )
    return ContentGenerator(
        router=ModelRouter({"openai": provider}, policy, generation=settings.generation)
    )
