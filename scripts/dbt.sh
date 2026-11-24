#!/usr/bin/env bash
# Run dbt with .env loaded and profiles.yml taken from dbt/.
#
#   scripts/dbt.sh build
#   scripts/dbt.sh run --select staging
#   scripts/dbt.sh source freshness
#
# dbt reads real environment variables, not .env, so this wrapper loads .env
# first. Without it, profiles.yml cannot resolve its env_var() calls.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DBT_PROFILES_DIR="$REPO/dbt"
cd "$REPO/dbt"
exec "$REPO/.venv/bin/dotenv" -f "$REPO/.env" run -- "$REPO/.venv/bin/dbt" "$@"
