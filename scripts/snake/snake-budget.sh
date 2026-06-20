#!/usr/bin/env bash
set -uo pipefail
ROOT="${THREE_HEADED_SNAKE_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
DIR="${THREE_HEADED_SNAKE_LISTENER_RUN_DIR:-$ROOT/runs/listeners}"
mkdir -p "$DIR"
KILL="$DIR/.SNAKE_KILL"; DAY="$(date -u +%F)"; CNT="$DIR/.budget-$DAY"
CAP="${SNAKE_DAILY_CAP:-300}"
case "${1:-check}" in
  check)
    [ -f "$KILL" ] && exit 1
    n=$(cat "$CNT" 2>/dev/null || echo 0)
    if [ "$n" -ge "$CAP" ]; then : > "$KILL"; echo "[budget] cap $CAP hit -> auto-kill" >&2; exit 1; fi
    exit 0 ;;
  tick)   n=$(cat "$CNT" 2>/dev/null || echo 0); echo $((n+1)) > "$CNT" ;;
  status) n=$(cat "$CNT" 2>/dev/null || echo 0); ks=OFF; [ -f "$KILL" ] && ks=ON
          echo "snake budget: $n/$CAP msgs today (UTC $DAY) | kill-switch: $ks" ;;
esac
