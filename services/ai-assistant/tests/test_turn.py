"""The turn graph (ADR-0031, ADR-0043, FR-67/70/71/72).

Every branch is driven by three scripted callables — no app, no network, no
key. That is the property ADR-0031 required of this module, and it is what
makes the short circuits cheap to prove: the strongest assertion in this
file is that the model was never CALLED.
"""

import pytest
from ai_assistant.domain.graph import NO_MATCH, SYSTEM, TurnState, build_turn
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


# ── cold start (FR-80) ──────────────────────────────────────────────


POPULAR = [
    Passage("itm_biryani", "rst_1", "Chicken Biryani"),
    Passage("itm_karahi", "rst_2", "Chicken Karahi"),
]


# ── order assistance in the graph (FR-78) ───────────────────────────


async def test_the_system_prompt_requires_declared_tags_not_inference():
    """FR-78's constraint. A customer asking whether something is spicy is
    asking what the restaurant said, not what a model would guess — the same
    posture ADR-0043 takes about dietary tags."""
    assert "DECLARED tags" in SYSTEM
    assert "not listed" in SYSTEM and "inferring" in SYSTEM
