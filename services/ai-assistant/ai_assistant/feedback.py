"""The feedback tab: raw rows always, a summary only when earned (FR-92).

Two reads and one job, and the shape is the whole point.

**The rows are always available.** UC-28's floor — fewer than N reviews
shows the raw rows and no summary — is not a degraded mode. It is the
honest answer at that size: three reviews do not have themes, they have
three opinions, and the rows say more than any sentence about them could.
Above the floor the rows are still shown; the summary sits beside them.

**Every number comes from the rows.** The count, the average, how many
carried a comment — all computed here. The model contributes themes and
quotes and nothing else, so there is no statistic anywhere that a model
wrote.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .domain.summaries import MIN_ROWS


@dataclass(frozen=True)
class FeedbackRow:
    order_id: str
    rating: int
    comment: str | None
    submitted_at: str


@dataclass(frozen=True)
class FeedbackCounts:
    """The figures an admin reads, every one of them derived from rows."""

    reviews: int
    with_comment: int
    average_rating: float
    ratings: dict[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reviews": self.reviews,
            "with_comment": self.with_comment,
            "average_rating": self.average_rating,
            "ratings": {str(star): self.ratings.get(star, 0) for star in range(1, 6)},
        }


def counts_for(rows: Sequence[FeedbackRow]) -> FeedbackCounts:
    if not rows:
        return FeedbackCounts(reviews=0, with_comment=0, average_rating=0.0, ratings={})
    ratings: dict[int, int] = {}
    for row in rows:
        ratings[row.rating] = ratings.get(row.rating, 0) + 1
    return FeedbackCounts(
        reviews=len(rows),
        with_comment=sum(1 for row in rows if (row.comment or "").strip()),
        # One decimal. A mean to six places implies a precision that
        # twenty reviews do not have.
        average_rating=round(sum(row.rating for row in rows) / len(rows), 1),
        ratings=ratings,
    )


def summarisable(rows: Sequence[FeedbackRow]) -> bool:
    """Is there enough here to find a theme in?

    Counted on rows WITH COMMENTS, not on reviews: a hundred ratings and no
    sentences is a corpus with nothing to summarise, and asking a model to
    find themes in it would get themes invented from star counts.
    """
    return sum(1 for row in rows if (row.comment or "").strip()) >= MIN_ROWS
