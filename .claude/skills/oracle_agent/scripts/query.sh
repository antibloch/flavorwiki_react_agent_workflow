#!/usr/bin/env bash
# Run one read-only SQL statement against gpi-db and print the result.
#
#   query.sh "SELECT count(*) FROM public.survey"
#   query.sh --csv "SELECT id, title FROM public.survey LIMIT 5"
#   echo "SELECT ..." | query.sh
#
# This is the SAME database the agent under test and other developers use
# (there is no separate oracle copy), so the statement runs inside a
# read-only transaction: a stray write fails loudly instead of mutating
# shared, live data.
set -euo pipefail

CONTAINER="${GPI_DB_CONTAINER:-gpi-db}"
DB_USER="${GPI_DB_USER:-gpi}"
DB_NAME="${GPI_DB_NAME:-gpi_sample_db}"

# Flags in any order, so `--why` can precede or follow a format flag.
FMT=()
WHY=""
while true; do
  case "${1:-}" in
    --csv)    FMT=(--csv);        shift ;;
    --tuples) FMT=(-tA -F'|');    shift ;;
    --why)    WHY="${2:-}";       shift 2 ;;
    *) break ;;
  esac
done

SQL="${1:-$(cat)}"

# Read-only is set through PGOPTIONS rather than a leading `SET` statement so
# psql prints only the query's own result (a `SET` command tag would otherwise
# land in --csv output and be parsed as the header row).
# errexit off around the query itself: a failing statement must still reach the trajectory
# log below, and a SQL error is information for the analyst, not a reason to abort silently.
set +e
OUT=$(docker exec -e PGOPTIONS="-c default_transaction_read_only=on" -i "$CONTAINER" \
  psql -U "$DB_USER" -d "$DB_NAME" \
  -v ON_ERROR_STOP=1 -P pager=off "${FMT[@]}" -c "$SQL" 2>&1)
RC=$?
set -e
printf '%s\n' "$OUT"

# Trajectory log (PROTOCOL.md §6): a no-op unless ORACLE_TRACE_FILE is set.
if [ -n "${ORACLE_TRACE_FILE:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -z "$OUT" ]; then ROWS=0
  elif [ "${FMT[0]:-}" = "--csv" ]; then ROWS=$(( $(printf '%s\n' "$OUT" | grep -c .) - 1 ))
  else ROWS=$(printf '%s\n' "$OUT" | grep -c .)
  fi
  [ "$ROWS" -lt 0 ] && ROWS=0
  python3 - "$SCRIPT_DIR" "$SQL" "$WHY" "$RC" "$ROWS" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import trace_log
_, _, sql, why, rc, rows = sys.argv
ok = rc == "0"
trace_log.log("sql_retrieval", "query.sh", purpose=why, args={"sql": sql},
              outcome="ok" if ok else "error",
              detail=f"{rows} row(s)" if ok else "",
              error=None if ok else "psql exited non-zero")
PY
fi
exit $RC
