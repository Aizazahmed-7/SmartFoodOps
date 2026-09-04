"""assistant_db schema.

B0 creates only the outbox. That looks premature — nothing publishes yet —
but it is the cheapest moment to prove the whole persistence path end to
end (initdb create_db -> Alembic at startup -> /readyz SELECT 1), rather
than discovering it is broken during B1 while also debugging embeddings.
The AI plane's facts are a product KPI, so they publish through the outbox
like every other domain fact (ADR-0002, PRD FR-94) — never direct to Kafka.

menu_chunks (B1), conversations/messages (B3), taste_profiles (B4),
content_drafts (B6) land in their own migrations. Everything here must stay
sqlite-creatable: the unit suite runs `metadata.create_all` on sqlite, so
pgvector columns arrive behind the same dialect split Postgres-only DDL
already uses elsewhere.
"""

import sqlalchemy as sa
from smartfood_outbox import outbox_table

metadata = sa.MetaData()

outbox = outbox_table(metadata)
