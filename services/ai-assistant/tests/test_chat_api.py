"""The chat surface (FR-67, FR-68, FR-69).

Two endpoints with two trust models, and the properties worth proving are
the ones a happy-path click never exercises: a RETRY streams the turn that
is already running rather than paying for a second one, and a RECONNECT
resumes from `Last-Event-ID` with no gap and no duplicate.

`stream_relay`'s own ordering contract is proved in
libs/smartfood-realtime/tests/test_stream_relay.py. What is proved here is
the wiring around it — who may open a stream, what the cursor is read from,
and what a reader sees when the turn finished while they were away.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from ai_assistant.adapters.conversations import COMPLETE, STREAMING, ConversationRepo
from ai_assistant.db import metadata
from ai_assistant.main import create_app
from ai_assistant.turns import channel_for, frame
from fastapi.testclient import TestClient
from smartfood_auth import AuthContext, headers_for

from .conftest import FakeBudgetStore, FakeLlm, settings

CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
RIDER = headers_for(AuthContext(sub="usr_2", roles=frozenset({"rider"})))
ASK = "/v1/assistant/messages"
QUESTION = {"question": "something light?", "city": "springfield"}


class FakeRealtime:
    """Tickets in a dict, one `asyncio.Queue` per channel.

    `next_message` blocks for a tick and returns None on a quiet one, the
    way the Redis adapter's `get_message(timeout=1.0)` does — a fake that
    blocks forever makes the stream's own heartbeat unreachable, which is
    exactly the bug the tracking lane found live.
    """

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.buses: dict[str, asyncio.Queue[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def put_ticket(self, ticket: str, channel: str, sub: str, *, ttl_s: int) -> None:
        self.tickets[ticket] = {"channel": channel, "sub": sub, "ttl": ttl_s}

    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None:
        return self.tickets.pop(ticket, None)  # GETDEL: redemption is destruction

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))
        self.buses.setdefault(channel, asyncio.Queue()).put_nowait(data)

    @asynccontextmanager
    async def subscription(self, channel: str):
        queue = self.buses.setdefault(channel, asyncio.Queue())

        class Sub:
            async def next_message(self) -> str | None:
                try:
                    return await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    return None

        yield Sub()


SEEN_HISTORY: list[list[tuple[str, str]]] = []


class Graph:
    """A turn without a model: emits its words, returns its verdict."""

    def __init__(self, emit: Any, answer: str = "Try the [item:itm_raita] .") -> None:
        self._emit = emit
        self._answer = answer

    async def ainvoke(self, state: Any) -> dict[str, Any]:
        SEEN_HISTORY.append([(m.role, m.content) for m in state.history])
        for word in self._answer.split(" "):
            await self._emit(word + " ")
        return {"answer": self._answer, "item_ids": ["itm_raita"], "dropped": 0}


def make_app(realtime: FakeRealtime | None, **knobs: Any):
    answer = knobs.pop("answer", "Try the [item:itm_raita] .")
    return create_app(
        settings(
            stream_heartbeat_seconds=knobs.pop("hb", 0.02),
            # Short, because ASGITransport does not cancel the generator when
            # the client breaks early — a default lifetime would be waited out.
            stream_lifetime_seconds=knobs.pop("life", 0.4),
        ),
        providers={"anthropic": FakeLlm()},
        budget_store=FakeBudgetStore(),
        runners=[],
        realtime=realtime,
        graph_builder=lambda emit: Graph(emit, answer),
    )


async def _schema(app: Any) -> None:
    """ASGITransport never runs the lifespan, so the async tests build the
    schema themselves (idempotent; TestClient tests get it from `create_all`)."""
    async with app.state.sessions() as session:
        conn = await session.connection()
        await conn.run_sync(metadata.create_all)
        await session.commit()


async def _drain(app: Any) -> None:
    """Wait for the detached turn.

    A turn deliberately outlives its request, so every test that STARTS one
    also finishes it — otherwise the task writes into an engine the next
    test already disposed, and the failure lands on whichever test the
    random ordering ran next.
    """
    while app.state.chat._tasks:  # noqa: SLF001 — the handles live nowhere else
        await asyncio.wait(set(app.state.chat._tasks))  # noqa: SLF001


@asynccontextmanager
async def _asking(app: Any):
    """An HTTP client over the app, with the schema up and every turn it
    starts drained before it hands back."""
    await _schema(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client
    await _drain(app)


# ── starting a turn ─────────────────────────────────────────────────


async def test_a_question_is_accepted_before_the_answer_exists():
    """202, not 200: the body carries an id and a ticket, never an answer,
    and a 200 would make the status line a lie about what is in it."""
    app = make_app(FakeRealtime())
    async with _asking(app) as client:
        response = await client.post(ASK, json=QUESTION, headers=CUSTOMER)
    assert response.status_code == 202
    body = response.json()
    assert body["message_id"].startswith("msg_")
    assert body["conversation_id"].startswith("cnv_")
    assert body["stream"] == f"/sse/assistant/{body['message_id']}"
    assert body["ticket"]


def test_asking_requires_a_customer_or_a_partner():
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        assert client.post(ASK, json=QUESTION).status_code == 401
        assert client.post(ASK, json=QUESTION, headers=RIDER).status_code == 403


def test_a_question_needs_a_city():
    """Retrieval is geo-scoped (FR-63). A city-less turn would have to invent
    a scope or silently drop it, and both answer for the wrong town."""
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        assert client.post(ASK, json={"question": "x"}, headers=CUSTOMER).status_code == 422


def test_the_assistant_is_unavailable_without_a_bus():
    """No Redis means no ticket and no channel. That is a dependency being
    down, not a bug — 503 with Retry-After, never a 500."""
    app = make_app(None)
    with TestClient(app) as client:
        started = client.post(ASK, json=QUESTION, headers=CUSTOMER)
        assert started.status_code == 503 and started.headers["Retry-After"] == "30"
        assert client.get(f"{ASK}/msg_1?ticket=x").status_code == 503


# ── who may open a stream ───────────────────────────────────────────


async def test_a_ticket_is_single_use_and_channel_scoped():
    """Redemption is destruction, so a replayed ticket is not merely
    forbidden but structurally spent. A ticket for another message is burned
    too: a probe learns nothing and loses its ticket learning it — which is
    also what makes a TRACKING ticket useless here."""
    app = make_app(FakeRealtime())
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        other = f"{ASK}/msg_somebody_else?ticket={started['ticket']}"
        assert (await client.get(other)).status_code == 401  # mismatch burns it…
        mine = f"{ASK}/{started['message_id']}?ticket={started['ticket']}"
        assert (await client.get(mine)).status_code == 401  # …so the real one is spent
        assert (await client.get(f"{ASK}/msg_1?ticket=never-issued")).status_code == 401


def test_a_streamed_route_is_excluded_from_the_latency_histogram():
    """A held connection's LIFETIME is not a latency. Without the prefix one
    slow reader pins the service p95 at the top bucket."""
    from ai_assistant.api.chat import STREAM_PREFIX

    app = make_app(FakeRealtime())
    registered = [m for m in app.user_middleware if "RequestContext" in str(m.cls)]
    assert STREAM_PREFIX in str(registered[0].kwargs["stream_prefixes"])


# ── following an answer ─────────────────────────────────────────────


async def _stream(app: Any, path: str, *, headers: dict[str, str] | None = None, deadline=5.0):
    transport = httpx.ASGITransport(app=app)
    lines: list[str] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("GET", path, headers=headers or {}) as response:
            if response.status_code != 200:
                return response.status_code, []
            async with asyncio.timeout(deadline):
                async for line in response.aiter_lines():
                    lines.append(line)
    return 200, lines


def _frames(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(ln[6:]) for ln in lines if ln.startswith("data: {")]


def _texts(lines: list[str]) -> list[str]:
    return [f["text"] for f in _frames(lines)]


async def test_a_finished_answer_replays_from_the_table_and_closes():
    """ADR-0042 §5: reconnecting after completion re-runs nothing. The
    snapshot IS the answer, and the stream ends rather than listening to a
    channel nobody will ever publish on again."""
    fake = FakeRealtime()
    app = make_app(fake)
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()

    await fake.put_ticket("tkt", channel_for(started["message_id"]), "usr_1", ttl_s=60)
    status, lines = await _stream(app, f"{ASK}/{started['message_id']}?ticket=tkt")

    assert status == 200
    # The marker never reaches the reader — neither live nor on replay, and
    # the citation rides the terminal frame instead.
    assert "".join(_texts(lines)) == "Try the . "
    # Four ids: three chunks of prose, then the terminal frame the snapshot
    # appended — the live reader's copy of it was published while this
    # reader was away, and nothing republishes it.
    assert [ln for ln in lines if ln.startswith("id: ")] == ["id: 1", "id: 2", "id: 3", "id: 4"]
    assert _frames(lines)[-1] == {"seq": 4, "text": "", "done": True, "item_ids": ["itm_raita"]}
    # Reaching here proves the generator RETURNED rather than waiting out
    # its lifetime on a channel that is finished.


async def test_a_reconnect_resumes_from_the_last_event_id():
    """FR-69. The browser resends the header unprompted, so it is the only
    cursor there is — and everything at or below it is already on screen."""
    fake = FakeRealtime()
    app = make_app(fake)
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()

    await fake.put_ticket("tkt", channel_for(started["message_id"]), "usr_1", ttl_s=60)
    status, lines = await _stream(
        app, f"{ASK}/{started['message_id']}?ticket=tkt", headers={"Last-Event-ID": "2"}
    )
    assert status == 200
    assert [ln for ln in lines if ln.startswith("id: ")] == ["id: 3", "id: 4"]


async def test_a_garbled_cursor_replays_the_whole_answer():
    """A reader holding a corrupt cursor should see the answer again, not an
    error: 400 here would strand a reconnect that the browser retries
    forever (ADR-0042 §4)."""
    fake = FakeRealtime()
    app = make_app(fake)
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()

    for cursor in ("not-a-number", "-5"):
        await fake.put_ticket("tkt", channel_for(started["message_id"]), "usr_1", ttl_s=60)
        _, lines = await _stream(
            app, f"{ASK}/{started['message_id']}?ticket=tkt", headers={"Last-Event-ID": cursor}
        )
        assert [ln for ln in lines if ln.startswith("id: ")] == [
            "id: 1",
            "id: 2",
            "id: 3",
            "id: 4",
        ]


async def test_a_live_turn_relays_chunks_and_closes_on_the_terminal_frame():
    """The in-flight case: nothing written yet, tokens arriving on the bus.
    The reader gets them in order and the stream ends on `done` rather than
    idling to its lifetime."""
    fake = FakeRealtime()
    app = make_app(fake)
    await _schema(app)
    async with app.state.sessions() as session:
        repo = ConversationRepo(session)
        now = datetime.now(UTC)
        await repo.ensure_conversation(
            conversation_id="cnv_live", user_id="usr_1", city="springfield", now=now
        )
        await repo.start_message(
            message_id="msg_live",
            conversation_id="cnv_live",
            role="assistant",
            content="",
            status=STREAMING,
            now=now,
        )
        await session.commit()

    channel = channel_for("msg_live")
    await fake.put_ticket("tkt", channel, "usr_1", ttl_s=60)
    await fake.publish(channel, frame(seq=1, text="Raita "))
    await fake.publish(channel, frame(seq=1, text="Raita "))  # duplicate: dropped by seq
    await fake.publish(channel, frame(seq=2, text="is light."))
    await fake.publish(channel, frame(seq=3, done=True))

    status, lines = await _stream(app, f"{ASK}/msg_live?ticket=tkt")
    assert status == 200
    # The terminal frame is relayed too — it carries no text, and it is how
    # the client learns the answer is finished rather than merely paused.
    assert _texts(lines) == ["Raita ", "is light.", ""]
    assert _frames(lines)[-1]["done"] is True


async def test_a_quiet_stream_heartbeats_and_then_reconnects():
    """A wedged generation must still put bytes on the wire — behind a
    60s-idle proxy a silent stream is a dead one — and must hand back a
    `reconnect` rather than EOFing when its lifetime runs out."""
    fake = FakeRealtime()
    app = make_app(fake, hb=0.02, life=0.2)
    await _schema(app)
    async with app.state.sessions() as session:
        repo = ConversationRepo(session)
        now = datetime.now(UTC)
        await repo.ensure_conversation(
            conversation_id="cnv_q", user_id="usr_1", city="springfield", now=now
        )
        await repo.start_message(
            message_id="msg_quiet",
            conversation_id="cnv_q",
            role="assistant",
            content="",
            status=STREAMING,
            now=now,
        )
        await session.commit()

    await fake.put_ticket("tkt", channel_for("msg_quiet"), "usr_1", ttl_s=60)
    status, lines = await _stream(app, f"{ASK}/msg_quiet?ticket=tkt")
    assert status == 200
    assert ": hb" in lines  # an SSE comment — invisible to EventSource handlers
    assert "event: reconnect" in lines and "data: lifetime" in lines


async def test_a_turn_records_its_answer_on_the_message_row():
    """The chunks are transient; the assembled answer is what a reloaded
    conversation reads (ADR-0042 §6)."""
    app = make_app(FakeRealtime())
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
    async with app.state.sessions() as session:
        stored = await ConversationRepo(session).message(started["message_id"])
    assert stored is not None
    assert stored.status == COMPLETE and "itm_raita" in stored.content


async def test_a_reader_can_buy_a_fresh_ticket_for_a_stream_in_flight():
    """FR-69 in a browser. `EventSource` reconnects to the same URL with the
    same spent ticket, so without a re-ticket every reconnect after the first
    is a 401 the retry loop never escapes."""
    app = make_app(FakeRealtime())
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        again = await client.post(f"{ASK}/{started['message_id']}/ticket", headers=CUSTOMER)
    assert again.status_code == 201
    body = again.json()
    assert body["ticket"] != started["ticket"]
    assert body["stream"] == f"/sse/assistant/{started['message_id']}"
    assert body["expires_in"] == 60


async def test_a_ticket_for_somebody_elses_conversation_is_a_404():
    """Not-yours and not-found answer identically — a distinguishable 403
    would turn this into an oracle for which message ids exist."""
    app = make_app(FakeRealtime())
    stranger = headers_for(AuthContext(sub="usr_9", roles=frozenset({"customer"})))
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        theirs = await client.post(f"{ASK}/{started['message_id']}/ticket", headers=stranger)
        missing = await client.post(f"{ASK}/msg_nope/ticket", headers=CUSTOMER)
    assert theirs.status_code == 404 and missing.status_code == 404


def test_a_reticket_needs_a_bus_too():
    app = make_app(None)
    with TestClient(app) as client:
        assert client.post(f"{ASK}/msg_1/ticket", headers=CUSTOMER).status_code == 503


async def test_history_is_what_came_before_not_this_turn():
    """Found live: the user row is inserted before history is read, so
    without the filter every turn — the FIRST one included — arrives at the
    graph carrying its own question. That sends the question to the model
    twice and makes every turn look like a reply, which disables the answer
    cache outright (ADR-0045)."""
    SEEN_HISTORY.clear()
    app = make_app(FakeRealtime())
    async with _asking(app) as client:
        first = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        await _drain(app)
        await client.post(
            ASK,
            json={
                "question": "and something spicier?",
                "city": "springfield",
                "conversation_id": first["conversation_id"],
            },
            headers=CUSTOMER,
        )
    assert SEEN_HISTORY[0] == []
    assert SEEN_HISTORY[1] == [
        ("user", "something light?"),
        ("assistant", "Try the [item:itm_raita] ."),
    ]


class FakeCards:
    """The card service, as the route sees it."""

    def __init__(self, cards):
        self.cards = cards
        self.asked: list[tuple[str, str]] = []

    async def for_message(self, *, message_id: str, user_id: str):
        self.asked.append((message_id, user_id))
        return self.cards

    async def for_items(self, *, passages):
        self.asked.append(("items", str(len(passages))))
        return self.cards or []


async def test_the_cited_dishes_are_priced_at_read_time_not_answer_time():
    """FR-60. The prose is final the moment it is written; a price is not,
    so they are two reads — a reader who reconnects tomorrow gets
    yesterday's words and today's menu."""
    app = make_app(FakeRealtime())
    card = {
        "item_id": "itm_raita",
        "name": "Raita",
        "price_cents": 350,
        "min_total_cents": 350,
        "restaurant_id": "rst_1",
        "orderable": True,
    }
    app.state.cards = FakeCards([card])
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        response = await client.get(f"{ASK}/{started['message_id']}/items", headers=CUSTOMER)
    assert response.status_code == 200
    assert response.json() == {"items": [card]}
    assert app.state.cards.asked == [(started["message_id"], "usr_1")]


async def test_cards_for_a_message_that_is_not_yours_are_a_404():
    app = make_app(FakeRealtime())
    app.state.cards = FakeCards(None)  # the service's "not yours, or not found"
    async with _asking(app) as client:
        response = await client.get(f"{ASK}/msg_somebody_else/items", headers=CUSTOMER)
    assert response.status_code == 404


def test_reading_cards_requires_signing_in():
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        assert client.get(f"{ASK}/msg_1/items").status_code == 401


async def test_a_browser_resumes_with_a_query_parameter():
    """`EventSource` cannot set a header, and its own reconnect reuses the
    ticket it already spent — so the header path is unreachable from a
    browser that has to re-ticket. `?after=` is how it resumes."""
    fake = FakeRealtime()
    app = make_app(fake)
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
    await fake.put_ticket("tkt", channel_for(started["message_id"]), "usr_1", ttl_s=60)
    _, lines = await _stream(app, f"{ASK}/{started['message_id']}?ticket=tkt&after=2")
    assert [ln for ln in lines if ln.startswith("id: ")] == ["id: 3", "id: 4"]


async def test_the_header_wins_over_the_query_parameter():
    """The browser sets the header without being asked, so it is the one
    that cannot be stale — a URL can be bookmarked, retyped or shared."""
    fake = FakeRealtime()
    app = make_app(fake)
    async with _asking(app) as client:
        started = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
    await fake.put_ticket("tkt", channel_for(started["message_id"]), "usr_1", ttl_s=60)
    _, lines = await _stream(
        app,
        f"{ASK}/{started['message_id']}?ticket=tkt&after=0",
        headers={"Last-Event-ID": "3"},
    )
    assert [ln for ln in lines if ln.startswith("id: ")] == ["id: 4"]


async def test_a_turn_cannot_be_filed_into_somebody_elses_conversation():
    """The B3 review's worst finding. `conversation_id` is client-supplied,
    and without an ownership predicate a customer could attach a turn to a
    stranger's conversation — `repo.history()` would then feed that
    stranger's questions and answers into their prompt, and the idempotency
    path would hand them a live stream ticket for the stranger's answer."""
    app = make_app(FakeRealtime())
    stranger = headers_for(AuthContext(sub="usr_9", roles=frozenset({"customer"})))
    async with _asking(app) as client:
        mine = (await client.post(ASK, json=QUESTION, headers=CUSTOMER)).json()
        await _drain(app)
        stolen = await client.post(
            ASK,
            json={**QUESTION, "conversation_id": mine["conversation_id"]},
            headers=stranger,
        )
    # Not-yours and not-found are one shape: a distinguishable 403 would
    # turn the id into an oracle for which conversations exist.
    assert stolen.status_code == 404


async def test_an_idempotency_key_cannot_reach_another_customers_answer():
    """The same hole through its other door: replaying a victim's key
    returned the victim's message id, and `ask` minted a ticket for it."""
    app = make_app(FakeRealtime())
    stranger = headers_for(AuthContext(sub="usr_9", roles=frozenset({"customer"})))
    async with _asking(app) as client:
        mine = (
            await client.post(ASK, json=QUESTION, headers={**CUSTOMER, "Idempotency-Key": "k1"})
        ).json()
        await _drain(app)
        stolen = await client.post(
            ASK,
            json={**QUESTION, "conversation_id": mine["conversation_id"]},
            headers={**stranger, "Idempotency-Key": "k1"},
        )
    assert stolen.status_code == 404


class FakeRecommender:
    def __init__(self, passages, basis: str = "popular", pairs=(), paired=()):
        self.passages = passages
        self.basis = basis
        self.pairs = list(pairs)
        self.paired = list(paired)
        self.asked: list[tuple[str, str, int]] = []
        self.hydrated: list[list[str]] = []
        self.shown: list[tuple] = []

    async def for_user(self, *, user_id: str, city: str, limit: int):
        self.asked.append((user_id, city, limit))
        return self.basis, list(self.passages)

    async def pairs_in(self, *, city: str):
        return list(self.pairs)

    async def hydrate(self, pinned):
        self.hydrated.append(list(pinned))
        return list(self.paired)

    async def record_shown(self, *, user_id, city, surface, basis, item_ids):
        self.shown.append((user_id, city, surface, basis, list(item_ids)))
        return "rec_1"


async def test_a_customer_with_nothing_typed_still_gets_dishes():
    """FR-80's actual surface. A fallback hung off the no-match path would
    be correct and unreachable — vector search returns nearest neighbours
    even for gibberish, so an empty retrieval almost never happens."""
    from ai_assistant.domain.retrieval import Passage

    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender([Passage("itm_raita", "rst_1", "Raita")])
    card = {
        "item_id": "itm_raita",
        "name": "Raita",
        "price_cents": 350,
        "min_total_cents": 350,
        "restaurant_id": "rst_1",
        "orderable": True,
    }
    app.state.cards = FakeCards([card])
    async with _asking(app) as client:
        response = await client.get(
            "/v1/assistant/recommendations", params={"city": "springfield"}, headers=CUSTOMER
        )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [card] and body["city"] == "springfield"
    # `basis` is what makes FR-75's comparison observable: a personalised
    # list and the popularity baseline must be distinguishable by the caller.
    assert body["basis"] == "popular"
    assert app.state.recommender.asked == [("usr_1", "springfield", 5)]


async def test_a_city_with_no_history_returns_an_empty_list_not_an_error():
    """Honest rather than invented: a brand-new city has no orders, and
    making one up is worse than showing nothing."""
    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender([])
    app.state.cards = FakeCards([])
    async with _asking(app) as client:
        response = await client.get(
            "/v1/assistant/recommendations", params={"city": "nowhereville"}, headers=CUSTOMER
        )
    assert response.status_code == 200 and response.json()["items"] == []


def test_recommendations_require_signing_in():
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        assert client.get("/v1/assistant/recommendations?city=x").status_code == 401


def test_recommendations_need_a_city():
    """Geo-scoped like everything else (FR-63) — a city-less request would
    have to invent a scope."""
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        assert client.get("/v1/assistant/recommendations", headers=CUSTOMER).status_code == 422


async def test_a_personalised_list_says_so():
    """FR-75's acceptance criterion is a COMPARISON — a personalised list
    must differ measurably from the popularity baseline, and neither the
    caller nor the eval suite can check that without knowing which it got."""
    from ai_assistant.domain.retrieval import Passage

    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender([Passage("itm_x", "rst_1", "X")], basis="taste")
    app.state.cards = FakeCards(
        [
            {
                "item_id": "itm_x",
                "min_total_cents": 500,
                "restaurant_id": "rst_1",
                "orderable": True,
            }
        ]
    )
    async with _asking(app) as client:
        response = await client.get(
            "/v1/assistant/recommendations", params={"city": "springfield"}, headers=CUSTOMER
        )
    assert response.json()["basis"] == "taste"


async def test_a_budget_filters_on_the_live_floor_not_the_indexed_price():
    """FR-76's hard predicate. The index carries a price that was true when
    the menu was last chunked, and a dish with a required paid option cannot
    be bought for its base price either — so the filter runs after pricing,
    on `min_total_cents`."""
    from ai_assistant.domain.retrieval import Passage

    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender(
        [Passage("cheap", "rst_1", "C"), Passage("looks_cheap", "rst_1", "L")]
    )
    app.state.cards = FakeCards(
        [
            {
                "item_id": "cheap",
                "price_cents": 900,
                "min_total_cents": 900,
                "restaurant_id": "rst_1",
                "orderable": True,
            },
            # Looks affordable; its required Size starts at +400.
            {
                "item_id": "looks_cheap",
                "price_cents": 900,
                "min_total_cents": 1300,
                "restaurant_id": "rst_1",
                "orderable": True,
            },
        ]
    )
    async with _asking(app) as client:
        response = await client.get(
            "/v1/assistant/recommendations",
            params={"city": "islamabad", "budget_cents": 1000},
            headers=CUSTOMER,
        )
    body = response.json()
    assert [c["item_id"] for c in body["items"]] == ["cheap"]
    assert body["budget_cents"] == 1000


async def test_a_combo_is_priced_even_when_its_dishes_are_not_recommended():
    """Drawing combos from the recommended list made them arbitrary and
    usually empty — the most co-ordered pair is rarely two of one
    customer's five best matches. They are priced in the same catalog
    fan-out instead."""
    from ai_assistant.domain.retrieval import Passage

    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender(
        [Passage("a", "rst_1", "A")],
        pairs=[("rst_1", "b", "c", 7)],
        paired=[Passage("b", "rst_1", "B"), Passage("c", "rst_1", "C")],
    )
    app.state.cards = FakeCards(
        [
            {
                "item_id": "a",
                "price_cents": 500,
                "min_total_cents": 500,
                "restaurant_id": "rst_1",
                "orderable": True,
            },
            {
                "item_id": "b",
                "price_cents": 700,
                "min_total_cents": 700,
                "restaurant_id": "rst_1",
                "orderable": True,
            },
            {
                "item_id": "c",
                "price_cents": 400,
                "min_total_cents": 400,
                "restaurant_id": "rst_1",
                "orderable": True,
            },
        ]
    )
    async with _asking(app) as client:
        response = await client.get(
            "/v1/assistant/recommendations", params={"city": "islamabad"}, headers=CUSTOMER
        )
    body = response.json()
    # `b` and `c` were never recommended, but the combo is still priced —
    # and carries its own cards, or a client could render neither.
    (combo,) = body["combos"]
    assert [c["item_id"] for c in combo["items"]] == ["b", "c"]
    assert combo["total_cents"] == 1100 and combo["orders"] == 7
    assert all("price_cents" in c for c in combo["items"])
    assert [c["item_id"] for c in body["items"]] == ["a"]
    assert app.state.recommender.hydrated == [[("rst_1", "b"), ("rst_1", "c")]]


async def test_a_budget_over_fetches_so_the_list_is_not_short():
    """The filter runs on live prices, which are only known after pricing —
    asking for exactly `limit` dishes and then dropping the unaffordable
    ones returns a short list whenever the expensive ones rank highest."""
    from ai_assistant.domain.retrieval import Passage

    app = make_app(FakeRealtime())
    app.state.recommender = FakeRecommender([Passage("a", "rst_1", "A")])
    app.state.cards = FakeCards([])
    async with _asking(app) as client:
        await client.get(
            "/v1/assistant/recommendations",
            params={"city": "islamabad", "budget_cents": 1000},
            headers=CUSTOMER,
        )
        await client.get(
            "/v1/assistant/recommendations", params={"city": "islamabad"}, headers=CUSTOMER
        )
    budgeted, plain = app.state.recommender.asked
    assert budgeted[2] > plain[2]


def test_a_budget_must_be_a_positive_amount():
    app = make_app(FakeRealtime())
    with TestClient(app) as client:
        url = "/v1/assistant/recommendations?city=islamabad&budget_cents="
        assert client.get(url + "0", headers=CUSTOMER).status_code == 422
        assert client.get(url + "-500", headers=CUSTOMER).status_code == 422
