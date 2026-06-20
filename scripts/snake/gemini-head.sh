#!/usr/bin/env bash
# gemini-head.sh — canonical Gemini head launcher for the Three Headed Snake.
# Forces Gemini API-key mode and blocks the rejected Code Assist/GCA OAuth path.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:$PATH"
unset GOOGLE_CLOUD_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS
export GOOGLE_GENAI_USE_GCA=false

if [ -z "${GEMINI_API_KEY:-}" ]; then
  key="$(/usr/bin/security find-generic-password -a "$USER" -s com.jakeratedr.thsxxx.gemini-api-key -w 2>/dev/null || true)"
  if [ -n "$key" ]; then
    export GEMINI_API_KEY="$key"
  fi
fi

exec "${GEMINI_REAL_BIN:-/opt/homebrew/bin/gemini}" "$@"
