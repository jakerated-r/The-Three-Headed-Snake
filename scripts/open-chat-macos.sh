#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WINDOW_TOKEN="three-headed-snake-live-im-window"
if /usr/bin/pgrep -f "[c]oop-chat.py .*--window-token $WINDOW_TOKEN" >/dev/null 2>&1; then
  echo "[skip] chat already running" >&2
  exit 0
fi
/usr/bin/osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  set newWindow to do script "cd '$ROOT' && if ! /usr/bin/pgrep -f '[t]hree-headed-snake-orchestrator.py' >/dev/null 2>&1; then /usr/bin/nohup bash scripts/start-orchestrator.sh --poll-ms 200 >>logs/orchestrator.log 2>&1 & echo '[orchestrator started]'; else echo '[orchestrator already running]'; fi; bash scripts/chat.sh --replay 30 --poll-ms 200 --no-auto-drain --window-token '$WINDOW_TOKEN'"
  set custom title of front window to "Three Headed Snake"
end tell
APPLESCRIPT
