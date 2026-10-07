"""`POST /v1/internal/assistant/retrieve` (FR-62, FR-65).

The endpoint's whole job is to be a boring, recognisable contract for
Catalog's `HybridSearch` adapter: ranked ids in, ranked ids out, a 503 when
retrieval cannot run. The adapter's fallback to lexical search depends on
that last part being an error it can *recognise* — a 500 would be a bug on
our side that reads, to the caller, exactly like a bug on theirs.
"""

from ai_assistant.domain.ports import EmbeddingUnavailable, Retrieved
from ai_assistant.domain.retrieval import Candidate
from ai_assistant.main import create_app
from fastapi.testclient import TestClient

from .conftest import FakeBudgetStore, FakeLlm, settings

SYSTEM = {"X-Auth-Sub": "svc:catalog", "X-Auth-Roles": "system"}
RETRIEVE = "/v1/internal/assistant/retrieve"


class FakeRetriever:
    def __init__(self, result=None, raises: Exception | None = None):
        self.result = result or Retrieved(items=(), restaurants=())
        self.raises = raises
        self.calls: list[dict] = []

    async def retrieve(self, *, query, filters, limit):
        self.calls.append({"query": query, "filters": filters, "limit": limit})
        if self.raises:
            raise self.raises
        return self.result


def _client(retriever: FakeRetriever) -> TestClient:
    app = create_app(
        settings(), providers={"anthropic": FakeLlm()}, budget_store=FakeBudgetStore(), runners=[]
    )
    app.state.retriever = retriever
    return TestClient(app)


def test_ranked_ids_come_back_split_by_shape():
    """Ids and scores, never cards: names and prices are Catalog's to give,
    and resolving them there is what keeps a stale indexed price
    structurally unable to reach a customer through search."""
    retriever = FakeRetriever(
        Retrieved(
            items=[Candidate("r1:i1", "r1", "i1", 0.03)],
            restaurants=[Candidate("r1:_self", "r1", None, 0.01)],
        )
    )
    with _client(retriever) as client:
        body = client.post(
            RETRIEVE, json={"query": "biryani", "city": "springfield"}, headers=SYSTEM
        ).json()
    assert body["items"] == [
        {"chunk_id": "r1:i1", "restaurant_id": "r1", "item_id": "i1", "score": 0.03}
    ]
    assert body["restaurants"][0]["item_id"] is None
    assert "name" not in body["items"][0] and "price_cents" not in body["items"][0]


def test_the_filters_reach_the_retriever():
    retriever = FakeRetriever()
    with _client(retriever) as client:
        client.post(
            RETRIEVE,
            json={
                "query": "something light",
                "city": "springfield",
                "max_price_cents": 1000,
                "tags": ["spicy"],
                "cuisines": ["thai"],
                "limit": 3,
            },
            headers=SYSTEM,
        )
    call = retriever.calls[0]
    assert call["limit"] == 3
    assert call["filters"].max_price_cents == 1000
    assert call["filters"].tags == ["spicy"]
    assert call["filters"].cuisines == ["thai"]


def test_a_city_is_required():
    """Every query is geo-scoped (FR-63). An unscoped retrieval would offer
    dishes from a city the customer cannot order from."""
    with _client(FakeRetriever()) as client:
        assert client.post(RETRIEVE, json={"query": "x"}, headers=SYSTEM).status_code == 422


def test_an_embedding_outage_is_a_503_the_caller_can_act_on():
    """Catalog's adapter falls back to lexical search on a recognisable
    failure (FR-65). A 500 would be indistinguishable from its own bug."""
    retriever = FakeRetriever(raises=EmbeddingUnavailable("provider down"))
    with _client(retriever) as client:
        response = client.post(RETRIEVE, json={"query": "x", "city": "springfield"}, headers=SYSTEM)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_retrieval_is_system_only():
    """Reachable inside the mesh and never through the edge — the gateway's
    allowlist does not route `/v1/internal/*`."""
    with _client(FakeRetriever()) as client:
        assert client.post(RETRIEVE, json={"query": "x", "city": "c"}).status_code == 401
        customer = {"X-Auth-Sub": "usr_1", "X-Auth-Roles": "customer"}
        assert (
            client.post(RETRIEVE, json={"query": "x", "city": "c"}, headers=customer).status_code
            == 403
        )


def test_an_empty_query_is_refused_rather_than_embedded():
    with _client(FakeRetriever()) as client:
        assert (
            client.post(RETRIEVE, json={"query": "", "city": "c"}, headers=SYSTEM).status_code
            == 422
        )


def test_the_limit_is_bounded():
    """A caller asking for 10,000 candidates is asking the fusion to sort a
    corpus, not to search it."""
    with _client(FakeRetriever()) as client:
        assert (
            client.post(
                RETRIEVE, json={"query": "x", "city": "c", "limit": 10_000}, headers=SYSTEM
            ).status_code
            == 422
        )


def test_paused_restaurants_are_excluded_by_default():
    """FR-63: the assistant must not recommend a kitchen that is closed."""
    retriever = FakeRetriever()
    with _client(retriever) as client:
        client.post(RETRIEVE, json={"query": "x", "city": "c"}, headers=SYSTEM)
    assert retriever.calls[0]["filters"].open_only is True


def test_a_caller_can_ask_for_paused_restaurants_too():
    """Catalog's `/v1/search` is a different contract from a chat answer: its
    card carries `status`, so the client badges a closed restaurant and still
    lets a customer browse. Hard-coding the strict rule made a config flag
    silently change what search returned."""
    retriever = FakeRetriever()
    with _client(retriever) as client:
        client.post(RETRIEVE, json={"query": "x", "city": "c", "open_only": False}, headers=SYSTEM)
    assert retriever.calls[0]["filters"].open_only is False
