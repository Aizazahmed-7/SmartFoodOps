"""The chat surface (FR-67, FR-68, FR-69).

Two endpoints, two trust models — the shape order tracking established, for
the same reason it established it:

  POST /v1/assistant/messages   arrives THROUGH the edge (JWT verified,
                                identity stamped). Starts the turn and sells
                                a single-use ticket. Auth happens here.
  GET  /v1/assistant/messages/… arrives DIRECT from the gateway, bypassing
                                the edge. The ticket IS the auth: EventSource
                                cannot set headers, and a JWT in a query
                                string soaks into access logs.

The POST **returns before the answer exists.** It starts a detached turn and
hands back an id — which is what makes the stream resumable at all, because
a generation owned by a request dies with that request (ADR-0042 §1).
"""

import json
import secrets
from typing import Annotated, Any, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import AuthContext, Role, require_role
from smartfood_realtime import StreamConfig, stream_events

from ..domain.combos import affordable, combine
from ..domain.ports import UpstreamUnavailable
from ..domain.summaries import MIN_ROWS
from ..drafts import Claim, DraftNotFound, WrongState
from ..explain_service import NotYours
from ..feedback import counts_for, summarisable
from ..menu_facts import MAX_ITEMS
from ..turns import channel_for, is_done

router = APIRouter()

Chatter = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]

STREAM_PREFIX = "/v1/assistant/messages/"
"""Registered in `stream_prefixes` so a held connection's LIFETIME never
lands in the HTTP latency histogram. Without it one slow reader pins the
service p95 at the top bucket — the live incident recorded in
smartfood_otel/middleware.py."""


class Tickets(Protocol):
    async def put_ticket(self, ticket: str, channel: str, sub: str, *, ttl_s: int) -> None: ...
    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None: ...


class AskIn(StrictModel):
    question: str = Field(min_length=1, max_length=2_000)
    city: str = Field(min_length=1, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=64)


def _chat(request: Request) -> Any:
    return request.app.state.chat


def _bus(request: Request) -> Any:
    return getattr(request.app.state, "realtime", None)


def _unavailable(detail: str) -> ApiError:
    return ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, detail, 503, headers={"Retry-After": "30"})


@router.post("/v1/assistant/messages", status_code=202)
async def ask(
    body: AskIn,
    ctx: Chatter,
    request: Request,
) -> dict[str, Any]:
    """Start a turn. **202, not 200** — the answer does not exist yet, and
    saying otherwise would make the status line a lie about what the body
    contains."""
    chat = _chat(request)
    bus = _bus(request)
    if bus is None:
        raise _unavailable("the assistant is unavailable")

    conversation_id = body.conversation_id or f"cnv_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    started = await chat.start(
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=ctx.sub,
        city=body.city,
        question=body.question,
    )
    if started is None:
        # Not-yours and not-found are one shape, as everywhere else in this
        # service: a distinguishable 403 would turn a client-supplied
        # `conversation_id` into an oracle for which conversations exist.
        raise ApiError(ErrorCode.NOT_FOUND, "no such conversation", 404)
    return {
        "conversation_id": conversation_id,
        "message_id": started,
        "ticket": await _mint(bus, request, channel_for(started), ctx.sub),
        "stream": f"/sse/assistant/{started}",
    }


async def _mint(bus: Tickets, request: Request, channel: str, sub: str) -> str:
    ticket = secrets.token_urlsafe(24)
    await bus.put_ticket(ticket, channel, sub, ttl_s=_config(request).ticket_ttl_s)
    return ticket


Partner = Annotated[AuthContext, Depends(require_role(Role.RESTAURANT_ADMIN))]


class MenuDraftIn(StrictModel):
    """Which dishes to write copy for (FR-88, UC-25).

    `item_ids` OR `category` — one click on a dish, or one on a whole
    section. Both are bounded: "draft my entire menu" is a legitimate thing
    to want and an illegitimate thing to do in a single request, because it
    is one admin turning one click into fifty provider calls.
    """

    item_ids: Annotated[list[str] | None, Field(default=None, min_length=1, max_length=MAX_ITEMS)]
    category: Annotated[str | None, Field(default=None, min_length=1, max_length=120)]
    request: Annotated[str | None, Field(default=None, max_length=500)]


@router.post("/v1/assistant/drafts/menu-items", status_code=202)
async def draft_menu_items(body: MenuDraftIn, ctx: Partner, request: Request) -> dict[str, Any]:
    """Enqueue copy for one dish or one category (FR-88).

    202, because drafting is a job: the response says what was accepted,
    and the console watches the rows. Nothing here touches `menu_items` —
    output lands in `content_drafts` and only a human's approve action
    (FR-93) ever moves it onward.

    The facts are read SCOPED to the caller's restaurant, which is also the
    ownership check: an item id belonging to someone else does not come
    back, so there is no second check to forget. The response reports how
    many were skipped without saying which, so a probe cannot distinguish
    "not yours" from "not indexed".
    """
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    if (body.item_ids is None) == (body.category is None):
        raise ApiError(ErrorCode.VALIDATION_FAILED, "give exactly one of item_ids or category", 422)

    reader = request.app.state.menu_facts
    if body.category is not None:
        facts = await reader.for_category(restaurant_id=ctx.restaurant_id, category=body.category)
    else:
        facts = await reader.for_items(
            restaurant_id=ctx.restaurant_id, item_ids=body.item_ids or []
        )

    store = request.app.state.drafts
    enqueue = request.app.state.enqueue_draft
    draft_ids: list[str] = []
    for fact in facts:
        draft_id = await store.create(
            # The dish's own branch, not the claim — a brand token drafting
            # for twelve branches must produce twelve rows that each name
            # the menu they are for.
            restaurant_id=fact.restaurant_id,
            brand_id=fact.brand_id,
            kind="menu_item",
            target_id=fact.item_id,
            request=body.request,
            subject=fact.as_subject(),
        )
        # Committed first, THEN enqueued — a worker that picks the id up
        # must find a row.
        enqueue(draft_id, "menu_item")
        draft_ids.append(draft_id)

    asked = len(body.item_ids) if body.item_ids else len(facts)
    return {
        "draft_ids": draft_ids,
        "queued": len(draft_ids),
        # A count, never a list of which: that difference is what keeps a
        # probe from mapping another restaurant's menu.
        "skipped": max(0, asked - len(draft_ids)),
    }


class BusinessDraftIn(StrictModel):
    """One sentence describing what the admin wants (UC-26, UC-27)."""

    request: Annotated[str, Field(min_length=1, max_length=500)]


async def _business_draft(kind: str, body: BusinessDraftIn, ctx: AuthContext, request: Request):
    """Shared by promotions and engagement copy (FR-89, FR-90).

    Both are copy about the BUSINESS rather than one dish, so both are
    written from the restaurant's own aggregates — counts and dish names,
    never a customer. One draft per request: unlike a menu fan-out, there
    is exactly one thing being written.
    """
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)

    facts = await request.app.state.restaurant_facts.for_restaurant(ctx.restaurant_id)
    if facts.thin:
        # A restaurant with a handful of orders has no aggregate worth
        # describing, and copy written from one would be a claim dressed
        # as a statistic. The same floor FR-92 puts under summaries.
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "not enough recent orders to write from yet",
            422,
        )

    draft_id = await request.app.state.drafts.create(
        restaurant_id=ctx.restaurant_id,
        brand_id=None,
        kind=kind,
        request=body.request,
        subject=facts.as_subject(),
    )
    request.app.state.enqueue_draft(draft_id, kind)
    return {"draft_id": draft_id, "kind": kind}


@router.post("/v1/assistant/drafts/promotions", status_code=202)
async def draft_promotion(body: BusinessDraftIn, ctx: Partner, request: Request) -> dict[str, Any]:
    """An offer, described in a sentence (FR-89, UC-26)."""
    return await _business_draft("promotion", body, ctx, request)


@router.post("/v1/assistant/drafts/engagement", status_code=202)
async def draft_engagement(body: BusinessDraftIn, ctx: Partner, request: Request) -> dict[str, Any]:
    """A message to lapsed or returning customers (FR-90, UC-27).

    Drafted from the restaurant's own aggregates, which carry no customer
    identity at all — so there is nothing for the copy to leak, and the
    "no PII" promise is a property of what was loaded rather than of the
    prompt.
    """
    return await _business_draft("engagement", body, ctx, request)


def _claim(ctx: AuthContext) -> Claim:
    """One claim id, checked against both columns.

    A restaurant_admin token names either a brand or a branch (ADR-0028),
    and the token itself does not say which — so it is compared against
    both, exactly as order's kitchen feed does.
    """
    return Claim(restaurant_id=ctx.restaurant_id, brand_id=ctx.restaurant_id)


def _draft_out(row: Any) -> dict[str, Any]:
    return {
        "draft_id": row.draft_id,
        # The restaurant this copy is FOR, which is where a menu write has
        # to go. A base item belongs to the brand and a branch-local item
        # to the branch (ADR-0028), and the console cannot tell them apart
        # without this — it guessed the branch, then guessed the brand, and
        # each guess broke the other half of the menu.
        "restaurant_id": row.restaurant_id,
        "brand_id": row.brand_id,
        "kind": row.kind,
        "status": row.status,
        "target_id": row.target_id,
        "request": row.request,
        "subject": row.subject,
        "content": row.content,
        "published_content": row.published_content,
        "model": row.model,
        "error": row.error,
        "created_at": row.created_at.isoformat(),
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


@router.get("/v1/assistant/drafts")
async def list_drafts(
    ctx: Partner,
    request: Request,
    status: Annotated[str | None, Query(max_length=32)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """The studio's list — this tenant's drafts, newest first.

    Parked rows are in here with everyone else, which is the point of
    making the dead-letter queue a row: the restaurant whose copy never
    arrived can see that it did not, and why.
    """
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    rows = await request.app.state.drafts.list_for(
        _claim(ctx), status=status, kind=kind, limit=limit
    )
    return {"drafts": [_draft_out(row) for row in rows]}


class ApproveIn(StrictModel):
    """The text the admin actually published.

    Required, and not defaulted to the model's draft: FR-93 is an EXPLICIT
    approve action, and a body that could be omitted would make "approve"
    mean "ship whatever it wrote" on a mis-click.
    """

    published_content: Annotated[str, Field(min_length=1, max_length=2000)]


@router.post("/v1/assistant/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: str, body: ApproveIn, ctx: Partner, request: Request
) -> dict[str, Any]:
    """Record that a human published this copy (FR-93, UC-29).

    **The menu write is not here.** The caller PATCHes Catalog's ordinary
    item endpoint with their own token first — the same call they use to
    edit a description by hand, which emits the catalog change that
    re-embeds the dish (FR-93's "triggers re-embedding" is the existing
    pipeline, not a second one). Then this records the decision.

    That order matters: a failure between the two leaves a draft that still
    needs action, rather than a row claiming a publication that never
    happened. Re-approving with the same text is idempotent so the retry
    is not refused.
    """
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    try:
        row = await request.app.state.drafts.approve(
            draft_id, _claim(ctx), published_content=body.published_content, by=ctx.sub
        )
    except DraftNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown draft", 404) from None
    except WrongState as exc:
        raise ApiError(ErrorCode.ORDER_STATE_CONFLICT, str(exc), 409) from None
    return _draft_out(row)


@router.post("/v1/assistant/drafts/{draft_id}/reject")
async def reject_draft(draft_id: str, ctx: Partner, request: Request) -> dict[str, Any]:
    """Decline a draft (FR-93). Retained, never deleted — the row is the
    only record of what a model proposed and a person turned down."""
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    try:
        await request.app.state.drafts.reject(draft_id, _claim(ctx), by=ctx.sub)
    except DraftNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown draft", 404) from None
    except WrongState as exc:
        raise ApiError(ErrorCode.ORDER_STATE_CONFLICT, str(exc), 409) from None
    return {"draft_id": draft_id, "status": "rejected"}


@router.post("/v1/assistant/drafts/{draft_id}/replay")
async def replay_draft(draft_id: str, ctx: Partner, request: Request) -> dict[str, Any]:
    """Put a parked draft back in the queue (UC-25's "replayable")."""
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    try:
        await request.app.state.drafts.replay(draft_id, _claim(ctx))
    except DraftNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown draft", 404) from None
    except WrongState as exc:
        raise ApiError(ErrorCode.ORDER_STATE_CONFLICT, str(exc), 409) from None
    row = await request.app.state.drafts.get(draft_id, _claim(ctx))
    request.app.state.enqueue_draft(draft_id, row.kind)
    return {"draft_id": draft_id, "status": "queued"}


@router.get("/v1/assistant/feedback")
async def read_feedback(ctx: Partner, request: Request) -> dict[str, Any]:
    """The feedback tab (FR-92, UC-28).

    The rows are ALWAYS here. Below the floor there is no summary and that
    is the honest answer rather than a degraded one: three reviews do not
    have themes, they have three opinions, and the rows say more than any
    sentence about them could.

    Every number in this response is computed from the rows. The model
    contributes themes and quotes and nothing else, so there is no
    statistic anywhere that a model wrote.
    """
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    try:
        rows = await request.app.state.feedback.for_restaurant(ctx.restaurant_id)
    except UpstreamUnavailable:
        # The client raises this rather than returning [] precisely so an
        # Order outage is not read as "you have no feedback". Letting it
        # reach the generic handler turned that care into a 500 blamed on
        # the assistant.
        raise _unavailable("order service is unavailable") from None
    # `status="drafted"`, not "whatever is newest". Asking for a refresh
    # created a `queued` row that became the newest, so a perfectly good
    # summary vanished from the panel the moment somebody pressed the
    # button to regenerate it — and stayed gone if the new job parked.
    latest = await request.app.state.drafts.latest(
        Claim(restaurant_id=ctx.restaurant_id, brand_id=ctx.restaurant_id),
        kind="feedback_summary",
        status="drafted",
    )
    summary: dict[str, Any] | None = None
    if latest is not None and latest.content:
        summary = {
            **json.loads(latest.content),
            "model": latest.model,
            "drafted_at": latest.updated_at.isoformat(),
        }
    return {
        "counts": counts_for(rows).as_dict(),
        "can_summarise": summarisable(rows),
        "summary": summary,
        "feedback": [
            {
                "order_id": row.order_id,
                "rating": row.rating,
                "comment": row.comment,
                "submitted_at": row.submitted_at,
            }
            for row in rows
        ],
    }


@router.post("/v1/assistant/feedback/summary", status_code=202)
async def summarise_feedback(ctx: Partner, request: Request) -> dict[str, Any]:
    """Ask for a summary of this restaurant's own reviews (FR-92)."""
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.FORBIDDEN_ROLE, "this token names no restaurant", 403)
    try:
        rows = await request.app.state.feedback.for_restaurant(ctx.restaurant_id)
    except UpstreamUnavailable:
        raise _unavailable("order service is unavailable") from None
    if not summarisable(rows):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"at least {MIN_ROWS} reviews with comments are needed to summarise",
            422,
        )
    draft_id = await request.app.state.drafts.create(
        restaurant_id=ctx.restaurant_id,
        brand_id=None,
        kind="feedback_summary",
    )
    request.app.state.enqueue_draft(draft_id, "feedback_summary")
    return {"draft_id": draft_id, "kind": "feedback_summary"}


@router.get("/v1/assistant/orders/{order_id}/explanation")
async def order_explanation(
    order_id: str,
    ctx: Chatter,
    request: Request,
    locale: Annotated[str, Query(min_length=2, max_length=16)] = "en",
) -> dict[str, Any]:
    """Why is my order where it is? (FR-83, FR-86, UC-22)

    A GET with no model on its critical path. The words come from the
    template floor (FR-87), so this answers with `llm_api_key=""` set and
    every provider absent — which is why it is a plain read rather than a
    turn through the graph.

    `reason` is in the response on purpose. The FE branches on it (a
    cancelled order offers a reorder, a stalled one offers support), and a
    caller that had to pattern-match English prose to decide would break
    the moment the copy improved.
    """
    service = request.app.state.explanations
    try:
        explanation = await service.explain(order_id, user_id=ctx.sub, locale=locale)
    except NotYours:
        explanation = None
    except UpstreamUnavailable:
        # Order could not answer. A 404 here would tell a customer their own
        # order does not exist, and would register as a client error on
        # every dashboard while the outage went unalerted.
        raise _unavailable("order service is unavailable") from None
    if explanation is None:
        # Not-found and not-yours are one answer: confirming an order
        # exists to someone who does not own it is the leak.
        raise ApiError(ErrorCode.NOT_FOUND, "unknown order", 404)
    return {
        "order_id": order_id,
        "reason": explanation.reason.value,
        "text": explanation.text,
        "bucket": explanation.bucket.value,
        "locale": explanation.locale,
        "source": explanation.source,
    }


@router.get("/v1/assistant/recommendations")
async def recommendations(
    ctx: Chatter,
    request: Request,
    city: Annotated[str, Query(min_length=1, max_length=80)],
    budget_cents: Annotated[int | None, Query(gt=0, le=1_000_000)] = None,
) -> dict[str, Any]:
    """What to order, for a customer who has not asked anything (FR-80).

    **Never an empty response** is the requirement, and the reason this is
    its own read rather than a branch of the turn: an empty retrieval almost
    never happens — vector search returns nearest neighbours for gibberish —
    so a fallback hung off the no-match path would be correct and
    unreachable. A customer opening the panel with nothing typed is the case
    FR-80 is actually about.

    `basis` says where the answer came from, so a personalised
    recommendation and the popularity baseline are distinguishable by the
    caller and by the eval suite (FR-75 asks for exactly that comparison).

    **A budget covers DISH prices, not the order total.** Delivery fee and
    tax are added by the pricing engine at quote time (199c flat + 825bp in
    the default config), so a $20 budget can return a $19.90 pairing that
    checks out at $23.53. That is a deliberate reading of FR-76's "at live
    prices" — the customer is choosing dishes, not approving a total, and a
    budget that silently reserved a fee would show them fewer dishes than
    they can afford. `budget_applies_to` says so in the response rather than
    leaving the caller to assume (raised by the B4 review).
    """
    recommender = request.app.state.recommender
    limit = _config_limit(request)
    # Over-fetch when a budget is set: the filter runs on LIVE prices, which
    # are only known after pricing, so asking for exactly `limit` dishes and
    # then dropping the unaffordable ones would return a short list whenever
    # the expensive ones happened to rank highest.
    basis, passages = await recommender.for_user(
        user_id=ctx.sub, city=city, limit=limit if budget_cents is None else limit * 8
    )

    # Combo candidates are priced ALONGSIDE the recommendations, in one
    # catalog fan-out, rather than being drawn from them. Restricting a
    # combo to dishes that happened to make the top five made it arbitrary —
    # and usually empty, since the most co-ordered pair is rarely two of the
    # same customer's five best matches.
    pairs = await recommender.pairs_in(city=city)
    paired = await recommender.hydrate(
        [(restaurant, item) for restaurant, a, b, _ in pairs for item in (a, b)]
    )
    recommended = {(p.restaurant_id, p.item_id) for p in passages}
    cards = await request.app.state.cards.for_items(
        passages=[
            *passages,
            *(p for p in paired if (p.restaurant_id, p.item_id) not in recommended),
        ]
    )
    # Keyed by the PAIR: one item id is served by every branch of a brand,
    # so an item-keyed floor prices a dish at whichever branch happened to
    # answer last.
    floors = {
        (c["restaurant_id"], c["item_id"]): c["min_total_cents"]
        for c in cards
        # A combo nobody named is a suggestion we chose to make, so an 86'd
        # dish or a shut kitchen simply cannot be part of one — unlike an
        # answer's citation, which is shown because the prose named it.
        if c["orderable"]
    }

    # FR-76's hard predicate, applied HERE and not to the indexed copy: the
    # index carries a price that was true when the menu was last chunked,
    # and a budget promise made against it is a promise about a stale
    # number. `min_total_cents` rather than `price_cents`, because a dish
    # with a required paid option cannot be bought for its base price.
    shown = [c for c in cards if (c["restaurant_id"], c["item_id"]) in recommended]
    if budget_cents is not None:
        shown = list(affordable(shown, budget_cents))
    shown = shown[:limit]

    combos = combine(pairs, floors, budget_cents=budget_cents, limit=_config_limit(request))
    by_pair = {(c["restaurant_id"], c["item_id"]): c for c in cards}
    rendered = [
        {
            "items": [by_pair[(c.restaurant_id, i)] for i in c.item_ids],
            "total_cents": c.total_cents,
            "orders": c.orders,
        }
        for c in combos
    ]

    # Everything the customer can see goes into the denominator, combos
    # included — the acceptance join can only find ids that were recorded,
    # so a surface left out of this call scores a structural zero forever.
    # It did: `record_shown` ran before `combine` and never saw a combo
    # (B4 review), while the comment here claimed otherwise.
    await recommender.record_shown(
        user_id=ctx.sub,
        city=city,
        surface="recommendations",
        basis=basis,
        item_ids=sorted(
            {c["item_id"] for c in shown}
            | {i["item_id"] for combo in rendered for i in combo["items"]}
        ),
    )

    return {
        "basis": basis,
        "city": city,
        "budget_cents": budget_cents,
        "budget_applies_to": "dish_subtotal",
        "items": shown,
        "combos": rendered,
    }


def _config_limit(request: Request) -> int:
    return int(request.app.state.recommend_limit)


@router.get(STREAM_PREFIX + "{message_id}/items")
async def cited_items(message_id: str, ctx: Chatter, request: Request) -> dict[str, Any]:
    """The dishes this answer named, priced NOW (FR-60).

    Separate from the stream on purpose. The answer is prose and is final
    the moment it is written; a price is not, and binding them together
    would mean either quoting a stale price or re-running a generation to
    refresh one. A reader who reconnects tomorrow gets yesterday's words and
    today's menu.
    """
    cards = await request.app.state.cards.for_message(message_id=message_id, user_id=ctx.sub)
    if cards is None:
        raise ApiError(ErrorCode.NOT_FOUND, "no such message", 404)
    return {"items": cards}


@router.get(STREAM_PREFIX + "{message_id}")
async def stream_answer(
    message_id: str,
    request: Request,
    ticket: Annotated[str, Query(min_length=1)],
) -> StreamingResponse:
    """Follow one answer as it is written.

    **Follow-only. There is no resume.** A reader that disconnects has lost
    the stream, and reconnecting starts from whatever is being published
    now — there is no stored chunk to replay, by design (ADR-0042,
    superseded). What survives a disconnect is the assembled `messages` row
    the turn writes when it finishes, which is what a reloaded conversation
    renders.

    The ticket is single-use and channel-scoped, so it is spent by the first
    GET that redeems it. An `EventSource` reconnecting on its own reuses the
    same URL and therefore the same spent ticket, and gets a 401 — which is
    the intended end of the stream rather than a bug to work around.
    """
    bus = _bus(request)
    if bus is None:
        raise _unavailable("the assistant is unavailable")
    claim = await bus.consume_ticket(ticket)
    if claim is None or claim.get("channel") != channel_for(message_id):
        # Burned either way — a mismatched ticket is consumed too, so a probe
        # learns nothing and loses its ticket doing so. Channel-scoped claims
        # also make a TRACKING ticket structurally useless here.
        raise ApiError(ErrorCode.AUTH_INVALID_CREDENTIALS, "invalid or spent ticket", 401)

    return StreamingResponse(
        stream_events(
            channel_for(message_id),
            bus,
            _config(request),
            event_name="chunk",
            ends_stream=is_done,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _config(request: Request) -> StreamConfig:
    return request.app.state.stream_config
