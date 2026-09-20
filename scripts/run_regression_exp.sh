#!/usr/bin/env bash
# funda_agent_exp against regression.xlsx, four variants per row, run concurrently so
# every variant sees identical API conditions on the same question.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO" || exit 1
PY="${PY:-BE/.venv/bin/python}"; OUT="${OUT:-$REPO/needle_haystack_results/regression_exp}"
LOGS="$OUT/logs"; mkdir -p "$OUT" "$LOGS"
for R in $(seq 1 18); do
  echo "=== row $R $(date +%T) ==="
  for V in none bindings prefetch_fixed both; do
    timeout 900 "$PY" scripts/bench_regression_exp.py --row "$R" --variant "$V" \
      --out "$OUT/R$(printf %02d "$R")_${V}.json" > "$LOGS/R$(printf %02d "$R")_${V}.log" 2>&1 &
  done
  wait
  grep -h "#####" "$LOGS"/R$(printf %02d "$R")_*.log 2>/dev/null
done
echo "ALL DONE $(date +%T)"
