#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export THREE_HEADED_SNAKE_ROOT="$ROOT"
export COOP_ROOT="$ROOT/data/coop"
export THREE_HEADED_SNAKE_RUN_ROOT="$ROOT/runs/orchestrator"
mkdir -p "$COOP_ROOT/broker" "$ROOT/logs" "$THREE_HEADED_SNAKE_RUN_ROOT"
while IFS='=' read -r env_name _; do
  case "$env_name" in
    *API_KEY*|*ACCESS_TOKEN*|*AUTH_TOKEN*|*SECRET*|*HMAC*|*TOKEN*)
      unset "$env_name"
      ;;
  esac
done < <(/usr/bin/env)
exec /usr/bin/python3 "$ROOT/src/orchestrator/three-headed-snake-orchestrator.py" "$@"
