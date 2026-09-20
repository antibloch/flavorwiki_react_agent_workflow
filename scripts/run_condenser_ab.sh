#!/usr/bin/env bash
# Workflow condenser vs agent condenser, on four SQL results large enough to park.
#
# The two arms differ by ENABLE_DIGEST alone -- every limit is left at its default, so both
# arms see the same park threshold and the same payload:
#   agent     ENABLE_DIGEST=0  weak model + 4 result-store tools, up to DRILL_MAX_STEPS turns
#   workflow  ENABLE_DIGEST=1  ONE weak turn reading Python statistics over EVERY row
#
# Both arms of a case run concurrently so they see identical API conditions. Payloads and the
# ground truth to score them against are in agent_exp_doc.md; the cases are chosen so the
# comparison discriminates -- L1/L3/L4 want coverage, L2 wants specific records.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO" || exit 1
PY="${PY:-uv run python}"; OUT="${OUT:-$REPO/needle_haystack_results/condenser_ab}"
LOGS="$OUT/logs"; mkdir -p "$OUT" "$LOGS"
CASES="${CASES:-L1 L2 L3 L4}"
# REPS matters: a single run of each arm cannot support a latency claim. Repeated measurement
# in this repo puts the wall-clock spread between identical runs at 2.1-5.4x, and an earlier
# A/B here moved with run ORDER (prompt-cache warmth) rather than with the build. So the arm
# order is alternated per rep as well, to keep cache warmth from loading one arm every time.
REPS="${REPS:-1}"
for R in $(seq 1 "$REPS"); do
for C in $CASES; do
  echo "=== $C rep$R $(date +%T) ==="
  A="$OUT/${C}_agent_r${R}.json"; W="$OUT/${C}_workflow_r${R}.json"
  LA="$LOGS/${C}_agent_r${R}.log"; LW="$LOGS/${C}_workflow_r${R}.log"
  if [ $((R % 2)) -eq 1 ]; then
    ( ENABLE_DIGEST=0 timeout 1500 $PY scripts/bench_bigoutput.py --case "$C" --arm agent \
        --out "$A" > "$LA" 2>&1 ) &
    ( ENABLE_DIGEST=1 timeout 1500 $PY scripts/bench_bigoutput.py --case "$C" --arm workflow \
        --out "$W" > "$LW" 2>&1 ) &
  else
    ( ENABLE_DIGEST=1 timeout 1500 $PY scripts/bench_bigoutput.py --case "$C" --arm workflow \
        --out "$W" > "$LW" 2>&1 ) &
    ( ENABLE_DIGEST=0 timeout 1500 $PY scripts/bench_bigoutput.py --case "$C" --arm agent \
        --out "$A" > "$LA" 2>&1 ) &
  fi
  wait
  grep -h "#####" "$LOGS"/"${C}"_*_r${R}.log 2>/dev/null || echo "  no summary for $C rep$R"
done
done
echo "ALL DONE $(date +%T)"
