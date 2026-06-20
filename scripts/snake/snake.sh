#!/usr/bin/env bash
# snake — Three Headed Snake XXX entrypoint. Posts prompts to the broker (real exe, not a zsh alias),
# so prompting ANY head wakes the others. Maestro-owned (ingestion + guardrails lane).
set -uo pipefail
BRAIN="${BRAIN_ROOT:-/Users/rated-r/rated r brain}"
CTL="$BRAIN/.claude/coop/scripts/coop-brokerctl.sh"
GUARD="$BRAIN/outputs/coop-tools/snake/snake-budget.sh"
cmd="${1:-help}"; shift || true
case "$cmd" in
  say)
    msg=""; to="all"; from="Architect"
    while [ $# -gt 0 ]; do
      case "$1" in
        --to) to="$2"; shift 2;;
        --from) from="$2"; shift 2;;
        *) [ -z "$msg" ] && msg="$1"; shift;;
      esac
    done
    [ -z "$msg" ] && { echo "usage: snake say \"message\" [--to all|codex|gemini|maestro] [--from NAME]"; exit 2; }
    if ! bash "$GUARD" check; then echo "[snake] BUDGET/KILL active — not sent. 'snake resume' to lift."; exit 3; fi
    body="@${to} ${msg}"
    case "$to" in
      all) for peer in Maestro Codex Gemini; do bash "$CTL" send --from "$from" --to "$peer" --kind prompt --priority 1 --body "$body" >/dev/null; done;;
      maestro) bash "$CTL" send --from "$from" --to Maestro --kind prompt --priority 1 --body "$body" >/dev/null;;
      codex)   bash "$CTL" send --from "$from" --to Codex   --kind prompt --priority 1 --body "$body" >/dev/null;;
      gemini)  bash "$CTL" send --from "$from" --to Gemini  --kind prompt --priority 1 --body "$body" >/dev/null;;
      *) echo "unknown --to $to"; exit 2;;
    esac
    bash "$GUARD" tick
    echo "[snake] sent (@${to}) from ${from}: ${msg}"
    ;;
  watch)  exec python3 "$BRAIN/outputs/coop-tools/coop-chat.py" --replay 30 --poll-ms 200 ;;
  kill)   : > "$BRAIN/outputs/coop-tools/snake/.SNAKE_KILL"; echo "[snake] KILL-SWITCH ON." ;;
  resume) rm -f "$BRAIN/outputs/coop-tools/snake/.SNAKE_KILL"; echo "[snake] kill-switch cleared." ;;
  budget) bash "$GUARD" status ;;
  help|*) echo "snake say \"msg\" [--to all|codex|gemini|maestro] | snake watch | snake budget | snake kill | snake resume" ;;
esac
