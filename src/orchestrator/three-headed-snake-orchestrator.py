#!/usr/bin/env python3
"""Persistent live-room orchestrator for The Three Headed Snake XXX.

The broker and chat console were already live. This daemon watches Jake's
broker prompts continuously, fans one-agent prompts out to all three heads, and
launches slower agent CLI receipts in the background. Diagnostic status events
still exist for --show-plumbing, but default chat view should show the real
agent replies instead of canned role ceremony.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BRAIN = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
COOP = Path(os.environ.get("COOP_ROOT", str(BRAIN / "data" / "coop")))
TOKEN_FILE = COOP / "broker" / ".token"
SQLITE_PATH = COOP / "broker" / "coop-broker.sqlite3"
BROKER_URL = os.environ.get("COOP_BROKER_URL", "http://127.0.0.1:17874")
RUN_ROOT = Path(os.environ.get("THREE_HEADED_SNAKE_RUN_ROOT", str(BRAIN / "runs" / "orchestrator")))
STATE_PATH = RUN_ROOT / "state.json"
LOG_PATH = RUN_ROOT / "orchestrator.log"
ARCHITECT_RUNNER = BRAIN / "src" / "runners" / "architect-runner" / "coop-architect-drain.py"
ARCHITECT_RUNNER_LOG = BRAIN / "logs" / "architect-runner.log"
PYTHON_BIN = os.environ.get("COOP_PYTHON_BIN", "/usr/bin/python3")

SNAKE_AGENTS = ("Codex", "Maestro", "Gemini")
ARCHITECT_AGENT = "Architect"
PEER_RING = {"Codex": "Maestro", "Maestro": "Gemini", "Gemini": "Codex"}

ACK_TEXT = {
    "Codex": "Got you. I’m checking it and I’ll drop the real answer here.",
    "Maestro": "I’m on it. I’ll keep it straight and skip the speech.",
    "Gemini": "Got it. I’ll give you the useful take, not the ceremony.",
}

PEER_HANDOFF = {
    "Codex": "Maestro, jump in only if you’ve got something useful.",
    "Maestro": "Gemini, keep it tight.",
    "Gemini": "Codex, bring proof if there’s proof.",
}

ROOM_DIALOGUE = {
    "Codex": {
        "to": ARCHITECT_AGENT,
        "text": "Got you. I’ll bring back the real answer.",
    },
    "Maestro": {
        "to": ARCHITECT_AGENT,
        "text": "Heard. I’ll keep it plain.",
    },
    "Gemini": {
        "to": ARCHITECT_AGENT,
        "text": "Got it. I’ll keep it useful.",
    },
}


@dataclass(frozen=True)
class PromptGroup:
    source_kind: str
    key: str
    thread: str
    prompt: str
    originator: str
    requested_recipients: tuple[str, ...]
    effective_agents: tuple[str, ...]
    source_messages: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class LiveEvent:
    from_agent: str
    to_agent: str
    kind: str
    phase: str
    text: str
    sequence: int
    delay_ms: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now()} {message}"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def clamp_priority(value: Any, default: int = 9) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = default
    return max(1, min(priority, 10))


def normalize_agent(agent: str) -> str | None:
    value = (agent or "").strip()
    if value == "Claude":
        value = "Maestro"
    return value if value in set(SNAKE_AGENTS) else None


def unique_agents(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values:
        agent = normalize_agent(str(raw))
        if agent and agent not in out:
            out.append(agent)
    return tuple(out)


def body_dict(message: dict[str, Any]) -> dict[str, Any]:
    body = message.get("body", {})
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return {}
    return body if isinstance(body, dict) else {}


def architect_prompt_text(message: dict[str, Any]) -> str:
    body = body_dict(message)
    if message.get("from_agent") != ARCHITECT_AGENT or body.get("type") != "architect-prompt":
        return ""
    return str(body.get("prompt") or body.get("message") or "").strip()


def auto_engage_text(message: dict[str, Any]) -> str:
    body = body_dict(message)
    if body.get("type") != "auto-engage":
        return ""
    if normalize_agent(str(message.get("from_agent", ""))) is None:
        return ""
    if normalize_agent(str(message.get("to_agent", ""))) is None:
        return ""
    return str(body.get("prompt_summary") or body.get("message") or "").strip()


def is_architect_prompt(message: dict[str, Any]) -> bool:
    return bool(architect_prompt_text(message))


def is_auto_engage(message: dict[str, Any]) -> bool:
    return bool(auto_engage_text(message))


def group_key(source_kind: str, thread: str, originator: str, prompt: str) -> str:
    return f"{source_kind}:{thread}:{originator}:{sha12(prompt)}"


def effective_room_agents(requested: tuple[str, ...]) -> tuple[str, ...]:
    # Jake's current criterion: prompting one head wakes the whole room.
    return SNAKE_AGENTS


def group_messages(messages: list[dict[str, Any]]) -> list[PromptGroup]:
    buckets: dict[str, dict[str, Any]] = {}
    for message in messages:
        if is_architect_prompt(message):
            body = body_dict(message)
            prompt = architect_prompt_text(message)
            thread = str(message.get("target") or body.get("thread") or "architect-terminal")
            requested = unique_agents(list(body.get("recipients") or []) + [str(message.get("to_agent", ""))])
            originator = ARCHITECT_AGENT
            source_kind = "architect-prompt"
        elif is_auto_engage(message):
            body = body_dict(message)
            prompt = auto_engage_text(message)
            thread = str(message.get("target") or body.get("thread") or "auto-engage")
            originator = normalize_agent(str(body.get("originating_agent") or message.get("from_agent"))) or str(message.get("from_agent"))
            requested = unique_agents([originator, str(message.get("to_agent", ""))])
            source_kind = "auto-engage"
        else:
            continue
        if not prompt:
            continue
        key = group_key(source_kind, thread, originator, prompt)
        bucket = buckets.setdefault(
            key,
            {
                "source_kind": source_kind,
                "key": key,
                "thread": thread,
                "prompt": prompt,
                "originator": originator,
                "requested": [],
                "messages": [],
            },
        )
        for agent in requested:
            if agent not in bucket["requested"]:
                bucket["requested"].append(agent)
        bucket["messages"].append(message)

    groups: list[PromptGroup] = []
    for bucket in buckets.values():
        requested = unique_agents(tuple(bucket["requested"]))
        groups.append(
            PromptGroup(
                source_kind=str(bucket["source_kind"]),
                key=str(bucket["key"]),
                thread=str(bucket["thread"]),
                prompt=str(bucket["prompt"]),
                originator=str(bucket["originator"]),
                requested_recipients=requested,
                effective_agents=effective_room_agents(requested),
                source_messages=tuple(bucket["messages"]),
            )
        )
    groups.sort(key=lambda group: min(int(m.get("id", 0) or 0) for m in group.source_messages))
    return groups


def build_live_events(group: PromptGroup) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    seq = 1
    for agent in group.effective_agents:
        events.append(
            LiveEvent(
                from_agent=agent,
                to_agent=ARCHITECT_AGENT,
                kind="status",
                phase="ack",
                text=ACK_TEXT[agent],
                sequence=seq,
            )
        )
        seq += 1
    for agent in group.effective_agents:
        peer = PEER_RING[agent]
        events.append(
            LiveEvent(
                from_agent=agent,
                to_agent=peer,
                kind="handoff",
                phase="handoff",
                text=PEER_HANDOFF[agent],
                sequence=seq,
                delay_ms=75,
            )
        )
        seq += 1
    for agent in group.effective_agents:
        events.append(
            LiveEvent(
                from_agent=agent,
                to_agent=ARCHITECT_AGENT,
                kind="status",
                phase="working",
                text=f"{agent} is working on it. The reply will show here.",
                sequence=seq,
                delay_ms=75,
            )
        )
        seq += 1
    return events


def build_room_dialogue(group: PromptGroup) -> list[LiveEvent]:
    """Plain-English lines meant for Jake's terminal, not broker diagnostics."""
    events: list[LiveEvent] = []
    seq = 100
    for agent in group.effective_agents:
        dialogue = ROOM_DIALOGUE[agent]
        events.append(
            LiveEvent(
                from_agent=agent,
                to_agent=dialogue["to"],
                kind="note",
                phase="dialogue",
                text=dialogue["text"],
                sequence=seq,
                delay_ms=50,
            )
        )
        seq += 1
    return events


class StateStore:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"last_seen_id": 0, "processed_keys": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"last_seen_id": 0, "processed_keys": {}}
            data.setdefault("last_seen_id", 0)
            data.setdefault("processed_keys", {})
            return data
        except json.JSONDecodeError:
            backup = self.path.with_suffix(f".bad-{int(time.time())}.json")
            self.path.rename(backup)
            return {"last_seen_id": 0, "processed_keys": {}}

    @property
    def last_seen_id(self) -> int:
        return int(self.data.get("last_seen_id", 0) or 0)

    def set_last_seen_id(self, value: int) -> None:
        self.data["last_seen_id"] = max(self.last_seen_id, int(value))

    def has_processed(self, key: str) -> bool:
        return key in dict(self.data.get("processed_keys") or {})

    def mark_processed(self, group: PromptGroup) -> None:
        processed = dict(self.data.get("processed_keys") or {})
        processed[group.key] = {
            "ts": utc_now(),
            "thread": group.thread,
            "source_kind": group.source_kind,
            "message_ids": [m.get("message_id") for m in group.source_messages],
            "agents": list(group.effective_agents),
        }
        if len(processed) > 500:
            keep = sorted(processed.items(), key=lambda item: item[1].get("ts", ""))[-500:]
            processed = dict(keep)
        self.data["processed_keys"] = processed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class BrokerClient:
    def __init__(self, base_url: str = BROKER_URL, token_file: Path = TOKEN_FILE, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout

    def token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("X-Coop-Token", self.token())
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch_messages(self, since_id: int, limit: int = 500) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({"since_id": since_id, "limit": min(max(limit, 1), 500)})
        data = self.request("GET", f"/v1/messages?{qs}")
        messages = data.get("messages", []) if data.get("ok") else []
        messages.sort(key=lambda msg: int(msg.get("id", 0) or 0))
        return messages

    def send_message(self, from_agent: str, to_agent: str, kind: str, priority: int, target: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/messages",
            {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "kind": kind,
                "priority": clamp_priority(priority),
                "target": target,
                "body": body,
            },
        )

    def ack_message(self, message_id: str, ack_by: str) -> dict[str, Any]:
        return self.request("POST", "/v1/ack", {"message_id": message_id, "ack_by": ack_by})


def current_max_message_id(sqlite_path: Path = SQLITE_PATH) -> int:
    if not sqlite_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=1.0)
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        conn.close()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def source_created_ns(group: PromptGroup) -> int:
    values = [int(m.get("created_ns", 0) or 0) for m in group.source_messages if int(m.get("created_ns", 0) or 0) > 0]
    return min(values) if values else 0


def post_live_event(client: BrokerClient, group: PromptGroup, event: LiveEvent) -> dict[str, Any]:
    created_ns = source_created_ns(group)
    latency_ms = int((time.time_ns() - created_ns) / 1_000_000) if created_ns else None
    body = {
        "type": "snake-live-event",
        "conversation_id": group.thread,
        "source_kind": group.source_kind,
        "source_message_ids": [str(m.get("message_id", "")) for m in group.source_messages],
        "originator": group.originator,
        "requested_recipients": list(group.requested_recipients),
        "effective_agents": list(group.effective_agents),
        "phase": event.phase,
        "text": event.text,
        "sequence": event.sequence,
        "latency_ms": latency_ms,
        "prompt_preview": group.prompt[:240],
    }
    return client.send_message(event.from_agent, event.to_agent, event.kind, 10, group.thread, body)


def post_room_dialogue(client: BrokerClient, group: PromptGroup, event: LiveEvent) -> dict[str, Any]:
    body = {
        "type": "snake-room-line",
        "conversation_id": group.thread,
        "source_kind": group.source_kind,
        "source_message_ids": [str(m.get("message_id", "")) for m in group.source_messages],
        "originator": group.originator,
        "speaker": event.from_agent,
        "listener": event.to_agent,
        "text": event.text,
        "sequence": event.sequence,
        "prompt_preview": group.prompt[:180],
    }
    return client.send_message(event.from_agent, event.to_agent, event.kind, 10, group.thread, body)


def fanout_architect_prompts(client: BrokerClient, group: PromptGroup) -> list[dict[str, Any]]:
    if group.source_kind != "architect-prompt":
        return []
    existing = unique_agents([str(m.get("to_agent", "")) for m in group.source_messages])
    missing = [agent for agent in group.effective_agents if agent not in existing]
    created: list[dict[str, Any]] = []
    for agent in missing:
        created.append(
            client.send_message(
                ARCHITECT_AGENT,
                agent,
                "task",
                10,
                group.thread,
                {
                    "type": "architect-prompt",
                    "prompt": group.prompt,
                    "message": group.prompt,
                    "thread": group.thread,
                    "source": "three-headed-snake-live-room-fanout",
                    "recipients": list(group.effective_agents),
                    "target_agent": agent,
                    "routed_by": "three-headed-snake-orchestrator",
                    "original_source_message_ids": [str(m.get("message_id", "")) for m in group.source_messages],
                    "instructions": (
                        "Jake prompted one head, but his current criterion is that The Three Headed Snake XXX works together. "
                        "Reply through the broker/terminal like a normal group chat: casual, direct, useful, and no role ceremony."
                    ),
                },
            )
        )
    return created


def launch_architect_runner(group: PromptGroup, timeout: int, allow_tools: bool = False) -> None:
    if group.source_kind != "architect-prompt" or not ARCHITECT_RUNNER.exists():
        return
    ARCHITECT_RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON_BIN,
        str(ARCHITECT_RUNNER),
        "--once",
        "--target",
        group.thread,
        "--agents",
        ",".join(group.effective_agents),
        "--limit",
        "500",
        "--max-messages",
        "1",
        "--timeout",
        str(timeout),
        "--ack-failures",
    ]
    if allow_tools:
        cmd.append("--allow-tools")
    log_handle = ARCHITECT_RUNNER_LOG.open("a", encoding="utf-8")
    subprocess.Popen(cmd, stdout=log_handle, stderr=log_handle, cwd=str(BRAIN), close_fds=True, start_new_session=True)
    log_handle.close()
    log(f"launched architect-runner thread={group.thread} agents={','.join(group.effective_agents)}")


def ack_source_messages(client: BrokerClient, group: PromptGroup) -> None:
    for message in group.source_messages:
        ack_by = normalize_agent(str(message.get("to_agent", "")))
        message_id = str(message.get("message_id", ""))
        if ack_by and message_id:
            try:
                client.ack_message(message_id, ack_by)
            except Exception as exc:  # noqa: BLE001 - daemon must keep running
                log(f"warn ack failed message_id={message_id[:8]} ack_by={ack_by}: {exc}")


def process_group(client: BrokerClient, state: StateStore, group: PromptGroup, args: argparse.Namespace) -> None:
    if state.has_processed(group.key):
        return
    log(f"processing {group.source_kind} thread={group.thread} agents={','.join(group.effective_agents)} key={group.key}")
    if args.room_dialogue:
        for event in build_room_dialogue(group):
            if event.delay_ms:
                time.sleep(event.delay_ms / 1000.0)
            post_room_dialogue(client, group, event)
    for event in build_live_events(group):
        if event.delay_ms:
            time.sleep(event.delay_ms / 1000.0)
        post_live_event(client, group, event)
    if args.fanout and group.source_kind == "architect-prompt":
        created = fanout_architect_prompts(client, group)
        if created:
            log(f"fanout created {len(created)} missing Architect prompt(s) for thread={group.thread}")
    if args.final_runners and group.source_kind == "architect-prompt":
        launch_architect_runner(group, timeout=args.runner_timeout, allow_tools=args.runner_allow_tools)
    else:
        ack_source_messages(client, group)
    state.mark_processed(group)


def run_once(client: BrokerClient, state: StateStore, args: argparse.Namespace) -> int:
    messages = client.fetch_messages(state.last_seen_id, limit=args.limit)
    if not messages:
        return 0
    max_id = max(int(m.get("id", 0) or 0) for m in messages)
    groups = group_messages(messages)
    processed = 0
    for group in groups:
        if not state.has_processed(group.key):
            process_group(client, state, group, args)
            processed += 1
    state.set_last_seen_id(max_id)
    state.save()
    return processed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent live-room orchestrator for The Three Headed Snake XXX")
    parser.add_argument("--once", action="store_true", help="Process one poll and exit")
    parser.add_argument("--replay-existing", action="store_true", help="Do not jump to current broker tail on first boot")
    parser.add_argument("--since-id", type=int, default=None, help="Override state last_seen_id for this run")
    parser.add_argument("--poll-ms", type=int, default=250)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--fanout", action=argparse.BooleanOptionalAction, default=True, help="Fan one-agent Architect prompts out to all three heads")
    parser.add_argument("--room-dialogue", action=argparse.BooleanOptionalAction, default=False, help="Post optional canned room dialogue before diagnostic status events; default off so the chat waits for real agent replies")
    parser.add_argument("--final-runners", action=argparse.BooleanOptionalAction, default=True, help="Launch slower real CLI receipts after instant live events")
    parser.add_argument("--runner-timeout", type=int, default=120)
    parser.add_argument("--runner-allow-tools", action="store_true", help="Allow final runner CLIs to use tools")
    args = parser.parse_args(argv)

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    state = StateStore(Path(args.state))
    if args.since_id is not None:
        state.set_last_seen_id(args.since_id)
    elif not args.replay_existing and state.last_seen_id == 0:
        state.set_last_seen_id(current_max_message_id())
    state.save()

    client = BrokerClient()
    log(
        "online "
        f"last_seen_id={state.last_seen_id} poll_ms={args.poll_ms} "
        f"fanout={args.fanout} room_dialogue={args.room_dialogue} final_runners={args.final_runners}"
    )
    while True:
        try:
            processed = run_once(client, state, args)
            if processed:
                log(f"processed_groups={processed} last_seen_id={state.last_seen_id}")
            if args.once:
                return 0
            time.sleep(max(25, args.poll_ms) / 1000.0)
        except KeyboardInterrupt:
            log("stopped by user")
            return 0
        except urllib.error.URLError as exc:
            log(f"broker unavailable: {exc}; retrying")
            if args.once:
                return 1
            time.sleep(2)
        except Exception as exc:  # noqa: BLE001 - room daemon should not die on one bad message
            log(f"error: {exc}; retrying")
            if args.once:
                return 1
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
