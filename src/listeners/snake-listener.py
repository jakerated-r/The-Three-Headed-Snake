#!/usr/bin/env python3
"""Always-on per-agent listener for the Three Headed Snake.

Polls open broker messages to one head, drains stale backlog safely, parses
Codex and Maestro envelopes, invokes the head launcher, replies, then ACKs.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time

BRAIN = "/Users/rated-r/rated r brain"
COOP = BRAIN + "/.claude/coop"
SNAKE = BRAIN + "/outputs/coop-tools/snake"
HOME = os.path.expanduser("~")
sys.path.insert(0, SNAKE)
import guardrails  # noqa: E402

HEAD = sys.argv[1] if len(sys.argv) > 1 else "Maestro"
if HEAD not in {"Codex", "Maestro", "Gemini"}:
    raise SystemExit("usage: snake-listener.py Codex|Maestro|Gemini")

CTL = COOP + "/scripts/coop-brokerctl.sh"
BUDGET = SNAKE + "/snake-budget.sh"
POLL = float(os.environ.get("SNAKE_POLL_MS", "400")) / 1000.0
FRESH_S = int(os.environ.get("SNAKE_FRESH_S", "240"))
LAUNCHER = {
    "Maestro": SNAKE + "/claude-head.sh",
    "Codex": SNAKE + "/codex-head.sh",
    "Gemini": SNAKE + "/gemini-head.sh",
}[HEAD]
GEMINI_QUOTA_FLAG = SNAKE + "/.gemini-quota-" + datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def cmd_for(prompt: str) -> list[str]:
    if HEAD == "Maestro":
        return [
            "bash",
            LAUNCHER,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--permission-mode",
            "plan",
            "--disable-slash-commands",
            "--tools",
            "",
            "--model",
            "opus",
        ]
    if HEAD == "Codex":
        return [
            "bash",
            LAUNCHER,
            "exec",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ephemeral",
            "-C",
            BRAIN,
            "-s",
            "read-only",
            "--color",
            "never",
            prompt,
        ]
    return [
        "bash",
        LAUNCHER,
        "-p",
        prompt,
        "-m",
        os.environ.get("SNAKE_GEMINI_MODEL", "gemini-2.5-flash-lite"),
        "--approval-mode",
        "plan",
        "--output-format",
        "text",
        "--skip-trust",
        "--extensions",
        "",
    ]


def ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", CTL, *args], capture_output=True, text=True, cwd=BRAIN, check=False)


def open_msgs() -> list[dict]:
    result = ctl("inbox", "--to", HEAD, "--status", "open", "--limit", "50")
    try:
        messages = json.loads(result.stdout).get("messages", [])
    except Exception:
        return []
    return messages if isinstance(messages, list) else []


def ack(message_id: str | None) -> None:
    if message_id:
        ctl("ack", "--message-id", message_id, "--by", HEAD)


def send(body: str, to_agent: str = "Architect", thread: str = "room") -> None:
    ctl("send", "--from", HEAD, "--to", to_agent, "--kind", "note", "--priority", "2", "--target", thread, "--body", body[:1800])


def budget_ok() -> bool:
    return subprocess.run(["bash", BUDGET, "check"], check=False).returncode == 0


def age(message: dict) -> float:
    created_ns = message.get("created_ns")
    if created_ns:
        try:
            return time.time() - (int(created_ns) / 1e9)
        except Exception:
            pass
    try:
        created_at = str(message.get("created_at", ""))[:19]
        ts = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return time.time() - ts.timestamp()
    except Exception:
        return 1e9


def clean_output(stdout: str, stderr: str) -> str:
    text = (stdout or "").strip() or (stderr or "").strip()
    quota = quota_receipt(text)
    if quota:
        return quota
    lines: list[str] = []
    skip_next_numeric = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if skip_next_numeric and line.isdigit():
            skip_next_numeric = False
            continue
        skip_next_numeric = False
        if line == "tokens used":
            skip_next_numeric = True
            continue
        if line == "codex":
            continue
        if line.startswith("THREE_HEADED_SNAKE_XXX_RECEIPT:"):
            continue
        if re.match(r"^20\d\d-\d\d-\d\dT.*\b(WARN|ERROR)\b", line):
            continue
        if line.startswith(("Warning:", "Skill conflict detected:", "Reading additional input from stdin")):
            continue
        lines.append(line)
    return "\n".join(lines).strip() or text[:1500] or "[empty]"


def quota_receipt(text: str) -> str:
    lower = (text or "").lower()
    if "terminalquotaerror" not in lower and "exhausted your daily quota" not in lower:
        return ""
    report_match = re.search(r"Full report available at:\s*(\S+)", text or "")
    report = report_match.group(1) if report_match else "Gemini CLI TerminalQuotaError"
    return f"Gemini is blocked: daily quota exhausted for the selected model. Evidence: {report}"


def read_gemini_quota_flag() -> str:
    if HEAD != "Gemini" or not os.path.exists(GEMINI_QUOTA_FLAG):
        return ""
    try:
        with open(GEMINI_QUOTA_FLAG, encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "Gemini is blocked: daily quota exhausted for the selected model."


def write_gemini_quota_flag(receipt: str) -> None:
    if HEAD != "Gemini" or not receipt:
        return
    try:
        with open(GEMINI_QUOTA_FLAG, "w", encoding="utf-8") as handle:
            handle.write(receipt + "\n")
    except Exception:
        pass


def model_safe_text(text: str) -> str:
    return re.sub(r"@([A-Za-z][A-Za-z0-9_-]*)", r"[at]\1", text or "")


def run_head(prompt: str) -> str:
    quota_block = read_gemini_quota_flag()
    if quota_block:
        return quota_block
    try:
        result = subprocess.run(cmd_for(prompt), capture_output=True, text=True, timeout=120, cwd=HOME, check=False)
        cleaned = clean_output(result.stdout, result.stderr)[:1500]
        write_gemini_quota_flag(quota_receipt((result.stdout or "") + "\n" + (result.stderr or "")))
        return cleaned
    except subprocess.TimeoutExpired:
        return "[timeout >120s]"


def reply_target(sender: str | None) -> str:
    return sender if sender in {"Architect", "Codex", "Maestro", "Gemini"} else "Architect"


def body_type(body: object) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("envelope"), dict):
            return str(body["envelope"].get("type") or "")
        return str(body.get("type") or "")
    return ""


def is_plumbing(message: dict) -> bool:
    """Ignore orchestrator visibility events so they do not trigger model chatter."""
    kind = str(message.get("kind") or "")
    if kind in {"status", "handoff", "heartbeat", "ack"}:
        return True
    body = message.get("body")
    if body_type(body) == "snake-live-event":
        return True
    return False


def has_explicit_other_head_tag(text: str, me: str) -> bool:
    tags = {tag.lower() for tag in re.findall(r"@([A-Za-z][A-Za-z0-9_-]*)", text or "")}
    if not tags or "all" in tags or me.lower() in tags:
        return False
    return bool(tags & {"codex", "maestro", "gemini"})


def actionable_plain(text: str) -> bool:
    lowered = text.lower().strip()
    if not lowered:
        return False
    if re.search(r"@(?:all|codex|maestro|gemini)\b", lowered):
        return True
    if "?" in lowered:
        return True
    if re.match(r"^(ready|ack|acknowledged|got it|standing by|copy|received)\b", lowered):
        return False
    action_words = (
        "verify", "check", "review", "test", "run", "patch", "fix", "ship", "install",
        "bring up", "status", "eta", "proof", "confirm", "coordinate", "continue",
        "need", "please", "can you", "what", "why", "how", "when", "where",
    )
    return any(word in lowered for word in action_words)


def main() -> int:
    print(f"[{HEAD}] listener UP poll={POLL}s fresh={FRESH_S}s envelope-v4", flush=True)
    while True:
        try:
            for message in open_msgs():
                message_id = message.get("message_id")
                sender = message.get("from_agent")
                body = message.get("body")
                thread = guardrails.thread_of(body, message.get("target") or "room")
                try:
                    if is_plumbing(message):
                        ack(message_id)
                        continue
                    if sender and sender != HEAD and age(message) <= FRESH_S:
                        text = guardrails.message_text(body)
                        if sender == "Architect" and has_explicit_other_head_tag(text, HEAD):
                            ack(message_id)
                            continue
                        guard_body = body
                        if not guardrails.is_addressed(body, HEAD) and message.get("to_agent") == HEAD:
                            if sender != "Architect" and body_type(body) not in {"architect-prompt", "auto-engage", "cross-fire-request", "cross-fire-reply"} and not actionable_plain(text):
                                ack(message_id)
                                continue
                            guard_body = f"@{HEAD.lower()} {text}"
                        ok, _why = guardrails.should_respond(guard_body, HEAD, thread)
                        if ok and budget_ok():
                            text = guardrails.message_text(body)
                            prompt = (
                                f"You are {HEAD}, one of three AI teammates (Maestro=Claude, Codex, Gemini) "
                                f"collaborating live on Jake's Mac. A teammate ({sender}) said: {model_safe_text(text)}. "
                                "Reply to them in ONE concise plain-English line."
                            )
                            reply = run_head(prompt)
                            send(reply, to_agent=reply_target(sender), thread=thread)
                            guardrails.record(thread, HEAD)
                            subprocess.run(["bash", BUDGET, "tick"], check=False)
                            print(f"[{HEAD}] replied to {sender} id={message.get('id')}: {reply[:80]}", flush=True)
                finally:
                    ack(message_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[{HEAD}] loop error: {exc}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    raise SystemExit(main())
