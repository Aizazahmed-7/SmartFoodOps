"""Copy that claims what nothing can support (FR-90).

FR-90 names fabricated claims and fabricated testimonials specifically, and
they are what a model reaches for unprompted when asked to make a business
sound appealing.
"""

import pytest
from ai_assistant.domain.claims import unsupportable


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('One guest told us "this is the best karahi I have eaten".', "testimonial"),
        ("Customers say our naan is the highlight of the meal.", "testimonial"),
        ("Diners love the slow-cooked mutton.", "testimonial"),
        ("Our regulars can't get enough of it.", "testimonial"),
        ("Our award-winning biryani is back on the menu.", "accolade"),
        ("Voted the finest kebab house in the sector.", "accolade"),
        ("The best in Islamabad, three years running.", "accolade"),
        ("We are the best in the sector, three years running.", "accolade"),
        ("Our best-selling dish is back.", "accolade"),
        ("Questions? Write to hello@example.com.", "contact_detail"),
        ("Call us on +92 51 234 5678 to book.", "contact_detail"),
        ("Order at https://example.com/offer.", "contact_detail"),
    ],
)
def test_the_shapes_fr90_names_are_refused(text, rule):
    claim = unsupportable(text)
    assert claim is not None, text
    assert claim.rule == rule


@pytest.mark.parametrize(
    "text",
    [
        "Half price on every karahi this Tuesday.",
        "Freshly baked naan brushed with garlic butter.",
        "We are open again from Monday, with the full menu back.",
        "Spiced minced meat skewers grilled over charcoal.",
        'Try our "Tangy Jalapeno" burger.',
    ],
)
def test_ordinary_copy_passes(text):
    """The guard must not be so blunt that nothing ships. The last case is
    a dish NAME in quotes, which is not a testimonial — the quoted-speech
    rule has a length floor precisely so a name is not mistaken for one."""
    assert unsupportable(text) is None


def test_the_rule_and_the_matched_text_both_come_back():
    """A rejection RATE is a number nobody can act on; "the model keeps
    writing testimonials" is a prompt change."""
    claim = unsupportable("Our award-winning biryani is back.")
    assert claim is not None
    assert claim.rule == "accolade"
    assert "award-winning" in claim.matched.lower()


def test_the_first_claim_wins_rather_than_the_longest():
    """One reason is enough to refuse, and reporting one keeps the message
    an operator reads short."""
    claim = unsupportable("Customers say we are award-winning. Call 0300 1234567.")
    assert claim is not None and claim.rule == "testimonial"


def test_an_invented_ingredient_is_not_caught_and_that_is_stated():
    """The honest limit, asserted rather than left in a docstring.

    "Served warm from the tandoor" on a dish whose facts never mentioned a
    tandoor passes every rule here, because "tandoor" is no more detectable
    than "delicious" without knowing how the dish is actually made. FR-93's
    human approval is the defence for this, not this module.
    """
    assert unsupportable("Served warm from the tandoor with fresh tomatoes.") is None


def test_a_hash_one_ranking_is_caught():
    """`#` opens a comment in a VERBOSE regex, so an unescaped `#1` made
    its whole alternation match the empty string — every piece of copy was
    rejected as an accolade until this was escaped."""
    assert unsupportable("Rated #1 for biryani in the sector.") is not None


def test_a_quoted_dish_name_is_not_a_testimonial():
    """The quoted arm needs three words inside the quotes — lowered from
    four after a review found "Zara S. called it \'best karahi ever\'"
    invisible, which is a fabricated testimonial attributed to a named
    person. A two-word dish name still passes."""
    assert unsupportable('Our "Karahi Special" is back on the menu.') is None
    assert unsupportable("A regular wrote 'best karahi ever here'.") is not None


def test_a_restaurants_own_dish_name_is_not_an_accolade():
    """Found by review: the guard blocked "World Famous Chicken Karahi"
    and "The Legendary Lahori Nihari" — a restaurant's own menu — and
    blocked them PERMANENTLY, because a rejection parks and a replay
    re-runs the same facts. It was refusing the model for repeating a name
    the restaurant chose."""
    for name in (
        "World Famous Chicken Karahi is back on the menu.",
        "The Legendary Lahori Nihari returns this weekend.",
        "Try our Best of Punjab Platter.",
        "Our chef rated this the spiciest option.",
    ):
        assert unsupportable(name) is None, name


def test_a_date_range_is_not_a_phone_number():
    """ "12.09.2026 - 15.09.2026" matched the phone arm as "2026 - 15"."""
    assert unsupportable("Pre-order for 12.09.2026 - 15.09.2026 delivery.") is None
    assert unsupportable("Call us on +92 51 234 5678 to book.") is not None


def test_a_bare_domain_is_a_contact_detail():
    """The arm required a scheme, and a bare domain is the form a model
    actually writes."""
    assert unsupportable("Order at quickserve.pk/offer today.") is not None
