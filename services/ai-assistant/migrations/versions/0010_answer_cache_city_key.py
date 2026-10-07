"""`city` belongs in the answer cache's key, not only its filter.

Found by the B3 adversarial review. The primary key was
`(hash(question), model_version)` with `city` as an ordinary column, so two
cities asking the same question overwrote one another's row — and each then
missed on the city predicate and regenerated, evicting the other again. The
more popular a question was across cities, the closer the semantic tier's
hit rate got to zero: ADR-0045's own stated revisit trigger ("a semantic hit
rate that stays near zero") arriving as a schema bug rather than a threshold
problem.

The table is a cache, so the rows are dropped rather than migrated: every
one of them is reachable again by asking, and keeping them would mean
deciding which of two colliding rows was the survivor.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM answer_cache"))
    op.drop_constraint("answer_cache_pkey", "answer_cache", type_="primary")
    op.create_primary_key("answer_cache_pkey", "answer_cache", ["id", "model_version", "city"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM answer_cache"))
    op.drop_constraint("answer_cache_pkey", "answer_cache", type_="primary")
    op.create_primary_key("answer_cache_pkey", "answer_cache", ["id", "model_version"])
