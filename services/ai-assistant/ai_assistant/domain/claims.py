"""Refusing copy that claims something nobody can stand behind (FR-90).

FR-90 names two failures by name — fabricated claims and fabricated
testimonials — and they are the ones a model reaches for unprompted. Asked
to write a message to returning customers, it writes "our customers rave
about the karahi" and "voted the best biryani in Islamabad". Both are
inventions, both are about a real business, and both would go out under
that business's name.

This is the same shape as B5's `polish.review`: rejection rules, named, with
the candidate kept out rather than patched up. The difference is what is
being defended. There, the facts were already correct and a model was only
allowed to rephrase them; here the model is WRITING, so there is no
original to compare against and the rules have to describe the shapes of
claim that are false by construction.

**What this cannot do.** It does not catch an invented ingredient or
cooking method — "served warm from the tandoor" on a dish whose facts never
mentioned a tandoor passes every rule below, because "tandoor" is no more
detectable than "delicious" without knowing how the dish is actually made.
That gap is real, it is why FR-93 requires a human to approve every draft
before publication, and it is not closed by this module.
"""

import re
from dataclasses import dataclass

_TESTIMONIAL = re.compile(
    r"""(
        # Quoted SPEECH: at least four words inside the quotes. A dish
        # name in quotes ("Tangy Jalapeno") is not a testimonial, and a
        # character floor alone could not tell them apart.
        ["“‘'](?:[^"”’']*\s+){2,}[^"”’']*["”’']
      | \b(our\s+|the\s+)?
        (customers?|diners?|guests?|locals?|regulars?|fans?|everyone|people)\s+
        (say|says|said|tell|told|love|loves|rave|raves|agree|agrees|call|calls
         |keep|keeps|can'?t|cannot|describe|describes|report|reports)\b
      | \b(one|a)\s+(customer|diner|guest|regular)\s+\w+
    )""",
    re.IGNORECASE | re.VERBOSE,
)
"""Someone else's words, invented.

The quoted-speech arm has a length floor so a dish name in quotes is not
mistaken for a testimonial. The noun and verb lists are the shape —
"customers say" is a claim about people who did not say it, whether or not
quotation marks appear.

The two arms used to carry DISJOINT vocabularies: one had the noun
`regulars` without the verb `say`, the other the verb without the noun, so
"our regulars say the nihari is unmissable" passed both. They are one arm
now. The single-quote character is in the opening class for the same
reason — a model writing '…' was invisible.
"""

_ACCOLADE = re.compile(
    r"""\b(
        award[- ]winning | prize[- ]winning
      | voted\s+(the\s+)?\w+ | rated\s+(the\s+)?(best|top|no|number|\#|\d)
      | number\s*one | no\.\s*1 | \#\s*1   # \# — bare # opens a comment in VERBOSE
      | best[- ]selling | bestselling
      # "best in <place>" is a ranking claim; "Best of Punjab Platter" is
      # a dish name, so `of` is deliberately absent.
      | (the\s+)?best\s+(in|thing|place|restaurant)\b
      | (we|it|they)\s+are\s+(the\s+)?(best|number\s*one)
      | five[- ]star | 5[- ]star
      | michelin
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)
"""Rankings, awards and reputation the system has no record of.

Scoped to CLAIMS rather than words. An adversarial pass found the earlier
version blocking a restaurant's own dish names — "World Famous Chicken
Karahi", "The Legendary Lahori Nihari", "Best of Punjab Platter" — and
blocking them PERMANENTLY, because a rejection parks and a replay re-runs
the same facts. The guard was refusing the model for repeating a name the
restaurant chose. So `legendary`, `famous`, `renowned` and bare `rated` are
gone, and what remains needs the shape of an assertion: "award-winning",
"voted the …", "rated the best", "we are the best", "five-star".

"Award-winning" is either true and verifiable or false and actionable, and
nothing in a draft's inputs can tell us which. A restaurant that really has
an award can write that sentence themselves — FR-93 gives them the edit box
to do it in.
"""

_CONTACT = re.compile(
    r"""(
        [\w.+-]+@[\w-]+\.[\w.]+              # an email address
      # A phone number: at least nine digits in the run, so a date range
      # ("12.09.2026 - 15.09.2026", which matched as "2026 - 15") is not
      # mistaken for one. Separators are spaces, hyphens and parens only —
      # dots belong to dates and domains.
      | \b\+?\d(?:[\s()-]*\d){8,}\b
      | \b[\w-]+\.(?:com|co|net|org|pk|io|shop|store|co\.uk)\b   # a bare domain
      | https?://\S+
    )""",
    re.IGNORECASE | re.VERBOSE,
)
"""Addresses and numbers the model made up.

A support email in menu copy was found in B5's rewrite guard too. Here it
is worse: engagement copy goes to customers, and an invented phone number
is one somebody else answers.
"""

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("testimonial", _TESTIMONIAL),
    ("accolade", _ACCOLADE),
    ("contact_detail", _CONTACT),
)


@dataclass(frozen=True)
class Claim:
    """What was rejected, and which rule caught it.

    The rule matters as much as the rejection: "the model keeps writing
    testimonials" is a prompt change, while a rejection rate is a number
    nobody can act on.
    """

    rule: str
    matched: str


def unsupportable(text: str) -> Claim | None:
    """The first claim this copy makes that nothing can support, or None."""
    for rule, pattern in RULES:
        found = pattern.search(text)
        if found:
            return Claim(rule=rule, matched=found.group(0)[:80])
    return None
