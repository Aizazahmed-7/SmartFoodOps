"""The turn graph (ADR-0031, ADR-0043, FR-67/70/71/72).

Every branch is driven by three scripted callables — no app, no network, no
key. That is the property ADR-0031 required of this module, and it is what
makes the short circuits cheap to prove: the strongest assertion in this
file is that the model was never CALLED.
"""

import pytest
from ai_assistant.domain.answers import Cached, Fence
from ai_assistant.domain.graph import COLD_START, NO_MATCH, SYSTEM, TurnState, build_turn
from ai_assistant.domain.policy import FENCE, REFUSAL
from ai_assistant.domain.ports import Message, TokenChunk
from ai_assistant.domain.retrieval import Hydrated, Passage
from ai_assistant.domain.router import Task

KARAHI = Passage("itm_karahi", "rst_biryani_house", "Chicken Karahi\nWok-cooked with tomatoes.")
RAITA = Passage("itm_raita", "rst_karachi_grill", "Raita\nCool yogurt with cucumber.")


class Model:
    """A scripted router. Records what it was asked, so a test can assert it
    was never asked at all."""

    def __init__(self, *answers: str):
        self.answers = list(answers) or ["Try the [item:itm_karahi]."]
        self.calls: list[tuple[Task, list]] = []

    def stream(self, task, messages):
        self.calls.append((task, list(messages)))
        text = self.answers.pop(0) if self.answers else ""

        async def frames():
            for word in text.split(" "):
                yield TokenChunk(text=word + " ")
            yield TokenChunk(done=True, finish_reason="stop")

        return frames()


def _turn(model: Model | None = None, candidates=None, emitted=None, **kwargs):
    model = model or Model()
    found = [KARAHI] if candidates is None else candidates
    sink = emitted if emitted is not None else []

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=list(found), query_vector=[0.1, 0.2, 0.3])

    async def emit(text: str) -> None:
        sink.append(text)

    return build_turn(retrieve=retrieve, stream=model.stream, emit=emit, **kwargs), model, sink


async def _run(graph, question="what's good?", city="springfield", history=()):
    return await graph.ainvoke(TurnState(question=question, city=city, history=history))


# ── the happy path ──────────────────────────────────────────────────


async def test_a_question_becomes_a_grounded_answer():
    graph, model, emitted = _turn(Model("Try the [item:itm_karahi] tonight."))
    state = await _run(graph)
    assert state["answer"] == "Try the tonight."
    assert state["item_ids"] == ["itm_karahi"]
    assert state["dropped"] == 0
    assert "".join(emitted).strip() == "Try the [item:itm_karahi] tonight."


async def test_tokens_are_emitted_as_they_arrive():
    """The sink is fed during generation, not after it — that is what makes
    a streamed answer streamed rather than a delayed one."""
    graph, _, emitted = _turn(Model("one two three"))
    await _run(graph)
    assert len(emitted) == 3


async def test_the_model_is_asked_for_a_generation_never_a_model_id():
    graph, model, _ = _turn()
    await _run(graph)
    assert model.calls[0][0] is Task.GENERATE


# ── the short circuits: not asking is the cheapest safety ────────────


async def test_a_safety_question_never_reaches_the_model():
    """THE assertion. Not "the model refused" — the model was never called,
    so no prompt engineering can change the outcome (ADR-0043 §5)."""
    graph, model, emitted = _turn()
    state = await _run(graph, question="I'm allergic to peanuts, is this safe?")
    assert state["answer"] == REFUSAL
    assert state["refusal_reason"] == "allergen"
    assert model.calls == []
    assert emitted == []


async def test_an_empty_retrieval_is_answered_without_a_model():
    """An empty candidate set is the most dangerous prompt there is: a food
    question, nothing to work with, and a reward for being helpful (UC-18)."""
    graph, model, _ = _turn(candidates=[])
    state = await _run(graph)
    assert state["answer"] == NO_MATCH
    assert model.calls == []
    assert state["item_ids"] == []


async def test_a_refusal_does_not_retrieve_either():
    called = []

    async def retrieve(question: str, filters: object) -> Hydrated:
        called.append(question)
        return Hydrated(passages=[KARAHI])

    async def emit(_: str) -> None:  # pragma: no cover — never reached
        raise AssertionError("a refused turn must not emit")

    model = Model()
    graph = build_turn(retrieve=retrieve, stream=model.stream, emit=emit)
    await graph.ainvoke(TurnState(question="I have coeliac disease, safe?", city="c"))
    assert called == []


# ── what the model is told ──────────────────────────────────────────


async def test_retrieved_text_reaches_the_model_fenced_as_data():
    """It arrives by RELEVANCE — whatever best matched — which is exactly
    the channel an attacker would use (ADR-0043 §4)."""
    graph, model, _ = _turn(candidates=[KARAHI, RAITA])
    await _run(graph)
    prompt = model.calls[0][1][-1].content
    assert prompt.count(FENCE) == 2
    assert "[item:itm_karahi]" in prompt and "[item:itm_raita]" in prompt


async def test_the_system_prompt_forbids_prices_and_safety_claims():
    graph, model, _ = _turn()
    await _run(graph)
    system = model.calls[0][1][0]
    assert system.role == "system" and system.content == SYSTEM
    assert "Never state a price" in system.content
    assert "not a guarantee" in system.content


async def test_history_is_carried_into_the_prompt():
    from ai_assistant.domain.ports import Message

    graph, model, _ = _turn()
    prior = [Message(role="user", content="something spicy?")]
    await _run(graph, history=prior)
    roles = [m.role for m in model.calls[0][1]]
    assert roles == ["system", "user", "user"]


async def test_the_candidate_list_is_capped():
    """A prompt is bounded; a retriever is not. Sending everything it found
    is a context-cap violation waiting for a broad query (ADR-0030 §5)."""
    many = [Passage(f"itm_{i}", "rst_1", f"Dish {i}") for i in range(20)]
    graph, model, _ = _turn(candidates=many, limit=3)
    state = await _run(graph)
    assert len(state["candidates"]) == 3
    assert model.calls[0][1][-1].content.count("[item:") == 3


# ── grounding, at the graph's edge ──────────────────────────────────


async def test_an_invented_citation_is_dropped_from_the_answer():
    graph, _, _ = _turn(Model("Try [item:itm_karahi] or [item:itm_pizza]."))
    state = await _run(graph)
    assert state["item_ids"] == ["itm_karahi"]
    assert state["dropped"] == 1
    assert "itm_pizza" not in state["answer"]


async def test_an_answer_citing_nothing_still_ships():
    """A model that ignores the format produces a less useful answer, not a
    failed turn."""
    graph, _, _ = _turn(Model("I'd go for something light."))
    state = await _run(graph)
    assert state["answer"] == "I'd go for something light."
    assert state["item_ids"] == [] and state["dropped"] == 0


async def test_a_model_that_says_nothing_yields_an_empty_answer_not_a_crash():
    graph, _, _ = _turn(Model(""))
    state = await _run(graph)
    assert state["answer"] == "" and state["dropped"] == 0


# ── failure ─────────────────────────────────────────────────────────


async def test_a_provider_failure_propagates_rather_than_inventing():
    """The turn has nothing truthful to say when generation fails, so it
    fails — the caller turns that into a 503 and the panel degrades."""

    class Broken(Model):
        def stream(self, task, messages):
            self.calls.append((task, list(messages)))

            async def frames():
                raise RuntimeError("provider down")
                yield  # pragma: no cover — unreachable, makes this a generator

            return frames()

    graph, _, _ = _turn(Broken())
    with pytest.raises(RuntimeError, match="provider down"):
        await _run(graph)


# ── the cache tiers in the graph (FR-74) ────────────────────────────

CACHED = Cached(answer="Raita, from earlier.", item_ids=["itm_raita"], restaurant_ids=["rst_1"])


def _cached_turn(*, exact=None, semantic=None, remember=None, candidates=None):
    model = Model()
    found = [KARAHI] if candidates is None else candidates
    asked: list[str] = []
    sink: list[str] = []

    async def retrieve(question: str, filters: object) -> Hydrated:
        asked.append(question)
        return Hydrated(passages=list(found), query_vector=[0.1, 0.2, 0.3])

    async def emit(text: str) -> None:
        sink.append(text)

    graph = build_turn(
        retrieve=retrieve,
        stream=model.stream,
        emit=emit,
        exact=exact,
        semantic=semantic,
        remember=remember,
    )
    return graph, model, asked, sink


READ_FENCE = Fence("springfield", 3)


async def _hit(*_args):
    """The exact tier hands back (hit, fence); the semantic tier just a hit.
    Both shapes are covered because `_served` ignores the extra."""
    return CACHED


async def _exact_hit(*_args):
    return CACHED, READ_FENCE


async def _no_hit(*_args):
    return None


async def _exact_miss(*_args):
    return None, READ_FENCE


async def test_an_exact_hit_costs_nothing_at_all():
    """No retrieval, no embedding, no provider call — the whole point of
    putting this tier before everything else."""
    graph, model, asked, sink = _cached_turn(exact=_exact_hit)
    state = await _run(graph)
    assert state["answer"] == "Raita, from earlier."
    assert state["cache_tier"] == "exact"
    assert state["item_ids"] == ["itm_raita"] and state["restaurant_ids"] == ["rst_1"]
    assert asked == [] and model.calls == []


async def test_a_semantic_hit_still_pays_retrieval_but_not_the_model():
    """It runs AFTER retrieval on purpose: the query vector is retrieval's
    own output, so asking costs one indexed lookup rather than a second
    embedding call per turn."""
    graph, model, asked, _ = _cached_turn(exact=_exact_miss, semantic=_hit)
    state = await _run(graph)
    assert state["cache_tier"] == "semantic"
    assert asked == ["what's good?"]  # retrieval happened
    assert model.calls == []  # generation did not


async def test_both_tiers_missing_falls_through_to_a_generation():
    graph, model, asked, _ = _cached_turn(exact=_exact_miss, semantic=_no_hit)
    state = await _run(graph)
    assert state["cache_tier"] == ""
    assert asked and model.calls


async def test_a_cached_answer_is_still_an_answer_not_a_stop():
    """`stopped` stays empty, so "questions answered" does not fall every
    time the cache gets better."""
    graph, *_ = _cached_turn(exact=_exact_hit)
    assert (await _run(graph))["stopped"] == ""


async def test_a_reply_never_consults_the_cache():
    """A turn with history is a reply: "what about something spicier?" means
    nothing on its own, and a text-keyed hit would hand one conversation's
    context to another."""
    consulted: list[str] = []

    async def spy(question: str, city: str):
        consulted.append(question)
        return CACHED, READ_FENCE

    graph, *_ = _cached_turn(exact=spy, semantic=spy)
    state = await _run(graph, history=(Message(role="user", content="something light"),))
    assert consulted == [] and state["cache_tier"] == ""


async def test_a_generated_answer_is_written_back_with_its_citations():
    written: list[tuple] = []

    async def remember(fence, question, vector, cached):
        written.append((question, fence, list(vector), cached))

    graph, *_ = _cached_turn(exact=_exact_miss, semantic=_no_hit, remember=remember)
    await _run(graph)
    (question, fence, vector, cached) = written[0]
    # The fence the EXACT lookup resolved, before retrieval — not one
    # resolved after a two-second generation, by which time the drain may
    # have moved the corpus on (B3 review).
    assert question == "what's good?" and fence == READ_FENCE
    assert vector == [0.1, 0.2, 0.3]  # the one retrieval computed, not a second
    assert cached.item_ids == ["itm_karahi"]


async def test_a_reply_is_not_written_back_either():
    written: list[tuple] = []

    async def remember(*args):
        written.append(args)

    graph, *_ = _cached_turn(exact=_exact_miss, semantic=_no_hit, remember=remember)
    await _run(graph, history=(Message(role="user", content="something light"),))
    assert written == []


async def test_an_empty_retrieval_is_not_written_back():
    """A no-match never reaches `ground`, so there is nothing to remember —
    and pinning "nothing matched" would outlive the restaurant that opens
    tomorrow."""
    written: list[tuple] = []

    async def remember(*args):
        written.append(args)

    graph, *_ = _cached_turn(exact=_exact_miss, semantic=_no_hit, remember=remember, candidates=[])
    state = await _run(graph)
    assert state["stopped"] == "no_match" and written == []


async def test_a_turn_with_no_cache_configured_behaves_as_before():
    """`answer_cache=off` must mean exactly what the turn did before FR-74,
    not a lookup that always misses."""
    graph, model, asked, _ = _cached_turn()
    state = await _run(graph)
    assert state["cache_tier"] == "" and asked and model.calls


# ── cold start (FR-80) ──────────────────────────────────────────────


POPULAR = [
    Passage("itm_biryani", "rst_1", "Chicken Biryani"),
    Passage("itm_karahi", "rst_2", "Chicken Karahi"),
]


def _cold_turn(popular=None):
    model = Model()
    sink: list[str] = []

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=[], query_vector=[0.1])

    async def emit(text: str) -> None:
        sink.append(text)

    async def fallback(city: str) -> list[Passage]:
        return list(POPULAR if popular is None else popular)

    graph = build_turn(
        retrieve=retrieve,
        stream=model.stream,
        emit=emit,
        fallback=None if popular == "none" else fallback,
    )
    return graph, model


async def test_an_empty_retrieval_answers_with_what_the_city_is_ordering():
    """FR-80: never an empty response. An apology with nothing under it is
    an empty response with manners."""
    graph, model = _cold_turn()
    state = await _run(graph, question="do you sell laptops?")
    assert state["answer"] == COLD_START
    assert state["item_ids"] == ["itm_biryani", "itm_karahi"]
    assert state["restaurant_ids"] == ["rst_1", "rst_2"]
    assert state["stopped"] == "cold_start"


async def test_the_fallback_is_answered_without_a_model():
    """The dishes are real rows from our own order history — there is
    nothing for a model to add except the chance to invent a fourth one
    (UC-18, ADR-0043)."""
    graph, model = _cold_turn()
    await _run(graph, question="do you sell laptops?")
    assert model.calls == []


async def test_a_city_with_no_orders_falls_through_to_the_apology():
    """Popularity is a fallback, not a guarantee — a brand-new city has no
    history, and inventing one would be worse than saying so."""
    graph, _ = _cold_turn(popular=[])
    state = await _run(graph, question="do you sell laptops?")
    assert state["answer"] == NO_MATCH and state["stopped"] == "no_match"
    assert state["item_ids"] == []


async def test_without_a_fallback_configured_nothing_changes():
    graph, _ = _cold_turn(popular="none")
    state = await _run(graph, question="do you sell laptops?")
    assert state["answer"] == NO_MATCH and state["stopped"] == "no_match"


async def test_a_cold_start_is_not_cached():
    """`stopped` is set, so `cacheable` refuses it — pinning "nothing
    matched, try these" would outlive both the miss and the popularity."""
    written: list[tuple] = []

    async def remember(*args):
        written.append(args)

    model = Model()

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=[], query_vector=[0.1])

    async def emit(text: str) -> None:
        return None

    async def fallback(city: str) -> list[Passage]:
        return list(POPULAR)

    graph = build_turn(
        retrieve=retrieve, stream=model.stream, emit=emit, remember=remember, fallback=fallback
    )
    await _run(graph, question="do you sell laptops?")
    assert written == []


# ── order assistance in the graph (FR-78) ───────────────────────────


async def test_a_pairing_question_brings_in_what_people_order_together():
    """Retrieval finds dishes that MATCH the words. "What goes with the
    biryani" needs dishes that go WITH it — which similarity cannot find,
    because naan and biryani are not alike."""
    asked: list[tuple[list[str], str]] = []

    async def goes_with(item_ids, city):
        asked.append((list(item_ids), city))
        return [Passage("itm_naan", "rst_1", "Garlic Naan")]

    model = Model()

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=[KARAHI], query_vector=[0.1])

    async def emit(text: str) -> None:
        return None

    graph = build_turn(retrieve=retrieve, stream=model.stream, emit=emit, goes_with=goes_with)
    await _run(graph, question="what goes with the karahi?")
    assert asked == [(["itm_karahi"], "springfield")]
    # The partner reached the model as a candidate it can cite.
    (_, messages) = model.calls[0]
    assert "Garlic Naan" in messages[-1].content


async def test_an_ordinary_question_does_not_pull_in_pairings():
    """A dilution guard: every extra candidate is prompt budget spent on a
    dish the customer did not ask about."""
    asked: list[tuple] = []

    async def goes_with(item_ids, city):
        asked.append((item_ids, city))
        return []

    model = Model()

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=[KARAHI], query_vector=[0.1])

    async def emit(text: str) -> None:
        return None

    graph = build_turn(retrieve=retrieve, stream=model.stream, emit=emit, goes_with=goes_with)
    await _run(graph, question="what is in the karahi?")
    assert asked == []


async def test_a_partner_already_retrieved_is_not_duplicated():
    async def goes_with(item_ids, city):
        return [KARAHI]  # already in the candidate list

    model = Model()

    async def retrieve(question: str, filters: object) -> Hydrated:
        return Hydrated(passages=[KARAHI], query_vector=[0.1])

    async def emit(text: str) -> None:
        return None

    graph = build_turn(retrieve=retrieve, stream=model.stream, emit=emit, goes_with=goes_with)
    state = await _run(graph, question="what goes with it?")
    assert [p.item_id for p in state["candidates"]] == ["itm_karahi"]


async def test_the_system_prompt_requires_declared_tags_not_inference():
    """FR-78's constraint. A customer asking whether something is spicy is
    asking what the restaurant said, not what a model would guess — the same
    posture ADR-0043 takes about dietary tags."""
    assert "DECLARED tags" in SYSTEM
    assert "not listed" in SYSTEM and "inferring" in SYSTEM
