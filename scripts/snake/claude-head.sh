#!/usr/bin/env bash
# claude-head.sh — canonical Maestro (Claude) head launcher for the Three Headed Snake.
# Forces SUBSCRIPTION auth (never API credits) and fixes the daemon-can't-find-node failure.
# Codex's Maestro listener daemon MUST invoke claude through this, never bare `claude`.
set -uo pipefail
# node + CLI dirs so a thin daemon env can launch claude (claude is a node script)
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:$PATH"
# FORCE subscription: Claude Code must use its own claude.ai keychain login, not
# Console/API credits and not a copied setup-token value that can stale out.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN
exec "${CLAUDE_REAL_BIN:-/Users/rated-r/.local/share/fnm/aliases/default/bin/claude}" "$@"
