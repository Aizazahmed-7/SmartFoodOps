"""Grounding (ADR-0043 §1–3, FR-70).

The check runs in code after the model has spoken, so these tests are the
enforcement rather than a description of it. The cases that matter are the
adversarial ones: an id that was never retrieved, a marker the model
mangled, and an answer that cites nothing at all.
"""

from ai_assistant.domain.grounding import MARKER, Grounded, Stripper, render_candidates, validate

ALLOWED = ["itm_karahi", "itm_raita"]


# ── the prompt side ─────────────────────────────────────────────────


def test_candidates_are_rendered_with_citable_markers():
    rendered = render_candidates(
        [("itm_karahi", "Chicken Karahi\nWok-cooked."), ("itm_raita", "Raita")]
    )
    assert "[item:itm_karahi] Chicken Karahi" in rendered
    assert "[item:itm_raita] Raita" in rendered


def test_a_marker_carries_nothing_but_an_id():
    """There is nothing in a marker for a model to paraphrase incorrectly,
    because there is nothing in it: no price, no availability, no
    restaurant."""
    rendered = render_candidates([("itm_karahi", "Chicken Karahi 899c available")])
    assert MARKER.findall(rendered) == ["itm_karahi"]


def test_no_candidates_renders_empty():
    assert render_candidates([]) == ""


# ── the check ───────────────────────────────────────────────────────


def test_a_cited_id_that_was_retrieved_becomes_a_card():
    result = validate("Try the [item:itm_karahi] — it's rich.", ALLOWED)
    assert result.item_ids == ["itm_karahi"]
    assert result.grounded and result.dropped == 0


def test_an_invented_id_never_reaches_a_customer():
    """THE test. The model cites a dish that was never retrieved; the link
    dies here regardless of what the prompt asked for."""
    result = validate("Try the [item:itm_nope] Pepperoni Pizza.", ALLOWED)
    assert result.item_ids == []
    assert result.dropped == 1
    assert not result.grounded
    assert "itm_nope" not in result.text


def test_a_fabricated_citation_degrades_the_sentence_not_the_turn():
    """Refusing to answer over one bad id converts a cosmetic model error
    into an outage, and the customer loses an answer that was probably
    fine."""
    result = validate("The [item:itm_karahi] is great, and so is the [item:itm_ghost].", ALLOWED)
    assert result.item_ids == ["itm_karahi"]  # the good one survives
    assert result.dropped == 1
    assert "is great" in result.text


def test_markers_never_appear_in_what_a_customer_reads():
    """They are a machine artifact on both paths — valid or not."""
    result = validate("[item:itm_raita] is light. [item:itm_nope] is not.", ALLOWED)
    assert "[item:" not in result.text
    assert MARKER.findall(result.text) == []


def test_stripping_leaves_readable_prose():
    result = validate("Try the [item:itm_karahi] tonight .", ALLOWED)
    assert result.text == "Try the tonight."


def test_a_repeated_citation_yields_one_card():
    result = validate("[item:itm_karahi] is rich. I'd order the [item:itm_karahi] again.", ALLOWED)
    assert result.item_ids == ["itm_karahi"]


def test_citation_order_is_the_order_a_customer_reads():
    result = validate("First [item:itm_raita], then [item:itm_karahi].", ALLOWED)
    assert result.item_ids == ["itm_raita", "itm_karahi"]


def test_an_answer_with_no_citations_is_grounded():
    """ "Nothing nearby matches" is a legitimate, fully grounded answer
    (UC-18's empty-retrieval case) — it claims nothing."""
    result = validate("Nothing nearby matches that tonight.", ALLOWED)
    assert result.grounded and result.item_ids == [] and result.dropped == 0


def test_an_empty_candidate_set_grounds_nothing():
    result = validate("Try the [item:itm_karahi].", [])
    assert result.dropped == 1 and result.item_ids == []


def test_every_invented_citation_is_counted_not_just_the_first():
    """The count is the alerting signal, so it has to be the real number."""
    result = validate("[item:a] [item:b] [item:itm_raita] [item:c]", ALLOWED)
    assert result.dropped == 3 and result.item_ids == ["itm_raita"]


def test_a_mangled_marker_is_left_as_text_and_cites_nothing():
    """A model that writes `[item: itm_karahi]` or `(item:x)` has not cited
    anything. Being liberal about the format would mean guessing what it
    meant — which is how a validator starts accepting what it cannot
    verify."""
    result = validate("Try the [item: itm_karahi] or (item:itm_raita).", ALLOWED)
    assert result.item_ids == [] and result.dropped == 0


def test_multiline_answers_keep_their_shape():
    result = validate("Options:\n- [item:itm_karahi] rich\n- [item:itm_raita] light", ALLOWED)
    assert result.item_ids == ["itm_karahi", "itm_raita"]
    assert result.text.splitlines() == ["Options:", "- rich", "- light"]


def test_the_dataclass_is_frozen():
    """A validated answer that can be edited afterwards is a validated
    answer in name only."""
    result = validate("ok", ALLOWED)
    assert isinstance(result, Grounded)
    try:
        result.text = "tampered"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Grounded must be immutable")


# ── stripping a STREAM ──────────────────────────────────────────────


def _stream(*chunks: str) -> str:
    stripper = Stripper()
    return "".join(stripper.push(c) for c in chunks) + stripper.flush()


def test_a_marker_split_across_chunks_is_still_removed():
    """The case from the first live generation. A provider splits wherever
    its tokenizer likes — `" [item:itm_e8d9…] R"` then `"aita, which…"` —
    so stripping each chunk on its own strips nothing at all."""
    assert (
        _stream("For a", " light option, I recommend the", " [item:itm_e8", "d9] R", "aita.")
        == "For a light option, I recommend the Raita."
    )


def test_a_marker_arriving_one_character_at_a_time_is_removed():
    assert _stream(*"Try [item:itm_x] Raita.") == "Try Raita."


def test_text_is_released_as_soon_as_it_cannot_be_a_marker():
    """Only the tail that could still GROW into a marker is held. Holding
    more would stall the stream behind punctuation."""
    stripper = Stripper()
    assert stripper.push("Raita is light. [") == "Raita is light. "
    assert stripper.push("1] is a footnote") == "[1] is a footnote"


def test_a_bracket_that_never_becomes_a_marker_survives():
    assert _stream("a cost of [", "10] rupees") == "a cost of [10] rupees"


def test_an_unterminated_marker_is_text_not_a_truncation():
    """A model that stops mid-marker left real characters in the buffer.
    Dropping them would silently truncate the last sentence of an answer."""
    assert _stream("Try the [item:itm_ab") == "Try the [item:itm_ab"


def test_a_chunk_that_is_nothing_but_a_marker_emits_nothing():
    stripper = Stripper()
    assert stripper.push("[item:itm_x] ") == ""
    assert stripper.flush() == ""


def test_the_space_after_a_marker_goes_with_it():
    """Otherwise the live text carries a double space that the final
    `validate` pass would have collapsed — the same answer, rendered two
    different ways depending on whether you watched it or reloaded it."""
    assert _stream("the [item:itm_x] Raita") == "the Raita"
