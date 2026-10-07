"""`feedback_summary` joins the draft kinds (FR-92).

A summary is generated text a human reads, with the same queue, the same
parking and the same replay as every other draft. A second table would
duplicate all of that to hold one more shape.

Rewriting the CHECK is the whole migration: SQLite cannot ALTER a
constraint, but this service runs on Postgres and the test suite builds its
schema from metadata, so the constraint is dropped and re-added rather than
the table being rebuilt.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

OLD = ("menu_item", "promotion", "engagement")
NEW = (*OLD, "feedback_summary")


def _swap(kinds: tuple[str, ...]) -> None:
    op.drop_constraint("ck_content_drafts_kind", "content_drafts", type_="check")
    op.create_check_constraint("ck_content_drafts_kind", "content_drafts", f"kind IN {kinds!r}")


def upgrade() -> None:
    _swap(NEW)


def downgrade() -> None:
    # Postgres validates a newly-added CHECK against existing rows, so
    # re-adding the narrower constraint fails the moment one summary exists
    # — which it does the first time anyone presses "Summarise". The rows
    # go first. They are generated text a human read and nothing published,
    # so deleting them on a rollback loses an audit trail of summaries and
    # not a record of any decision.
    op.execute(sa.text("DELETE FROM content_drafts WHERE kind = 'feedback_summary'"))
    _swap(OLD)
