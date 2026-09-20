#!/usr/bin/env bash
# Four large-SQL-output edge cases, drilling on vs off. Both arms of a case run
# concurrently so they see identical API conditions.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO" || exit 1
PY="${PY:-BE/.venv/bin/python}"; OUT="${OUT:-$REPO/needle_haystack_results/bigoutput}"
LOGS="$OUT/logs"; mkdir -p "$OUT" "$LOGS"
BIG=100000000
for C in E1 E2 E3 E4; do
  echo "=== $C $(date +%T) ==="
  ( TOOL_CHAR_LIMIT=8000 SMALL_RESULT_ROWS=25 SMALL_RESULT_CHARS=8000 CANDIDATE_LIST_CHARS=40000 \
    timeout 1500 "$PY" scripts/bench_bigoutput.py --case "$C" --arm drill_on \
      --out "$OUT/${C}_drill_on.json" > "$LOGS/${C}_drill_on.log" 2>&1 ) &
  ( TOOL_CHAR_LIMIT=$BIG SMALL_RESULT_ROWS=$BIG SMALL_RESULT_CHARS=$BIG CANDIDATE_LIST_CHARS=$BIG \
    timeout 1500 "$PY" scripts/bench_bigoutput.py --case "$C" --arm drill_off \
      --out "$OUT/${C}_drill_off.json" > "$LOGS/${C}_drill_off.log" 2>&1 ) &
  wait
  grep -h "#####" "$LOGS"/"${C}"_*.log 2>/dev/null || echo "  no summary for $C"
done
echo "ALL DONE $(date +%T)"
