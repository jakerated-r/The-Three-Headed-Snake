#!/usr/bin/env bash
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
exec "${CODEX_BIN:-/opt/homebrew/bin/codex}" "$@"
