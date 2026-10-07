"""Scoring the retrieval rubric (FR-64, FR-104).

The assertions that matter are the refusals: this suite must not produce a
number when it cannot stand behind one. `/v1/search` answers perfectly with
the assistant down — that is the fallback working — so a run that did not
check would grade the lexical path and report it as semantic.
"""

import httpx
import pytest
from eval.retrieval import load_cases, names_from, render, run, score_case


def _payload(*names):
    return {
        "results": [
            {
                "restaurant_id": "r1",
                "matched_items": [{"id": f"i{i}", "name": n} for i, n in enumerate(names)],
            }
        ]
    }


def _case(**over):
    return {
        "id": "c1",
        "query": "something light",
        "city": "springfield",
        "k": 3,
        "relevant": ["Raita"],
        **over,
    }


# ── scoring ─────────────────────────────────────────────────────────


def test_a_case_passes_only_when_every_relevant_dish_is_in_the_top_k():
    """Partial credit is reported and does not pass — the threshold is what
    stops a rubric drifting down one case at a time."""
    both = score_case(_case(relevant=["Raita", "Garlic Naan"]), _payload("Raita", "Garlic Naan"))
    assert both.recall == 1.0 and both.passed

    half = score_case(_case(relevant=["Raita", "Garlic Naan"]), _payload("Raita", "Chicken Karahi"))
    assert half.recall == 0.5 and not half.passed


def test_relevance_is_case_insensitive():
    assert score_case(_case(relevant=["raita"]), _payload("Raita")).passed


def test_mrr_is_how_far_the_customer_had_to_read():
    assert score_case(_case(), _payload("Raita")).reciprocal_rank == 1.0
    assert score_case(_case(), _payload("Karahi", "Raita")).reciprocal_rank == 0.5
    assert score_case(
        _case(), _payload("Karahi", "Naan", "Raita")
    ).reciprocal_rank == pytest.approx(1 / 3)


def test_nothing_relevant_scores_zero_rather_than_erroring():
    empty = score_case(_case(), _payload("Karahi"))
    assert empty.recall == 0.0 and empty.reciprocal_rank == 0.0 and not empty.passed


def test_results_past_k_do_not_count():
    """A dish at rank 9 is not an answer a customer found."""
    assert not score_case(_case(k=2), _payload("A", "B", "Raita")).passed


def test_a_dish_on_several_branches_is_counted_once():
    """A base item inherited by twelve branches would otherwise fill k by
    itself and flatter every score."""
    payload = {
        "results": [
            {"matched_items": [{"id": "i1", "name": "Sprite"}]},
            {"matched_items": [{"id": "i1", "name": "Sprite"}]},
            {"matched_items": [{"id": "i2", "name": "Coke"}]},
        ]
    }
    assert names_from(payload) == ["Sprite", "Coke"]


def test_an_empty_response_yields_no_names():
    assert names_from({}) == []
    assert names_from({"results": [{"matched_items": None}]}) == []


# ── the refusals ────────────────────────────────────────────────────


async def test_the_run_refuses_to_grade_the_fallback():
    """THE guard. With the assistant down, `/v1/search` still answers — that
    is HybridSearch doing its job — and scoring it would report the lexical
    path's numbers as the semantic path's."""

    def down(request):
        if request.url.path == "/readyz":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=_payload("Raita"))  # pragma: no cover — never reached

    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    scored, reason = await run(
        client, catalog_url="http://catalog", assistant_url="http://assistant", cases=[_case()]
    )
    assert scored == [] and reason is not None and "unreachable" in reason


async def test_an_unready_assistant_is_also_a_refusal():
    def unready(request):
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(unready))
    scored, reason = await run(
        client, catalog_url="http://catalog", assistant_url="http://assistant", cases=[_case()]
    )
    assert scored == [] and reason is not None and "503" in reason


async def test_no_cases_is_a_reason_not_a_score():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    scored, reason = await run(client, catalog_url="http://c", assistant_url="http://a", cases=[])
    assert scored == [] and reason == "no cases"


async def test_a_healthy_stack_is_scored():
    def stack(request):
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(
            200, json=_payload("Raita", "Garlic Naan"), headers={"X-Search-Path": "hybrid"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(stack))
    scored, reason = await run(
        client, catalog_url="http://catalog", assistant_url="http://assistant", cases=[_case()]
    )
    assert reason is None
    assert [s.id for s in scored] == ["c1"] and scored[0].passed


# ── the shipped golden set ──────────────────────────────────────────


def test_every_shipped_case_records_why_it_is_relevant():
    """A golden case without a rationale is unarguable: a future reader
    cannot disagree on the merits, only guess what was meant."""
    cases = load_cases()
    assert cases, "the retrieval golden set must not be empty"
    for case in cases:
        assert case["why"].strip(), case["id"]
        assert case["city"], case["id"]
        # Exactly one shape: dishes that MUST come back, or the set that MAY.
        # Both would be ambiguous about which metric grades the case, and
        # neither leaves nothing to compare against.
        named = [key for key in ("relevant", "acceptable") if case.get(key)]
        assert named == [named[0]] and len(named) == 1, case["id"]


def test_the_vague_cases_fr64_names_are_present():
    """FR-64 names 'something light' and 'something spicy' explicitly."""
    queries = {c["query"] for c in load_cases()}
    assert {"something light", "something spicy"} <= queries


def test_the_report_names_the_query_that_regressed():
    """An aggregate score tells you a rubric regressed and never WHICH query
    stopped working — so a failure prints what came back beside what a human
    expected, and a pass does not."""
    failing = score_case(_case(relevant=["Raita"]), _payload("Chicken Karahi"))
    passing = score_case(_case(relevant=["Raita"]), _payload("Raita"))
    text = render([failing, passing])
    assert "FAIL c1" in text
    assert "expected: Raita" in text
    assert "Chicken Karahi" in text
    assert "mean" in text


def test_the_report_says_nothing_rather_than_an_empty_list():
    """ "(nothing)" is a result; a blank column is a rendering bug."""
    assert "(nothing)" in render([score_case(_case(), {"results": []})])


def test_rendering_no_cases_omits_the_mean():
    assert "mean" not in render([])


async def test_a_reachable_but_unused_retriever_is_still_a_refusal():
    """THE regression this guard was rewritten for. When real embeddings
    pushed every query past the 150 ms timeout, HybridSearch fell back to
    lexical, `/v1/search` returned 200 throughout, and the suite scored the
    wrong system at 0.60 and called it the semantic path. Reachable is not
    the same as used."""

    def degraded(request):
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        # A perfectly good lexical answer, stamped as such.
        return httpx.Response(200, json=_payload("Raita"), headers={"X-Search-Path": "lexical"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(degraded))
    scored, reason = await run(
        client, catalog_url="http://catalog", assistant_url="http://assistant", cases=[_case()]
    )
    assert scored == []
    assert reason is not None and "fallback" in reason


async def test_a_missing_header_is_treated_as_a_fallback():
    """An older catalog, or a proxy that strips it: absence must fail
    closed. A guard that trusts a missing header is not a guard."""

    def unstamped(request):
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=_payload("Raita"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(unstamped))
    scored, reason = await run(
        client, catalog_url="http://catalog", assistant_url="http://assistant", cases=[_case()]
    )
    assert scored == [] and reason is not None


def test_a_vague_case_is_graded_on_precision_not_recall():
    """A case naming an ACCEPTABLE SET asks "was everything it offered
    relevant", which recall cannot express: five dishes declare `spicy`, so
    demanding two specific ones in the top 3 asserts retrieval should prefer
    a particular pair rather than that it understood the word."""
    case = {
        "id": "vague",
        "query": "something spicy",
        "city": "islamabad",
        "k": 3,
        "acceptable": ["Chapli Kebab", "Arrabbiata", "Tangy Jalapeno"],
    }
    scored = score_case(case, _payload("Tangy Jalapeno", "Arrabbiata", "Chapli Kebab"))
    assert scored.precision == 1.0 and scored.passed


def test_an_unacceptable_dish_in_the_top_k_fails_a_vague_case():
    case = {"id": "vague", "query": "light", "city": "islamabad", "k": 3, "acceptable": ["Raita"]}
    scored = score_case(case, _payload("Sprite", "Raita", "Coke"))
    assert scored.precision is not None and scored.precision < 1.0
    assert not scored.passed
    # The first acceptable dish was second, so MRR reports how far a
    # customer had to read before something useful appeared.
    assert scored.reciprocal_rank == 0.5


def test_a_vague_case_that_returns_nothing_scores_zero():
    case = {"id": "vague", "query": "light", "city": "islamabad", "k": 3, "acceptable": ["Raita"]}
    scored = score_case(case, {"results": []})
    assert scored.precision == 0.0 and not scored.passed
