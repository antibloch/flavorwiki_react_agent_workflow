#!/usr/bin/env bash
# Three-way prefetch test on funda_agent_exp: none / prefetch / prefetch + join-gap fix.
# The three variants of a case run concurrently so they see identical API conditions.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO" || exit 1
PY="${PY:-BE/.venv/bin/python}"; OUT="${OUT:-$REPO/needle_haystack_results/prefetch}"
LOGS="$OUT/logs"; mkdir -p "$OUT" "$LOGS"
for C in T1 T2 T3 T4 T5 T6; do
  echo "=== $C $(date +%T) ==="
  for V in none prefetch prefetch_fixed; do
    timeout 900 "$PY" scripts/bench_prefetch.py --case "$C" --variant "$V" \
      --out "$OUT/${C}_${V}.json" > "$LOGS/${C}_${V}.log" 2>&1 &
  done
  wait
  grep -h "#####" "$LOGS"/"${C}"_*.log 2>/dev/null
done
echo "ALL DONE $(date +%T)"
