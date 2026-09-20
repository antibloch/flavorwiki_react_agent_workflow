#!/usr/bin/env bash
# Verify the shared gpi-db Postgres is up and queryable.
#
# The oracle has no database of its own - it reads the same gpi-db container
# the agent under test uses (flavorai_v2/docker-compose.yml, host port 5433).
# This script never starts or restores anything; it only checks readiness and
# points at the right place to start it if it's down.
set -euo pipefail

CONTAINER="${GPI_DB_CONTAINER:-gpi-db}"
DB_USER="${GPI_DB_USER:-gpi}"
DB_NAME="${GPI_DB_NAME:-gpi_sample_db}"
FLAVORAI_COMPOSE="/home/junaid/codework/Flavorwiki/flavorai_v2/docker-compose.yml"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "ERROR: container $CONTAINER is not running." >&2
  echo "This is the flavorai_v2 repo's own database, not the oracle's - start it there:" >&2
  echo "  docker compose -f $FLAVORAI_COMPOSE up -d" >&2
  exit 1
fi

count="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" \
  -tAc 'SELECT count(*) FROM public.survey' 2>/dev/null | tr -d '[:space:]')" || count=""
if [ -z "$count" ] || [ "$count" -le 0 ] 2>/dev/null; then
  echo "ERROR: $CONTAINER is running but public.survey is empty or unreadable." >&2
  docker logs --tail 30 "$CONTAINER" >&2 || true
  exit 1
fi

echo "$CONTAINER is up and queryable (host port 5433)."
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -P pager=off -c "
SELECT (SELECT count(*) FROM public.client)       AS clients,
       (SELECT count(*) FROM public.organization) AS organizations,
       (SELECT count(*) FROM public.survey)       AS surveys,
       (SELECT count(*) FROM public.enrollment)   AS enrollments,
       (SELECT count(*) FROM public.answer)       AS answers;"

# Trajectory log (PROTOCOL.md §6): a no-op unless ORACLE_TRACE_FILE is set.
if [ -n "${ORACLE_TRACE_FILE:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  python3 - "$SCRIPT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import trace_log
trace_log.log("db_health", "db_up.sh", purpose="confirm gpi-db is up and populated",
              outcome="ok", detail="container reachable, row counts printed")
PY
fi
