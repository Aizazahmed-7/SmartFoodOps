"""Drop `model_version` and `content_hash` from the knowledge index.

**`model_version`** existed so two embedding generations could coexist while
a rolling reindex moved the corpus between them. With the reindex gone and
the model fixed by `Settings`, every row carried the same value — which made
it a constant in both primary keys and the leading column of all four
indexes, where it contributed no selectivity at all, and a predicate on every
query that was always true.

It also leaves `answer_cache`, where it fenced cached answers against a
generation change. The remaining fence (city, epoch) covers that: a model
change is now a rebuild, the rebuild re-drains every restaurant, and that
bumps every city's epoch.

**`content_hash`** was a derived column — `sha256(content)` — used to decide
which chunks still needed embedding. The comparison it served is load-bearing
and stays; only the storage goes. `content` is already on the row for B2's
lexical leg, so the drain now compares the text itself. The digest is still
computed in `chunk()` and used in-memory to collapse chunks that share text
within one pass; it simply is not persisted.

Both are rebuild-safe: nothing reads either column after this.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-08
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_CHUNKS = ("item_chunks", "restaurant_chunks")


def upgrade() -> None:
    for table in _CHUNKS:
        op.drop_index(f"ix_{table}_scope", table_name=table)
        op.drop_index(f"ix_{table}_restaurant", table_name=table)
    op.drop_index("ix_answer_cache_fence", table_name="answer_cache")

    # The PK is composite on (id, model_version); rebuild it on id alone.
    for table in _CHUNKS:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.drop_column(table, "model_version")
        op.drop_column(table, "content_hash")
        op.create_primary_key(f"{table}_pkey", table, ["id"])
        op.create_index(f"ix_{table}_scope", table, ["city"])
        op.create_index(f"ix_{table}_restaurant", table, ["restaurant_id"])

    op.drop_constraint("answer_cache_pkey", "answer_cache", type_="primary")
    op.drop_column("answer_cache", "model_version")
    op.create_primary_key("answer_cache_pkey", "answer_cache", ["id", "city"])
    op.create_index("ix_answer_cache_fence", "answer_cache", ["city", "epoch"])


def downgrade() -> None:
    raise NotImplementedError(
        "the dropped columns cannot be reconstructed — restore by truncating "
        "the chunk tables and replaying c1.catalog.changes"
    )
