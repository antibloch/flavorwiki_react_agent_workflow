#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "deployment/.env is missing; copy .env.example and supply the required values." >&2
  exit 1
fi

exec uvicorn api_funda_agent_exp:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}" \
  --workers 1
