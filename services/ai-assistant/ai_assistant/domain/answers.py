"""What may be cached, and under what name (FR-74, ADR-0045).

Pure, so the rules that decide whether a customer sees a stale answer are
testable without Redis, Postgres, an embedder or a key. Everything here is
a predicate or a string — the I/O lives behind the ports the graph is built
from.
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

_WHITESPACE = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s?!.,;:]+$")


def normalize(question: str) -> str:
    """The form two questions must share to be "the same question".

    Case, spacing and trailing punctuation only. Deliberately NOT stemming,
    stop-word removal or synonym folding: those make "is the biryani spicy"
    and "is the biryani not spicy" collide, and an exact-match tier that can
    be wrong is worse than no exact-match tier — the semantic tier exists to
    take the fuzzy cases, with a similarity threshold and a vector behind it.

    NFKC first, so a pasted question with a full-width or composed character
    matches the typed one instead of missing forever.
    """
    folded = unicodedata.normalize("NFKC", question).casefold().strip()
    return _TRAILING.sub("", _WHITESPACE.sub(" ", folded))


@dataclass(frozen=True)
class Fence:
    """What a cached answer is only valid WITHIN (FR-74).

    All three parts earn their place:

    `model_version` — a question vector written by one embedder is
    meaningless to another, and the rolling reindex means two can coexist
    (FR-61). Sharing a cache across them would compare distances in
    different spaces.

    `city` — retrieval is geo-scoped (FR-63), so an answer is about one
    city's menus. This is FR-74's "geo bucket", and it is the coarsest
    bucket that is still correct: a finer one (a neighbourhood) would shred
    the hit rate for no additional truth.

    `epoch` — FR-74's "menu_version". The assistant's corpus is a Kafka
    projection, not a versioned blob, so the version is a per-city counter
    the drain bumps when it changes that city's chunks.
    """

    model_version: str
    city: str
    epoch: int

    def key(self, question: str) -> str:
        """The exact tier's Redis key.

        The fence is IN the key rather than checked after the read. A key
        that can be read and then rejected is a key that will one day be
        read and not rejected; this way a stale entry is simply unreachable.
        """
        digest = hashlib.sha256(normalize(question).encode()).hexdigest()[:32]
        return f"assistant:ans:{self.model_version}:{self.city}:{self.epoch}:{digest}"

    def row_id(self, question: str) -> str:
        return hashlib.sha256(normalize(question).encode()).hexdigest()


EXACT = "exact"
SEMANTIC = "semantic"
"""The tier names, in one place: they are a metric label (FR-74's
`assistant_cache_total{tier,...}`) and a field on the interaction fact, so
a typo in either would split a dashboard silently."""


@dataclass(frozen=True)
class Cached:
    """An answer a tier had already.

    Deliberately does NOT say which tier found it. The node that called the
    lookup knows, and having the adapter declare it instead means a port
    that forgets to set it returns a hit the graph reads as a miss — which
    is a fall-through to a paid generation that no test of the adapter
    would catch.
    """

    answer: str
    item_ids: Sequence[str] = field(default_factory=tuple)
    restaurant_ids: Sequence[str] = field(default_factory=tuple)


def cacheable(
    *,
    answer: str,
    history: Sequence[object],
    stopped: str,
    dropped: int = 0,
    item_ids: Sequence[str] = (),
) -> bool:
    """May this turn's answer be served to somebody else later?

    **This is the gate, called from the graph.** It briefly was not — the
    rule lived inlined in `ground` and this function guarded nothing but its
    own tests, which is how the ungroundedness rule below came to be missing
    from the thing that enforces it.

    Four refusals, and the first is the one that matters:

    **A turn with history is not a question, it is a reply.** "What about
    something spicier?" means nothing without the turn before it, and
    caching it under its own text would hand that answer to the next person
    who typed those words in a different conversation. No fence catches
    that, because nothing about the corpus is wrong — the CONTEXT is.

    **A short-circuited turn is not worth caching.** A refusal is already
    fixed text and costs no provider call (ADR-0043), so caching it saves
    nothing. A no-match is the cheap path too, and pinning "nothing matched"
    risks a stale negative surviving in a city that just gained a
    restaurant — the epoch would clear it, but the saving never justified
    the risk.

    **An empty answer is not an answer.** A failed generation must never be
    replayed as though it were one.

    **An answer that cited something that did not exist is never pinned.**
    ADR-0043 §2 accepts degrading one sentence when a model invents a
    citation — the marker is stripped and the claim survives. It does not
    accept serving that sentence to everyone who asks the same question for
    the next hour, from a tier that bypasses retrieval and grounding
    entirely. `dropped > 0` is a fabrication we caught; an answer with no
    citations at all is one we could not check and which renders no cards.
    """
    return bool(answer) and not history and not stopped and dropped == 0 and bool(item_ids)
