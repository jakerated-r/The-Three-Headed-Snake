# Always-On Per-Head Listeners

Each head (Maestro / Codex / Gemini) runs a warm listener that polls the broker for messages addressed
to it and replies via that head's CLI launcher. The room should feel instant because ACK / handoff /
working events post in milliseconds; real model replies still take as long as each CLI takes to infer.

Guardrails prevent runaway loops / token burn: Maestro + Codex envelope parsing, explicit `@head` /
`@all` tags, per-thread max-turns + cooldown, plumbing-event filtering, a daily budget cap +
kill-switch, and a freshness window.

- `src/listeners/snake-listener.py`  — per-head listener (`python3 snake-listener.py Maestro`)
- `src/listeners/guardrails.py`       — addressing + relevance + budget gate
- `scripts/snake/claude-head.sh` / `codex-head.sh` / `gemini-head.sh` — per-head CLI launchers.
  Claude strips Anthropic API env so subscription login wins; Gemini defaults to `gemini-2.5-flash-lite`
  and strips rejected GCA / Code Assist env.
- `scripts/snake/snake.sh`            — `snake say "..."` ingestion + `snake watch` readable chat
- `scripts/snake/snake-budget.sh`     — daily cap + kill-switch
- `scripts/snake/snake-doctor.sh`     — true per-head auth/credit health
- `scripts/snake/start-snake-listeners.sh` — install all three under launchd KeepAlive

By default the public scripts derive paths from the repo root. Override with `THREE_HEADED_SNAKE_ROOT`,
`COOP_ROOT`, `SNAKE_GEMINI_MODEL`, `CLAUDE_REAL_BIN`, `CODEX_BIN`, or `GEMINI_REAL_BIN` when needed.
