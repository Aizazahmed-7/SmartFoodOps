"""The content studio's job lifecycle (FR-88..FR-93, UC-25).

Everything a draft can do, in one place, so the state machine is readable
in one file rather than inferred from five call sites.

Three rules run through it:

**The row is written and committed BEFORE the job is enqueued.** A worker
that picks up a draft id must find a row. The reverse order has a real
window — enqueue, crash, commit never happens — and notification's receipts
learned it the same way.

**Generated text never reaches the catalog from here.** A draft lands in
`content_drafts` and stops. The only path to a customer is a human's
approve action (FR-93), and the absence of any other code path is what
makes "never a half-written menu" structural.

**`parked` is the dead-letter queue.** Not a broker artifact: a row, which
the restaurant whose copy never arrived can see, and which an operator
replays with an UPDATE rather than a console. Every permanent failure lands
there with a reason in words.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_otel import get_logger
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import content_drafts

log = get_logger("ai-assistant.drafts")


class DraftNotFound(Exception):
    """Unknown, or not this restaurant's. One answer, as everywhere else —
    confirming a draft exists to a rival's token is the leak."""


class WrongState(Exception):
    """The draft is real and theirs, but not in a state this action
    accepts. Safe to be specific about: the caller already owns it."""

    def __init__(self, status: str, action: str) -> None:
        self.status = status
        super().__init__(f"a draft in {status} cannot be {action}")


@dataclass(frozen=True)
class Claim:
    """Who is asking, as the token says it.

    A brand token may act for any of its branches (ADR-0028); a branch
    token only for itself. Carrying both and checking both is what makes a
    cross-tenant read unrepresentable rather than merely unlikely.
    """

    restaurant_id: str | None
    brand_id: str | None = None

    def owns(self, row: Row[Any]) -> bool:
        return (self.restaurant_id is not None and self.restaurant_id == row.restaurant_id) or (
            self.brand_id is not None and self.brand_id == row.brand_id
        )


class DraftStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        restaurant_id: str,
        brand_id: str | None,
        kind: str,
        target_id: str | None = None,
        request: str | None = None,
        subject: dict[str, Any] | None = None,
    ) -> str:
        """Write the row, commit, return its id. Enqueuing is the caller's
        next step and deliberately not this method's: a store that enqueued
        would be untestable without a broker, and the ordering guarantee
        this class exists to hold is exactly "committed first"."""
        draft_id = f"cdr_{uuid.uuid4().hex}"
        now = datetime.now(UTC)
        async with self._sessions() as session:
            await session.execute(
                content_drafts.insert().values(
                    draft_id=draft_id,
                    restaurant_id=restaurant_id,
                    brand_id=brand_id,
                    kind=kind,
                    target_id=target_id,
                    request=request,
                    subject=subject,
                    status="queued",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        return draft_id

    async def get(self, draft_id: str, claim: Claim) -> Row[Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    sa.select(content_drafts).where(content_drafts.c.draft_id == draft_id)
                )
            ).one_or_none()
        if row is None or not claim.owns(row):
            raise DraftNotFound
        return row

    async def load(self, draft_id: str) -> Row[Any] | None:
        """Unscoped, for the worker.

        No claim, because a Celery task has no token — it is the system
        acting on a job it was handed. Deliberately a different method from
        `get`, so the scoped and unscoped reads cannot be confused at a
        call site: everything a human reaches goes through `get`.
        """
        async with self._sessions() as session:
            return (
                await session.execute(
                    sa.select(content_drafts).where(content_drafts.c.draft_id == draft_id)
                )
            ).one_or_none()

    async def list_for(
        self,
        claim: Claim,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> Sequence[Row[Any]]:
        """The console's read. Scoped by the claim in the WHERE clause, not
        filtered afterwards — a cross-tenant row is never loaded, so it
        cannot be leaked by a later bug."""
        predicates = []
        if claim.restaurant_id is not None:
            predicates.append(content_drafts.c.restaurant_id == claim.restaurant_id)
        if claim.brand_id is not None:
            predicates.append(content_drafts.c.brand_id == claim.brand_id)
        if not predicates:
            # A token naming neither owns nothing. Returning everything
            # here would be the whole-table read this scoping prevents.
            return []
        where = sa.or_(*predicates)
        if status is not None:
            where = sa.and_(where, content_drafts.c.status == status)
        if kind is not None:
            where = sa.and_(where, content_drafts.c.kind == kind)
        async with self._sessions() as session:
            result = await session.execute(
                sa.select(content_drafts)
                .where(where)
                .order_by(content_drafts.c.created_at.desc())
                .limit(limit)
            )
            return result.all()

    async def latest(
        self, claim: Claim, *, kind: str, status: str | None = None
    ) -> Row[Any] | None:
        """The newest draft of one kind for this tenant, or None.

        A summary is regenerated rather than versioned, so "the summary" is
        always the most recent one that finished. Older rows stay for the
        same reason rejected drafts do (FR-93): they are the record of what
        was said about this restaurant and when.
        """
        rows = await self.list_for(claim, status=status, limit=1, kind=kind)
        return rows[0] if rows else None

    async def complete(self, draft_id: str, *, content: str, model: str) -> bool:
        """Generation succeeded. Guarded on `queued`, so a redelivered task
        — acks_late makes delivery at-least-once — cannot overwrite a draft
        a human has already acted on. False = somebody got there first."""
        return await self._move(
            draft_id,
            expected="queued",
            values={"status": "drafted", "content": content, "model": model, "error": None},
        )

    async def park(self, draft_id: str, *, error: str) -> bool:
        """Generation failed in a way retrying cannot fix.

        Also guarded on `queued`: a late failure from a retry whose sibling
        already succeeded must not bury a good draft.
        """
        log.warning("content draft parked", draft_id=draft_id, error=error)
        return await self._move(
            draft_id, expected="queued", values={"status": "parked", "error": error[:500]}
        )

    async def approve(
        self, draft_id: str, claim: Claim, *, published_content: str, by: str
    ) -> Row[Any]:
        """Record that a human published this copy (FR-93).

        **This does not write to the catalog, and that is deliberate.** The
        menu write is the admin's own PATCH through Catalog's ordinary
        endpoint — the same one they use to edit a description by hand,
        with their own token and their own ownership check. Giving the
        GenAI plane menu-write authority would be a real expansion of what
        an advisory plane can do (ADR-0029), to save one round trip.

        So this is the RECORD of the decision: who approved it, when, and
        the text they actually shipped, which may not be the text the model
        wrote. Keeping both is the only way to answer "how much of this did
        people have to fix".

        Idempotent on a re-approve with the same text: the client writes to
        Catalog first and records second, so a failure between the two
        leaves the draft needing action rather than claiming a publication
        that did not happen — and the retry must not then be refused.
        """
        row = await self.get(draft_id, claim)
        if row.status == "published" and row.published_content == published_content:
            return row
        if row.status != "drafted":
            raise WrongState(row.status, "approved")
        if row.kind == "feedback_summary":
            # There is nothing to publish. A summary is something a
            # restaurant reads about itself, not copy that goes anywhere.
            raise WrongState(row.kind, "published")
        if not await self._decide(
            draft_id,
            expected="drafted",
            status="published",
            by=by,
            extra={"published_content": published_content},
        ):
            # Somebody decided this row between our read and our write — a
            # colleague rejecting it, or a second approve. The UPDATE
            # matched nothing and this used to be DISCARDED: the caller got
            # a 200 and a row saying `rejected`, while the menu had already
            # been written by the client's earlier Catalog PATCH. Raising
            # turns a lost update into a 409 the console can show.
            raise WrongState((await self.get(draft_id, claim)).status, "approved")
        return await self.get(draft_id, claim)

    async def reject(self, draft_id: str, claim: Claim, *, by: str) -> None:
        """A human said no (FR-93).

        Retained, never deleted: the row is the record of what a model
        proposed and a person declined, which is the only audit trail this
        feature has.
        """
        row = await self.get(draft_id, claim)
        if row.status == "rejected":
            return  # already declined; saying so twice is the same decision
        if row.status != "drafted":
            raise WrongState(row.status, "rejected")
        if not await self._decide(draft_id, expected="drafted", status="rejected", by=by, extra={}):
            raise WrongState((await self.get(draft_id, claim)).status, "rejected")

    async def _decide(
        self, draft_id: str, *, expected: str, status: str, by: str, extra: dict[str, Any]
    ) -> bool:
        """Whether the guarded UPDATE actually matched.

        Returned rather than discarded. The guard is only a guard if
        somebody reads its answer: ignoring it turned every lost update
        into a silent success, which is how a draft could end up `rejected`
        while its copy was live on a menu.
        """
        return await self._move(
            draft_id,
            expected=expected,
            values={
                "status": status,
                "decided_by": by,
                "decided_at": datetime.now(UTC),
                **extra,
            },
        )

    async def replay(self, draft_id: str, claim: Claim) -> None:
        """The human replay lever UC-25 asks for: parked → queued.

        An UPDATE, which is the point — a broker DLQ needs a console and a
        person who knows it exists. The caller enqueues afterwards, same
        ordering rule as `create`.
        """
        row = await self.get(draft_id, claim)
        if row.status != "parked":
            raise WrongState(row.status, "replayed")
        if not await self._move(
            draft_id, expected="parked", values={"status": "queued", "error": None}
        ):
            # Two replays raced. Without this the second one enqueued a
            # SECOND job for one row — two provider calls, one discarded
            # result, and a partner token able to amplify N concurrent
            # clicks into N calls.
            raise WrongState((await self.get(draft_id, claim)).status, "replayed")

    async def _move(self, draft_id: str, *, expected: str, values: dict[str, Any]) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                content_drafts.update()
                .where(
                    (content_drafts.c.draft_id == draft_id) & (content_drafts.c.status == expected)
                )
                .values(updated_at=datetime.now(UTC), **values)
            )
            await session.commit()
        return bool(cast("CursorResult[Any]", result).rowcount)
