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
