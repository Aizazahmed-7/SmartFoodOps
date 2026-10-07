"""The knowledge index: `item_chunks` + `restaurant_chunks` (ADR-0032).

`CREATE EXTENSION IF NOT EXISTS vector` is here for completeness and no-ops
in practice: `initdb/01-databases.sh` pre-creates it as superuser, exactly
like `pg_trgm` for catalog, because `assistant_svc` holds no superuser
rights and is not being given any. On a database where someone forgot the
pre-create, this line fails loudly at migration time — which is the right
place to find out.

TWO tables rather than one with a `kind` discriminator, because the two
vector spaces are not comparable: a query embedding sits systematically
closer to one text shape than the other, so the legs are retrieved
separately and fused (FR-62) — and two queries want two indexes. db.py
carries the full argument.

The indexes are HNSW rather than IVFFlat: IVFFlat wants a trained list count
sized to a corpus that does not exist yet, and is rebuilt when that corpus
grows. HNSW needs no training pass and degrades gracefully instead of
falling off a recall cliff — the right trade for an index that starts at a
handful of rows and has no idea what it will hold in a year.

ONE HNSW index per table, not one per city. ADR-0032 §5 originally said
per-city partial indexes; writing them exposed the problem — a partial index
per city is DDL keyed on DATA, so the set could only be completed by an
ingest path issuing CREATE INDEX for a city it had never seen. Schema writes
from a data path is a worse failure mode than a filtered scan. The filter
rides the query with `ix_*_chunks_scope` behind it, and declarative LIST
partitioning by city is the named escalation. The ADR was amended to match
this file rather than the other way round.

GIN on `tags` and `cuisines` because they are filtered by containment
(`@>`), which B-tree cannot serve.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Pinned to ai_assistant.db by a test: the column width and the opclass must
# match the constants the service embeds and queries with, or the planner
# silently ignores the index and the provider is asked for the wrong width.
EMBEDDING_DIMENSIONS = 512
VECTOR_OPS = "vector_cosine_ops"
ITEM_HNSW_INDEX = "ix_item_chunks_embedding_hnsw"
RESTAURANT_HNSW_INDEX = "ix_restaurant_chunks_embedding_hnsw"


def _hnsw(name: str, table: str) -> None:
    op.create_index(
        name,
        table,
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": VECTOR_OPS},
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "item_chunks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("model_version", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("item_id", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Text, nullable=True),
        sa.Column("cuisines", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("tags", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("available", sa.Boolean, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "restaurant_chunks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("model_version", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Text, nullable=True),
        sa.Column("cuisines", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_index("ix_item_chunks_scope", "item_chunks", ["model_version", "city"])
    op.create_index("ix_restaurant_chunks_scope", "restaurant_chunks", ["model_version", "city"])
    op.create_index("ix_item_chunks_restaurant", "item_chunks", ["restaurant_id", "model_version"])
    op.create_index(
        "ix_restaurant_chunks_restaurant",
        "restaurant_chunks",
        ["restaurant_id", "model_version"],
    )
    op.create_index("ix_item_chunks_tags", "item_chunks", ["tags"], postgresql_using="gin")
    op.create_index("ix_item_chunks_cuisines", "item_chunks", ["cuisines"], postgresql_using="gin")
    op.create_index(
        "ix_restaurant_chunks_cuisines",
        "restaurant_chunks",
        ["cuisines"],
        postgresql_using="gin",
    )
    _hnsw(ITEM_HNSW_INDEX, "item_chunks")
    _hnsw(RESTAURANT_HNSW_INDEX, "restaurant_chunks")


def downgrade() -> None:
    # The extension is deliberately NOT dropped: initdb owns it, other
    # objects may come to depend on it, and dropping a superuser-created
    # extension from a service migration is a privilege this role lacks.
    op.drop_table("restaurant_chunks")
    op.drop_table("item_chunks")
