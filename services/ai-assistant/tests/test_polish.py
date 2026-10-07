"""The model may change words, never facts (FR-83).

`accept()` is the guarantee; the prompt is only a request. These are the
ways a well-meaning model makes an explanation wrong, and each one has to
be rejected rather than repaired — a silently patched rewrite would hide
how often this is going astray.
"""

import pytest
from ai_assistant.domain.explain import ReasonCode
from ai_assistant.domain.polish import MAX_CHARS, accept, prompt_for, review

ORIGINAL = "Your food has been ready for {elapsed} and we're still finding a courier."


def test_a_faithful_rewrite_is_kept():
    better = "Your meal has been sitting ready for {elapsed} while we look for a courier."
    assert accept(ORIGINAL, better) == better


def test_surrounding_whitespace_is_trimmed():
    assert accept(ORIGINAL, f"  {ORIGINAL}  ") == ORIGINAL


def test_an_invented_number_is_rejected():
    """The failure this guard exists for. Every figure a customer reads
    arrives through a placeholder, so a digit the original did not have is
    a number nobody observed — here, an estimate for a stage the system
    cannot bound at all."""
    assert (
        accept(
            ORIGINAL,
            "Your food has been ready for {elapsed}; a courier should arrive in 10 minutes.",
        )
        is None
    )


def test_a_digit_is_allowed_when_the_original_had_one():
    original = "The kitchen is at capacity — {active} of {capacity}, its 1st busy hour today."
    candidate = "The kitchen is full — {active} of {capacity} — in its 1st busy hour."
    assert accept(original, candidate) == candidate


def test_numbers_inside_placeholders_do_not_trip_the_guard():
    """`{active} of {capacity}` renders as "8 of 8" and must stay
    rewritable — the check runs on the template, with placeholders
    removed."""
    original = "The kitchen is at capacity — {active} of {capacity} orders."
    candidate = "The kitchen is handling {active} of {capacity} orders and cannot take more."
    assert accept(original, candidate) == candidate


def test_a_dropped_placeholder_is_rejected():
    """It would lose the only fact in the sentence."""
    assert accept(ORIGINAL, "Your food is ready and we're still finding a courier.") is None


def test_a_duplicated_placeholder_is_rejected():
    assert accept(ORIGINAL, "Ready for {elapsed} — yes, {elapsed} — and still no courier.") is None


def test_an_invented_placeholder_is_rejected():
    """`format_map` would raise on it, and the fallback would swallow a
    perfectly good template."""
    assert accept(ORIGINAL, "Your food from {restaurant} is ready ({elapsed}).") is None


def test_a_stray_brace_is_rejected():
    assert accept(ORIGINAL, "Ready for {elapsed} } and still looking.") is None
    assert accept(ORIGINAL, "Ready for {elapsed} { and still looking.") is None


def test_an_empty_or_whitespace_rewrite_is_rejected():
    assert accept(ORIGINAL, "") is None
    assert accept(ORIGINAL, "   \t ") is None


def test_a_rewrite_that_became_a_paragraph_is_rejected():
    long = "Your food has been ready for {elapsed}. " + "We are so very sorry. " * 30
    assert len(long) > MAX_CHARS
    assert accept(ORIGINAL, long) is None


def test_a_multiline_rewrite_is_rejected():
    """The surface is one line on an order page."""
    assert accept(ORIGINAL, "Your food is ready for {elapsed}.\n- still looking") is None


# The adversarial pass that found these had them ALL accepted. The claim
# in this file used to be "the damage is tone, not truth"; it was false.
@pytest.mark.parametrize(
    ("original", "candidate", "rule"),
    [
        (
            "The restaurant has had your order for {elapsed} and hasn't accepted it yet.",
            "The restaurant has had your order for {elapsed}; they usually accept "
            "within another five minutes.",
            "invented_word_number",
        ),
        (
            "The restaurant has had your order for {elapsed} and hasn't accepted it yet.",
            "Good news - the restaurant has had your order for {elapsed} and has now accepted it.",
            "negation_lost",
        ),
        (
            "Your food is ready and we're finding a courier to collect it.",
            "Your food is ready and a courier will be there in about half an hour.",
            "invented_word_number",
        ),
        (
            "We couldn't complete this order and cancelled it. {money} Please try again.",
            "{money} We cancelled this order because the restaurant refused it. Please try again.",
            "negation_lost",
        ),
        (
            "Your order was delivered.",
            "Your order was delivered - email refunds@example.com if anything was missing.",
            "grew_too_much",
        ),
    ],
)
def test_the_rewrites_an_adversarial_pass_got_through(original, candidate, rule):
    """Each of these was accepted before the guards were widened: an
    invented estimate spelled in words, an inverted meaning, a delivery-time
    promise, an invented cause, and an invented support channel."""
    verdict = review(original, candidate)
    assert verdict.kept is None
    assert verdict.rule == rule


def test_an_invented_cause_is_rejected_even_without_a_negation():
    """The resolver owns the cause. A rewrite may not attach one — least of
    all to a specific business that did nothing wrong."""
    verdict = review(
        "Your order was cancelled. {money}",
        "Your order was cancelled because the restaurant was too busy. {money}",
    )
    assert verdict.rule == "invented_cause"


def test_a_faithful_rewrite_that_keeps_polarity_survives():
    """The guards must not be so blunt that nothing passes — the point is
    better copy, not no copy."""
    original = "The restaurant has had your order for {elapsed} and hasn't accepted it yet."
    candidate = "The restaurant has held your order for {elapsed} and hasn't confirmed it yet."
    assert review(original, candidate).kept == candidate


def test_tone_padding_remains_unguarded_and_that_is_the_line():
    """Still honest about a real limit, now a narrower one. This invents no
    fact, keeps polarity, adds no cause and barely grows — the damage is
    voice, not truth."""
    chatty = "Your food is ready and we are still looking for a courier for you."
    assert accept("Your food is ready and we're finding a courier to collect it.", chatty)


def test_the_prompt_names_the_situation_and_the_sentence():
    prompt = prompt_for(ReasonCode.AWAITING_COURIER, ORIGINAL)
    assert "awaiting_courier" in prompt
    assert ORIGINAL in prompt


# ── which rule fired ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("candidate", "rule"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Ready for {elapsed}. " + "Sorry. " * 80, "too_long"),
        ("Ready for {elapsed}.\nStill looking.", "multiline"),
        ("Your food is ready and we're still looking.", "placeholders_changed"),
        ("Ready for {elapsed} and {elapsed}.", "placeholders_changed"),
        ("Ready for {elapsed} from {restaurant}.", "placeholders_changed"),
        ("Ready for {elapsed} } still looking.", "stray_brace"),
        ("Ready for {elapsed}; a courier arrives in 10 minutes.", "invented_number"),
    ],
)
def test_a_rejection_names_the_rule_that_caught_it(candidate, rule):
    """A rejection RATE is a number nobody can act on; "the model keeps
    inventing durations" is a prompt change. Rejections are the only
    feedback this layer gets."""
    verdict = review(ORIGINAL, candidate)
    assert verdict.kept is None
    assert verdict.rule == rule


def test_an_accepted_rewrite_names_no_rule():
    verdict = review(ORIGINAL, "Your meal has been ready {elapsed}; still finding a courier.")
    assert verdict.kept is not None and verdict.rule is None
