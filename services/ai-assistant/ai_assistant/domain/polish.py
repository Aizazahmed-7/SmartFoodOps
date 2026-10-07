"""Letting a model improve the wording, and never the content (FR-83).

The explanation engine decides WHAT to say in `explain.py` and says it from
`render.py`. This module is the optional third step: a cheap model rewrites
a template so the copy reads less like a form letter. It is allowed to
change words. It is not allowed to change facts, and the guards below are
what make that a property rather than a hope.

**It rewrites the TEMPLATE, not the sentence.** `"...ready for {elapsed}..."`
goes to the model with the placeholder intact and comes back with it intact;
the facts are substituted afterwards, per request. That is what makes FR-87's
`(reason_code, locale, bucket)` cache key correct — a polished sentence
containing "4 minutes" would be served to the customer at nine minutes, which
is the exact trap caching generated prose usually falls into.

**A number the model wrote is a number nobody observed.** Every figure in an
explanation arrives through a placeholder, so a candidate containing a digit
the original did not have is rejected outright. That single rule catches the
most likely and most damaging failure: a model helpfully adding "about 10
minutes" to a stage the system cannot bound.

Rejection is cheap and silent — the template was already a good answer, and
it is the answer the customer gets. Nothing here can make an explanation
fail to exist.
"""

import re
from dataclasses import dataclass

from .explain import ReasonCode

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_DIGIT = re.compile(r"\d")

_WORD_NUMBERS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "fifteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "ninety",
    "hundred",
    "half",
    "quarter",
    "couple",
    "few",
    "several",
    "dozen",
)
_WORD_NUMBER = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.IGNORECASE)

_NEGATION = re.compile(
    r"\b(not|no|never|n't|cannot|couldn't|hasn't|haven't|isn't|won't|didn't|don't)\b",
    re.IGNORECASE,
)
"""Most of this copy is about something NOT having happened yet.

"The restaurant has had your order for {elapsed} and hasn't accepted it"
became "…and has now accepted it" in an adversarial pass — every
placeholder intact, no number, barely longer, and the exact opposite of
the truth. Polarity is the cheapest signal that a sentence still means
what it meant, and losing it is the most damaging thing a rewrite can do
that no other rule here sees.
"""

_CAUSAL = re.compile(r"\b(because|due to|since|owing to|as a result of)\b", re.IGNORECASE)
"""The resolver owns the cause; the rewriter may not add one.

"We couldn't complete this order and cancelled it" became "We cancelled
this order because the restaurant refused it" — an invented cause on a
system-timeout cancellation, attached to a specific business that did
nothing wrong.
"""
"""Numbers spelled out.

The digit rule alone was claimed to catch "the most likely and most
damaging failure". It caught the most likely SPELLING of it: an adversarial
pass found "they usually accept within another five minutes" and "a courier
will be there in about half an hour" sailing through — invented estimates
for stages the system cannot bound at all, which is the exact harm the
digit rule exists to prevent.

Deliberately blunt. A rewrite that loses the word "few" is a rewrite we did
not need; a rewrite that invents a delivery time is a lie we cannot take
back, and the cache would serve it to every customer in that situation for
the life of the process.
"""

MAX_CHARS = 400
"""A hard ceiling. A rewrite that grew into a paragraph is not a rewrite,
and the surface it renders into is one line on an order page."""


SYSTEM_PROMPT = """You rewrite one sentence of customer-facing copy for a
food delivery app, to read naturally and kindly.

Rules, all absolute:
- Keep every {placeholder} exactly as written, same spelling, same count.
- Never add a number, a time, an estimate, a duration, or a price.
- Never add a fact that is not already in the sentence. You do not know
  where the order is, what the restaurant is doing, or when it will arrive.
- Never apologise for a cause the sentence does not state.
- One sentence or two. No greeting, no sign-off, no emoji, no markdown.

Reply with the rewritten sentence and nothing else."""
"""Prescriptive on purpose.

The model is being handed copy that is already correct, so every degree of
freedom it is given is a way for the answer to get worse. The rules are
also not load-bearing: `accept()` below enforces the ones that matter,
because a prompt is a request and a guard is a guarantee.
"""


def prompt_for(reason: ReasonCode, template: str) -> str:
    return f"Situation: {reason.value}\n\nSentence to rewrite:\n{template}"


@dataclass(frozen=True)
class Review:
    """The verdict on one rewrite, and WHICH rule decided it.

    The rule matters as much as the outcome. A rejection rate is a number
    nobody can act on; "the model keeps inventing durations" is a prompt
    change. Rejections are the only feedback this layer ever gets, so
    throwing away the reason would leave it unimprovable.
    """

    kept: str | None
    rule: str | None = None


def review(original: str, candidate: str) -> Review:
    """Judge one rewrite. Every check is a rejection, never a repair:
    silently fixing a bad rewrite would hide how often the model is
    getting it wrong."""
    candidate = candidate.strip()
    if not candidate:
        return Review(None, "empty")
    if len(candidate) > MAX_CHARS:
        return Review(None, "too_long")
    if "\n" in candidate:
        return Review(None, "multiline")

    # Same placeholders, same number of times. A dropped `{elapsed}` loses
    # the only fact in the sentence; a duplicated one reads as a stutter.
    if sorted(_PLACEHOLDER.findall(original)) != sorted(_PLACEHOLDER.findall(candidate)):
        return Review(None, "placeholders_changed")

    # A brace that is not one of those placeholders would reach the
    # customer as literal `{`, or blow up `format_map`.
    bare = _PLACEHOLDER.sub("", candidate)
    if "{" in bare or "}" in bare:
        return Review(None, "stray_brace")

    # The rule that matters: no invented figures. Compared outside the
    # placeholders so that `{active} of {capacity}` is unaffected, and
    # against the original so copy that legitimately contains a digit is
    # not blocked from being rewritten.
    bare_original = _PLACEHOLDER.sub("", original)
    if _DIGIT.search(bare) and not _DIGIT.search(bare_original):
        return Review(None, "invented_number")
    if _WORD_NUMBER.search(bare) and not _WORD_NUMBER.search(bare_original):
        return Review(None, "invented_word_number")

    if _NEGATION.search(bare_original) and not _NEGATION.search(bare):
        return Review(None, "negation_lost")
    if _CAUSAL.search(bare) and not _CAUSAL.search(bare_original):
        return Review(None, "invented_cause")

    # Length is a crude proxy for "is this still the same sentence", and the
    # last defence against the failures no rule can name.
    if len(candidate) > len(original) * 1.6 + 20:
        return Review(None, "grew_too_much")

    return Review(candidate)


def accept(original: str, candidate: str) -> str | None:
    """The rewritten template, or None to keep the original."""
    return review(original, candidate).kept
