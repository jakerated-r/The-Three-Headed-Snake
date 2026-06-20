#!/usr/bin/env bash
# snake doctor — true per-head CLI health. Separates the daemon-PATH failure (node not found)
# from account failures (credit/auth/rate-limit) so the chat stops mislabeling everything "auth failed".
set -uo pipefail
BRAIN="/Users/rated-r/rated r brain"
CLAUDE_HEAD="$BRAIN/outputs/coop-tools/snake/claude-head.sh"
GEMINI_HEAD="$BRAIN/outputs/coop-tools/snake/gemini-head.sh"
classify() {
  local o="$1"
  if   echo "$o" | grep -qiE "env: node|node: No such|command not found|No such file or directory"; then echo "RUNNER-PATH — node/CLI not on PATH (daemon env too thin; run via login shell or set PATH)"
  elif echo "$o" | grep -qiE "credit balance is too low|insufficient cred|billing"; then echo "WRONG-AUTH-PATH — Claude saw API credits; Maestro launcher must strip Anthropic API env"
  elif echo "$o" | grep -qiE "rate limit|usage limit|\b429\b";                     then echo "RATE-LIMIT — cool down / reset"
  elif echo "$o" | grep -qiE "log ?in|unauthor|not authenticated|invalid api key|expired|/login"; then echo "AUTH — needs login"
  elif echo "$o" | grep -qiE "(^|[^a-z])OK([^a-z]|$)";                              then echo "OK — authenticated + funded"
  else echo "UNKNOWN — $(echo "$o" | head -1)"; fi
}
NODE_BARE="$(command -v node 2>/dev/null || echo MISSING)"
echo "== THREE HEADED SNAKE — head health ($(date -u +%H:%M:%SZ)) =="
echo "-- node on DAEMON PATH: $NODE_BARE $([ "$NODE_BARE" = MISSING ] && echo '  <<< runner MUST add node to PATH (the real CLI-failed cause)')"
echo "-- Maestro / claude :"
echo "   subscription launcher : $(classify "$(env ANTHROPIC_API_KEY=DO_NOT_USE ANTHROPIC_AUTH_TOKEN=DO_NOT_USE ANTHROPIC_BASE_URL=https://invalid.example "$CLAUDE_HEAD" -p 'reply with the single word OK' --output-format text --permission-mode plan --disable-slash-commands --tools '' --model opus 2>&1 | head -3)")"
echo "   auth source           : $(env ANTHROPIC_API_KEY=DO_NOT_USE ANTHROPIC_AUTH_TOKEN=DO_NOT_USE ANTHROPIC_BASE_URL=https://invalid.example "$CLAUDE_HEAD" auth status 2>&1 | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g; s/\"email\"[[:space:]]*:[[:space:]]*\"[^\"]+\"/\"email\":\"<redacted>\"/g; s/\"orgId\"[[:space:]]*:[[:space:]]*\"[^\"]+\"/\"orgId\":\"<redacted>\"/g; s/\"orgName\"[[:space:]]*:[[:space:]]*\"[^\"]+\"/\"orgName\":\"<redacted>\"/g; s/(sk-ant-[A-Za-z0-9_-]+)/<redacted-token>/g' | cut -c1-220)"
echo "-- Codex   / codex  : $(zsh -lc 'command -v codex' 2>/dev/null || echo NOT-FOUND)  $([ -f "$HOME/.codex/auth.json" ] && echo '(auth.json present)' || echo '(no auth.json)')"
echo "-- Gemini  / gemini : $(classify "$(env GOOGLE_GENAI_USE_GCA=true GOOGLE_CLOUD_ACCESS_TOKEN=DO_NOT_USE "$GEMINI_HEAD" -p 'reply with the single word OK' -m gemini-2.5-flash --approval-mode plan --output-format text --skip-trust --extensions '' 2>&1 | head -5)")"
