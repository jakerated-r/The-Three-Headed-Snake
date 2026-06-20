#!/usr/bin/env python3
"""
coop-chat.py — IM/Messages-style live chat console for the Maestro/Codex/Gemini three-headed-snake.

Renders broker messages like a group-chat app — color-coded name tags, plain
English bodies (JSON envelopes extracted to readable text), HH:MM:SS timestamps,
blank-line spacing between turns. Infinite loop — NEVER times out. Architect's
directive: "to the second" + "plain English" + "instant messaging platform between you 3."

What you see:

    [14:54:32] MAESTRO
      YES handle these suggestions — execute all

    [14:54:33] CODEX → Architect
      I read you. I’m checking the moving parts and I’ll bring back the real result here.

    [14:54:33] GEMINI → Maestro · co-builder auto-engage
      Standing by. Pre-prompt wrapper installed via ~/.zshrc alias.

Modes:
  default                tail forever, second-precision, plain English
  interactive TTY        press Enter to broadcast Jake's prompt to all three
  /to Gemini <prompt>    send to one agent from the live console
  --send TEXT            send one Architect prompt and exit (test/script mode)
  --from-agent Codex     send a visible agent room line instead of a Jake/Architect prompt
  --replay N             show last N then tail
  --no-tail              show replay/new messages then exit (test/snapshot mode)
  --sqlite               read SQLite directly (faster, no token)
  --no-color             plain text (pipe-to-file mode)
  --raw                  also show raw JSON envelope (debug)
  --show-plumbing        show broker/tool envelopes, fanout duplicates, and artifacts
  --since-id N           start watermark
  --poll-ms N            poll interval (default 250ms)

Canonized 2026-05-27 by Maestro under Architect "IM platform between you 3 · never times out · to the second · plain English" directive.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

BRAIN = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
COOP = Path(os.environ.get("COOP_ROOT", str(BRAIN / "data" / "coop")))
BROKER_URL = os.environ.get("COOP_BROKER_URL", "http://127.0.0.1:17874")
TOKEN_FILE = COOP / "broker" / ".token"
SQLITE_PATH = COOP / "broker" / "coop-broker.sqlite3"
ARCHITECT_RUNNER = BRAIN / "src" / "runners" / "architect-runner" / "coop-architect-drain.py"
ARCHITECT_RUNNER_LOG = BRAIN / "logs" / "architect-runner.log"
ORCHESTRATOR_SCRIPT = BRAIN / "src" / "orchestrator" / "three-headed-snake-orchestrator.py"
PYTHON_BIN = os.environ.get("COOP_PYTHON_BIN", "/usr/bin/python3")
POLL_MS_DEFAULT = 25
ARCHITECT_AGENT = "Architect"
SNAKE_AGENTS = ("Codex", "Maestro", "Gemini")
CHAT_SENDERS = ("Architect", "Codex", "Maestro", "Gemini", "Bridge")

# ----- ANSI colors --------------------------------------------------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITAL = "\033[3m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GOLD = "\033[38;5;214m"
    GRAY = "\033[90m"

NAME_COLOR = {
    "Maestro": C.BOLD + C.BLUE,
    "Codex": C.BOLD + C.RED,
    "Gemini": C.BOLD + C.GREEN,
    "Architect": C.BOLD + C.GOLD,
    "Bridge": C.BOLD + C.CYAN,
    "broadcast": C.BOLD + C.MAGENTA,
}

# Per-agent emoji avatar — Terminal.app renders 🧠⚡✨👑🌉 cleanly on macOS.
# ASCII fallback retained for terminals that lack emoji rendering — toggle via --ascii flag.
NAME_AVATAR_EMOJI = {
    "Maestro": "🧠",
    "Codex": "⚡",
    "Gemini": "✨",
    "Architect": "👑",
    "Bridge": "🌉",
    "broadcast": "📣",
}
NAME_AVATAR_ASCII = {
    "Maestro": "«M»",
    "Codex": "«C»",
    "Gemini": "«G»",
    "Architect": "«A»",
    "Bridge": "«B»",
    "broadcast": "«*»",
}
# Default to emoji; --ascii flag flips this at runtime in main()
NAME_GLYPH = NAME_AVATAR_EMOJI

# Kinds that trigger a Terminal bell/chime — pulls Architect attention
CHIME_KINDS = {"emergency-stop", "review", "blocker"}

KIND_TAG = {
    "auto-engage": "auto-engage",
    "handoff": "handoff",
    "review": "review",
    "task": "task",
    "receipt": "receipt",
    "ack": "ack",
    "status": "status",
    "heartbeat": "heartbeat",
    "note": "note",
    "blocker": "blocker",
    "emergency-stop": "EMERGENCY-STOP",
    "request": "request",
    "health": "health",
}


def fmt_time(iso_or_db: str) -> str:
    """HH:MM:SS UTC — fallback when no nanosecond timestamp."""
    try:
        s = iso_or_db.replace("Z", "+00:00")
        if " " in s and "+" not in s and "T" not in s:
            s = s.replace(" ", "T") + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return iso_or_db[:8]


def fmt_ns(ns: int) -> str:
    """Epoch nanoseconds → HH:MM:SS.nnnnnnnnn UTC."""
    seconds, nanos = divmod(int(ns), 1_000_000_000)
    d = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{d.strftime('%H:%M:%S')}.{nanos:09d}"


def fmt_second_from_ns(ns: int) -> str:
    """Epoch nanoseconds → HH:MM:SS UTC for IM-style second-level display."""
    seconds = int(ns) // 1_000_000_000
    d = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return d.strftime("%H:%M:%S")


def strip_control_noise(text: str) -> str:
    """Remove boilerplate that makes broker payloads read less like conversation."""
    text = text.replace("Read the originating prompt summary. ", "")
    text = text.replace("advisory=read+FYI, co-builder=claim parallel lane and build, verifier=stand by for verify-me envelope.", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_runner_stdout(text: str) -> str:
    """Strip CLI/tool protocol noise before a runner receipt hits Jake's chat."""
    cleaned = "\n".join(line for line in text.splitlines() if not line.startswith("THREE_HEADED_SNAKE_XXX_RECEIPT:")).strip()
    cleaned = re.sub(r"(?s)^\s*update_topic\([^\n]*?\)", "", cleaned).strip()
    return cleaned


def summarize_runner_failure(body: dict) -> str:
    """Collapse CLI stderr into one plain-language blocker line for the live IM view."""
    agent = str(body.get("agent") or "Agent")
    exit_code = body.get("exit")
    stderr = str(body.get("stderr_tail") or "").strip()
    artifacts = str(body.get("artifacts") or "").strip()
    lower = stderr.lower()
    if "credit balance is too low" in lower or "credit_balance_too_low" in lower:
        reason = "Claude tried the API-credit path; Maestro must use subscription auth"
    elif agent == "Maestro" and ("authentication_error" in lower or "invalid authentication credentials" in lower or "failed to authenticate" in lower):
        reason = "Claude subscription token needs refresh; API auth is disabled for Maestro"
    elif "authentication_error" in lower or "invalid authentication credentials" in lower or "failed to authenticate" in lower or "invalid x-api-key" in lower or "api key" in lower and "invalid" in lower:
        reason = "CLI authentication failed"
    elif "ineligibletiererror" in stderr or "not eligible" in lower and "gemini" in lower:
        reason = "Gemini CLI account tier is not eligible for this run"
    elif "code assist" in lower and ("not eligible" in lower or "tier" in lower):
        reason = "Gemini Code Assist is blocked by account tier"
    elif "[timeout]" in lower or "timed out" in lower:
        reason = "CLI run timed out"
    elif "cli binary is not available" in lower:
        reason = "CLI binary is missing"
    elif exit_code is not None:
        reason = f"CLI exited with code {exit_code}"
    else:
        reason = "CLI failed"
    suffix = f"\n  Evidence: {artifacts}" if artifacts else ""
    return f"{agent} is blocked: {reason}.{suffix}"


def parse_body(body: object) -> tuple[dict, str]:
    """Returns (body_dict_or_None, plain_text). Handles JSON-string bodies + envelope unwrap."""
    if isinstance(body, str):
        s = body.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                body = json.loads(s)
            except Exception:
                return None, s
        else:
            return None, body
    if isinstance(body, dict):
        return body, ""
    return None, str(body)


def extract_plain_english(body: object, *, show_plumbing: bool = False) -> str:
    """Convert a broker message body to chat-readable plain English."""
    b, plain = parse_body(body)
    if plain:
        return plain
    if not b:
        return repr(body)

    # Jake prompt injected from the Three Headed Snake XXX terminal.
    if b.get("type") == "architect-prompt":
        prompt = (b.get("prompt") or b.get("message") or "").strip()
        source = b.get("source", "three-headed-snake-chat-cli")
        return prompt or f"(Architect prompt from {source}, empty body)"

    # Plain-English room dialogue: this is the default surface Jake asked for.
    if b.get("type") == "snake-room-line":
        return str(b.get("text") or "").strip() or "(empty room line)"

    # If body wraps a cross-fire envelope, unwrap it
    if "envelope" in b and isinstance(b["envelope"], dict):
        env = b["envelope"]
        env_type = env.get("type", "")
        payload = env.get("payload", {}) or {}
        if env_type == "cross-fire-request":
            briefing = payload.get("briefing", "").strip()
            asks = payload.get("asks", []) or []
            parts = []
            if briefing:
                parts.append(briefing)
            for a in asks[:3]:
                qid = a.get("id", "?")
                q = a.get("question", "")
                parts.append(f"  {qid}: {q}")
            if len(asks) > 3:
                parts.append(f"  (+ {len(asks)-3} more questions)")
            return "\n".join(parts) if parts else "(cross-fire request, empty payload)"
        if env_type == "cross-fire-reply":
            answers = payload.get("answers", []) or []
            parts = []
            for a in answers[:3]:
                qid = a.get("id", "?")
                ans = a.get("answer", "")
                parts.append(f"  {qid}: {ans}")
            if len(answers) > 3:
                parts.append(f"  (+ {len(answers)-3} more answers)")
            nx = payload.get("next_actions", [])
            if nx:
                parts.append(f"  Next: {'; '.join(nx[:3])}")
            return "\n".join(parts) if parts else "(reply envelope, empty payload)"
        # Generic envelope payload
        notes = payload.get("notes") or payload.get("message") or payload.get("status")
        if notes:
            return str(notes)
        return f"(envelope type={env_type})"

    # Auto-engage payload (broker task envelope)
    if b.get("type") == "auto-engage":
        mode = b.get("engagement_mode", "?")
        origin = b.get("originating_agent", "?")
        summary = (b.get("prompt_summary") or "").strip()
        thread = b.get("thread", "?")
        out = f"ENGAGE [{mode}] from {origin} · thread={thread}"
        if summary:
            out += f"\n  prompt: {strip_control_noise(summary)[:300]}"
        return out

    # Live-room orchestrator events: render as speech/status, not JSON.
    if b.get("type") == "snake-live-event":
        phase = str(b.get("phase") or "status").upper()
        text = str(b.get("text") or b.get("status") or "").strip()
        if not show_plumbing:
            return text or phase
        latency = b.get("latency_ms")
        conversation = b.get("conversation_id")
        suffix = []
        if isinstance(latency, int):
            suffix.append(f"{latency}ms")
        if conversation:
            suffix.append(str(conversation))
        tail = f" · {' · '.join(suffix)}" if suffix else ""
        return f"{phase}{tail}\n  {text}" if text else f"{phase}{tail}"

    # Peer-verification-required payload
    if b.get("type") == "peer-verification-required":
        goal = b.get("goal", "?")
        claim_id = (b.get("claim_id") or "?")[:8]
        ev = b.get("evidence") or {}
        if not isinstance(ev, dict):
            ev = {"summary": str(ev)}
        summary = ev.get("summary", "")
        return f"VERIFY REQUEST · claim={claim_id} · {goal}\n  {summary[:500]}"

    # Runner/test receipts: show the actual spoken/stdout text, not just PASS.
    if b.get("type") in {"gemini-run-result", "test-run-result", "three-headed-snake-agent-run-result"}:
        verdict = b.get("verdict") or ("PASS" if b.get("exit") == 0 else "FAIL")
        agent = b.get("agent")
        stdout = (b.get("stdout_tail") or "").strip()
        stderr = (b.get("stderr_tail") or "").strip()
        artifacts = b.get("artifacts")
        parts = [f"{agent}: {verdict}" if agent else f"{verdict}"] if show_plumbing else []
        if verdict != "PASS" and not show_plumbing:
            failure_body = dict(b)
            failure_body["stderr_tail"] = "\n".join(part for part in (stdout, stderr) if part).strip()
            parts.append(summarize_runner_failure(failure_body))
        elif stdout:
            cleaned = clean_runner_stdout(stdout)
            if cleaned:
                parts.append(cleaned)
        if stderr and verdict != "PASS" and show_plumbing:
            parts.append(f"stderr: {stderr[:300]}")
        if artifacts and show_plumbing:
            parts.append(f"artifacts: {artifacts}")
        return "\n".join(parts) if parts else str(verdict)

    # Common keys
    for key in ("message", "notes", "summary", "body", "ask", "request", "verdict", "status"):
        v = b.get(key)
        if isinstance(v, str) and v.strip():
            return strip_control_noise(v.strip())

    # Fall back to compact JSON
    return json.dumps(b, separators=(", ", ": "))[:500]


def render_chat_message(msg: dict, no_color: bool, width: int, show_raw: bool = False, show_plumbing: bool = False) -> str:
    # Prefer epoch nanoseconds, display to the second for IM-style readability.
    ns = int(msg.get("created_ns", 0) or 0)
    ts = fmt_second_from_ns(ns) if ns else fmt_time(msg.get("created_at", ""))
    frm = str(msg.get("from_agent", "?"))
    to = str(msg.get("to_agent", "?"))
    kind = str(msg.get("kind", "?"))
    body = msg.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass

    name_c = NAME_COLOR.get(frm, C.BOLD + C.WHITE) if not no_color else ""
    dim_c = C.DIM if not no_color else ""
    rst = C.RESET if not no_color else ""
    to_c = NAME_COLOR.get(to, C.WHITE).replace(C.BOLD, "") if not no_color else ""

    # Compact "→ <to> · <kind>" suffix (skip if to is broadcast or kind is plain)
    suffix_parts = []
    if to and to not in ("broadcast",):
        suffix_parts.append(f"→ {to_c}{to}{rst}")
    if show_plumbing and kind in ("auto-engage", "review", "emergency-stop", "blocker", "verify-request", "task", "receipt", "handoff", "status"):
        suffix_parts.append(f"{dim_c}· {kind}{rst}")
    suffix = "  " + "  ".join(suffix_parts) if suffix_parts else ""

    # Header line: [HH:MM:SS] «M» MAESTRO   → To · kind
    glyph = NAME_GLYPH.get(frm, "•")
    name_upper = frm.upper()
    header = f"{dim_c}[{ts}]{rst} {name_c}{glyph} {name_upper:<10s}{rst}{suffix}"

    # Body lines: extracted plain English, indented 2 spaces
    text = extract_plain_english(body, show_plumbing=show_plumbing)
    indent = "  "
    body_lines = [indent + line for line in text.split("\n")]
    # Wrap long lines to width
    wrapped = []
    target_w = max(40, width - 4)
    for line in body_lines:
        while len(line) > target_w:
            # Find a space to break at
            br = line.rfind(" ", 0, target_w)
            if br < 20:
                br = target_w
            wrapped.append(line[:br])
            line = indent + line[br:].lstrip()
        wrapped.append(line)

    out = header + "\n" + "\n".join(wrapped)
    if show_raw:
        raw = json.dumps(body, separators=(",", ":"))[:200] if body else ""
        if raw:
            out += f"\n{dim_c}  raw: {raw}{rst}"
    return out


def architect_prompt_key(msg: dict) -> tuple[str, str] | None:
    body = msg.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict) or body.get("type") != "architect-prompt":
        return None
    prompt = str(body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        return None
    return (str(msg.get("target") or body.get("thread") or ""), prompt)


def is_legacy_role_ceremony(body: dict) -> bool:
    text = str(body.get("text") or "").lower()
    legacy_phrases = (
        "i’ve got the build lane",
        "i've got the build lane",
        "standards lane",
        "outside-angle lane",
        "implementation lane claimed",
        "i’ll hold the criteria and final judgment",
        "i'll hold the criteria and final judgment",
        "watch for blind spots",
        "keep the room honest",
        "pressure-test the standard",
        "this room is moving in plain english now. the plumbing is still running underneath",
    )
    return any(phrase in text for phrase in legacy_phrases)


def is_plumbing_message(msg: dict, seen_architect_prompts: set[tuple[str, str]]) -> bool:
    """Default chat hides durable broker machinery and keeps human-readable room lines."""
    body = msg.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = {}
    body = body if isinstance(body, dict) else {}
    body_type = str(body.get("type") or "")

    if body_type == "snake-room-line":
        return is_legacy_role_ceremony(body)
    if body_type in {"auto-engage", "peer-verification-required", "snake-live-event"}:
        return True
    if body_type in {"gemini-run-result", "test-run-result"}:
        return True
    if body_type == "architect-prompt":
        source = str(body.get("source") or "")
        key = architect_prompt_key({**msg, "body": body})
        if source == "three-headed-snake-live-room-fanout":
            return True
        if key is not None:
            if key in seen_architect_prompts:
                return True
            seen_architect_prompts.add(key)
        return False
    if str(msg.get("kind") or "") in {"review", "blocker", "emergency-stop"}:
        return False
    return False


# ----- data sources --------------------------------------------------------
def fetch_sqlite(since_id: int, limit: int = 200) -> list[dict]:
    uri = f"file:{SQLITE_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    created_ns_expr = "created_ns" if "created_ns" in columns else "0 AS created_ns"
    rows = conn.execute(
        f"SELECT id, message_id, created_at, {created_ns_expr}, from_agent, to_agent, kind, priority, "
        "target, body_json, status, ack_by, ack_at "
        "FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
        (since_id, limit),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["body"] = json.loads(d.pop("body_json"))
        except Exception:
            d["body"] = d.pop("body_json", "")
        out.append(d)
    return out


def fetch_http(since_id: int, limit: int = 200) -> list[dict]:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
    qs = urllib.parse.urlencode({"limit": limit, "since_id": since_id})
    req = urllib.request.Request(f"{BROKER_URL}/v1/messages?{qs}", method="GET")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("X-Coop-Token", token)
    with urllib.request.urlopen(req, timeout=3) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msgs = data.get("messages", [])
    msgs.sort(key=lambda m: int(m.get("id", 0)))
    return msgs


def fetch(since_id: int, limit: int, source: str) -> list[dict]:
    if source == "sqlite":
        return fetch_sqlite(since_id, limit)
    try:
        return fetch_http(since_id, limit)
    except (urllib.error.URLError, urllib.error.HTTPError):
        return fetch_sqlite(since_id, limit)


def latest_message_id() -> int:
    """Return the true latest broker message id without relying on capped HTTP pages."""
    if SQLITE_PATH.exists():
        uri = f"file:{SQLITE_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    msgs = fetch(0, 10000, "http")
    return max((int(m.get("id", 0) or 0) for m in msgs), default=0)


# ----- Architect prompt input / dispatch ----------------------------------
class ChatCommandError(ValueError):
    """Bad Architect console command."""


def broker_token() -> str:
    if not TOKEN_FILE.exists():
        raise RuntimeError(f"broker token missing: {TOKEN_FILE}")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def post_broker_message(payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{BROKER_URL}/v1/messages", data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Coop-Token", broker_token())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = parsed.get("error", body)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"broker rejected message ({exc.code}): {detail}") from exc


def normalize_recipient(raw: str) -> str:
    value = (raw or "").strip().lower()
    aliases = {
        "codex": "Codex",
        "maestro": "Maestro",
        "claude": "Maestro",
        "gemini": "Gemini",
    }
    if value not in aliases:
        raise ChatCommandError("recipient must be Codex, Maestro, Gemini, Claude, or all")
    return aliases[value]


def normalize_sender(raw: str) -> str:
    value = (raw or ARCHITECT_AGENT).strip()
    aliases = {
        "jake": ARCHITECT_AGENT,
        "architect": ARCHITECT_AGENT,
        "codex": "Codex",
        "maestro": "Maestro",
        "claude": "Maestro",
        "gemini": "Gemini",
        "bridge": "Bridge",
    }
    normalized = aliases.get(value.lower(), value)
    if normalized not in CHAT_SENDERS:
        raise ChatCommandError("sender must be Architect, Jake, Codex, Maestro, Claude, Gemini, or Bridge")
    return normalized


def recipients_from_target(raw: str) -> tuple[str, ...]:
    value = (raw or "all").strip().lower()
    if value in {"all", "three-headed-snake", "everyone", "broadcast"}:
        return SNAKE_AGENTS
    return (normalize_recipient(value),)


def new_architect_thread() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"architect-cli-{stamp}-{uuid.uuid4().hex[:6]}"


def build_architect_prompt_messages(
    prompt: str,
    recipients: tuple[str, ...] | list[str],
    *,
    target: str | None = None,
    priority: int = 10,
) -> tuple[str, list[dict]]:
    text = (prompt or "").strip()
    if not text:
        raise ChatCommandError("prompt cannot be empty")
    unique_recipients = tuple(dict.fromkeys(normalize_recipient(agent) for agent in recipients))
    thread = target or new_architect_thread()
    payloads = []
    for recipient in unique_recipients:
        payloads.append(
            {
                "from_agent": ARCHITECT_AGENT,
                "to_agent": recipient,
                "kind": "task",
                "priority": priority,
                "target": thread,
                "body": {
                    "type": "architect-prompt",
                    "prompt": text,
                    "message": text,
                    "thread": thread,
                    "source": "three-headed-snake-chat-cli",
                    "recipients": list(unique_recipients),
                    "target_agent": recipient,
                    "instructions": (
                        "Treat this as Jake speaking from the Three Headed Snake XXX terminal. "
                        "Answer through the coop broker/terminal like a normal group chat: casual, direct, useful, and no role ceremony."
                    ),
                },
            }
        )
    return thread, payloads


def build_room_line_message(
    text: str,
    sender: str,
    recipients: tuple[str, ...] | list[str],
    *,
    target: str | None = None,
    priority: int = 7,
) -> tuple[str, dict]:
    clean_text = (text or "").strip()
    if not clean_text:
        raise ChatCommandError("message cannot be empty")
    source = normalize_sender(sender)
    if source == ARCHITECT_AGENT:
        raise ChatCommandError("Architect prompts must use build_architect_prompt_messages")
    normalized_recipients = tuple(dict.fromkeys(normalize_recipient(agent) for agent in recipients))
    if len(normalized_recipients) == 1 and normalized_recipients[0] != source:
        to_agent = normalized_recipients[0]
        listener = to_agent
    else:
        to_agent = ARCHITECT_AGENT
        listener = "room"
    thread = target or new_architect_thread()
    return thread, {
        "from_agent": source,
        "to_agent": to_agent,
        "kind": "note",
        "priority": max(1, min(priority, 10)),
        "target": thread,
        "body": {
            "type": "snake-room-line",
            "conversation_id": thread,
            "source_kind": "agent-room-line",
            "originator": source,
            "speaker": source,
            "listener": listener,
            "recipients": list(normalized_recipients),
            "text": clean_text,
        },
    }


def send_architect_prompt(
    prompt: str,
    recipients: tuple[str, ...] | list[str],
    *,
    target: str | None = None,
    priority: int = 10,
) -> tuple[str, list[dict]]:
    thread, payloads = build_architect_prompt_messages(prompt, recipients, target=target, priority=priority)
    responses = [post_broker_message(payload) for payload in payloads]
    return thread, responses


def send_room_line(
    text: str,
    sender: str,
    recipients: tuple[str, ...] | list[str],
    *,
    target: str | None = None,
    priority: int = 7,
) -> tuple[str, list[dict]]:
    thread, payload = build_room_line_message(text, sender, recipients, target=target, priority=priority)
    return thread, [post_broker_message(payload)]


def trigger_architect_runner(thread: str, recipients: tuple[str, ...] | list[str]) -> None:
    if not ARCHITECT_RUNNER.exists():
        return
    ARCHITECT_RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON_BIN,
        str(ARCHITECT_RUNNER),
        "--once",
        "--target",
        thread,
        "--agents",
        ",".join(recipients),
        "--limit",
        "500",
        "--ack-failures",
    ]
    log_handle = ARCHITECT_RUNNER_LOG.open("a", encoding="utf-8")
    subprocess.Popen(cmd, stdout=log_handle, stderr=log_handle, cwd=str(BRAIN), close_fds=True, start_new_session=True)
    log_handle.close()


def orchestrator_running() -> bool:
    if not ORCHESTRATOR_SCRIPT.exists():
        return False
    try:
        proc = subprocess.run(
            ["/usr/bin/pgrep", "-f", "[t]hree-headed-snake-orchestrator.py"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False


def parse_composer_line(line: str) -> tuple[str, tuple[str, ...], str]:
    stripped = line.strip()
    if not stripped:
        return "noop", (), ""
    lowered = stripped.lower()
    if lowered in {"/quit", "/exit"}:
        return "quit", (), ""
    if lowered in {"/help", "help"}:
        return "help", (), ""
    if lowered == "/clear":
        return "clear", (), ""
    if lowered.startswith("/all "):
        return "send", SNAKE_AGENTS, stripped[5:].strip()
    if lowered == "/all":
        raise ChatCommandError("usage: /all <prompt>")
    if lowered.startswith("/to "):
        parts = stripped.split(maxsplit=2)
        if len(parts) < 3:
            raise ChatCommandError("usage: /to Codex|Maestro|Gemini <prompt>")
        return "send", recipients_from_target(parts[1]), parts[2].strip()

    alias_commands = {
        "/codex": "Codex",
        "/maestro": "Maestro",
        "/claude": "Maestro",
        "/gemini": "Gemini",
    }
    for alias, recipient in alias_commands.items():
        if lowered == alias:
            raise ChatCommandError(f"usage: {alias} <prompt>")
        if lowered.startswith(alias + " "):
            return "send", (recipient,), stripped[len(alias):].strip()

    if stripped.startswith("/"):
        raise ChatCommandError("unknown command; type /help")
    return "send", SNAKE_AGENTS, stripped


def chat_help() -> str:
    return "\n".join(
        [
            "Jake input is live:",
            "  plain text              broadcast to Codex, Maestro, and Gemini; live-room orchestrator wakes the room",
            "  /all <prompt>           broadcast to all three",
            "  /to Gemini <prompt>     send to one agent",
            "  /codex <prompt>         direct alias",
            "  /maestro <prompt>       direct alias",
            "  /gemini <prompt>        direct alias",
            "  /clear                  clear the terminal",
            "  /quit                   stop this chat window",
            "  diagnostics             run with --show-plumbing to see broker/tool envelopes",
        ]
    )


def print_prompt(no_color: bool) -> None:
    prompt = "Jake > " if no_color else f"{C.BOLD}{C.GOLD}Jake > {C.RESET}"
    sys.stdout.write(prompt)
    sys.stdout.flush()


def handle_composer_line(line: str, no_color: bool, priority: int, auto_drain: bool) -> bool:
    try:
        action, recipients, prompt = parse_composer_line(line)
        if action == "noop":
            return False
        if action == "quit":
            print("[three-headed-snake chat stopped by Jake]" if no_color else f"{C.DIM}[three-headed-snake chat stopped by Jake]{C.RESET}")
            return True
        if action == "clear":
            print("\033c", end="")
            return False
        if action == "help":
            print(chat_help())
            return False
        thread, responses = send_architect_prompt(prompt, recipients, priority=priority)
        if not all(item.get("ok") for item in responses):
            raise RuntimeError(json.dumps(responses, ensure_ascii=False))
        should_trigger_legacy = auto_drain and not orchestrator_running()
        if should_trigger_legacy:
            trigger_architect_runner(thread, recipients)
        names = ", ".join(recipients)
        if orchestrator_running():
            drain_note = " · live room waking"
        else:
            drain_note = " · runners waking" if should_trigger_legacy else ""
        msg = f"[sent] Jake -> {names} · thread={thread}{drain_note}"
        print(msg if no_color else f"{C.GREEN}{msg}{C.RESET}")
        return False
    except Exception as exc:
        msg = f"[input error] {exc}"
        print(msg if no_color else f"{C.RED}{msg}{C.RESET}")
        return False


# ----- main loop -----------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="IM-style live three-headed-snake chat console (never times out)")
    p.add_argument("--replay", type=int, default=20, help="Show last N then tail (default 20)")
    p.add_argument("--sqlite", action="store_true", help="Read SQLite directly")
    p.add_argument("--no-tail", action="store_true", help="Print replay/current messages then exit")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--raw", action="store_true", help="Also show raw JSON body")
    p.add_argument("--show-plumbing", action="store_true", help="Show broker/tool envelopes, fanout duplicates, diagnostic status, and artifacts")
    p.add_argument("--since-id", type=int, default=None)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--poll-ms", type=int, default=POLL_MS_DEFAULT)
    p.add_argument("--window-token", default="", help=argparse.SUPPRESS)
    p.add_argument("--no-input", action="store_true", help="Disable Architect prompt input in interactive TTY mode")
    p.add_argument("--send", default=None, help="Send one Architect prompt through the broker and exit")
    p.add_argument("--to", default="all", help="Recipient for --send: all, Codex, Maestro, Gemini, or Claude")
    p.add_argument("--from-agent", default="Architect", help="Sender for --send. Use Codex/Maestro/Gemini/Bridge for agent room lines; Architect/Jake for real Jake prompts")
    p.add_argument("--priority", type=int, default=10, help="Broker priority for Architect prompts (default 10)")
    p.add_argument("--auto-drain", action="store_true", help="With --send, also run local agent CLIs once for replies")
    p.add_argument("--no-auto-drain", action="store_true", help="Disable local agent CLI replies for typed Jake prompts")
    p.add_argument("--ascii", action="store_true", help="Use «M»«C»«G» ASCII glyphs instead of 🧠⚡✨ emoji (terminals without emoji font fallback)")
    args = p.parse_args()

    # Glyph mode — emoji by default (confirmed Terminal.app render 2026-05-27), ASCII fallback
    global NAME_GLYPH
    if args.ascii:
        NAME_GLYPH = NAME_AVATAR_ASCII
    else:
        NAME_GLYPH = NAME_AVATAR_EMOJI

    source = "sqlite" if args.sqlite else "http"
    width = args.width or shutil.get_terminal_size((120, 50)).columns

    if args.send is not None:
        recipients = recipients_from_target(args.to)
        from_agent = normalize_sender(args.from_agent)
        if from_agent == ARCHITECT_AGENT:
            thread, responses = send_architect_prompt(args.send, recipients, priority=args.priority)
        else:
            thread, responses = send_room_line(args.send, from_agent, recipients, priority=args.priority)
        if not all(item.get("ok") for item in responses):
            print(json.dumps({"ok": False, "responses": responses}, indent=2, ensure_ascii=False), file=sys.stderr)
            return 1
        if args.auto_drain and from_agent == ARCHITECT_AGENT:
            trigger_architect_runner(thread, recipients)
        drain_note = " · runners waking in background" if args.auto_drain and from_agent == ARCHITECT_AGENT else ""
        display_sender = "Jake" if from_agent == ARCHITECT_AGENT else from_agent
        display_target = (
            ", ".join(recipients)
            if from_agent == ARCHITECT_AGENT
            else ("room" if args.to.lower() in {"all", "everyone", "broadcast", "three-headed-snake"} else ", ".join(recipients))
        )
        print(f"[sent] {display_sender} -> {display_target} · thread={thread}{drain_note}")
        return 0

    # Starting watermark
    if args.since_id is not None:
        since_id = args.since_id
    else:
        since_id = max(0, latest_message_id() - args.replay)

    # Banner
    title = (
        f"{C.BOLD}{C.GOLD}══ THREE HEADED SNAKE XXX LIVE CHAT ══{C.RESET}   "
        f"{NAME_COLOR['Maestro']}MAESTRO{C.RESET}  "
        f"{NAME_COLOR['Codex']}CODEX{C.RESET}  "
        f"{NAME_COLOR['Gemini']}GEMINI{C.RESET}  "
        f"{NAME_COLOR['Architect']}ARCHITECT{C.RESET}    "
        f"{C.DIM}poll={args.poll_ms}ms · source={source} · conversation view · never times out{C.RESET}"
    ) if not args.no_color else "== THREE HEADED SNAKE XXX LIVE CHAT ==  poll={}ms source={} conversation-view never-timeout".format(args.poll_ms, source)
    print(title)
    print("─" * min(width, 100) if not args.no_color else "-" * min(width, 100))
    print()
    input_enabled = sys.stdin.isatty() and not args.no_input and not args.no_tail
    if input_enabled:
        print(chat_help())
        print()
        print_prompt(args.no_color)

    last_id = since_id
    seen_architect_prompts: set[tuple[str, str]] = set()

    # Infinite loop — NEVER times out per Architect mandate
    while True:
        try:
            msgs = fetch(last_id, 200, source)
            saw_visible_messages = False
            for m in msgs:
                if not args.show_plumbing and is_plumbing_message(m, seen_architect_prompts):
                    last_id = max(last_id, int(m["id"]))
                    continue
                saw_visible_messages = True
                line = render_chat_message(m, args.no_color, width, show_raw=args.raw, show_plumbing=args.show_plumbing)
                # Terminal bell chime for high-attention kinds (Architect polish)
                kind = str(m.get("kind", ""))
                if kind in CHIME_KINDS:
                    sys.stdout.write("\a")  # ASCII BEL — Terminal.app rings
                print(line)
                print()  # blank line between turns — IM-style spacing
                sys.stdout.flush()
                last_id = max(last_id, int(m["id"]))
            if saw_visible_messages and input_enabled:
                print_prompt(args.no_color)
            if args.no_tail:
                if not saw_visible_messages:
                    print("[no new three-headed-snake chat messages]" if args.no_color else f"{C.DIM}[no new three-headed-snake chat messages]{C.RESET}")
                return 0
            if input_enabled:
                ready, _, _ = select.select([sys.stdin], [], [], args.poll_ms / 1000.0)
                if ready:
                    line = sys.stdin.readline()
                    if line == "":
                        input_enabled = False
                    elif handle_composer_line(line, args.no_color, args.priority, auto_drain=not args.no_auto_drain):
                        return 0
                    if input_enabled:
                        print_prompt(args.no_color)
            else:
                time.sleep(args.poll_ms / 1000.0)
        except KeyboardInterrupt:
            print(f"\n{C.DIM}[three-headed-snake chat stopped by user]{C.RESET}" if not args.no_color else "\n[three-headed-snake chat stopped by user]")
            return 0
        except Exception as exc:
            # Never crash — log + retry
            print(f"{C.RED}[chat error] {exc} · retrying in 2s{C.RESET}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main() or 0)
