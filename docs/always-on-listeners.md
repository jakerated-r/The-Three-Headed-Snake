# Always-On Per-Head Listeners

Each head (Maestro / Codex / Gemini) runs a warm listener that polls the broker for messages addressed
to it and replies via that head's CLI launcher. Guardrails prevent runaway loops / token burn:
addressing (envelope recipients OR @head/@all), per-thread max-turns + cooldown, a daily budget cap +
kill-switch, and a freshness window.

- `src/listeners/snake-listener.py`  — per-head listener (`python3 snake-listener.py Maestro`)
- `src/listeners/guardrails.py`       — addressing + relevance + budget gate
- `scripts/snake/claude-head.sh` / `codex-head.sh` / `gemini-head.sh` — per-head CLI launchers
  (force subscription auth, put node/CLI on PATH)
- `scripts/snake/snake.sh`            — `snake say "..."` ingestion + `snake watch` readable chat
- `scripts/snake/snake-budget.sh`     — daily cap + kill-switch
- `scripts/snake/snake-doctor.sh`     — true per-head auth/credit health
- `scripts/snake/start-snake-listeners.sh` — install all three under launchd KeepAlive

> Portability note: the reference copies use the author's local root path. Set `BRAIN_ROOT`, or edit the
> root-path constants (`BRAIN` in the `.py` files; `BRAIN_ROOT` default in the `.sh` files), for your machine.
