"""The evaluation rubric and its report (FR-104, NFR-31).

B0 promised a harness skeleton and did not deliver one; B1 closes that. What
lands here is the SHAPE — the rubrics, the scoring, and the one distinction
that makes the whole thing worth running — not the cases, which arrive with
the milestones that can answer them.

**A rubric with no cases is `n/a`, never `pass`.** That is the entire point
of this file. A suite that silently scores 100% on an empty golden set is
worse than no suite: it reports health it has not measured, and the first
person to read a green nightly run will believe the assistant is grounded
when nothing has ever checked. `Rubric.status` therefore has three values,
and `make eval` exits non-zero on `fail` while printing `n/a` loudly.

Why nightly and not per-PR (FR-104): every case here costs provider calls
and wall-clock, and the failure signal is a REGRESSION against a moving
baseline rather than a broken build. Running it per-PR would make it the
slowest, flakiest gate in the repo and teach everyone to skip it.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "golden"

RUBRICS = {
    "retrieval": "recall@k and MRR over vague queries (FR-64)",
    "groundedness": "every item id in an answer came from the retrieved set (FR-70)",
    "refusals": "allergen and medical questions refused with a hand-off (FR-72)",
    "injection": "instructions planted in retrieved text are not followed (FR-71)",
    "assistance": "what-is-in-it / how-spicy / what-goes-with answered from declared data (FR-78)",
}


@dataclass(frozen=True)
class Case:
    """One golden case. `id` is stable and referenced from incident notes —
    renaming one breaks the trail from "this went wrong once" to "this is
    checked now"."""

    id: str
    rubric: str
    passed: bool | None = None


@dataclass
class Rubric:
    name: str
    description: str
    cases: list[Case] = field(default_factory=list)
    # Why a rubric could not be measured — "the stack is not running" is a
    # legitimate state for a developer's machine and must look like neither
    # a pass nor a failure.
    skipped: str | None = None

    @property
    def status(self) -> str:
        if self.skipped or not self.cases:
            return "n/a"
        return "pass" if all(case.passed for case in self.cases) else "fail"

    @property
    def score(self) -> str:
        if self.skipped:
            return self.skipped
        if not self.cases:
            return "0 cases"
        passed = sum(1 for case in self.cases if case.passed)
        return f"{passed}/{len(self.cases)}"


def load(golden: Path = GOLDEN) -> list[Rubric]:
    """Read the golden set. A missing file is an empty rubric rather than an
    error: the directory grows one rubric at a time, and a milestone that has
    not started should report `n/a`, not crash the nightly run."""
    rubrics: list[Rubric] = []
    for name, description in RUBRICS.items():
        path = golden / f"{name}.json"
        raw = json.loads(path.read_text()) if path.exists() else []
        rubrics.append(
            Rubric(
                name=name,
                description=description,
                cases=[Case(id=entry["id"], rubric=name) for entry in raw],
            )
        )
    return rubrics


def render(rubrics: Sequence[Rubric]) -> str:
    width = max(len(r.name) for r in rubrics)
    lines = [f"{'rubric'.ljust(width)}  status  score      description", "-" * 78]
    for rubric in rubrics:
        lines.append(
            f"{rubric.name.ljust(width)}  {rubric.status.ljust(6)}  "
            f"{rubric.score.ljust(9)}  {rubric.description}"
        )
    pending = [r.name for r in rubrics if r.status == "n/a"]
    if pending:
        lines.append("")
        lines.append(
            f"NOT MEASURED: {', '.join(pending)} — these rubrics have no cases yet. "
            "An empty rubric is not a passing one."
        )
    return "\n".join(lines)


def exit_code(rubrics: Sequence[Rubric], *, strict: bool) -> int:
    """1 = a rubric regressed. 2 = a rubric could not be measured. 0 = clean.

    Two codes and not one, because "the assistant got worse" and "nobody
    checked" need different responses and look identical from a red build.

    `strict` is what the NIGHTLY runs with, and it closes the last hole in
    this file's own argument. Printing `n/a` loudly is only loud if someone
    reads it; a scheduled job that exits 0 when every rubric skipped is a
    green check beside the words "not measured", and the green wins. A run
    that measured nothing must not be able to report health.
    """
    if any(rubric.status == "fail" for rubric in rubrics):
        return 1
    if strict and any(rubric.status == "n/a" for rubric in rubrics):
        return 2
    return 0


def main() -> int:  # pragma: no cover — the CLI shell; load/render are tested
    import asyncio
    import os

    strict = os.environ.get("EVAL_STRICT", "") not in ("", "0", "false")
    rubrics = load()
    scored, detail = asyncio.run(_measure(rubrics))
    print(render(rubrics))
    if detail:
        print()
        print(detail)
    code = exit_code(scored, strict=strict)
    if code == 2:
        print()
        print("STRICT: a rubric could not be measured — this run proves nothing.")
    return code


async def _measure(rubrics: list[Rubric]) -> tuple[list[Rubric], str]:  # pragma: no cover — live
    """Fill in the rubrics that need a running stack. Everything else keeps
    whatever `load()` said.

    Every rubric is measured independently, and a skip in one does not stop
    the others: "retrieval could not be scored because search fell back" is
    a different fact from "refusals regressed", and a run that abandoned the
    remaining rubrics on the first skip would hide the second behind the
    first.
    """
    import os

    import httpx

    from . import assistant, retrieval

    by_name = {rubric.name: rubric for rubric in rubrics}
    catalog_url = os.environ.get("CATALOG_URL", "http://localhost:8002")
    assistant_url = os.environ.get("ASSISTANT_URL", "http://localhost:8013")
    sections: list[str] = []

    async with httpx.AsyncClient() as client:
        target = by_name["retrieval"]
        scored, reason = await retrieval.run(
            client,
            catalog_url=catalog_url,
            assistant_url=assistant_url,
            cases=retrieval.load_cases(),
        )
        if reason:
            target.cases, target.skipped = [], reason
        else:
            target.cases = [Case(id=s.id, rubric="retrieval", passed=s.passed) for s in scored]
            sections.append("retrieval\n" + retrieval.render(scored))

        for name in assistant.RUBRICS:
            rubric = by_name[name]
            verdicts, why = await assistant.run(
                client,
                assistant_url=assistant_url,
                rubric=name,
                cases=assistant.load_cases(name),
            )
            if why:
                rubric.cases, rubric.skipped = [], why
                continue
            rubric.cases = [Case(id=v.id, rubric=name, passed=v.passed) for v in verdicts]
            sections.append(f"{name}\n" + assistant.render(verdicts))

    return rubrics, "\n\n".join(sections)
