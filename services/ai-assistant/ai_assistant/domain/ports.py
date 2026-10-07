"""Outbound ports for the GenAI plane (ADR-0030) — the ONLY seams through
which a model provider is reached. Adding Groq or Bedrock is a new adapter
file plus a router row; nothing else moves.

ADR-0010's discipline, transferred verbatim: a refusal, a length
truncation, a tool call and an empty answer are RESULTS, because they are
business outcomes the caller must branch on. Only transport failures are
exceptions. Getting this boundary wrong is how a content-policy refusal
ends up in a 500 and an alert.

`VectorStore` arrives with B1, the milestone that first needs it (ADR-0032).
It is declared with the two methods the knowledge pipeline actually calls
and nothing else: retrieval is B2's, and a port guessed a milestone early is
worse than a port declared on time.
"""

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .retrieval import Candidate, Filters

Role = Literal["system", "user", "assistant"]

FinishReason = Literal["stop", "length", "refusal", "tool_call"]
"""Why generation stopped.

stop      — the model finished its answer
length    — hit max_output_tokens; the answer is TRUNCATED, not wrong
refusal   — the provider declined on content grounds
tool_call — the model asked for a tool (B3; no tools are offered in B0)
"""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: FinishReason
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class TokenChunk:
    """One frame of a streamed answer.

    `text` is the delta to append. The terminal frame carries `done=True`,
    an empty delta, and the usage the provider only reveals at the end —
    which is why token accounting cannot be done from the deltas alone.
    """

    text: str = ""
    done: bool = False
    finish_reason: FinishReason | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class PlaneShed(Exception):
    """The per-cell spend/quota breaker is open. Maps to 503 ADMISSION_SHED,
    not DEPENDENCY_UNAVAILABLE: nothing is broken, we chose to stop
    spending. Callers with a non-generative answer available (lexical
    search, popularity recommendations, a rendered template) must catch this
    and serve that instead — ladder step 2a. A breaker that errors where it
    could return a worse-but-real answer is a worse breaker.
    """


class LlmUnavailable(Exception):
    """Provider unreachable, timed out, or 5xx after bounded retries.

    Unlike `PspUnavailable`, the outcome is NOT ambiguous and nothing must
    be retained: a completion has no side effect at the provider, so a
    retry — or a failover to another vendor entirely — cannot double-charge
    anyone or leave books to reconcile (ADR-0030 §4).
    """


class LlmRateLimited(Exception):
    """Provider refused on quota (429 / tokens-per-minute exhausted).

    Distinct from `LlmUnavailable` because it is the EXPECTED failure at
    scale — quota, not availability, is the real ceiling (ADR-0029) — and
    because it should fail over to a different vendor immediately rather
    than burn the local retry budget against a wall.
    """


class LlmPort(Protocol):
    """One model provider. Model ids are passed in, never chosen here: the
    router owns task->model policy (ADR-0030 §3)."""

    @property
    def provider(self) -> str:
        """Stable short name used in metrics labels and router policy."""
        ...

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Completion: ...

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[TokenChunk]:
        """Not a coroutine — returns the iterator, so a caller can start it
        and observe whether the FIRST frame fails (the only point at which
        failover is still safe; see ModelRouter.stream)."""
        ...


class EmbeddingUnavailable(Exception):
    """Embedding provider unreachable after bounded retries. The knowledge
    consumer raises to retry (ADR-0021), so a provider outage delays the
    index and never drops a menu change."""


class EmbeddingPort(Protocol):
    """Text -> vectors. `model` and `dimensions` are exposed because both
    are written to every row: vectors from different models are not
    comparable, so a change to either is a version bump and a rolling
    reindex, never an in-place edit (PRD FR-61)."""

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Batch by contract: per-text calls are the difference between one
        request and thirty at ingestion volume. Returns vectors positionally
        aligned with `texts`."""
        ...


# ── Knowledge index (B1, ADR-0032) ──────────────────────────────────

# Two chunk shapes, because the index is two tables (see db.py): an item
# chunk and a restaurant chunk describe different things, embed to different
# text shapes, and are retrieved separately and fused. A single dataclass
# with half its fields optional would push that distinction into every
# `if chunk.item_id is not None` downstream.


@dataclass(frozen=True)
class ItemChunk:
    """One dish, as the index knows it.

    `id` is the NATURAL KEY the whole pipeline dedupes on (DoD-2): derived
    from the ids it describes, never minted, so an at-least-once redelivery
    upserts the same row instead of growing the index.

    `content` holds ONLY durable text — name, description, tags, category,
    cuisine. Price, availability and status are excluded from it on purpose
    (FR-60): they change far faster than an embedding can be refreshed, and
    an answer that quotes them from here quotes a number that was true once.
    They ride as FIELDS instead, where they narrow a query cheaply and are
    then re-resolved live before anything reaches a customer.

    Every filterable attribute is a field rather than prose for the same
    reason: a filter that lives in the text is a filter the database cannot
    apply (FR-62, FR-63).

    `content` is RESTAURANT-AUTHORED TEXT and therefore untrusted. Nothing
    here sanitises it — prompt assembly delimits and labels it as data
    (FR-71), which is the only place that can do it correctly.
    """

    id: str
    restaurant_id: str
    item_id: str
    city: str
    brand_id: str | None
    cuisines: Sequence[str]
    category: str
    tags: Sequence[str]
    # The dish's name, carried as a field as well as in `content`'s first
    # line. The content studio writes copy from name, tags, category and
    # cuisine (FR-88); the other three were already fields, and a consumer
    # should not have to parse the embedded text to get the fourth.
    name: str
    price_cents: int
    available: bool
    status: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class RestaurantChunk:
    """One restaurant, as the index knows it — name, cuisines.

    Not every question is about a dish. "Thai near me" matches a restaurant's
    identity, and there may be no single item whose text contains "Thai" at
    all; without this chunk, cuisine-shaped questions have nothing to
    retrieve (FR-58, UC-21).
    """

    id: str
    restaurant_id: str
    city: str
    brand_id: str | None
    cuisines: Sequence[str]
    status: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class ItemUpsert:
    """A chunk plus, when the text actually changed, its new vector.

    `embedding=None` means "the text is byte-identical to what is stored,
    keep the vector you already have". That split is the whole economy of
    the pipeline: a price edit, a pause, or an 86'd dish rewrites cheap
    columns and asks the provider for nothing, while a replayed topic
    (FR-59) rewrites every row and embeds NOTHING at all.
    """

    chunk: ItemChunk
    embedding: Sequence[float] | None


@dataclass(frozen=True)
class RestaurantUpsert:
    chunk: RestaurantChunk
    embedding: Sequence[float] | None


class VectorStore(Protocol):
    """The knowledge index, behind a port so retrieval can graduate to its
    own service without a caller noticing (ADR-0029 §5)."""

    async def hashes_for(self, *, restaurant_id: str, model_version: str) -> dict[str, str]:
        """`{chunk_id: content_hash}` already stored for this restaurant at
        this model version, across BOTH tables — chunk ids are unique across
        them, so one mapping is unambiguous and the drain makes one round
        trip instead of two.

        The drain diffs against this to decide what to EMBED, never what to
        write: rows are rewritten unconditionally because Postgres writes are
        cheap and provider calls are not (FR-58, FR-59).
        """
        ...

    async def vectors_by_hash(
        self, *, content_hashes: Iterable[str], model_version: str
    ) -> dict[str, list[float]]:
        """Vectors already computed for these exact texts, ANYWHERE in the
        index — not just this restaurant's rows.

        An embedding is a pure function of (text, model), so a vector
        computed for one restaurant is the correct vector for another with
        the same text, not an approximation of it. That is what makes the
        ADR-0028 fan-out affordable: a base dish inherited by twelve
        branches is twelve rows and one provider call, because item chunks
        deliberately omit the restaurant's name (ADR-0033 §4).
        """
        ...

    async def replace_restaurant(
        self,
        *,
        restaurant_id: str,
        model_version: str,
        restaurant: RestaurantUpsert,
        items: Sequence[ItemUpsert],
        now: datetime,
    ) -> int:
        """Apply one restaurant's new state in ONE transaction: upsert the
        restaurant chunk and every item chunk, then delete every item chunk
        for this restaurant and version that is not in `items`. Returns the
        number deleted.

        `items` is the COMPLETE desired set, not a delta. An earlier draft
        took changed rows plus a separate `keep_item_ids` list; two arguments
        that must agree are two arguments that can disagree, and the failure
        mode — sweeping a live dish out of the index — is invisible until a
        customer asks for it.

        The delete leg is not housekeeping, it is correctness. Catalog's
        payloads are full snapshots, so a deleted dish arrives as an ABSENCE,
        never a tombstone; without reconciliation the assistant keeps
        recommending a dish nobody can order, which reads to a customer as a
        lie and to FR-70 as an ungrounded answer sourced from ingestion.

        The restaurant chunk needs no reconcile leg: there is exactly one per
        restaurant per version, so an upsert is the whole story.
        """
        ...


# ── Retrieval (B2, ADR-0029 §5) ─────────────────────────────────────


@dataclass(frozen=True)
class Retrieved:
    """What one retrieval returns: two ranked lists, deliberately unmerged.

    Items and restaurants are NOT fused together, for the reason ADR-0032 §5
    split the tables in the first place — a query embedding sits
    systematically closer to one text shape than the other, so a combined
    ranking would order a mediocre restaurant above an excellent dish for
    reasons unrelated to the question. Keeping them apart also happens to be
    exactly what Catalog's `SearchPort` wants: restaurants matched directly,
    and restaurants surfaced via matching items.
    """

    items: Sequence[Candidate]
    restaurants: Sequence[Candidate]
    # The embedding this retrieval actually used. Handed back rather than
    # recomputed because the semantic answer cache needs exactly this vector
    # (FR-74), and embedding the same question twice per turn would double
    # the provider bill of the feature meant to reduce it. Empty when no
    # index existed, because then nothing was embedded at all.
    query_vector: Sequence[float] = ()


class Retriever(Protocol):
    """Hybrid retrieval over the knowledge index.

    Declared now because B2 is the milestone that calls it — and with only
    the method B2 calls, the same discipline that kept `VectorStore`
    undeclared until B1. Reranking (FR-66) is a P2 that will arrive as its
    own seam, not as a flag on this one.
    """

    async def retrieve(self, *, query: str, filters: Filters, limit: int) -> Retrieved:
        """Both legs, both tables, fused per table.

        `limit` bounds each returned list, not the per-leg fetch: fusion
        needs more candidates than it returns, or the middling-but-agreed
        results it exists to promote would be truncated before the merge.
        """
        ...


# ── kitchen congestion (FR-85) ─────────────────────────────────────


@dataclass(frozen=True)
class KitchenLoad:
    """What Inventory's capacity gate currently sees for one kitchen.

    Facts only. `active >= capacity` is a legal, meaningful state — lowering
    capacity below current active stops new orders and lets running ones
    drain — so nothing here clamps, and nothing here decides what "busy"
    means. That judgement belongs to FR-82's resolver, in one pure function
    that can be tested over every branch, rather than being half-made at a
    port and half-made downstream.

    `as_of` is the reading's clock, not ours. A congestion number is only
    as true as the moment it was taken, and the caller is about to write
    prose containing the words "right now".
    """

    restaurant_id: str
    active: int
    capacity: int
    as_of: datetime


class UpstreamUnavailable(Exception):
    """A first-party service could not be reached, as distinct from it
    telling us the thing does not exist.

    Lives here with the other port failures rather than in the adapter that
    raises it: the API layer must branch on it (an outage is a 503, never a
    404 saying the customer's own order is unknown) and the layer-contract
    scan forbids `api/` importing `adapters/` — correctly, since that is
    exactly the coupling that lets a transport type leak upward.
    """


class KitchenLoadPort(Protocol):
    async def load(self, restaurant_id: str) -> KitchenLoad | None:
        """None = no fact, for ANY reason.

        An unknown kitchen and an unreachable Inventory are different
        operationally and identical to the resolver: in both cases nobody
        observed a congestion number, and an explanation must not imply one.
        Collapsing them here keeps that single truth at the seam instead of
        asking every caller to re-derive it.
        """
        ...
