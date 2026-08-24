"""Read-side aggregation — the domain owns the session, the repo owns SQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import AnalyticsRepo


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
