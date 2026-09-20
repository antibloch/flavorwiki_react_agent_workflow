#!/usr/bin/env bash
# Phase 2. Three things phase 1 could not answer:
#   1. order control -- phase 1 always ran the pre-memory build first, so the patched build
#      inherited a warmer prompt cache. Here the order is reversed.
#   2. a MULTI-TURN loop -- phase 1's question answered in one turn, which never revisits
#      call_model and so cannot show per-turn overhead. --no-inventory forces SQL discovery.
#   3. the trim actually ENGAGING -- 12 rounds pushes the thread past HISTORY_MAX_MESSAGES+1,
#      where the window stops being the whole history.
set -u
cd "$(dirname "$0")/.."
OUT=needle_haystack_results/memory
mkdir -p "$OUT"
PY=${PY:-python}

for r in 5 6 7 8; do
  echo "### single A/B rep $r  (PATCHED FIRST -- order control)"
  $PY scripts/bench_memory.py --agent funda_agent_exp            --mode single --rep "$r" \
      --order patched-first --out "$OUT/single_patched_r$r.json" 2>&1 | tail -2
  $PY scripts/bench_memory.py --agent funda_agent_exp_oracle_duo --mode single --rep "$r" \
      --order patched-first --out "$OUT/single_oracle_r$r.json"  2>&1 | tail -2
done

for r in 1 2 3; do
  echo "### multi-turn A/B rep $r  (no inventory -> SQL discovery loop)"
  $PY scripts/bench_memory.py --agent funda_agent_exp_oracle_duo --mode single --rep "$r" \
      --no-inventory --order oracle-first --out "$OUT/multiturn_oracle_r$r.json"  2>&1 | tail -2
  $PY scripts/bench_memory.py --agent funda_agent_exp            --mode single --rep "$r" \
      --no-inventory --order oracle-first --out "$OUT/multiturn_patched_r$r.json" 2>&1 | tail -2
done
for r in 4 5 6; do
  echo "### multi-turn A/B rep $r  (reversed order)"
  $PY scripts/bench_memory.py --agent funda_agent_exp            --mode single --rep "$r" \
      --no-inventory --order patched-first --out "$OUT/multiturn_patched_r$r.json" 2>&1 | tail -2
  $PY scripts/bench_memory.py --agent funda_agent_exp_oracle_duo --mode single --rep "$r" \
      --no-inventory --order patched-first --out "$OUT/multiturn_oracle_r$r.json"  2>&1 | tail -2
done

for r in 1 2; do
  echo "### 12-round conversation, one thread, rep $r  (trim engages from round 7)"
  $PY scripts/bench_memory.py --agent funda_agent_exp --mode rounds --rounds 12 --rep "1$r" \
      --out "$OUT/rounds12_patched_r$r.json" 2>&1 | tail -2
done

echo "### done -> $OUT"
