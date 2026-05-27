#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export THREE_HEADED_SNAKE_ROOT="$ROOT"
export COOP_ROOT="$ROOT/data/coop"
mkdir -p "$COOP_ROOT/broker" "$ROOT/logs"
exec /usr/bin/python3 "$ROOT/src/broker/coop_broker.py" serve \
  --host 127.0.0.1 \
  --port "${THREE_HEADED_SNAKE_PORT:-17874}" \
  --db "$COOP_ROOT/broker/coop-broker.sqlite3" \
  --token-file "$COOP_ROOT/broker/.token" \
  --mirror "$ROOT/logs/LIVE_BRIDGE.md" \
  --work-mirror "$ROOT/logs/TWIN_TOWERS.md"
