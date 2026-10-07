"""The answer cache and its fence (FR-74, ADR-0045).

Two tiers share one fence: `(model_version, city, epoch)`. The first two are
obvious; `epoch` is what "fenced by menu_version" means for a service whose
corpus is a Kafka projection rather than a versioned blob — a per-city
counter the knowledge drain bumps, in the same transaction as the chunk
write that changed the city. A cache entry keyed on epoch N becomes
unreachable the instant N+1 commits, so invalidation is free and stale rows
age out rather than needing to be found and deleted.

`answer_cache` carries the question's embedding and nothing else vector-
shaped: the semantic tier's whole job is "has anyone asked something close
to this, in this city, since the menus last moved".

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Pinned here, not imported: a migration describes the database at ONE
# moment, and an import would let a later constant edit rewrite history.
EMBEDDING_DIMENSIONS = 512
VECTOR_OPS = "vector_cosine_ops"
ANSWER_HNSW_INDEX = "ix_answer_cache_embedding_hnsw"


def upgrade() -> None:
    op.create_table(
        "knowledge_epochs",
        sa.Column("city", sa.Text, primary_key=True),
        sa.Column("epoch", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "answer_cache",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("model_version", sa.Text, primary_key=True),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("epoch", sa.BigInteger, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("item_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("restaurant_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_answer_cache_fence", "answer_cache", ["model_version", "city", "epoch"])
    # One index over every cached question rather than one per fence: a
    # partial index per city would be DDL keyed on data (ADR-0032 §5), and
    # the fence columns filter cheaply on top of the ANN scan.
    op.create_index(
        ANSWER_HNSW_INDEX,
        "answer_cache",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": VECTOR_OPS},
    )


def downgrade() -> None:
    op.drop_index(ANSWER_HNSW_INDEX, table_name="answer_cache")
    op.drop_index("ix_answer_cache_fence", table_name="answer_cache")
    op.drop_table("answer_cache")
    op.drop_table("knowledge_epochs")
