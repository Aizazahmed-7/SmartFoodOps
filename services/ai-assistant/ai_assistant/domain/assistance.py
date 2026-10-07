"""Order-assistance question shapes (FR-78).

Three things a customer asks once a dish is in front of them: what is in
it, how hot is it, and what goes with it. Two of those are already
answerable from the chunk — it carries the restaurant's own description and
its declared tags — and the third is not, because nothing in the retrieval
path knows what people actually order together.

This module decides only the third: is the customer asking for an
accompaniment? Pure, so the branch can be argued with over a table of
phrasings rather than by running a model.
"""

import re

_PAIRING = re.compile(
    r"\b(goes?\s+(well\s+)?with|go\s+with|pairs?\s+(well\s+)?with|paired\s+with|"
    r"what\s+(else\s+)?(should|shall|can|could)\s+i\s+(have|get|order|add)|"
    r"accompan\w*|on\s+the\s+side|as\s+a\s+side|side\s+(dish|order)|"
    r"to\s+go\s+(with|alongside)|alongside|complement\w*)\b",
    re.IGNORECASE,
)


def asks_for_pairing(question: str) -> bool:
    """Is this a "what goes with it" question?

    Deliberately narrow. A false positive costs a few extra candidates in
    the prompt — harmless. A false negative costs nothing either: the turn
    answers from the retrieved dish exactly as it did before, which is a
    worse answer but not a wrong one. So the pattern errs toward being
    sure, which is the opposite of the safety guard's posture and for the
    opposite reason — nothing here is unsafe, only unhelpful.

    "What goes with the biryani?" and "what should I get with it?" match;
    "what is in the biryani?" and "is it spicy?" do not, because those are
    already answered by the chunk itself.
    """
    return bool(_PAIRING.search(question))
