#!/usr/bin/env bash
# gemini-head.sh — canonical Gemini head launcher for the Three Headed Snake.
# Forces Gemini API-key mode and blocks the rejected Code Assist/GCA OAuth path.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:$PATH"
unset GOOGLE_CLOUD_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS
export GOOGLE_GENAI_USE_GCA=false

if [ -z "${GEMINI_API_KEY:-}" ] && command -v security >/dev/null 2>&1; then
  key="$(/usr/bin/security find-generic-password -a "$USER" -s "${GEMINI_KEYCHAIN_SERVICE:-three-headed-snake.gemini-api-key}" -w 2>/dev/null || true)"
  if [ -n "$key" ]; then
    export GEMINI_API_KEY="$key"
  fi
fi

exec "${GEMINI_REAL_BIN:-gemini}" "$@"
