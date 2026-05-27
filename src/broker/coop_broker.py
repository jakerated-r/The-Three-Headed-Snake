#!/usr/bin/env python3
"""Local Codex <-> Maestro live bridge broker.

Stdlib-only on purpose. The broker binds to 127.0.0.1, requires a local token,
redacts secrets before persistence, stores typed messages in SQLite, and mirrors
human-readable receipts into markdown for the existing coop layer.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import posixpath
import re
import secrets
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
COOP_DIR = Path(os.environ.get("COOP_ROOT", str(REPO_ROOT / "data" / "coop")))
BRAIN_DIR = REPO_ROOT
BROKER_DIR = COOP_DIR / "broker"
DEFAULT_DB = BROKER_DIR / "coop-broker.sqlite3"
DEFAULT_TOKEN_FILE = BROKER_DIR / ".token"
DEFAULT_MIRROR = COOP_DIR / "LIVE_BRIDGE.md"
DEFAULT_WORK_MIRROR = COOP_DIR / "TWIN_TOWERS.md"
OFF_LIMITS_PATH = COOP_DIR / "OFF_LIMITS.md"
IDENTITY_SCRIPT = BRAIN_DIR / "src" / "identity" / "per-agent-envelope.py"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17874
AGENTS = {"Architect", "Codex", "Maestro", "Claude", "Gemini"}
KINDS = {
    "ack",
    "blocker",
    "emergency-stop",
    "handoff",
    "health",
    "heartbeat",
    "note",
    "receipt",
    "request",
    "review",
    "status",
    "task",
}

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("generic_api_key_assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API|TOKEN|SECRET|KEY|PAT)[A-Z0-9_]*\s*=\s*)"
        r"([\"']?)[A-Za-z0-9_./+=:-]{16,}\2"
    )),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_./+=:-]{20,}\b")),
]
MAX_CLAIM_PATHS = 8
MAX_CLAIM_TTL_SECONDS = 4 * 3600
MIN_CLAIM_TTL_SECONDS = 5 * 60
EMERGENCY_STOP_RATE_LIMIT_SECONDS = 60
_IDENTITY_MODULE = None


class BrokerError(Exception):
    """Expected request/validation error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    BROKER_DIR.mkdir(parents=True, exist_ok=True)


def ensure_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    ensure_dirs()
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(token_file, 0o600)
    return token


def read_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    return ensure_token(token_file)


def redact_text(value: str) -> str:
    redacted = value
    for label, pattern in SECRET_PATTERNS:
        if label == "generic_api_key_assignment":
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED:{label}]", redacted)
        else:
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checksum_record(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


def identity_module() -> Any:
    global _IDENTITY_MODULE
    if _IDENTITY_MODULE is not None:
        return _IDENTITY_MODULE
    spec = importlib.util.spec_from_file_location("per_agent_envelope", IDENTITY_SCRIPT)
    if spec is None or spec.loader is None:
        raise BrokerError(500, f"identity verifier unavailable: {IDENTITY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _IDENTITY_MODULE = module
    return module


def verify_signed_verdict_payload(payload: dict[str, Any], claim_id: str, verifier: str,
                                  verdict: str, verification: Any) -> None:
    signed = payload.get("signed_verdict")
    if not isinstance(signed, dict):
        raise BrokerError(400, "signed_verdict is required for claim verification")
    if signed.get("claim_id") != claim_id:
        raise BrokerError(400, "signed_verdict claim_id mismatch")
    if signed.get("verifier") != verifier:
        raise BrokerError(400, "signed_verdict verifier mismatch")
    if signed.get("verdict") != verdict:
        raise BrokerError(400, "signed_verdict verdict mismatch")
    if canonical_json(signed.get("verification", {})) != canonical_json(verification):
        raise BrokerError(400, "signed_verdict verification body mismatch")
    ok, reason = identity_module().verify_verdict(signed)
    if not ok:
        raise BrokerError(403, f"signed verdict rejected: {reason}")


def extract_off_limits_for(agent: str, off_limits_path: Path = OFF_LIMITS_PATH) -> list[str]:
    if not off_limits_path.exists():
        return []
    text = off_limits_path.read_text(encoding="utf-8")
    section_markers = {
        "Codex": "## Maestro/Claude — Codex stays OFF",
        "Maestro": "## Codex — Maestro stays OFF",
    }
    marker = section_markers.get(agent)
    if not marker:
        return []
    start = text.find(marker)
    if start < 0:
        return []
    next_section = text.find("\n## ", start + len(marker))
    section = text[start: next_section if next_section >= 0 else len(text)]
    paths: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        match = re.search(r"`([^`]+)`", line)
        raw = match.group(1) if match else line[2:].split(" — ", 1)[0].strip()
        if not raw or raw.startswith("Anything inside"):
            continue
        paths.append(normalize_path(raw))
    return paths


def path_is_under(path: str, root: str) -> bool:
    left = normalize_path(path)
    right = normalize_path(root)
    return left == right or left.startswith(right.rstrip("/") + "/")


def enforce_off_limits_for_envelope(envelope: dict[str, Any], recipient: str) -> None:
    payload = envelope.get("payload", {})
    if not isinstance(payload, dict):
        return
    targeted_reads = payload.get("targeted_reads", [])
    if not targeted_reads:
        return
    if not isinstance(targeted_reads, list):
        raise BrokerError(400, "payload.targeted_reads must be a list")
    blocked_roots = extract_off_limits_for(recipient)
    for item in targeted_reads:
        candidate = normalize_path(str(item))
        for blocked in blocked_roots:
            if path_is_under(candidate, blocked):
                raise BrokerError(403, f"targeted read violates OFF_LIMITS for {recipient}: {item}")


def enforce_broker_message_policy(conn: sqlite3.Connection, from_agent: str, to_agent: str,
                                  kind: str, target: str, body: Any) -> None:
    if isinstance(body, dict) and isinstance(body.get("envelope"), dict):
        enforce_off_limits_for_envelope(body["envelope"], to_agent)
    if kind == "emergency-stop":
        if not isinstance(body, dict):
            raise BrokerError(400, "emergency-stop body must be a JSON object")
        reason = str(body.get("reason", "")).strip()
        evidence = body.get("evidence")
        if len(reason) < 10:
            raise BrokerError(400, "emergency-stop requires a reason of at least 10 characters")
        if not isinstance(evidence, list) or not evidence:
            raise BrokerError(400, "emergency-stop requires non-empty evidence list")
        cutoff_ns = time.time_ns() - EMERGENCY_STOP_RATE_LIMIT_SECONDS * 1_000_000_000
        recent = conn.execute(
            """
            SELECT message_id FROM messages
            WHERE kind = 'emergency-stop'
              AND from_agent = ?
              AND to_agent = ?
              AND target = ?
              AND status = 'open'
              AND created_ns > ?
            LIMIT 1
            """,
            (from_agent, to_agent, target, cutoff_ns),
        ).fetchone()
        if recent:
            raise BrokerError(429, f"emergency-stop rate-limited; open stop already exists: {recent['message_id']}")


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_ns INTEGER NOT NULL DEFAULT 0,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            kind TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 5,
            target TEXT NOT NULL DEFAULT '',
            body_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            ack_by TEXT NOT NULL DEFAULT '',
            ack_at TEXT NOT NULL DEFAULT '',
            checksum TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_to_agent_id ON messages(to_agent, id);
        CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);

        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            agent TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);

        CREATE TABLE IF NOT EXISTS work_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL UNIQUE,
            project TEXT NOT NULL,
            agent TEXT NOT NULL,
            peer_agent TEXT NOT NULL,
            goal TEXT NOT NULL,
            paths_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            verification_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            verified_at TEXT NOT NULL DEFAULT '',
            checksum TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_work_claims_status ON work_claims(status);
        CREATE INDEX IF NOT EXISTS idx_work_claims_agent ON work_claims(agent);
        """
    )
    ensure_column(conn, "messages", "created_ns", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def validate_agent(agent: str, field_name: str) -> str:
    value = (agent or "").strip()
    if value == "Claude":
        value = "Maestro"
    if value not in AGENTS:
        raise BrokerError(400, f"{field_name} must be Architect, Codex, Maestro, or Gemini")
    return value


def validate_kind(kind: str) -> str:
    value = (kind or "").strip().lower()
    if value not in KINDS:
        raise BrokerError(400, f"kind must be one of: {', '.join(sorted(KINDS))}")
    return value


def peer_agent(agent: str) -> str:
    validated = validate_agent(agent, "agent")
    if validated == "Codex":
        return "Maestro"
    return "Codex"


def normalize_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise BrokerError(400, "path is required")
    expanded = os.path.expanduser(value)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = BRAIN_DIR / candidate
    return posixpath.normpath(str(candidate))


def paths_conflict(left: str, right: str) -> bool:
    a = normalize_path(left)
    b = normalize_path(right)
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def append_mirror(message: dict[str, Any], mirror: Path = DEFAULT_MIRROR) -> None:
    mirror.parent.mkdir(parents=True, exist_ok=True)
    body = message.get("body", {})
    body_preview = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(body_preview) > 1200:
        body_preview = body_preview[:1200] + "..."
    with mirror.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write(
            f"## {message['created_at']} · {message['kind']} · "
            f"{message['from_agent']} -> {message['to_agent']} · {message['message_id']}\n\n"
        )
        handle.write(f"- Status: {message['status']}\n")
        handle.write(f"- Priority: {message['priority']}\n")
        if message.get("target"):
            handle.write(f"- Target: `{message['target']}`\n")
        handle.write(f"- Checksum: `{message['checksum']}`\n")
        handle.write("- Body:\n")
        handle.write("```json\n")
        handle.write(body_preview + "\n")
        handle.write("```\n")


def append_ack_mirror(message: dict[str, Any], mirror: Path = DEFAULT_MIRROR) -> None:
    mirror.parent.mkdir(parents=True, exist_ok=True)
    with mirror.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write(
            f"## {message['ack_at']} · ack · {message['ack_by']} -> "
            f"{message['from_agent']} · {message['message_id']}\n\n"
        )
        handle.write("- Status: acked\n")
        handle.write(
            f"- Original: `{message['kind']}` · "
            f"{message['from_agent']} -> {message['to_agent']}\n"
        )
        if message.get("target"):
            handle.write(f"- Target: `{message['target']}`\n")
        handle.write(f"- Checksum: `{message['checksum']}`\n")


def append_work_mirror(event: str, claim: dict[str, Any], mirror: Path = DEFAULT_WORK_MIRROR) -> None:
    mirror.parent.mkdir(parents=True, exist_ok=True)
    paths = claim.get("paths", [])
    paths_text = ", ".join(f"`{path}`" for path in paths) if paths else "`(none)`"
    with mirror.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write(f"## {utc_now()} · {event} · {claim['claim_id']}\n\n")
        handle.write(f"- Status: {claim['status']}\n")
        handle.write(f"- Project: `{claim['project']}`\n")
        handle.write(f"- Owner: {claim['agent']}\n")
        handle.write(f"- Peer verifier: {claim['peer_agent']}\n")
        handle.write(f"- Goal: {claim['goal']}\n")
        handle.write(f"- Paths: {paths_text}\n")
        if claim.get("completed_at"):
            handle.write(f"- Completed at: {claim['completed_at']}\n")
        if claim.get("verified_at"):
            handle.write(f"- Verified at: {claim['verified_at']}\n")
        handle.write(f"- Checksum: `{claim['checksum']}`\n")


def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    body = json.loads(row["body_json"])
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "created_at": row["created_at"],
        "created_ns": row["created_ns"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "kind": row["kind"],
        "priority": row["priority"],
        "target": row["target"],
        "body": body,
        "status": row["status"],
        "ack_by": row["ack_by"],
        "ack_at": row["ack_at"],
        "checksum": row["checksum"],
    }


def row_to_claim(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "claim_id": row["claim_id"],
        "project": row["project"],
        "agent": row["agent"],
        "peer_agent": row["peer_agent"],
        "goal": row["goal"],
        "paths": json.loads(row["paths_json"]),
        "status": row["status"],
        "evidence": json.loads(row["evidence_json"]),
        "verification": json.loads(row["verification_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
        "completed_at": row["completed_at"],
        "verified_at": row["verified_at"],
        "checksum": row["checksum"],
    }


def create_message(conn: sqlite3.Connection, payload: dict[str, Any], mirror: Path = DEFAULT_MIRROR) -> dict[str, Any]:
    from_agent = validate_agent(str(payload.get("from_agent", "")), "from_agent")
    to_agent = validate_agent(str(payload.get("to_agent", "")), "to_agent")
    if from_agent == to_agent:
        raise BrokerError(400, "from_agent and to_agent must differ")
    kind = validate_kind(str(payload.get("kind", "note")))
    priority = int(payload.get("priority", 5))
    if priority < 1 or priority > 10:
        raise BrokerError(400, "priority must be between 1 and 10")
    target = redact_text(str(payload.get("target", "")))[:500]
    body = redact_payload(payload.get("body", {}))
    if body in ({}, "", None):
        raise BrokerError(400, "body is required")
    enforce_broker_message_policy(conn, from_agent, to_agent, kind, target, body)
    created_ns = time.time_ns()
    created_at = utc_now()
    message_id = str(uuid.uuid4())
    record_for_checksum = {
        "message_id": message_id,
        "created_at": created_at,
        "created_ns": created_ns,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "kind": kind,
        "priority": priority,
        "target": target,
        "body": body,
        "status": "open",
    }
    checksum = checksum_record(record_for_checksum)
    conn.execute(
        """
        INSERT INTO messages
        (message_id, created_at, created_ns, from_agent, to_agent, kind, priority, target, body_json, status, checksum)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (message_id, created_at, created_ns, from_agent, to_agent, kind, priority, target, canonical_json(body), checksum),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    message = row_to_message(row)
    append_mirror(message, mirror=mirror)
    return message


def list_messages(conn: sqlite3.Connection, to_agent: str | None = None, since_id: int = 0,
                  status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses = ["id > ?"]
    params: list[Any] = [since_id]
    if to_agent:
        clauses.append("to_agent = ?")
        params.append(validate_agent(to_agent, "to_agent"))
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",
        params,
    ).fetchall()
    return [row_to_message(row) for row in rows]


def ack_message(conn: sqlite3.Connection, message_id: str, ack_by: str,
                mirror: Path | None = None) -> dict[str, Any]:
    ack_agent = validate_agent(ack_by, "ack_by")
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    if not row:
        raise BrokerError(404, "message not found")
    now = utc_now()
    conn.execute(
        "UPDATE messages SET status = 'acked', ack_by = ?, ack_at = ? WHERE message_id = ?",
        (ack_agent, now, message_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    message = row_to_message(row)
    if mirror is not None:
        append_ack_mirror(message, mirror=mirror)
    return message


def active_reservation_conflict(conn: sqlite3.Connection, path: str, agent: str) -> sqlite3.Row | None:
    normalized = normalize_path(path)
    now = utc_now()
    conn.execute("UPDATE reservations SET status = 'expired' WHERE status = 'active' AND expires_at < ?", (now,))
    rows = conn.execute(
        "SELECT path, agent, reason, created_at, expires_at, status FROM reservations WHERE status = 'active'"
    ).fetchall()
    for row in rows:
        if row["agent"] != agent and paths_conflict(normalized, row["path"]):
            return row
    return None


def reserve_path(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    path = normalize_path(str(payload.get("path", "")))
    agent = validate_agent(str(payload.get("agent", "")), "agent")
    reason = redact_text(str(payload.get("reason", "")))[:800]
    ttl_seconds = int(payload.get("ttl_seconds", 3600))
    ttl_seconds = max(60, min(ttl_seconds, 24 * 3600))
    conflict = active_reservation_conflict(conn, path, agent)
    if conflict:
        raise BrokerError(
            409,
            f"path under construction by {conflict['agent']}: {conflict['path']} ({conflict['reason']})",
        )
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires_at = now + dt.timedelta(seconds=ttl_seconds)
    conn.execute(
        """
        INSERT INTO reservations(path, agent, reason, created_at, expires_at, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        ON CONFLICT(path) DO UPDATE SET
            agent = excluded.agent,
            reason = excluded.reason,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            status = 'active'
        """,
        (path, agent, reason, now.isoformat().replace("+00:00", "Z"), expires_at.isoformat().replace("+00:00", "Z")),
    )
    conn.commit()
    return {"path": path, "agent": agent, "reason": reason, "expires_at": expires_at.isoformat().replace("+00:00", "Z")}


def list_reservations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    now = utc_now()
    conn.execute("UPDATE reservations SET status = 'expired' WHERE status = 'active' AND expires_at < ?", (now,))
    conn.commit()
    rows = conn.execute(
        "SELECT path, agent, reason, created_at, expires_at, status FROM reservations ORDER BY path ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def release_claim_reservations(conn: sqlite3.Connection, claim_id: str) -> None:
    conn.execute(
        "UPDATE reservations SET status = 'released' WHERE status = 'active' AND reason LIKE ?",
        (f"claim {claim_id}:%",),
    )
    conn.commit()


def refresh_claim_expirations(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.execute(
        "UPDATE work_claims SET status = 'expired', updated_at = ? "
        "WHERE status IN ('active', 'needs_work') AND expires_at < ?",
        (now, now),
    )
    conn.execute("UPDATE reservations SET status = 'expired' WHERE status = 'active' AND expires_at < ?", (now,))
    conn.commit()


def claim_checksum(record: dict[str, Any]) -> str:
    return checksum_record(record)


def create_work_claim(conn: sqlite3.Connection, payload: dict[str, Any],
                      work_mirror: Path = DEFAULT_WORK_MIRROR) -> dict[str, Any]:
    agent = validate_agent(str(payload.get("agent", "")), "agent")
    peer = validate_agent(str(payload.get("peer_agent", "") or peer_agent(agent)), "peer_agent")
    if peer == agent:
        raise BrokerError(400, "peer_agent must differ from agent")
    project = redact_text(str(payload.get("project", "")).strip())[:200]
    goal = redact_text(str(payload.get("goal", "")).strip())[:1000]
    if not project:
        raise BrokerError(400, "project is required")
    if not goal:
        raise BrokerError(400, "goal is required")
    raw_paths = payload.get("paths", [])
    if isinstance(raw_paths, str):
        raw_paths = [item.strip() for item in raw_paths.split(",") if item.strip()]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise BrokerError(400, "paths must be a non-empty list")
    if len(raw_paths) > MAX_CLAIM_PATHS:
        raise BrokerError(400, f"claims may cover at most {MAX_CLAIM_PATHS} paths")
    paths = [normalize_path(str(item)) for item in raw_paths]
    for path in paths:
        if path in {str(BRAIN_DIR), str(BRAIN_DIR / "projects"), str(BRAIN_DIR / "outputs"), str(COOP_DIR.parent)}:
            raise BrokerError(400, f"claim path is too broad: {path}")
    ttl_seconds = max(MIN_CLAIM_TTL_SECONDS, min(int(payload.get("ttl_seconds", MAX_CLAIM_TTL_SECONDS)), MAX_CLAIM_TTL_SECONDS))
    for path in paths:
        conflict = active_reservation_conflict(conn, path, agent)
        if conflict:
            raise BrokerError(
                409,
                f"path under construction by {conflict['agent']}: {conflict['path']} ({conflict['reason']})",
            )
    claim_id = str(uuid.uuid4())
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat().replace("+00:00", "Z")
    expires_at = (now_dt + dt.timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    record = {
        "claim_id": claim_id,
        "project": project,
        "agent": agent,
        "peer_agent": peer,
        "goal": goal,
        "paths": paths,
        "status": "active",
        "created_at": now,
        "expires_at": expires_at,
    }
    checksum = claim_checksum(record)
    conn.execute(
        """
        INSERT INTO work_claims
        (claim_id, project, agent, peer_agent, goal, paths_json, status, created_at, updated_at, expires_at, checksum)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (claim_id, project, agent, peer, goal, canonical_json(paths), now, now, expires_at, checksum),
    )
    for path in paths:
        reserve_path(conn, {
            "path": path,
            "agent": agent,
            "reason": f"claim {claim_id}: {project} — {goal}",
            "ttl_seconds": ttl_seconds,
        })
    conn.commit()
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    claim = row_to_claim(row)
    append_work_mirror("claim-started", claim, mirror=work_mirror)
    return claim


def list_work_claims(conn: sqlite3.Connection, status: str | None = None,
                     agent: str | None = None, project: str | None = None,
                     limit: int = 50) -> list[dict[str, Any]]:
    refresh_claim_expirations(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if agent:
        clauses.append("(agent = ? OR peer_agent = ?)")
        normalized_agent = validate_agent(agent, "agent")
        params.extend([normalized_agent, normalized_agent])
    if project:
        clauses.append("project = ?")
        params.append(project)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    rows = conn.execute(
        f"SELECT * FROM work_claims {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [row_to_claim(row) for row in rows]


def complete_work_claim(conn: sqlite3.Connection, payload: dict[str, Any],
                        live_mirror: Path = DEFAULT_MIRROR,
                        work_mirror: Path = DEFAULT_WORK_MIRROR) -> dict[str, Any]:
    claim_id = str(payload.get("claim_id", "")).strip()
    agent = validate_agent(str(payload.get("agent", "")), "agent")
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if not row:
        raise BrokerError(404, "claim not found")
    claim = row_to_claim(row)
    if claim["agent"] != agent:
        raise BrokerError(403, "only the claim owner can complete this work")
    if claim["status"] not in {"active", "needs_work"}:
        raise BrokerError(409, f"claim cannot be completed from status {claim['status']}")
    evidence = redact_payload(payload.get("evidence", {}))
    if not evidence:
        raise BrokerError(400, "evidence is required")
    now = utc_now()
    updated_record = {
        "claim_id": claim_id,
        "project": claim["project"],
        "agent": agent,
        "peer_agent": claim["peer_agent"],
        "goal": claim["goal"],
        "paths": claim["paths"],
        "status": "completed",
        "evidence": evidence,
        "completed_at": now,
    }
    checksum = claim_checksum(updated_record)
    conn.execute(
        """
        UPDATE work_claims
        SET status = 'completed', evidence_json = ?, updated_at = ?, completed_at = ?, checksum = ?
        WHERE claim_id = ?
        """,
        (canonical_json(evidence), now, now, checksum, claim_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    claim = row_to_claim(row)
    append_work_mirror("completion-claimed-peer-verification-required", claim, mirror=work_mirror)
    create_message(conn, {
        "from_agent": agent,
        "to_agent": claim["peer_agent"],
        "kind": "review",
        "priority": 10,
        "target": claim["project"],
        "body": {
            "type": "peer-verification-required",
            "claim_id": claim_id,
            "project": claim["project"],
            "goal": claim["goal"],
            "paths": claim["paths"],
            "evidence": evidence,
            "required_action": "Run independent verification, then call claim-verify with pass or needs_work.",
        },
    }, mirror=live_mirror)
    return claim


def verify_work_claim(conn: sqlite3.Connection, payload: dict[str, Any],
                      live_mirror: Path = DEFAULT_MIRROR,
                      work_mirror: Path = DEFAULT_WORK_MIRROR) -> dict[str, Any]:
    claim_id = str(payload.get("claim_id", "")).strip()
    verifier = validate_agent(str(payload.get("verifier", "")), "verifier")
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in {"pass", "needs_work"}:
        raise BrokerError(400, "verdict must be pass or needs_work")
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if not row:
        raise BrokerError(404, "claim not found")
    claim = row_to_claim(row)
    if claim["peer_agent"] != verifier:
        raise BrokerError(403, "only the assigned peer can verify this claim")
    if claim["status"] != "completed":
        raise BrokerError(409, f"claim must be completed before verification, not {claim['status']}")
    verification = redact_payload(payload.get("verification", {}))
    if not verification:
        raise BrokerError(400, "verification is required")
    verify_signed_verdict_payload(payload, claim_id, verifier, verdict, verification)
    now = utc_now()
    new_status = "verified" if verdict == "pass" else "needs_work"
    updated_record = {
        "claim_id": claim_id,
        "project": claim["project"],
        "agent": claim["agent"],
        "peer_agent": verifier,
        "goal": claim["goal"],
        "paths": claim["paths"],
        "status": new_status,
        "verification": verification,
        "verified_at": now if verdict == "pass" else "",
    }
    checksum = claim_checksum(updated_record)
    conn.execute(
        """
        UPDATE work_claims
        SET status = ?, verification_json = ?, updated_at = ?, verified_at = ?, checksum = ?
        WHERE claim_id = ?
        """,
        (new_status, canonical_json(verification), now, now if verdict == "pass" else "", checksum, claim_id),
    )
    if verdict == "pass":
        release_claim_reservations(conn, claim_id)
    conn.commit()
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    claim = row_to_claim(row)
    append_work_mirror("peer-verified" if verdict == "pass" else "peer-rejected-needs-work", claim, mirror=work_mirror)
    create_message(conn, {
        "from_agent": verifier,
        "to_agent": claim["agent"],
        "kind": "receipt" if verdict == "pass" else "blocker",
        "priority": 10,
        "target": claim["project"],
        "body": {
            "type": "peer-verification-result",
            "claim_id": claim_id,
            "project": claim["project"],
            "verdict": verdict,
            "verification": verification,
            "status": new_status,
        },
    }, mirror=live_mirror)
    return claim


def cancel_work_claim(conn: sqlite3.Connection, payload: dict[str, Any],
                      work_mirror: Path = DEFAULT_WORK_MIRROR) -> dict[str, Any]:
    claim_id = str(payload.get("claim_id", "")).strip()
    agent = validate_agent(str(payload.get("agent", "")), "agent")
    reason = redact_text(str(payload.get("reason", "")).strip())[:1000]
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if not row:
        raise BrokerError(404, "claim not found")
    claim = row_to_claim(row)
    if claim["agent"] != agent:
        raise BrokerError(403, "only the claim owner can cancel this work")
    now = utc_now()
    verification = {"cancelled_by": agent, "reason": reason}
    updated_record = {
        "claim_id": claim_id,
        "project": claim["project"],
        "agent": agent,
        "peer_agent": claim["peer_agent"],
        "goal": claim["goal"],
        "paths": claim["paths"],
        "status": "cancelled",
        "verification": verification,
    }
    checksum = claim_checksum(updated_record)
    conn.execute(
        """
        UPDATE work_claims
        SET status = 'cancelled', verification_json = ?, updated_at = ?, checksum = ?
        WHERE claim_id = ?
        """,
        (canonical_json(verification), now, checksum, claim_id),
    )
    release_claim_reservations(conn, claim_id)
    conn.commit()
    row = conn.execute("SELECT * FROM work_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    claim = row_to_claim(row)
    append_work_mirror("claim-cancelled", claim, mirror=work_mirror)
    return claim


def json_response(handler: http.server.BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class BrokerHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CoopBroker/1.0"

    def _authed(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        supplied = self.headers.get("X-Coop-Token", "")
        return bool(supplied) and hmac.compare_digest(expected, supplied)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise BrokerError(413, "payload too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError(400, "invalid json") from exc
        if not isinstance(payload, dict):
            raise BrokerError(400, "json object required")
        return payload

    def _conn(self) -> sqlite3.Connection:
        return connect(self.server.db_path)  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                with self._conn() as conn:
                    count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
                json_response(self, 200, {"ok": True, "messages": count, "time": utc_now()})
                return
            if not self._authed():
                raise BrokerError(401, "unauthorized")
            if parsed.path == "/v1/messages":
                q = parse_qs(parsed.query)
                with self._conn() as conn:
                    messages = list_messages(
                        conn,
                        to_agent=(q.get("to", [""])[0] or None),
                        since_id=int(q.get("since_id", ["0"])[0] or 0),
                        status=(q.get("status", [""])[0] or None),
                        limit=int(q.get("limit", ["50"])[0] or 50),
                    )
                json_response(self, 200, {"ok": True, "messages": messages})
                return
            if parsed.path == "/v1/reservations":
                with self._conn() as conn:
                    reservations = list_reservations(conn)
                json_response(self, 200, {"ok": True, "reservations": reservations})
                return
            if parsed.path == "/v1/claims":
                q = parse_qs(parsed.query)
                with self._conn() as conn:
                    claims = list_work_claims(
                        conn,
                        status=(q.get("status", [""])[0] or None),
                        agent=(q.get("agent", [""])[0] or None),
                        project=(q.get("project", [""])[0] or None),
                        limit=int(q.get("limit", ["50"])[0] or 50),
                    )
                json_response(self, 200, {"ok": True, "claims": claims})
                return
            if parsed.path == "/v1/stream":
                q = parse_qs(parsed.query)
                to_agent = validate_agent(q.get("to", [""])[0], "to")
                since_id = int(q.get("since_id", ["0"])[0] or 0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                deadline = time.time() + 55
                last_id = since_id
                while time.time() < deadline:
                    with self._conn() as conn:
                        messages = list_messages(conn, to_agent=to_agent, since_id=last_id, limit=25)
                    for message in messages:
                        last_id = max(last_id, int(message["id"]))
                        self.wfile.write(f"id: {message['id']}\n".encode("utf-8"))
                        self.wfile.write(b"event: message\n")
                        self.wfile.write(("data: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    time.sleep(1)
                return
            raise BrokerError(404, "not found")
        except BrokerError as exc:
            json_response(self, exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # pragma: no cover - defensive server guard
            json_response(self, 500, {"ok": False, "error": f"broker error: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._authed():
                raise BrokerError(401, "unauthorized")
            parsed = urlparse(self.path)
            payload = self._read_json()
            with self._conn() as conn:
                if parsed.path == "/v1/messages":
                    message = create_message(conn, payload, mirror=self.server.mirror_path)  # type: ignore[attr-defined]
                    json_response(self, 201, {"ok": True, "message": message})
                    return
                if parsed.path == "/v1/ack":
                    message = ack_message(
                        conn,
                        str(payload.get("message_id", "")),
                        str(payload.get("ack_by", "")),
                        mirror=self.server.mirror_path,  # type: ignore[attr-defined]
                    )
                    json_response(self, 200, {"ok": True, "message": message})
                    return
                if parsed.path == "/v1/reservations":
                    reservation = reserve_path(conn, payload)
                    json_response(self, 201, {"ok": True, "reservation": reservation})
                    return
                if parsed.path == "/v1/claims/start":
                    claim = create_work_claim(conn, payload, work_mirror=self.server.work_mirror_path)  # type: ignore[attr-defined]
                    json_response(self, 201, {"ok": True, "claim": claim})
                    return
                if parsed.path == "/v1/claims/complete":
                    claim = complete_work_claim(
                        conn,
                        payload,
                        live_mirror=self.server.mirror_path,  # type: ignore[attr-defined]
                        work_mirror=self.server.work_mirror_path,  # type: ignore[attr-defined]
                    )
                    json_response(self, 200, {"ok": True, "claim": claim})
                    return
                if parsed.path == "/v1/claims/verify":
                    claim = verify_work_claim(
                        conn,
                        payload,
                        live_mirror=self.server.mirror_path,  # type: ignore[attr-defined]
                        work_mirror=self.server.work_mirror_path,  # type: ignore[attr-defined]
                    )
                    json_response(self, 200, {"ok": True, "claim": claim})
                    return
                if parsed.path == "/v1/claims/cancel":
                    claim = cancel_work_claim(
                        conn,
                        payload,
                        work_mirror=self.server.work_mirror_path,  # type: ignore[attr-defined]
                    )
                    json_response(self, 200, {"ok": True, "claim": claim})
                    return
            raise BrokerError(404, "not found")
        except BrokerError as exc:
            json_response(self, exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # pragma: no cover
            json_response(self, 500, {"ok": False, "error": f"broker error: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{utc_now()}] {self.address_string()} {fmt % args}\n")


def serve(args: argparse.Namespace) -> int:
    token = ensure_token(Path(args.token_file))
    db_path = Path(args.db)
    mirror_path = Path(args.mirror)
    work_mirror_path = Path(args.work_mirror)
    server = http.server.ThreadingHTTPServer((args.host, args.port), BrokerHandler)
    server.token = token  # type: ignore[attr-defined]
    server.db_path = db_path  # type: ignore[attr-defined]
    server.mirror_path = mirror_path  # type: ignore[attr-defined]
    server.work_mirror_path = work_mirror_path  # type: ignore[attr-defined]
    shutdown = False

    def handle_signal(signum: int, _frame: Any) -> None:
        nonlocal shutdown
        shutdown = True
        server.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    print(f"coop-broker listening on http://{args.host}:{args.port}", flush=True)
    while not shutdown:
        server.serve_forever(poll_interval=0.5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Coop Broker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument("--db", default=str(DEFAULT_DB))
    serve_p.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    serve_p.add_argument("--mirror", default=str(DEFAULT_MIRROR))
    serve_p.add_argument("--work-mirror", default=str(DEFAULT_WORK_MIRROR))
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return serve(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
