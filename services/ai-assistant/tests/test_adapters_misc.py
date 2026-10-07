"""Embeddings, the Redis spend counters, and the SSE frame reader."""

from typing import Any

import httpx
import pytest
from ai_assistant.adapters._http import RetryPolicy, sse_payloads
from ai_assistant.adapters.budget_redis import RedisBudgetStore
from ai_assistant.adapters.embeddings_openai import OpenAiEmbeddings
from ai_assistant.domain.ports import EmbeddingUnavailable

FAST = RetryPolicy(attempts=2, delay_s=0.0)


def embedder(handler: Any, **kwargs: Any) -> OpenAiEmbeddings:
    base: dict[str, Any] = dict(model="text-embedding-3-small", dimensions=512)
    base.update(kwargs)
    return OpenAiEmbeddings(
        api_key="k",
        base_url="https://api.test/",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry=FAST,
        **base,
    )


async def test_embed_sends_the_dimensions_and_exposes_the_model_version():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    adapter = embedder(handler)
    assert (adapter.model, adapter.dimensions) == ("text-embedding-3-small", 512)
    vectors = await adapter.embed(["a light soup"])
    assert seen["dimensions"] == 512
    assert vectors == [[0.1, 0.2]]


async def test_embed_realigns_out_of_order_responses():
    """The API returns them in order; sorting makes the alignment explicit
    and removes a silent, catastrophic failure mode where every chunk gets
    its neighbour's vector."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2.0]},
                    "not-a-dict",
                    {"index": 0, "embedding": [1.0]},
                ]
            },
        )

    assert await embedder(handler).embed(["first", "second"]) == [[1.0], [2.0]]


async def test_embed_short_circuits_on_an_empty_batch():
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover — must not be called
        raise AssertionError("no request should be made for an empty batch")

    assert await embedder(handler).embed([]) == []


async def test_embed_refuses_a_count_mismatch_loudly():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    with pytest.raises(EmbeddingUnavailable, match="count mismatch"):
        await embedder(handler).embed(["one", "two"])


async def test_embed_maps_transport_failure_to_its_own_taxonomy():
    with pytest.raises(EmbeddingUnavailable):
        await embedder(lambda _: httpx.Response(500, text="boom")).embed(["x"])


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expiries: list[tuple[str, int, bool]] = []

    async def incrby(self, key: str, amount: int) -> int:
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self.expiries.append((key, seconds, nx))
        return True


async def test_redis_store_admits_until_the_budget_is_reached():
    redis = FakeRedis()
    store = RedisBudgetStore(redis)
    assert await store.consume("k", 60, budget=100, window_s=30) is True
    assert await store.consume("k", 60, budget=100, window_s=30) is False


async def test_redis_store_always_sets_a_ttl_with_nx():
    """Every key gets a TTL (NFR-13), and NX rather than a first-increment
    test — the latter loses the TTL entirely if the process dies between
    the two commands."""
    redis = FakeRedis()
    await RedisBudgetStore(redis).consume("k", 1, budget=10, window_s=45)
    assert redis.expiries == [("k", 45, True)]


def response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body)


async def test_sse_reader_skips_everything_that_is_not_a_json_object():
    body = (
        b": keep-alive comment\n\n"
        b"event: named\n"
        b'data: {"type":"kept"}\n\n'
        b"data:\n\n"
        b"data: [DONE]\n\n"
        b"data: not-json\n\n"
        b'data: ["a","list"]\n\n'
        b'data: {"type":"also-kept"}\n\n'
    )
    frames = [frame async for frame in sse_payloads(response(body))]
    assert [frame["type"] for frame in frames] == ["kept", "also-kept"]
