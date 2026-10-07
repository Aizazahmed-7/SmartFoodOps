"""The retrieval rubric: recall@k and MRR over the vague-query golden set
(FR-64, FR-104, NFR-31).

**It measures `/v1/search`, not the retriever.** The retriever is what we
built; search is what a customer experiences, and it is the composition —
fusion, hydration, the fallback — that can be wrong in ways no component
test would show. Scoring the customer path is the only way a regression in
any of those reaches this number.

**It refuses to score the fallback**, and the first version of this guard
was not enough. It checked only that the assistant was REACHABLE — so when
real embeddings arrived and every query began exceeding the 150 ms timeout,
`HybridSearch` fell back to lexical on all five cases, `/v1/search` returned
200 throughout, and the suite printed **0.60 as if it were the semantic
path's score**. A suite that silently grades the wrong system is worse than
no suite, and this one did exactly that once.

So the check is now positive: every scored response must carry the
`X-Search-Path: hybrid` header catalog stamps on results the assistant
actually produced. Reachable is not the same as used.

**Relevance is by dish NAME, judged by a human.** Item ids are minted per
environment, so a golden set keyed on them would be unrunnable anywhere but
the machine that wrote it. Names are stable, and the `why` on every case
records the judgement so a future reader can disagree with it on the merits
rather than guessing what was meant.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "retrieval.json"


@dataclass(frozen=True)
class Scored:
    id: str
    query: str
    recall: float
    reciprocal_rank: float
    returned: Sequence[str]
    expected: Sequence[str]

    # Set when the case names an ACCEPTABLE SET rather than a required one
    # — see `score_case`. Precision then replaces recall as the verdict.
    precision: float | None = None

    @property
    def passed(self) -> bool:
        """A case passes when EVERY dish a human called relevant is in the
        top k. Partial credit is reported (`recall`) but does not pass: the
        threshold is what stops a rubric drifting down one case at a time,
        and the number beside it is what tells you how far off it was.

        Unless the case named an acceptable SET, in which case everything
        returned has to be in it and nothing is required — see `score_case`
        for why a vague query needs that shape.
        """
        if self.precision is not None:
            return self.precision == 1.0
        return self.recall == 1.0


def load_cases(path: Path = GOLDEN) -> list[dict[str, Any]]:
    return json.loads(path.read_text()) if path.exists() else []


def names_from(payload: dict[str, Any]) -> list[str]:
    """Matched dish names in DISH rank order, de-duplicated.

    `/v1/search` returns restaurants, each carrying its matched items, and
    this used to flatten them in restaurant order. That made the rubric
    measure which RESTAURANT ranked first while every case in the golden set
    names dishes — a mismatch that could not show on the old single-
    restaurant corpus, because there was only one restaurant to rank.

    On a real city it dominates: RRF sums an item's score into its
    restaurant, so a kitchen with four weak matches outranks one with a
    single strong match, and the first k names all come from the wrong
    kitchen. Ranking by each item's OWN score is what the cases have always
    described.

    A dish inherited by several branches appears once per branch; counting
    it twice would let one popular item fill k and flatter the score.
    """
    scored: list[tuple[float, str]] = []
    for hit in payload.get("results") or []:
        for item in hit.get("matched_items") or []:
            scored.append((float(item.get("score", 0.0)), item["name"]))
    scored.sort(key=lambda pair: -pair[0])
    seen: list[str] = []
    for _score, name in scored:
        if name not in seen:
            seen.append(name)
    return seen


def score_case(case: dict[str, Any], payload: dict[str, Any]) -> Scored:
    """Score one case, by recall or by precision.

    Two shapes, because two kinds of question live in this golden set.

    `relevant` names dishes that MUST come back — right for a query with a
    definite answer ("biriani", "chapli kebab", "cold drink"), where missing
    one is a real failure.

    `acceptable` names the dishes that MAY come back, and the case passes
    only if everything in the top k is one of them. That is the shape a
    vague query needs. Five dishes on this menu declare `spicy`; asking for
    two specific ones in the top 3 asserts that retrieval should prefer MY
    two, which is not a defensible relevance judgement — it failed a case
    by returning a different, equally spicy dish. Measured, then fixed: the
    first Islamabad run marked `vague-spicy` failed for returning Tangy
    Jalapeno, which is declared spicy and which any customer would accept.
    """
    k = int(case.get("k", 5))
    returned = names_from(payload)
    top = [name.lower() for name in returned[:k]]

    if case.get("acceptable"):
        allowed = {name.lower() for name in case["acceptable"]}
        hits = [name for name in top if name in allowed]
        return Scored(
            id=case["id"],
            query=case["query"],
            recall=len(hits) / len(top) if top else 0.0,
            reciprocal_rank=1.0 / (top.index(hits[0]) + 1) if hits else 0.0,
            returned=returned[:k],
            expected=case["acceptable"],
            precision=len(hits) / len(top) if top else 0.0,
        )

    expected = [name.lower() for name in case["relevant"]]

    found = sum(1 for name in expected if name in top)
    # MRR over the FIRST relevant hit: what a customer experiences is how
    # far down the list they had to read before something useful appeared.
    reciprocal = 0.0
    for rank, name in enumerate(top, start=1):
        if name in expected:
            reciprocal = 1.0 / rank
            break
    return Scored(
        id=case["id"],
        query=case["query"],
        recall=found / len(expected) if expected else 0.0,
        reciprocal_rank=reciprocal,
        returned=returned[:k],
        expected=case["relevant"],
    )


async def run(
    client: httpx.AsyncClient, *, catalog_url: str, assistant_url: str, cases: list[dict[str, Any]]
) -> tuple[list[Scored], str | None]:
    """Score every case, or explain why nothing was scored.

    The second element is a REASON, not an error: "the stack is not running"
    and "the assistant is down" are both legitimate states for a developer's
    machine, and neither should look like a failing rubric or a passing one.
    """
    if not cases:
        return [], "no cases"
    try:
        health = await client.get(f"{assistant_url}/readyz", timeout=2.0)
        if health.status_code != 200:
            return [], f"assistant not ready (HTTP {health.status_code})"
    except httpx.HTTPError as exc:
        return [], f"assistant unreachable ({type(exc).__name__})"

    scored: list[Scored] = []
    for case in cases:
        response = await client.get(
            f"{catalog_url}/v1/search",
            params={"q": case["query"], "city": case["city"]},
            timeout=10.0,
        )
        response.raise_for_status()
        if response.headers.get("X-Search-Path") != "hybrid":
            # Answered, plausibly, by the wrong system. Scoring it would
            # report the lexical path's numbers under the semantic path's
            # name — the exact failure this rubric exists to catch.
            return [], f"'{case['query']}' was served by the fallback, not the retriever"
        scored.append(score_case(case, response.json()))
    return scored, None


def render(scored: Sequence[Scored]) -> str:
    """Per-case detail, because an aggregate score tells you a rubric
    regressed and never which query stopped working."""
    # "score" rather than "recall": a case naming an acceptable set is
    # graded on PRECISION, and a column headed `recall` showing a precision
    # would be a report that lies quietly.
    lines = [f"{'case':18} {'':4} {'score':>7} {'MRR':>6}  returned"]
    lines.append("-" * 78)
    for case in scored:
        mark = "ok " if case.passed else "FAIL"
        metric = "prec" if case.precision is not None else "rec "
        lines.append(
            f"{mark} {case.id:14} {metric} {case.recall:7.2f} {case.reciprocal_rank:6.2f}  "
            f"{', '.join(case.returned) or '(nothing)'}"
        )
        if not case.passed:
            label = "acceptable" if case.precision is not None else "expected"
            lines.append(f"{'':18} {'':4} {'':7} {'':6}  {label}: {', '.join(case.expected)}")
    if scored:
        mean_recall = sum(c.recall for c in scored) / len(scored)
        mean_mrr = sum(c.reciprocal_rank for c in scored) / len(scored)
        lines.append("-" * 78)
        lines.append(f"{'mean':18} {'':4} {mean_recall:7.2f} {mean_mrr:6.2f}")
    return "\n".join(lines)
