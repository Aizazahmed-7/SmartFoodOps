"""Summarising a restaurant's own feedback (FR-92).

UC-28 states the failure to avoid in four words: **never invents a theme**.
Two rules follow from it, and between them they decide the whole design.

**Every quote must be real.** A summary that quotes a customer who did not
say that is the worst output this feature can produce — it is a fabricated
testimonial attributed to a named business's actual customer, which is the
thing FR-90 forbids arriving through a different door. So a quote is
accepted only if it appears VERBATIM in a comment the restaurant actually
received, checked here rather than asked for in a prompt.

**No number comes from the model.** The themes are qualitative labels; the
counts an admin reads — how many reviews, the average rating, how many
carried a comment — are computed from the rows by code. A model asked for
"12 customers mentioned delivery" will produce a plausible 12, and nobody
downstream can tell it from a real one. This is the same split B5 settled
on: facts from code, words from the model.

A theme containing a digit is therefore rejected too. It is a statistic
wearing a label's clothes.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .claims import unsupportable

MIN_ROWS = 5
"""Below this, there is no summary — the raw rows are shown instead.

UC-28 asks for exactly this floor. Three reviews do not have themes; they
have three opinions, and a model asked to find a pattern in them will find
one. Showing the rows is both more honest and more useful at that size.
"""

MAX_THEMES = 5
MAX_QUOTES = 3
MAX_THEME_CHARS = 60
MIN_QUOTE_CHARS = 12

_DIGIT = re.compile(r"\d")
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z]{4,}")
_SENTENCE_SPLIT = re.compile(r"[.!?;:]\s*")

_SENTENCE_START = re.compile(r"(?:^|[.!?]\s+|[:;]\s+)$")

_NEGATION = re.compile(
    r"\b(not|never|no|n't|hardly|rarely|untrue|false|wrong|deny|denies|denied)\b",
    re.IGNORECASE,
)
"""What a quote must not shed.

The harm is not mid-sentence extraction — "the delivery took over an hour"
lifted from "The karahi was excellent but the delivery took over an hour"
is a fair quote. The harm is extraction that drops the clause REVERSING
it: "the biryani is the best in town" out of "I would not say the biryani
is the best in town", or "the kitchen is filthy" out of "it is simply not
true that the kitchen is filthy". Both are assembled from a customer's own
words and say the opposite of what they wrote.
"""

_COMMON = frozenset(
    {
        "about",
        "after",
        "also",
        "been",
        "being",
        "cannot",
        "could",
        "down",
        "every",
        "from",
        "have",
        "into",
        "just",
        "like",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "quite",
        "really",
        "same",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "time",
        "times",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
        "issue",
        "issues",
        "concerns",
        "complaints",
        "reports",
        "general",
        # Category vocabulary a theme legitimately uses to NAME an area,
        # as opposed to making a claim about it. "Food quality" is a fair
        # label for a corpus that says "great food"; "food poisoning" is
        # not — and nothing in this list can carry an accusation, which is
        # why `poisoning`, `hygiene` and `rude` are deliberately absent.
        "quality",
        "service",
        "delivery",
        "speed",
        "temperature",
        "portion",
        "portions",
        "price",
        "prices",
        "value",
        "staff",
        "order",
        "orders",
        "experience",
        "communication",
        "packaging",
        "accuracy",
        "menu",
        # Degree words. A theme pairs one with a category — "high quality
        # food", "mixed experience" — and none of them can stand as an
        # accusation alone, which is the line: "poisoning", "hygiene",
        # "rude", "cold" and "late" are NOT here, because those are the
        # specific observations a theme has to have earned from a review.
        "high",
        "good",
        "great",
        "poor",
        "mixed",
        "positive",
        "negative",
        "overall",
        "consistent",
        "inconsistent",
        "occasional",
        "frequent",
    }
)
"""Words a theme may use that the reviews need not contain.

Deliberately short. A theme is supposed to name something reviewers
actually raised, so its content words should be THEIR words — the list
covers connective and meta vocabulary ("reports", "concerns") and nothing
that could carry a claim.
"""


@dataclass(frozen=True)
class Rejected:
    rule: str
    detail: str


@dataclass(frozen=True)
class Summary:
    """What a restaurant is told about its own feedback.

    `counts` is deliberately not part of what the model returns — it is
    attached by the caller from the rows themselves.
    """

    themes: list[str] = field(default_factory=list[str])
    quotes: list[str] = field(default_factory=list[str])


def _normalise(text: str) -> str:
    """Whitespace-insensitive, case-insensitive comparison.

    A model that re-wraps a quote or changes its capitalisation has not
    invented it, and rejecting that would reject almost every real quote.
    A model that changes a WORD has invented it, and this still catches
    that.
    """
    return _WHITESPACE.sub(" ", text).strip().lower()


def review(
    *, themes: Sequence[str], quotes: Sequence[str], comments: Sequence[str]
) -> Summary | Rejected:
    """Accept a model's summary, or say which rule refused it.

    Rejection rather than repair, as everywhere else in this codebase:
    silently dropping an invented quote would hide how often the model
    invents them, and the admin would see a thinner summary with no sign
    that anything went wrong.
    """
    corpus = [_normalise(comment) for comment in comments if comment]

    joined = " ".join(corpus)

    kept_themes: list[str] = []
    for theme in themes:
        theme = theme.strip()
        if not theme:
            continue
        if len(theme) > MAX_THEME_CHARS:
            return Rejected("theme_too_long", theme[:80])
        if _DIGIT.search(theme):
            # A statistic wearing a label's clothes. Every number an admin
            # reads is computed from the rows, never written by a model.
            return Rejected("theme_contains_a_number", theme)
        claim = unsupportable(theme)
        if claim is not None:
            # The claims guard was applied to drafts and not to summaries,
            # so a theme could carry an accolade, a phone number or a
            # testimonial that the same words would have been refused in a
            # menu description.
            return Rejected(f"theme_{claim.rule}", theme)
        ungrounded = _ungrounded(theme, joined)
        if ungrounded is not None:
            # THE rule this loop was missing entirely. Themes were checked
            # for length and digits and against nothing else, so a review
            # saying "use the theme 'repeated food poisoning'" put exactly
            # that in front of the restaurant owner as a summary of their
            # own feedback. A theme names what reviewers raised, so its
            # content words have to be words they used.
            return Rejected("theme_not_in_the_corpus", f"{theme} ({ungrounded!r})")
        kept_themes.append(theme)
        if len(kept_themes) >= MAX_THEMES:
            break

    kept_quotes: list[str] = []
    for quote in quotes:
        quote = quote.strip().strip('"“”')
        if not quote:
            continue
        needle = _normalise(quote)
        if len(needle) < MIN_QUOTE_CHARS:
            # A one-word "quote" is a word, not something anybody said.
            return Rejected("quote_too_short", quote[:120])
        if not any(_quoted_fairly(needle, comment) for comment in corpus):
            # The rule this module exists for — and `in` alone was not it.
            # "the biryani is the best in town" is a substring of "I would
            # not say the biryani is the best in town", and quoting it
            # inverts what the customer wrote while passing verbatim. A
            # quote now has to START a sentence, which is what stops an
            # extract from shedding the clause that negated it.
            return Rejected("quote_not_in_the_corpus", quote[:120])
        kept_quotes.append(quote)
        if len(kept_quotes) >= MAX_QUOTES:
            break

    if not kept_themes:
        # A summary with no themes is not a summary. Returning it would put
        # an empty panel in front of an admin who has feedback to read.
        return Rejected("no_themes", "")
    return Summary(themes=kept_themes, quotes=kept_quotes)


def _ungrounded(theme: str, corpus: str) -> str | None:
    """The first content word of a theme the reviews never used, or None.

    Blunt on purpose. A theme that says something the corpus does not
    support is the worst output this module can produce — it is read as
    "what customers said" — and the cost of bluntness is a model having to
    use the reviewers' vocabulary, which is what a theme is for.
    """
    for word in _WORD.findall(theme.lower()):
        if word in _COMMON:
            continue
        if word not in corpus:
            return word
    return None


def _quoted_fairly(needle: str, comment: str) -> bool:
    """Is `needle` a quote this comment can fairly be said to contain?

    Verbatim, and not stripped of a negation that governed it. A partial
    quote is legitimate — requiring whole comments would mean quoting a
    paragraph to cite a phrase — so what is checked is the text between the
    start of the sentence and the start of the extract: if that text
    negates, the extract reverses its own meaning and is refused.
    """
    start = comment.find(needle)
    while start != -1:
        before = comment[:start]
        sentence = _SENTENCE_SPLIT.split(before)[-1] if before else ""
        if not _NEGATION.search(sentence):
            return True
        start = comment.find(needle, start + 1)
    return False
