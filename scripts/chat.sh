#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export THREE_HEADED_SNAKE_ROOT="$ROOT"
export COOP_ROOT="$ROOT/data/coop"
mkdir -p "$COOP_ROOT/broker" "$ROOT/logs"
exec /usr/bin/python3 "$ROOT/src/chat/coop-chat.py" "$@"
