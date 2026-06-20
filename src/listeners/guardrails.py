#!/usr/bin/env python3
"""Snake guardrails v4.

Understands plain @tags, Codex room envelopes, and Maestro cross-fire envelopes.
Keeps always-on listeners quiet-by-default with per-head turn/cooldown state.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

DIR = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.environ.get("THREE_HEADED_SNAKE_LISTENER_RUN_DIR")
if RUN_DIR is None:
    root = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
    RUN_DIR = str(root / "runs" / "listeners")
os.makedirs(RUN_DIR, exist_ok=True)
STATE = os.environ.get("SNAKE_THREAD_STATE", os.path.join(RUN_DIR, ".thread-state.json"))
MAX_TURNS = int(os.environ.get("SNAKE_MAX_TURNS", "6"))
COOLDOWN_S = int(os.environ.get("SNAKE_COOLDOWN_S", "8"))
HEADS = {"codex", "maestro", "gemini", "architect", "bridge"}


def _jsonish(body: Any) -> Any:
    if isinstance(body, str) and body.strip().startswith("{"):
        try:
            return json.loads(body)
        except Exception:
            return body
    return body


def _env(body: Any) -> dict[str, Any] | None:
    value = _jsonish(body)
    if not isinstance(value, dict):
        return None
    nested = value.get("envelope")
    if isinstance(nested, dict):
        return nested
    return value


def _payload(env: dict[str, Any] | None) -> dict[str, Any]:
    payload = (env or {}).get("payload")
    return payload if isinstance(payload, dict) else {}


def _lower_set(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if isinstance(value, list):
            out.update(_lower_set(value))
        elif value is not None:
            out.add(str(value).lower())
    return out


def is_addressed(body: Any, me: str) -> bool:
    me_l = me.lower()
    env = _env(body)
    payload = _payload(env)
    if env:
        addressed = _lower_set([
            env.get("to"),
            env.get("target_agent"),
            env.get("listener"),
            env.get("speaker"),
            env.get("effective_agents"),
            env.get("requested_recipients"),
            env.get("recipients"),
            payload.get("to"),
            payload.get("target_agent"),
            payload.get("effective_agents"),
            payload.get("requested_recipients"),
            payload.get("recipients"),
        ])
        if me_l in addressed or "all" in addressed or "broadcast" in addressed:
            return True
        text = message_text(env)
    else:
        text = str(body or "")
    tags = {tag.lower() for tag in re.findall(r"@([A-Za-z][A-Za-z0-9_-]*)", text)}
    return "all" in tags or me_l in tags


def message_text(body: Any) -> str:
    env = _env(body)
    if not env:
        return str(body or "")[:1500]

    payload = _payload(env)
    direct = env.get("text") or env.get("prompt") or env.get("message") or env.get("prompt_summary") or env.get("prompt_preview")
    if direct:
        return str(direct)[:1500]

    env_type = str(env.get("type") or "")
    if env_type == "cross-fire-request":
        parts: list[str] = []
        briefing = str(payload.get("briefing") or "").strip()
        if briefing:
            parts.append(briefing)
        asks = payload.get("asks") or []
        if isinstance(asks, list):
            for idx, ask in enumerate(asks[:4], start=1):
                if isinstance(ask, dict):
                    qid = ask.get("id") or f"Q{idx}"
                    question = ask.get("question") or ask.get("ask") or ""
                    if question:
                        parts.append(f"{qid}: {question}")
                elif ask:
                    parts.append(str(ask))
            if len(asks) > 4:
                parts.append(f"(+ {len(asks) - 4} more asks)")
        return "\n".join(parts).strip()[:1500] or "(cross-fire request)"

    if env_type == "cross-fire-reply":
        parts = []
        answers = payload.get("answers") or []
        if isinstance(answers, list):
            for idx, answer in enumerate(answers[:4], start=1):
                if isinstance(answer, dict):
                    qid = answer.get("id") or f"A{idx}"
                    text = answer.get("answer") or answer.get("response") or ""
                    if text:
                        parts.append(f"{qid}: {text}")
                elif answer:
                    parts.append(str(answer))
            if len(answers) > 4:
                parts.append(f"(+ {len(answers) - 4} more answers)")
        next_actions = payload.get("next_actions") or []
        if isinstance(next_actions, list) and next_actions:
            parts.append("Next: " + "; ".join(str(item) for item in next_actions[:3]))
        return "\n".join(parts).strip()[:1500] or "(cross-fire reply)"

    generic = (
        payload.get("briefing")
        or payload.get("notes")
        or payload.get("message")
        or payload.get("status")
        or payload.get("summary")
        or env.get("notes")
        or env.get("status")
    )
    return str(generic or env.get("type") or body or "")[:1500]


def thread_of(body: Any, fallback: str = "room") -> str:
    env = _env(body)
    if not env:
        return fallback
    return str(env.get("thread") or env.get("conversation_id") or _payload(env).get("thread") or fallback)


def _state_key(thread: str, me: str) -> str:
    return f"{thread}::{me.lower()}"


def should_respond(body: Any, me: str, thread: str) -> tuple[bool, str]:
    if not is_addressed(body, me):
        return False, "not addressed"
    state = _load().get(_state_key(thread, me), {"turns": 0, "last": 0})
    if state["turns"] >= MAX_TURNS:
        return False, "max-turns"
    if time.time() - state["last"] < COOLDOWN_S:
        return False, "cooldown"
    return True, "ok"


def record(thread: str, me: str = "room") -> None:
    data = _load()
    key = _state_key(thread, me)
    state = data.get(key, {"turns": 0, "last": 0})
    state["turns"] += 1
    state["last"] = time.time()
    data[key] = state
    _save(data)


def _load() -> dict[str, Any]:
    try:
        with open(STATE, encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


if __name__ == "__main__":
    codex_env = {"effective_agents": ["Codex", "Maestro"], "text": "hi"}
    maestro_req = {
        "envelope": {
            "envelope_version": "1.0",
            "from": "Maestro",
            "to": "Codex",
            "thread": "t3",
            "type": "cross-fire-request",
            "payload": {"briefing": "Check this.", "asks": [{"id": "Q1", "question": "Does it work?"}]},
        }
    }
    maestro_reply = {
        "envelope_version": "1.0",
        "from": "Codex",
        "to": "Maestro",
        "thread": "t4",
        "type": "cross-fire-reply",
        "payload": {"answers": [{"id": "Q1", "answer": "Yes."}], "next_actions": ["Ship it."]},
    }
    assert is_addressed(codex_env, "Maestro") is True
    assert is_addressed({"effective_agents": ["Codex"], "text": "x"}, "Maestro") is False
    assert is_addressed("@all hello", "Gemini") is True
    assert is_addressed("@maestro hello", "Maestro") is True
    assert is_addressed("@maestro hello", "Gemini") is False
    assert is_addressed(maestro_req, "Codex") is True
    assert "Does it work?" in message_text(maestro_req)
    assert "Ship it." in message_text(maestro_reply)
    assert thread_of(maestro_req) == "t3"
    record("t5", "Codex")
    assert should_respond("@all sound off", "Codex", "t5")[0] is False
    assert should_respond("@all sound off", "Maestro", "t5")[0] is True
    print("guardrails v4 self-test PASS (Codex + Maestro envelopes + @tag addressing)")
