"""Read-side aggregation — the domain owns the session, the repo owns SQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import AnalyticsRepo


def _ms(value: float | None) -> float | None:
    """Milliseconds to one decimal; None stays None (no data ≠ instant)."""
    return None if value is None else round(value, 1)


def _rate(part: int, whole: int) -> float | None:
    """None, not 0.0, when the denominator is empty: 'no data yet' and
    'perfectly zero' are different answers and dashboards must not
    conflate them."""
    return None if whole == 0 else round(part / whole, 4)


class AnalyticsService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    @staticmethod
    def _since(days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    async def ops_metrics(self, days: int) -> dict[str, Any]:
        """FR-43's buildable eight (rider utilization is blocked on the
        dispatch milestone — reported as null with a reason, never faked;
        'failed events' lives in Prometheus where the DLQ and workflow
        counters already are, so this answers with the pointer)."""
        since = self._since(days)
        async with self._sessions() as session:
            repo = AnalyticsRepo(session)
            counts = await repo.counts(since)
            per_restaurant = await repo.orders_per_restaurant(since, limit=10)
            peak = await repo.peak_hour(since)
            avg_delivery = await repo.avg_delivery_seconds(since)
        return {
            "window_days": days,
            "total_orders": counts["placed"],
            "orders_per_restaurant": per_restaurant,
            "peak_hour": peak,
            "avg_delivery_seconds": avg_delivery,
            "cancellation_rate": _rate(counts["cancelled"], counts["placed"]),
            "acceptance_rate": (
                None
                if counts["confirmed"] == 0
                else round(1 - counts["rejected"] / counts["confirmed"], 4)
            ),
            "delivery_success_rate": _rate(counts["delivered"], counts["confirmed"]),
            "revenue_cents": counts["revenue_cents"],
            "failed_events": "see prometheus: consumer_events_total{result='dlq'}",
            "rider_utilization": None,  # blocked on the dispatch milestone
        }

    async def restaurant_metrics(self, restaurant_id: str, days: int) -> dict[str, Any]:
        since = self._since(days)
        async with self._sessions() as session:
            repo = AnalyticsRepo(session)
            days_rows = await repo.daily(restaurant_id, since)
            counts = await repo.restaurant_counts(restaurant_id, since)
            lifetime = await repo.restaurant_lifetime(restaurant_id)
            funnel = await repo.funnel(restaurant_id, since)
        # AOV in integer cents, floor division — the house money rule. None
        # (not 0) when nothing has settled: "no sales yet" and "average of
        # zero" are different answers.
        aov = None if lifetime["settled"] == 0 else lifetime["revenue_cents"] // lifetime["settled"]
        return {
            "restaurant_id": restaurant_id,
            "window_days": days,
            "days": days_rows,
            "window": {
                "orders": counts["placed"],
                "settled": counts["settled"],
                "cancelled": counts["cancelled"],
            },
            "cancellation_rate": _rate(counts["cancelled"], counts["placed"]),
            "acceptance_rate": (
                None
                if counts["confirmed"] == 0
                else round(1 - counts["rejected"] / counts["confirmed"], 4)
            ),
            "totals": {
                **lifetime,
                "aov_cents": aov,
                "repeat_rate": _rate(lifetime["repeat_customers"], lifetime["customers"]),
            },
            # Conversion over SIGNED-IN viewers (anonymous views count toward
            # volume only); sampled at the emitter — a rate survives sampling.
            "funnel": {
                **funnel,
                "conversion_rate": _rate(funnel["converted_viewers"], funnel["viewers"]),
            },
        }

    async def ai_metrics(self, days: int) -> dict[str, Any]:
        """FR-95's six metrics, read from `assistant_facts` (FR-94).

        Five of the six come from the interaction facts alone. The sixth,
        order conversion after an interaction, joins across two fact tables
        with attribution rules of its own (FR-97) and is counted from both
        ends — see `assistant_conversion`, and note that `converted_turns`
        and `attributed_orders` are not meant to match.
        """
        since = self._since(days)
        async with self._sessions() as session:
            repo = AnalyticsRepo(session)
            totals = await repo.assistant_totals(since)
            outcomes = await repo.assistant_outcomes(since)
            returning = await repo.assistant_returning(since)
            acceptance = await repo.assistant_acceptance(since)
            conversion = await repo.assistant_conversion(since)
        turns, conversations = totals["turns"], totals["conversations"]
        # "Answered" means exactly the `answered` outcome. A refusal and a
        # no-match are both honest replies and neither answered anything;
        # folding them in would make the rate rise as the assistant got
        # less useful.
        answered = outcomes.get("answered", 0)
        return {
            "window_days": days,
            "usage": {
                "interactions": turns,
                "users": totals["users"],
                "conversations": conversations,
            },
            "questions": {
                "asked": turns,
                "answered": answered,
                "answer_rate": _rate(answered, turns),
                "by_outcome": outcomes,
            },
            # FR-97. `converted_turns` and `attributed_orders` are counted
            # from opposite ends and will not match: three turns about one
            # restaurant followed by one dinner are three turns that worked
            # and one order. Revenue hangs off the ORDER count, never the
            # turn count, or it is multiplied by however many questions the
            # customer asked first.
            "order_conversion": {
                "turns_naming_restaurants": conversion["naming"],
                "converted_turns": conversion["converted"],
                "conversion_rate": _rate(conversion["converted"], conversion["naming"]),
                "attributed_orders": conversion["orders"],
                "attributed_revenue_cents": conversion["revenue_cents"],
                "window_hours": conversion["window_hours"],
                # Said in the payload, not only in a docstring, because the
                # number travels further than the code does: this is
                # correlation inside a window. A customer who was going to
                # order anyway and asked a question first is counted here.
                "basis": "correlation within the window; not a controlled measurement",
            },
            "recommendations": {
                "turns_naming_items": acceptance["recommending"],
                "accepted": acceptance["accepted"],
                "acceptance_rate": _rate(acceptance["accepted"], acceptance["recommending"]),
                "window_hours": acceptance["window_hours"],
            },
            "response_time_ms": {
                "avg": _ms(totals["avg_ms"]),
                "avg_generated": _ms(totals["avg_generated"]),
                "avg_cached": _ms(totals["avg_cached"]),
                "cache_hit_rate": _rate(totals["cached"], turns),
                # The mean is what FR-95 asks for; the tail is what a
                # customer feels. Prometheus already keeps the histogram,
                # so this points at it instead of re-deriving a worse one.
                "percentiles": "see prometheus: assistant_response_seconds_bucket",
            },
            "engagement": {
                "turns_per_conversation": (
                    None if conversations == 0 else round(turns / conversations, 2)
                ),
                "returning_users": returning,
                "returning_rate": _rate(returning, totals["users"]),
            },
            # Not one of the six, read anyway: these are the columns 7.1
            # added for the grounding alert, and an unread column is how
            # `c1.assistant.events` sat unconsumed for four milestones.
            "grounding": {
                "candidates_retrieved": totals["candidates"],
                "dropped_ungrounded": totals["ungrounded"],
            },
        }

    async def restaurant_ai_metrics(self, restaurant_id: str, days: int) -> dict[str, Any]:
        """FR-98: what the assistant did for THIS owner, claim-scoped.

        The claim is normally a brand and a citation names a branch, so the
        scope goes through the branch→brand mapping rather than a column —
        see `_claim_cites`. There is no path parameter here for the same
        reason the FR-55 view has none: a cross-tenant read is not something
        to reject, it is something to leave unrepresentable.
        """
        since = self._since(days)
        async with self._sessions() as session:
            repo = AnalyticsRepo(session)
            views = await repo.restaurant_assistant_views(restaurant_id, since)
            conversion = await repo.restaurant_assistant_conversion(restaurant_id, since)
        return {
            "restaurant_id": restaurant_id,
            "window_days": days,
            # The AI-driven equivalent of a menu view: the assistant put
            # this restaurant in front of someone who was asking.
            "views": {
                "turns": views["turns"],
                "customers": views["customers"],
            },
            "conversions": {
                "converted_turns": conversion["converted"],
                "conversion_rate": _rate(conversion["converted"], conversion["naming"]),
                # Distinct orders, so three questions and one dinner are one
                # order here and the revenue beside it is not tripled.
                "attributed_orders": conversion["orders"],
                "attributed_revenue_cents": conversion["revenue_cents"],
                "window_hours": conversion["window_hours"],
            },
            "basis": "correlation within the window; not a controlled measurement",
        }
