#!/bin/bash
# One logical database per service, one role per service, no cross-database grants.
# Mirrors ADR-0016 (sfo-aurora-main) structurally: same names, same isolation.
#
# IDEMPOTENT on purpose: postgres only runs this directory on a FRESH
# volume, so adding a service's database later would otherwise require
# `make nuke`. The up-m* targets re-run this script against the live
# container instead — existing databases are left untouched, missing ones
# converge.
set -euo pipefail

create_db() {
  local db="$1" role="$2"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
    DO \$\$
    BEGIN
      CREATE ROLE ${role} LOGIN PASSWORD '${role}';
    EXCEPTION WHEN duplicate_object THEN
      NULL;  -- role exists (re-run against a live volume)
    END
    \$\$;
    SELECT 'CREATE DATABASE ${db} OWNER ${role}'
      WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')
    \gexec
    REVOKE CONNECT ON DATABASE ${db} FROM PUBLIC;
    GRANT CONNECT ON DATABASE ${db} TO ${role};
SQL
}

create_db identity_db identity_svc
create_db catalog_db catalog_svc

# pg_trgm needs superuser; pre-create it here so catalog's migration
# (CREATE EXTENSION IF NOT EXISTS) no-ops as catalog_svc. ADR-0019.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d catalog_db \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
create_db inventory_db inventory_svc
create_db order_db order_svc
create_db payment_db payment_svc
create_db notification_db notification_svc
create_db analytics_db analytics_svc
create_db assistant_db assistant_svc

# `vector` and `pg_trgm` both need superuser, exactly like pg_trgm for
# catalog above: pre-create them here so the assistant's migrations
# (CREATE EXTENSION IF NOT EXISTS) no-op as assistant_svc, which holds no
# superuser rights and is not getting any. ADR-0032. The image that ships
# pgvector is pinned to a TRIXIE-based tag — see the postgres block in
# docker-compose.yml for why that suffix matters.
#
# pg_trgm is the hybrid retriever's fuzzy half (B2): the lexical leg lives
# HERE rather than in catalog, because catalog's HybridSearch adapter calls
# the assistant — calling back for the lexical leg would be a cycle.
#
# hnsw.iterative_scan is a CORRECTNESS setting, not a tuning one. pgvector
# applies a WHERE clause as a POST-filter on the ANN walk, so under a
# selective filter — a price band, a tag, one restaurant — a query silently
# returns fewer rows than it asked for. Measured 2026-09-21 on 20k chunks
# with a filter matching 400: LIMIT 10 returned EIGHT rows at the default,
# and ten with iterative_scan on. `strict_order` because RRF fuses by rank,
# so rows arriving out of distance order would feed the fusion a rank the
# index never meant.
#
# Set here, as SUPERUSER, and not in a migration: until pgvector's library
# is loaded these are PLACEHOLDER parameters, and Postgres requires
# superuser to set a placeholder it cannot classify — the service role owns
# the database and still gets "permission denied to set parameter".
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d assistant_db \
  -c "CREATE EXTENSION IF NOT EXISTS vector" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" \
  -c "ALTER DATABASE assistant_db SET hnsw.iterative_scan = strict_order"

# Read-only role for Grafana's business dashboard (S7). SELECT and nothing
# else: a dashboard is a guest in the database — it may look, never touch.
# Default privileges cover tables analytics_svc creates AFTER this grant
# (migrations run at service boot, often later than this script).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-'SQL'
  DO $$
  BEGIN
    CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro';
  EXCEPTION WHEN duplicate_object THEN
    NULL;
  END
  $$;
  GRANT CONNECT ON DATABASE analytics_db TO grafana_ro;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d analytics_db <<-'SQL'
  GRANT USAGE ON SCHEMA public TO grafana_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
  ALTER DEFAULT PRIVILEGES FOR ROLE analytics_svc IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_ro;
SQL
