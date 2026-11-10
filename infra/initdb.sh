#!/usr/bin/env bash
# Runs once, on first start of an empty data volume: creates the owner/migrator/app roles and the `memoria` schema.
# Passwords come from Docker secrets and are piped through stdin (never as process arguments).
set -euo pipefail
{
  printf "\\set pw_app '%s'\n\\set pw_mig '%s'\n\\set db '%s'\n" \
    "$(cat /run/secrets/dfmem_app)" "$(cat /run/secrets/dfmem_migrador)" "$POSTGRES_DB"
  cat /bootstrap/bootstrap.sql
} | psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"
