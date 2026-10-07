"""The one behaviour that matters in an empty harness: it must not claim to
have measured anything."""

import json

from eval.report import RUBRICS, Case, Rubric, load, render


def test_an_empty_rubric_is_not_a_passing_one():
    """A suite that scores 100% on zero cases reports health it has not
    measured — and the first person to read a green nightly run believes
    the assistant is grounded when nothing has ever checked."""
    empty = Rubric(name="groundedness", description="")
    assert empty.status == "n/a"
    assert empty.score == "0 cases"


def test_a_rubric_fails_if_any_case_fails():
    rubric = Rubric(
        name="injection",
        description="",
        cases=[Case("a", "injection", True), Case("b", "injection", False)],
    )
    assert rubric.status == "fail"
    assert rubric.score == "1/2"


def test_a_fully_passing_rubric_passes():
    rubric = Rubric(name="refusals", description="", cases=[Case("a", "refusals", True)])
    assert rubric.status == "pass" and rubric.score == "1/1"


def test_the_shipped_golden_set_loads_and_covers_every_rubric(tmp_path):
    assert {r.name for r in load()} == set(RUBRICS)


def test_a_missing_file_is_an_empty_rubric_not_a_crash(tmp_path):
    """The directory grows one rubric at a time; a milestone that has not
    started reports n/a rather than breaking the nightly run."""
    assert all(r.status == "n/a" for r in load(tmp_path))


def test_cases_are_read_from_the_golden_set(tmp_path):
    (tmp_path / "injection.json").write_text(json.dumps([{"id": "planted-instruction-1"}]))
    injection = next(r for r in load(tmp_path) if r.name == "injection")
    assert [c.id for c in injection.cases] == ["planted-instruction-1"]


def test_the_report_says_out_loud_what_was_not_measured(tmp_path):
    """Silence about an unmeasured rubric reads as a pass to everyone who
    skims the output.

    Built from an EMPTY golden directory rather than the real one: this
    used to read `load()` and passed only because B3's rubrics had no cases
    yet, so filling them in turned a behavioural assertion into a failure
    about the repo's progress."""
    rendered = render(load(tmp_path))
    assert "NOT MEASURED" in rendered
    assert "An empty rubric is not a passing one." in rendered


def test_a_full_golden_set_reports_nothing_as_unmeasured(tmp_path):
    """The other half of the same rule — once every rubric has cases, the
    warning must go away, or it becomes noise everyone learns to skip."""
    from eval.report import RUBRICS

    for name in RUBRICS:
        (tmp_path / f"{name}.json").write_text(json.dumps([{"id": f"{name}-1"}]))
    assert "NOT MEASURED" not in render(load(tmp_path))


def test_the_entry_point_imports_cleanly():
    """`make eval` runs this module; importing it must not execute the
    report, or the coverage gate could not measure the package."""
    import eval.__main__ as entry

    assert entry.main is not None


def test_a_skipped_rubric_reports_its_reason_not_a_score():
    """ "the assistant is unreachable" is a legitimate state for a
    developer's machine, and it must read as neither a pass nor a
    failure — the number it would otherwise print is one nobody can stand
    behind."""
    rubric = Rubric(
        name="retrieval", description="", skipped="assistant unreachable (ConnectError)"
    )
    assert rubric.status == "n/a"
    assert rubric.score == "assistant unreachable (ConnectError)"


# ── the nightly's exit code (NFR-31) ───────────────────────────────


def _rubric(name, *, passed=None, skipped=""):
    from eval.report import Case

    cases = [] if passed is None else [Case(id=f"{name}-1", rubric=name, passed=passed)]
    return Rubric(name=name, description="", cases=cases, skipped=skipped)


def test_a_regression_exits_one():
    from eval.report import exit_code

    rubrics = [_rubric("groundedness", passed=True), _rubric("refusals", passed=False)]
    assert exit_code(rubrics, strict=True) == 1
    assert exit_code(rubrics, strict=False) == 1


def test_a_clean_run_exits_zero():
    from eval.report import exit_code

    rubrics = [_rubric("groundedness", passed=True), _rubric("refusals", passed=True)]
    assert exit_code(rubrics, strict=True) == 0


def test_strict_refuses_to_call_an_unmeasured_run_healthy():
    """The last hole in this file's own argument. Printing `n/a` loudly only
    works if somebody reads it — and beside a green check, nobody does. The
    nightly runs strict so a scheduled job that measured nothing cannot
    report health."""
    from eval.report import exit_code

    rubrics = [_rubric("groundedness", passed=True), _rubric("refusals", skipped="unreachable")]
    assert exit_code(rubrics, strict=False) == 0  # the interactive default
    assert exit_code(rubrics, strict=True) == 2  # what the nightly runs


def test_a_regression_outranks_an_unmeasured_rubric():
    """Both wrong at once reports the REGRESSION: one is a thing that got
    worse, the other is a thing nobody looked at."""
    from eval.report import exit_code

    rubrics = [_rubric("refusals", passed=False), _rubric("injection", skipped="unreachable")]
    assert exit_code(rubrics, strict=True) == 1
