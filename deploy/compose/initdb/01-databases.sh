#!/bin/bash
# One logical database per service, one role per service, no cross-database grants.
# Mirrors ADR-0016 (sfo-aurora-main) structurally: same names, same isolation.
set -euo pipefail

create_db() {
  local db="$1" role="$2"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
    CREATE ROLE ${role} LOGIN PASSWORD '${role}';
    CREATE DATABASE ${db} OWNER ${role};
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
