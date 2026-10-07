"""The knowledge schema's standing risks (ADR-0032).

1. The dialect splits. `embedding` is `vector(512)` on Postgres and JSON on
   sqlite; `tags`/`cuisines` are `ARRAY(Text)` and JSON the same way. If a
   variant ever breaks, EVERY unit suite in this service dies at
   `create_all` — so it is asserted directly rather than left to be
   discovered as a confusing collection-time error.

2. Drift between the service's constants and the migration's literals. The
   column width decides what the provider is asked for, and the opclass
   decides whether the planner uses the HNSW index at all; both fail
   SILENTLY when they disagree — a mismatched opclass just turns every
   search into a sequential scan. Catalog pins its FTS expressions to its
   migration for the same reason.

3. The item/restaurant split staying honest. The whole argument for two
   tables is that nothing is "required, but only for half the rows", so a
   nullable column creeping back in is a regression worth failing on.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from ai_assistant.config import Settings
from ai_assistant.db import (
    EMBEDDING_DIMENSIONS,
    ITEM_HNSW_INDEX,
    RESTAURANT_HNSW_INDEX,
    VECTOR_OPS,
    item_chunks,
    metadata,
    restaurant_chunks,
)
from sqlalchemy.ext.asyncio import create_async_engine

MIGRATION = (
    Path(__file__).parent.parent / "migrations" / "versions" / "0003_menu_chunks.py"
).read_text()


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


async def test_metadata_creates_on_sqlite(engine):
    """The variants hold: pgvector and ARRAY columns do not stop the unit
    suite."""
    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    assert {"item_chunks", "restaurant_chunks"} <= set(tables)


async def test_item_chunk_round_trips_through_both_variants(engine):
    """Both dialects bind and return plain Python values — list[float] for
    the vector, list[str] for the slug arrays — which is what lets the
    ingestion path stay dialect-agnostic. Only the DISTANCE query is
    Postgres-only."""
    vector = [0.5] * EMBEDDING_DIMENSIONS
    async with engine.begin() as conn:
        await conn.execute(
            item_chunks.insert().values(
                id="r1:i1",
                model_version="m:512",
                restaurant_id="r1",
                item_id="i1",
                name="i1",
                city="springfield",
                brand_id=None,
                cuisines=["pakistani", "bbq"],
                category="Mains",
                tags=["spicy", "halal"],
                price_cents=899,
                available=True,
                status="open",
                content="Chicken Biryani",
                content_hash="deadbeef",
                embedding=vector,
                updated_at=sa.func.now(),
            )
        )
        row = (await conn.execute(sa.select(item_chunks))).mappings().one()
    assert list(row["embedding"]) == vector
    assert list(row["tags"]) == ["spicy", "halal"]
    assert list(row["cuisines"]) == ["pakistani", "bbq"]
    assert row["price_cents"] == 899
    assert row["status"] == "open"


async def test_restaurant_chunk_round_trips(engine):
    async with engine.begin() as conn:
        await conn.execute(
            restaurant_chunks.insert().values(
                id="r1:_self",
                model_version="m:512",
                restaurant_id="r1",
                city="springfield",
                brand_id="b1",
                cuisines=["thai"],
                status="paused",
                content="Biryani House",
                content_hash="cafe",
                embedding=[0.1] * EMBEDDING_DIMENSIONS,
                updated_at=sa.func.now(),
            )
        )
        row = (await conn.execute(sa.select(restaurant_chunks))).mappings().one()
    assert list(row["cuisines"]) == ["thai"]
    assert row["status"] == "paused"


def test_vector_width_is_pinned_to_the_migration():
    """A width change is a migration plus a rolling reindex, never an env
    flip — so the DDL literal, the module constant and the settings default
    are one number in three places, held together here."""
    assert f"EMBEDDING_DIMENSIONS = {EMBEDDING_DIMENSIONS}" in MIGRATION
    assert Settings().embedding_dimensions == EMBEDDING_DIMENSIONS


def test_index_definitions_are_pinned_to_the_migration():
    """The opclass must match the query operator verbatim or the planner
    ignores the index and search degrades to a seq scan with no error."""
    assert f'VECTOR_OPS = "{VECTOR_OPS}"' in MIGRATION
    assert f'ITEM_HNSW_INDEX = "{ITEM_HNSW_INDEX}"' in MIGRATION
    assert f'RESTAURANT_HNSW_INDEX = "{RESTAURANT_HNSW_INDEX}"' in MIGRATION
    assert 'postgresql_using="hnsw"' in MIGRATION


def test_slug_filters_are_gin_indexed():
    """`tags @> ARRAY['spicy']` is containment; B-tree cannot serve it, and
    without GIN the filter is a scan of every candidate the ANN walk sees."""
    for index in ("ix_item_chunks_tags", "ix_item_chunks_cuisines"):
        assert index in MIGRATION
    assert 'postgresql_using="gin"' in MIGRATION


def test_hnsw_indexes_are_postgres_only():
    """Declaring them on the tables would put meaningless B-trees over JSON
    columns in every sqlite unit run."""
    declared = {i.name for i in item_chunks.indexes} | {i.name for i in restaurant_chunks.indexes}
    assert ITEM_HNSW_INDEX not in declared
    assert RESTAURANT_HNSW_INDEX not in declared


def test_chunk_identity_is_composite():
    """(id, model_version): a rolling reindex writes the new model's rows
    beside the old ones, so the same chunk legitimately exists twice."""
    for table in (item_chunks, restaurant_chunks):
        assert [c.name for c in table.primary_key.columns] == ["id", "model_version"]


def test_only_brand_id_is_nullable():
    """The point of splitting the tables: every column means something for
    every row. `brand_id` is the one honest exception — a transitional legacy
    branch has no brand minted yet, and catalog's payload says None rather
    than inventing one."""
    for table in (item_chunks, restaurant_chunks):
        nullable = {c.name for c in table.columns if c.nullable}
        assert nullable == {"brand_id"}, (table.name, nullable)
