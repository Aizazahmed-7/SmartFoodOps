"""Citations the model cannot fake (ADR-0043 §1–3, FR-70).

The model is handed candidates as `[item:<id>] <text>` and asked to cite the
markers it uses. Afterwards — in code, not in the prompt — every marker in
the output is checked against the set that was actually retrieved. Unknown
ids never reach a customer.

Markers rather than dish names, and that is the load-bearing choice.
Validating by NAME means fuzzy-matching model prose against a menu, which
fails open exactly where it matters: "the chicken karahi" nearly matches
"Chicken Karahi" and also nearly matches a dish that does not exist. An id
either was in the retrieved set or was not, and that question has an answer.

Pure, so the whole rule is testable without a model, a database or a key.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

MARKER = re.compile(r"\[item:([A-Za-z0-9_.:-]+)\]")

_WHITESPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")


@dataclass(frozen=True)
class Grounded:
    """An answer that has been through the check.

    `text` is what a customer reads; `item_ids` is what the client renders
    cards for, priced live (FR-60) — which is why the model is never asked
    for a price and none is parsed out of here. `dropped` is the count of
    citations that referenced nothing, and it is an alerting signal rather
    than a curiosity (NFR-26).
    """

    text: str
    item_ids: Sequence[str]
    dropped: int

    @property
    def grounded(self) -> bool:
        return self.dropped == 0


def render_candidates(items: Sequence[tuple[str, str]]) -> str:
    """The retrieved set, in the shape the model is asked to cite.

    Ids are opaque and carry no price, availability or restaurant identity —
    there is nothing in a marker for a model to paraphrase incorrectly,
    because there is nothing in it at all.
    """
    return "\n".join(f"[item:{item_id}] {text}" for item_id, text in items)


def validate(answer: str, allowed: Sequence[str]) -> Grounded:
    """Strip every marker; keep the ids that were actually retrieved.

    Markers are removed from the prose either way — they are a machine
    artifact and a customer should never see one. What differs is what
    happens to the reference: a known id becomes a card, an unknown one
    becomes nothing and increments `dropped`.

    **A fabricated citation degrades the sentence, it does not fail the
    turn** (ADR-0043 §2). Refusing to answer over one bad id would convert a
    cosmetic model error into an outage, and the customer loses an answer
    that was probably fine.

    The honest limit, stated here because it is easy to miss: stripping a
    marker removes the LINK, not the CLAIM. If a model invents
    "[item:itm_nope] Pepperoni Pizza", the customer still reads the words
    "Pepperoni Pizza" — with no card, no price and nothing to tap, but the
    words survive. That is why `dropped` is alerted on rather than merely
    counted: the fix for a model that invents dishes is to notice it is
    happening, not to launder each instance.
    """
    known = set(allowed)
    seen: list[str] = []
    dropped = 0

    for item_id in MARKER.findall(answer):
        if item_id in known:
            if item_id not in seen:
                seen.append(item_id)
        else:
            dropped += 1

    text = MARKER.sub("", answer)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", _WHITESPACE.sub(" ", text))
    return Grounded(
        text="\n".join(line.strip() for line in text.splitlines()).strip(),
        item_ids=seen,
        dropped=dropped,
    )


_PARTIAL = re.compile(r"\[(?:i(?:t(?:e(?:m(?::[A-Za-z0-9_.:-]*)?)?)?)?)?$")
"""A trailing fragment that could still GROW into a marker: `[`, `[i`, …,
`[item:itm_ab`. Anything else ending in `[` is just a bracket."""


class Stripper:
    """Removes markers from a stream of tokens, one chunk at a time.

    `validate` cannot do this job. It sees the whole answer, and a stream has
    no whole answer — a provider splits `[item:itm_abc] Raita` across chunks
    wherever its tokenizer likes, and live output showed exactly that:
    `" [item:itm_e8d9…] R"` then `"aita, which is…"`. Stripping each chunk
    independently therefore strips nothing, and the reader watches opaque
    ids scroll past mid-sentence.

    So this holds back the smallest tail that could still become a marker,
    and releases it the moment it cannot. A chunk ending in an ordinary `[`
    costs one chunk of latency and no correctness; a chunk ending mid-marker
    is held until the closing bracket arrives.

    Applied BEFORE the chunk is persisted, so the replay a reconnect reads
    and the live text a reader saw are the same bytes — a stripper on the
    read side instead would have to be applied identically in two places,
    and the day they diverge is the day a reconnect changes the answer.
    """

    def __init__(self) -> None:
        self._held = ""
        self._last = ""

    def push(self, text: str) -> str:
        """The text safe to send now — possibly empty, never a partial marker."""
        buffered = _MARKER_SPACE.sub("", self._held + text)
        partial = _PARTIAL.search(buffered)
        if partial is None:
            out, self._held = buffered, ""
        else:
            out, self._held = buffered[: partial.start()], buffered[partial.start() :]
        return self._spaced(out)

    def _spaced(self, out: str) -> str:
        """Collapse the gap a stripped marker leaves, ACROSS chunks.

        `_MARKER_SPACE` can only eat a space it can see. When a marker ends
        one chunk and its trailing space opens the next — which is what a
        model streaming character by character does — the space arrives after
        the marker is already gone, and the reader gets a double space that
        the final `validate` pass would have collapsed. Remembering the last
        character emitted is what closes that seam.
        """
        out = _WHITESPACE.sub(" ", out)
        if self._last in (" ", "") and out.startswith(" "):
            out = out.lstrip(" ")
        if out:
            self._last = out[-1]
        return out

    def flush(self) -> str:
        """Whatever is still held at the end of the stream.

        A trailing `[item:itm_x` with no closing bracket never became a
        marker, so it is text — dropping it would silently truncate the last
        sentence of an answer.
        """
        held, self._held = self._held, ""
        return self._spaced(held)


_MARKER_SPACE = re.compile(MARKER.pattern + r"\s?")
"""The marker and the single space that usually follows it. Without the
space, stripping `"the [item:x] Raita"` leaves a double space mid-sentence
in the live stream that the final `validate` pass would have collapsed."""
