"""Nothing reaches a customer without a person saying so (FR-93, UC-29).

The sharpest thing to test here is what approve does NOT do: it does not
write to the catalog. The GenAI plane is advisory (ADR-0029), and giving it
menu-write authority to save a round trip would be a real expansion of what
it can do to a restaurant's live menu.
"""

import pytest
from ai_assistant.db import metadata
from ai_assistant.drafts import Claim, DraftNotFound, DraftStore, WrongState
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

OWNER = Claim(restaurant_id="rst_1", brand_id="brn_1")
RIVAL = Claim(restaurant_id="rst_9", brand_id="brn_9")
MODEL_WROTE = "A rich Lahori karahi, generous with chilli."
ADMIN_SHIPPED = "A rich Lahori karahi — our cook's own recipe."


async def _store() -> tuple[DraftStore, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return DraftStore(async_sessionmaker(engine, expire_on_commit=False)), None  # type: ignore[return-value]


async def _drafted(store, kind="menu_item") -> str:
    draft_id = await store.create(
        restaurant_id="rst_1", brand_id="brn_1", kind=kind, target_id="itm_a"
    )
    await store.complete(draft_id, content=MODEL_WROTE, model="m1")
    return draft_id


# ── approve ────────────────────────────────────────────────────────


async def test_approval_records_who_what_and_when():
    store, _ = await _store()
    draft_id = await _drafted(store)
    row = await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_owner")
    assert row.status == "published"
    assert row.decided_by == "usr_owner" and row.decided_at is not None


async def test_both_what_the_model_wrote_and_what_shipped_are_kept():
    """The only way to ever answer "how much of this did people have to
    fix" is to keep both."""
    store, _ = await _store()
    draft_id = await _drafted(store)
    row = await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_owner")
    assert row.content == MODEL_WROTE
    assert row.published_content == ADMIN_SHIPPED
    assert row.content != row.published_content


async def test_re_approving_the_same_text_is_idempotent():
    """The client writes to Catalog first and records second, so a failure
    between the two leaves a draft needing action — and the retry must not
    then be refused."""
    store, _ = await _store()
    draft_id = await _drafted(store)
    first = await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    again = await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    assert first.decided_at == again.decided_at  # the same decision, not a second one


async def test_re_approving_with_different_text_is_refused():
    """A second, different publication is a second decision, and the row
    records one. Rejecting it keeps the audit trail honest."""
    store, _ = await _store()
    draft_id = await _drafted(store)
    await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    with pytest.raises(WrongState):
        await store.approve(draft_id, OWNER, published_content="something else", by="usr_a")


@pytest.mark.parametrize("status", ["queued", "parked"])
async def test_only_a_finished_draft_can_be_approved(status):
    """Approving something nobody has read would be exactly the
    auto-publication FR-93 forbids."""
    store, _ = await _store()
    draft_id = await store.create(restaurant_id="rst_1", brand_id="brn_1", kind="menu_item")
    if status == "parked":
        await store.park(draft_id, error="broken")
    with pytest.raises(WrongState) as exc:
        await store.approve(draft_id, OWNER, published_content="x", by="usr_a")
    assert exc.value.status == status


async def test_a_feedback_summary_cannot_be_published():
    """There is nothing to publish — a summary is something a restaurant
    reads about itself, not copy that goes anywhere."""
    store, _ = await _store()
    draft_id = await _drafted(store, kind="feedback_summary")
    with pytest.raises(WrongState):
        await store.approve(draft_id, OWNER, published_content="x", by="usr_a")


async def test_a_rivals_draft_cannot_be_approved():
    store, _ = await _store()
    draft_id = await _drafted(store)
    with pytest.raises(DraftNotFound):
        await store.approve(draft_id, RIVAL, published_content="x", by="usr_rival")


# ── reject ─────────────────────────────────────────────────────────


async def test_a_rejected_draft_is_retained_with_its_content():
    """FR-93 keeps them for audit: the row is the only record of what a
    model proposed and a person declined."""
    store, _ = await _store()
    draft_id = await _drafted(store)
    await store.reject(draft_id, OWNER, by="usr_owner")
    row = await store.get(draft_id, OWNER)
    assert row.status == "rejected"
    assert row.content == MODEL_WROTE  # not cleared
    assert row.decided_by == "usr_owner"
    assert row.published_content is None  # it never shipped


async def test_rejecting_twice_is_the_same_decision():
    store, _ = await _store()
    draft_id = await _drafted(store)
    await store.reject(draft_id, OWNER, by="usr_a")
    await store.reject(draft_id, OWNER, by="usr_b")
    assert (await store.get(draft_id, OWNER)).decided_by == "usr_a"


async def test_a_published_draft_cannot_then_be_rejected():
    store, _ = await _store()
    draft_id = await _drafted(store)
    await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    with pytest.raises(WrongState):
        await store.reject(draft_id, OWNER, by="usr_a")


async def test_a_rivals_draft_cannot_be_rejected():
    store, _ = await _store()
    draft_id = await _drafted(store)
    with pytest.raises(DraftNotFound):
        await store.reject(draft_id, RIVAL, by="usr_rival")


# ── the thing approve does not do ──────────────────────────────────


def test_the_assistant_never_writes_to_the_catalog():
    """ADR-0029 keeps the GenAI plane advisory. A publish path that let it
    PATCH a live menu would be a real authority expansion, and the saving
    would be one round trip the frontend already makes.

    Enforced on the import graph, like FR-87's template floor: nothing in
    the draft lifecycle may reach a catalog write.
    """
    import pathlib

    source = (pathlib.Path(__file__).parent.parent / "ai_assistant" / "drafts.py").read_text()
    banned = ("httpx", "catalog_client", "requests", "socket", "adapters")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and any(word in line.lower() for word in banned)
    ]
    assert offenders == [], f"the draft lifecycle reaches outward: {offenders}"


# ── the endpoints ──────────────────────────────────────────────────

from smartfood_auth import AuthContext, headers_for  # noqa: E402

PARTNER = headers_for(
    AuthContext(sub="usr_owner", roles=frozenset({"restaurant_admin"}), restaurant_id="rst_1")
)
SHOPPER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
NO_TENANT = headers_for(AuthContext(sub="usr_x", roles=frozenset({"restaurant_admin"})))


class FakeDrafts:
    def __init__(self) -> None:
        self.approved: list[dict] = []
        self.rejected: list[dict] = []
        self.replayed: list[str] = []
        self.listed: list[dict] = []
        self.raise_with: Exception | None = None
        self.row = _row()

    async def list_for(self, claim, *, status=None, kind=None, limit=50):
        self.listed.append({"claim": claim, "status": status, "kind": kind, "limit": limit})
        return [self.row]

    async def approve(self, draft_id, claim, *, published_content, by):
        if self.raise_with:
            raise self.raise_with
        self.approved.append({"draft_id": draft_id, "content": published_content, "by": by})
        return self.row

    async def reject(self, draft_id, claim, *, by):
        if self.raise_with:
            raise self.raise_with
        self.rejected.append({"draft_id": draft_id, "by": by})

    async def replay(self, draft_id, claim):
        if self.raise_with:
            raise self.raise_with
        self.replayed.append(draft_id)

    async def get(self, draft_id, claim):
        return self.row


def _row():
    from datetime import UTC, datetime

    class Row:
        draft_id = "cdr_1"
        restaurant_id = "rst_1"
        brand_id = "brn_1"
        kind = "menu_item"
        status = "published"
        target_id = "itm_a"
        request = None
        subject = {"name": "Karahi"}
        content = MODEL_WROTE
        published_content = ADMIN_SHIPPED
        model = "m1"
        error = None
        created_at = datetime(2026, 10, 5, tzinfo=UTC)
        decided_by = "usr_owner"
        decided_at = datetime(2026, 10, 5, tzinfo=UTC)

    return Row()


@pytest.fixture()
def studio(client):
    drafts, enqueued = FakeDrafts(), []
    client.app.state.drafts = drafts
    client.app.state.enqueue_draft = lambda draft_id, kind: enqueued.append((draft_id, kind))
    return client, drafts, enqueued


def test_approve_passes_the_admins_own_text_and_identity(studio):
    client, drafts, _ = studio
    r = client.post(
        "/v1/assistant/drafts/cdr_1/approve",
        json={"published_content": ADMIN_SHIPPED},
        headers=PARTNER,
    )
    assert r.status_code == 200
    assert drafts.approved == [{"draft_id": "cdr_1", "content": ADMIN_SHIPPED, "by": "usr_owner"}]
    assert r.json()["published_content"] == ADMIN_SHIPPED


def test_approve_requires_the_text_rather_than_defaulting_to_the_models(studio):
    """FR-93 is an EXPLICIT approve action. A body that could be omitted
    would make approve mean "ship whatever it wrote" on a mis-click."""
    client, drafts, _ = studio
    assert (
        client.post("/v1/assistant/drafts/cdr_1/approve", json={}, headers=PARTNER).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/assistant/drafts/cdr_1/approve",
            json={"published_content": ""},
            headers=PARTNER,
        ).status_code
        == 422
    )
    assert drafts.approved == []


def test_reject_records_who_declined(studio):
    client, drafts, _ = studio
    r = client.post("/v1/assistant/drafts/cdr_1/reject", headers=PARTNER)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert drafts.rejected == [{"draft_id": "cdr_1", "by": "usr_owner"}]


def test_replay_requeues_with_the_rows_own_kind(studio):
    """A summary and a menu draft go to different tasks; replaying must
    not send a summary to the drafting task."""
    client, drafts, enqueued = studio
    drafts.row.kind = "feedback_summary"
    r = client.post("/v1/assistant/drafts/cdr_1/replay", headers=PARTNER)
    assert r.status_code == 200
    assert enqueued == [("cdr_1", "feedback_summary")]


def test_an_unknown_or_rivals_draft_is_one_404(studio):
    client, drafts, _ = studio
    drafts.raise_with = DraftNotFound()
    for path, body in (("approve", {"published_content": "x"}), ("reject", None), ("replay", None)):
        r = client.post(f"/v1/assistant/drafts/cdr_1/{path}", json=body, headers=PARTNER)
        assert r.status_code == 404, path


def test_a_wrong_state_is_a_409_naming_it(studio):
    client, drafts, _ = studio
    drafts.raise_with = WrongState("queued", "approved")
    r = client.post(
        "/v1/assistant/drafts/cdr_1/approve",
        json={"published_content": "x"},
        headers=PARTNER,
    )
    assert r.status_code == 409
    assert "queued" in r.json()["error"]["message"]


def test_the_list_is_scoped_and_filterable(studio):
    client, drafts, _ = studio
    body = client.get("/v1/assistant/drafts?status=parked&kind=menu_item", headers=PARTNER).json()
    assert len(body["drafts"]) == 1
    call = drafts.listed[0]
    assert call["status"] == "parked" and call["kind"] == "menu_item"
    assert call["claim"].restaurant_id == "rst_1" and call["claim"].brand_id == "rst_1"


def test_parked_drafts_appear_in_the_list_with_their_reason(studio):
    """The point of making the dead-letter queue a row: the restaurant
    whose copy never arrived can see that it did not, and why."""
    client, drafts, _ = studio
    drafts.row.status = "parked"
    drafts.row.error = "the model declined to write this copy"
    row = client.get("/v1/assistant/drafts?status=parked", headers=PARTNER).json()["drafts"][0]
    assert row["status"] == "parked"
    assert "declined" in row["error"]


@pytest.mark.parametrize("path", ["approve", "reject", "replay"])
def test_a_customer_cannot_decide_anything(studio, path):
    client, drafts, _ = studio
    r = client.post(
        f"/v1/assistant/drafts/cdr_1/{path}",
        json={"published_content": "x"} if path == "approve" else None,
        headers=SHOPPER,
    )
    assert r.status_code in (401, 403)
    assert drafts.approved == [] and drafts.rejected == []


def test_a_token_naming_no_restaurant_reaches_nothing(studio):
    client, drafts, _ = studio
    assert client.get("/v1/assistant/drafts", headers=NO_TENANT).status_code == 403
    for path in ("approve", "reject", "replay"):
        r = client.post(
            f"/v1/assistant/drafts/cdr_1/{path}",
            json={"published_content": "x"} if path == "approve" else None,
            headers=NO_TENANT,
        )
        assert r.status_code == 403, path
    assert drafts.listed == [] and drafts.approved == []


@pytest.mark.parametrize("path", ["reject", "replay"])
def test_a_wrong_state_on_reject_or_replay_is_a_409_too(studio, path):
    """Replaying something that is not parked, or declining something
    already published, is a state conflict and not a 500."""
    client, drafts, _ = studio
    drafts.raise_with = WrongState("published", path)
    r = client.post(f"/v1/assistant/drafts/cdr_1/{path}", headers=PARTNER)
    assert r.status_code == 409
    assert "published" in r.json()["error"]["message"]


# ── the lost update the review found ───────────────────────────────


async def test_a_decision_that_lost_a_race_is_a_conflict_not_a_success():
    """The guarded UPDATE's rowcount was DISCARDED. Concretely: admin A
    clicks approve (the console has already PATCHed Catalog, so the copy is
    live), admin B rejects first, A's record lands on a row that is no
    longer `drafted` — and A got a 200 with a row saying `rejected` while
    the menu carried the text. Unrecoverable: re-approving then 409s
    forever."""
    store, _ = await _store()
    draft_id = await _drafted(store)

    # B decides first.
    await store.reject(draft_id, OWNER, by="usr_b")
    # A's record arrives second and must not be reported as success.
    with pytest.raises(WrongState):
        await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    row = await store.get(draft_id, OWNER)
    assert row.status == "rejected" and row.published_content is None


async def test_a_second_replay_does_not_enqueue_a_second_job():
    """`replay` ignored its rowcount and the route enqueued regardless, so
    two concurrent clicks became two provider calls for one row — and a
    partner token could amplify N clicks into N calls."""
    import asyncio

    store, _ = await _store()
    # Parked straight from `queued` — `park` is guarded on `queued`, so
    # parking a drafted row is a no-op (which is itself the guard working).
    draft_id = await store.create(restaurant_id="rst_1", brand_id="brn_1", kind="menu_item")
    assert await store.park(draft_id, error="broken")

    # CONCURRENT, not sequential: the pre-read catches the sequential case
    # already. Both of these see `parked` and pass the check; only one
    # UPDATE can match, and the loser must raise rather than fall through
    # to the route's unconditional enqueue.
    outcomes = await asyncio.gather(
        store.replay(draft_id, OWNER), store.replay(draft_id, OWNER), return_exceptions=True
    )
    failures = [o for o in outcomes if isinstance(o, WrongState)]
    assert len(failures) == 1, outcomes


async def test_a_completion_that_lost_a_race_says_so():
    """`complete`'s bool was thrown away, so a duplicate delivery's
    discarded answer was indistinguishable from a clean run in every log
    and metric — a double spend nobody could see."""
    store, _ = await _store()
    draft_id = await store.create(restaurant_id="rst_1", brand_id="brn_1", kind="menu_item")
    assert await store.complete(draft_id, content="first", model="m1")
    assert not await store.complete(draft_id, content="second", model="m2")


async def test_a_reject_that_lost_a_race_is_a_conflict_too():
    """The mirror of the approve case: `reject` ignored its rowcount and
    returned 200 for a row that was already published."""
    import asyncio

    store, _ = await _store()
    draft_id = await _drafted(store)
    outcomes = await asyncio.gather(
        store.reject(draft_id, OWNER, by="usr_a"),
        store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_b"),
        return_exceptions=True,
    )
    assert len([o for o in outcomes if isinstance(o, WrongState)]) == 1, outcomes


async def test_rejecting_a_row_someone_published_mid_flight_is_a_conflict():
    """`reject`'s own lost-update arm, driven directly: the pre-read says
    `drafted`, the UPDATE matches nothing because a publish landed in
    between."""
    store, _ = await _store()
    draft_id = await _drafted(store)
    row = await store.get(draft_id, OWNER)

    class Racing(type(store)):  # type: ignore[misc]
        async def get(self, draft_id, claim):
            # Always reports the pre-race view, so the guarded UPDATE is
            # the only thing standing between two decisions.
            return row

    racing = Racing(store._sessions)  # noqa: SLF001
    await store.approve(draft_id, OWNER, published_content=ADMIN_SHIPPED, by="usr_a")
    with pytest.raises(WrongState):
        await racing.reject(draft_id, OWNER, by="usr_b")
