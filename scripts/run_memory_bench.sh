#!/usr/bin/env bash
# A/B the short-term-memory patch against the pre-memory build, then measure a real
# multi-round conversation. Alternates the two builds rep by rep so cache warmth and any
# service-side drift fall on both equally; the first pair is a discarded warmup.
set -u
cd "$(dirname "$0")/.."
OUT=needle_haystack_results/memory
mkdir -p "$OUT"
PY=${PY:-python}
REPS=${REPS:-4}

echo "### warmup (discarded)"
$PY scripts/bench_memory.py --agent funda_agent_exp_oracle_duo --mode single --rep 99 \
    --out "$OUT/warmup_oracle.json"  >/dev/null 2>&1
$PY scripts/bench_memory.py --agent funda_agent_exp            --mode single --rep 99 \
    --out "$OUT/warmup_patched.json" >/dev/null 2>&1

for r in $(seq 1 "$REPS"); do
  echo "### single A/B rep $r"
  $PY scripts/bench_memory.py --agent funda_agent_exp_oracle_duo --mode single --rep "$r" \
      --out "$OUT/single_oracle_r$r.json"  2>&1 | tail -2
  $PY scripts/bench_memory.py --agent funda_agent_exp            --mode single --rep "$r" \
      --out "$OUT/single_patched_r$r.json" 2>&1 | tail -2
done

for r in 1 2; do
  echo "### 6-round conversation, one thread, rep $r"
  $PY scripts/bench_memory.py --agent funda_agent_exp --mode rounds --rep "$r" \
      --out "$OUT/rounds_patched_r$r.json" 2>&1 | tail -2
done

echo "### same 6 questions with NO memory (fresh thread each), baseline"
$PY scripts/bench_memory.py --agent funda_agent_exp --mode nomemory --rep 1 \
    --out "$OUT/nomemory_r1.json" 2>&1 | tail -2

echo "### done -> $OUT"
