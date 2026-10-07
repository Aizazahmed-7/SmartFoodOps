"""The lexical half of hybrid retrieval (FR-62).

Two legs, one table, one set of hard predicates — which is the whole reason
ADR-0032 put vectors in the shared Postgres rather than a dedicated store.
The vector leg answers "close in meaning"; this one answers "contains the
words", and RRF fuses them. Neither is sufficient alone: embeddings miss
exact names and typos, and full-text misses everything a customer phrases
differently from the menu.

Mirrors catalog's ADR-0019 shape deliberately, down to the `'simple'`
configuration (no stemming — the same choice that keeps multilingual
retrieval a deferred option rather than a blocked one) and the trigram pass
for typos. What differs is the corpus: catalog indexes `name` and
`description` columns, we index the composed `content`, because `content` is
by construction exactly the text that was embedded. One string, both legs —
so the two can never disagree about what a chunk says.

Expression indexes only work when the query expression matches VERBATIM, so
the expressions live as constants in `retrieval.py` and a test pins them to
this file — catalog's `RESTAURANT_FTS` lesson, applied before it bites twice.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Pinned to ai_assistant.domain.retrieval by a test.
CONTENT_FTS = "to_tsvector('simple', content)"


def upgrade() -> None:
    # Superuser-provisioned in initdb, exactly like `vector`; this is the
    # loud failure for a database where someone forgot.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table in ("item_chunks", "restaurant_chunks"):
        op.execute(f"CREATE INDEX ix_{table}_fts ON {table} USING gin ({CONTENT_FTS})")
        # Trigram over the raw text for the typo pass. `gin_trgm_ops` rather
        # than GiST: reads dominate by orders of magnitude here, and GIN is
        # the faster of the two to search even though it is slower to build.
        op.execute(
            f"CREATE INDEX ix_{table}_content_trgm ON {table} USING gin (content gin_trgm_ops)"
        )


def downgrade() -> None:
    # The extensions stay: initdb owns them, and dropping a superuser-created
    # extension is a privilege this role lacks.
    for table in ("item_chunks", "restaurant_chunks"):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_fts")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_content_trgm")
