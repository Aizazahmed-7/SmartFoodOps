"""Building taste profiles, offline (FR-75).

One pass over everyone who has ordered recently. Rebuilt whole rather than
updated incrementally, and that is the same reasoning the fact tables use:
an incremental profile is a counter, a counter cannot absorb a replayed
order, and a taste profile that drifts is one nobody can debug because
there is no state to compare against. A full rebuild is its own retry
policy — running it twice is running it once.
"""

from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.features import FeatureRepo
from .domain.taste import WINDOW, build

log = get_logger("ai-assistant.profiles")


async def build_profiles(
    sessions: async_sessionmaker[AsyncSession],
    *,
    model_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebuild every recent customer's profile. Returns a summary.

    Per-user transactions rather than one big one: a hundred thousand
    profiles in a single transaction is a lock held for the whole pass, and
    a failure halfway through would throw away the half that worked. Each
    profile is independent, so each gets its own commit and a crash costs
    the pass, not the progress.
    """
    at = now or datetime.now(UTC)
    since = at - WINDOW
    async with sessions() as session:
        users = await FeatureRepo(session).users_with_history(since=since)

    built = 0
    for user_id in users:
        async with sessions() as session:
            repo = FeatureRepo(session)
            rows = await repo.taste_rows(user_id=user_id, since=since, model_version=model_version)
            if not rows:
                # `active` is read once at the top of the pass. If a reindex
                # lands mid-pass the old version's chunks go away, every
                # remaining user reads zero rows, and an unguarded write
                # would upsert an EMPTY profile over a good one — costing
                # those customers personalisation until the next pass
                # (B4 review). Keeping the stale profile is strictly better
                # than replacing it with nothing.
                continue
            profile = build(rows)
            # Browsing is the weaker signal and enters as familiarity only:
            # a view says somebody looked at a RESTAURANT, not that they
            # wanted a dish (FR-75).
            viewed = await repo.viewed_restaurants(user_id=user_id, since=since)
            if viewed:
                familiar = dict(profile.restaurants)
                for restaurant_id in viewed:
                    familiar[restaurant_id] = familiar.get(restaurant_id, 0) + 1
                profile = type(profile)(
                    cuisines=profile.cuisines,
                    tags=profile.tags,
                    restaurants=familiar,
                    ordered=profile.ordered,
                    orders=profile.orders,
                )
            await repo.save_profile(user_id=user_id, profile=profile, now=at)
            await session.commit()
            built += 1
    log.info("taste profiles built", users=len(users), built=built)
    return {"users": len(users), "built": built}


class ProfileBuilder:
    """The periodic rebuild, on the service's own loop.

    On the loop rather than on Celery, unlike the reindex, and the
    difference is what each job costs: a reindex is hundreds of provider
    calls and wants its own queue and DLQ; this is a handful of indexed
    queries and no network at all. Putting it on a broker would add a
    dependency to the one job that exists to keep a read path fast.

    A failed pass is logged and the loop continues. The profiles it did not
    rebuild are the profiles from the last pass, which are stale rather than
    wrong — and stopping the loop over one bad pass would make every
    subsequent profile stale instead.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        interval_s: float,
        model_version: str,
    ) -> None:
        self._sessions = sessions
        self._interval = interval_s
        self._model_version = model_version

    async def run(self) -> None:  # pragma: no cover — the loop; the pass is tested
        import asyncio

        while True:
            try:
                await build_profiles(self._sessions, model_version=self._model_version)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("profile pass failed — retrying next interval", error=str(exc))
            await asyncio.sleep(self._interval)
