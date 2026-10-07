"""One assistant turn, as a graph (ADR-0031, FR-67).

LangGraph owns the CONTROL FLOW and nothing else: which node runs, and
whether the turn stops early. Every piece of I/O goes through our own ports,
which is what keeps this module testable with no app, no network and no key
— a scripted fake retriever and a scripted fake router drive every branch.

The shape:

    guard ─────────(refuse)───────────────────────► END
      │
    exact_cache ───(hit)──────────────────────────► END
      │
    retrieve ──────(nothing matched)──────────────► END
      │
    semantic_cache (hit)──────────────────────────► END
      │
    generate ──► ground ──────────────────────────► END

Four short circuits, and every one of them exists because the cheapest way
to avoid a bad or expensive answer is not to ask for one. A safety question
never reaches a model (ADR-0043 §5); an empty retrieval is answered by a
fixed line rather than by inviting a model to fill a silence — exactly the
prompt under which one invents a dish (UC-18); and a question somebody
already asked is answered from what we told them (FR-74).

The two cache tiers sit where they do for one reason each. Exact match goes
FIRST because it needs nothing — no embedding, no query — so a hit collapses
the turn to a single round trip. Semantic match goes AFTER retrieval because
its input, the query vector, is retrieval's own output; in front of it, every
miss would embed the question a second time, and a cache that doubles the
embedding bill is not a cache (ADR-0045).
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (LangGraph ships no py.typed. ADR-0031 predicted this and set the rule:
# the framework is held behind OUR types, so `TurnRunner` below is the typed
# boundary and nothing past it leaks a partially-unknown generic into the
# rest of the service. Same treatment aiokafka gets in smartfood-kafka.)

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from langgraph.graph import END, START, StateGraph

from ..answers import EXACT, SEMANTIC, Cached, cacheable
from ..assistance import asks_for_pairing
from ..grounding import render_candidates, validate
from ..policy import REFUSAL, SafetyReason, as_data, safety_check, sanitize
from ..ports import Message, TokenChunk
from ..retrieval import Filters, Hydrated, Passage
from ..router import Task

NO_MATCH = (
    "I couldn't find anything on the menus near you that matches that. "
    "Try a different dish, or widen the search."
)
"""Said when retrieval came back empty AND nothing popular could stand in.

Kept as the floor rather than the answer: FR-80's rule is that a customer
never gets an empty response, and a fixed apology with nothing under it is
an empty response with manners.
"""

COLD_START = (
    "I couldn't find a match for that, but here's what people near you are ordering at the moment."
)
"""FR-80. An empty retrieval used to end the turn with an apology and no
cards; now it falls back to what the city is actually ordering around this
hour.

Still answered WITHOUT a model, and that is deliberate: the dishes are real
rows from our own order history, so there is nothing for a model to add
except the opportunity to invent a fourth one (UC-18, ADR-0043).
"""
"""Said when retrieval came back empty, and said WITHOUT a model.

An empty candidate set is the single most dangerous prompt there is: the
model has been asked a food question, given nothing to work with, and
rewarded for being helpful. Answering it ourselves costs nothing and removes
the invitation.
"""

SYSTEM = """You are a food assistant for a delivery platform.

Rules you must follow:
- Recommend ONLY dishes from the candidate list below. Always write the
  dish's NAME in your sentence, and put its exact [item:<id>] marker
  immediately before that name.
- The marker is REMOVED before the customer reads your answer. It is a
  citation, never a substitute for the name — a sentence that leans on it
  to name the dish arrives as a gap where the dish should be.
- Never state a price, a delivery time, or whether something is in stock.
  The app shows those next to each dish you cite.
- Never make a safety, allergy, medical or nutritional claim. You may repeat
  that a restaurant lists a dish as e.g. halal or vegetarian, but that is the
  restaurant's own description and not a guarantee.
- Answer "what is in it" from the dish's own description, and "how spicy is
  it" from its DECLARED tags. If a dish does not declare something, say it
  is not listed rather than inferring it from the name or the description —
  a customer asking whether something is spicy is asking what the restaurant
  said, not what you would guess.
- Be brief. Two or three sentences unless asked for more.
"""


@dataclass
class TurnState:
    """Plain data, and deliberately nothing else.

    The ports live in the closures the nodes are built from rather than in
    here, because state that holds a database session is state that cannot
    be logged, compared in a test, or serialised the day somebody turns the
    checkpointer on. (It stays off — ADR-0031.)
    """

    question: str
    city: str
    history: Sequence[Message] = ()
    refusal_reason: SafetyReason = "none"
    answer: str = ""
    raw_answer: str = ""
    candidates: list[Passage] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    restaurant_ids: list[str] = field(default_factory=list)
    dropped: int = 0
    stopped: str = ""
    # Which tier answered, "" when the model did. Carried out of the graph
    # so the interaction fact can split response time by tier — a cache hit
    # counted as a generation flatters the FR-95 average into meaninglessness.
    cache_tier: str = ""
    query_vector: Sequence[float] = ()
    # The fence the corpus was READ under, captured before retrieval and
    # carried to the write-back. Opaque here on purpose — the graph never
    # looks inside it, it only refuses to let it drift.
    fence: Any = None


class TurnRunner(Protocol):
    """What a compiled turn is, to everyone outside this module.

    One method, ours, fully typed — so the service, the tests and pyright
    all see a narrow contract instead of LangGraph's generics. Swapping the
    framework out later changes this file and nothing that calls it.
    """

    async def ainvoke(self, state: TurnState) -> dict[str, Any]: ...


Emit = Callable[[str], Awaitable[None]]
"""Where a token goes the moment it exists. Slice 3.5 hands this a sink that
writes the chunk and then publishes it (ADR-0042 §2); a test hands it a
list."""

Retrieve = Callable[[str, Filters], Awaitable[Hydrated]]
"""Question + scope -> ranked passages and the vector that found them."""

Stream = Callable[[Task, Sequence[Message]], AsyncIterator[TokenChunk]]
"""`ModelRouter.stream` — a task, never a model id (ADR-0030 §3)."""

ExactLookup = Callable[[str, str], Awaitable[tuple[Cached | None, Any]]]
"""(question, city) -> a previous answer to the SAME question, or None.

City travels because the cache is geo-bucketed (FR-74) and the graph is the
only thing that knows which city this turn is for. How the rest of the
fence is resolved — the model version, the corpus epoch — is the adapter's
business; the graph only asks."""

SemanticLookup = Callable[[Sequence[float], Any], Awaitable[Cached | None]]
"""(query vector, fence) -> a previous answer to a CLOSE ENOUGH question.

The FENCE, not the city: it was resolved when the corpus was read, and
re-resolving it mid-turn is how an answer ends up filed under a corpus it
was not computed from."""

Remember = Callable[[Any, str, Sequence[float], Cached], Awaitable[None]]
"""Write-back. Best-effort by contract: it must never fail a turn that has
already produced a good answer."""

GoesWith = Callable[[Sequence[str], str], Awaitable[list[Passage]]]
"""(item ids, city) -> what people order alongside them (FR-78).

Separate from `retrieve` because it answers a different question. Retrieval
finds dishes that MATCH the words; this finds dishes that go with a dish,
which no amount of semantic similarity can tell you — "naan" and "biryani"
are not similar, they are ordered together."""

Fallback = Callable[[str], Awaitable[list[Passage]]]
"""City -> what it is ordering around now (FR-80).

A `Passage` like any other, so the cold-start path and the retrieval path
hand `ground` the same shape — one place that decides what a citation means,
rather than two that have to agree."""
"""Write-back. Best-effort by contract: it must never fail a turn that has
already produced a good answer."""


def _served(hit: Cached | None, tier: str) -> dict[str, object]:
    """A cache hit, in the shape a settled turn has.

    `stopped` stays empty and `cache_tier` carries the news instead. A hit
    is a turn that ANSWERED — marking it stopped would fold it in with
    refusals and no-matches in the one field FR-95 splits on, and "how many
    questions did we answer" would fall every time the cache got better.
    """
    if hit is None:
        return {}
    return {
        "answer": hit.answer,
        "item_ids": list(hit.item_ids),
        "restaurant_ids": list(hit.restaurant_ids),
        "cache_tier": tier,
    }


def build_turn(
    *,
    retrieve: Retrieve,
    stream: Stream,
    emit: Emit,
    exact: ExactLookup | None = None,
    semantic: SemanticLookup | None = None,
    remember: Remember | None = None,
    fallback: Fallback | None = None,
    goes_with: GoesWith | None = None,
    limit: int = 8,
) -> TurnRunner:
    """Compile the turn.

    Dependencies arrive as callables rather than as objects so the graph
    cannot reach past them: there is no `self._session` to be tempted by, and
    a test supplies three functions instead of a fixture tree.
    """

    async def guard(state: TurnState) -> dict[str, object]:
        # The whole conversation, not just this turn: a condition disclosed
        # earlier is still true, and the model is handed that history.
        verdict = safety_check(state.question, [m.content for m in state.history])
        if verdict.refuse:
            return {"answer": REFUSAL, "refusal_reason": verdict.reason, "stopped": "refused"}
        return {}

    async def exact_cache(state: TurnState) -> dict[str, object]:
        if exact is None or state.history:
            # A turn with history is a REPLY, not a question: "what about
            # something spicier?" means nothing on its own, so answering it
            # from a text-keyed cache would hand one conversation's context
            # to another (ADR-0045).
            return {}
        hit, fence = await exact(state.question, state.city)
        return {"fence": fence, **_served(hit, EXACT)}

    async def retrieve_node(state: TurnState) -> dict[str, object]:
        found = await retrieve(state.question, Filters(city=state.city))
        if found.passages:
            candidates = found.passages[:limit]
            if goes_with is not None and asks_for_pairing(state.question):
                # "What goes with the biryani?" is not a similarity question
                # — naan and biryani are not alike, they are ordered
                # together. Without this the model is asked to invent a
                # pairing from a candidate list that contains none (FR-78).
                known = {p.item_id for p in candidates}
                partners = await goes_with([p.item_id for p in candidates], state.city)
                candidates = [*candidates, *(p for p in partners if p.item_id not in known)]
            return {"candidates": candidates, "query_vector": found.query_vector}
        # FR-80: never an empty response. What the city is ordering around
        # now is a real answer to "I have no idea what you meant" — and it
        # is answered without a model for the same reason NO_MATCH is, since
        # the one thing an empty candidate set reliably produces is an
        # invented dish.
        popular = await fallback(state.city) if fallback is not None else []
        if popular:
            return {
                "answer": COLD_START,
                "item_ids": [p.item_id for p in popular],
                "restaurant_ids": sorted({p.restaurant_id for p in popular}),
                "candidates": popular,
                "stopped": "cold_start",
            }
        return {"answer": NO_MATCH, "stopped": "no_match"}

    async def semantic_cache(state: TurnState) -> dict[str, object]:
        if semantic is None or state.history or not state.query_vector:
            return {}
        return _served(await semantic(state.query_vector, state.fence), SEMANTIC)

    async def generate(state: TurnState) -> dict[str, object]:
        # Candidate text is restaurant-authored and arrives here by
        # RELEVANCE — the retriever hands us whatever best matched, which is
        # precisely the channel an attacker would use (ADR-0043 §4).
        context = as_data(render_candidates([(p.item_id, p.text) for p in state.candidates]))
        messages = [
            Message(role="system", content=SYSTEM),
            *state.history,
            # The question is untrusted too. `as_data` cleans the retrieved
            # block, and a fence the CUSTOMER can close is exactly as open as
            # one a restaurant can close (ADR-0043 §4, amended).
            Message(role="user", content=f"{context}\n\nQuestion: {sanitize(state.question)}"),
        ]
        collected: list[str] = []
        async for chunk in stream(Task.GENERATE, messages):
            if chunk.text:
                collected.append(chunk.text)
                await emit(chunk.text)
        return {"raw_answer": "".join(collected)}

    async def ground(state: TurnState) -> dict[str, object]:
        grounded = validate(state.raw_answer, [p.item_id for p in state.candidates])
        # Only the restaurants actually CITED, not every one retrieved: the
        # fact is what the answer recommended, and a conversion attributed to
        # a restaurant the customer was never shown is a made-up number.
        cited = {p.item_id: p.restaurant_id for p in state.candidates}
        item_ids = list(grounded.item_ids)
        restaurant_ids = sorted({cited[i] for i in grounded.item_ids})
        if remember is not None and cacheable(
            answer=grounded.text,
            history=state.history,
            stopped=state.stopped,
            dropped=grounded.dropped,
            item_ids=item_ids,
        ):
            # The rule lives in `cacheable`, not here. Inlining it is how
            # the ungroundedness case went missing: two places to remember,
            # and the one with the tests was not the one being run.
            await remember(
                state.fence,
                state.question,
                state.query_vector,
                Cached(answer=grounded.text, item_ids=item_ids, restaurant_ids=restaurant_ids),
            )
        return {
            "answer": grounded.text,
            "item_ids": item_ids,
            "restaurant_ids": restaurant_ids,
            "dropped": grounded.dropped,
        }

    def onwards(nxt: str) -> Callable[[TurnState], str]:
        """Every edge asks the same question — did the last node settle the
        turn? — so there is one function for it, and a new short circuit is
        a node that sets one of these fields, not a new predicate.

        Two fields because they mean different things: `stopped` is "we did
        not answer" and `cache_tier` is "we answered without a model". Both
        end the turn; only one of them is a non-answer.
        """

        def decide(state: TurnState) -> str:
            return END if state.stopped or state.cache_tier else nxt

        return decide

    graph = StateGraph(TurnState)
    graph.add_node("guard", guard)
    graph.add_node("exact_cache", exact_cache)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("semantic_cache", semantic_cache)
    graph.add_node("generate", generate)
    graph.add_node("ground", ground)
    graph.add_edge(START, "guard")
    graph.add_conditional_edges(
        "guard", onwards("exact_cache"), {"exact_cache": "exact_cache", END: END}
    )
    graph.add_conditional_edges(
        "exact_cache", onwards("retrieve"), {"retrieve": "retrieve", END: END}
    )
    graph.add_conditional_edges(
        "retrieve", onwards("semantic_cache"), {"semantic_cache": "semantic_cache", END: END}
    )
    graph.add_conditional_edges(
        "semantic_cache", onwards("generate"), {"generate": "generate", END: END}
    )
    graph.add_edge("generate", "ground")
    graph.add_edge("ground", END)
    # No checkpointer, deliberately (ADR-0031): a half-finished turn is
    # abandoned and the user re-asks. The durable record is the messages row.
    # An explicit cast, not a structural assignment: LangGraph's `ainvoke`
    # is overloaded with runtime-config parameters we never pass. The cast
    # states exactly the slice of it this service depends on, which is the
    # whole job of a boundary around an untyped dependency.
    return cast(TurnRunner, graph.compile())
