#!/usr/bin/env bash
# Start the API and all workers for local development; Ctrl+C stops everything.
#   ./run_dev.sh            # 1 analytics shard, 1 aggregator shard
#   SHARDS=2 ./run_dev.sh   # N_w = 2 analytics and aggregator processes
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-.venv/bin/python}
SHARDS=${SHARDS:-1}

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" & pids+=($!)
"$PY" -m workers.archiver & pids+=($!)
for ((i = 0; i < SHARDS; i++)); do
  "$PY" -m workers.analytics --shard "$i" --shards "$SHARDS" & pids+=($!)
  "$PY" -m workers.aggregator --shard "$i" --shards "$SHARDS" & pids+=($!)
done
wait -n
