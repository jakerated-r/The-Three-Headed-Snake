#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "broker"))

import coop_broker  # noqa: E402

IDENTITY_SCRIPT = REPO_ROOT / "src" / "identity" / "per-agent-envelope.py"
spec = importlib.util.spec_from_file_location("per_agent_envelope", IDENTITY_SCRIPT)
assert spec is not None and spec.loader is not None
per_agent_envelope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(per_agent_envelope)


class CoopBrokerTests(unittest.TestCase):
    def test_redaction_removes_common_secret_shapes(self) -> None:
        text = (
            "OPENAI_API_KEY=" + "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz012345" + " "
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456 "
            "GITHUB_TOKEN=" + "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz123456"
        )
        redacted = coop_broker.redact_text(text)
        self.assertNotIn("sk-proj-", redacted)
        self.assertNotIn("ghp_", redacted)
        self.assertNotIn("Bearer abc", redacted)
        self.assertIn("[REDACTED:", redacted)

    def test_create_list_ack_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broker.sqlite3"
            mirror = Path(tmp) / "LIVE_BRIDGE.md"
            conn = coop_broker.connect(db)
            message = coop_broker.create_message(
                conn,
                {
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "task",
                    "priority": 2,
                    "target": "projects/onyx",
                    "body": {"ask": "verify live model", "secret": "sk-" + "ant-" + "abcdefghijklmnopqrstuvwxyz012345"},
                },
                mirror=mirror,
            )
            self.assertEqual(message["status"], "open")
            self.assertGreater(message["created_ns"], 0)
            self.assertEqual(message["from_agent"], "Codex")
            self.assertEqual(message["to_agent"], "Maestro")
            self.assertNotIn("sk-ant-", json.dumps(message["body"]))
            inbox = coop_broker.list_messages(conn, to_agent="Maestro")
            self.assertEqual(len(inbox), 1)
            acked = coop_broker.ack_message(conn, message["message_id"], "Maestro", mirror=mirror)
            self.assertEqual(acked["status"], "acked")
            self.assertEqual(acked["ack_by"], "Maestro")
            mirror_text = mirror.read_text(encoding="utf-8")
            self.assertIn(message["message_id"], mirror_text)
            self.assertIn("· ack · Maestro -> Codex", mirror_text)
            self.assertIn("- Status: acked", mirror_text)

    def test_existing_message_schema_migrates_created_ns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broker.sqlite3"
            raw = sqlite3.connect(db)
            raw.executescript(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
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
                """
            )
            raw.close()
            conn = coop_broker.connect(db)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            self.assertIn("created_ns", columns)

    def test_reservation_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            first = coop_broker.reserve_path(
                conn,
                {"path": "projects/onyx", "agent": "Codex", "reason": "patching", "ttl_seconds": 60},
            )
            second = coop_broker.reserve_path(
                conn,
                {"path": "projects/onyx", "agent": "Codex", "reason": "still patching", "ttl_seconds": 60},
            )
            reservations = coop_broker.list_reservations(conn)
            self.assertEqual(len(reservations), 1)
            self.assertEqual(first["path"], second["path"])
            self.assertEqual(reservations[0]["agent"], "Codex")
            self.assertEqual(reservations[0]["reason"], "still patching")

    def test_reservation_blocks_parent_child_conflicts_across_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            coop_broker.reserve_path(
                conn,
                {"path": "projects/onyx", "agent": "Codex", "reason": "editing voice loop", "ttl_seconds": 600},
            )
            with self.assertRaises(coop_broker.BrokerError) as ctx:
                coop_broker.reserve_path(
                    conn,
                    {
                        "path": "projects/onyx/src/main.py",
                        "agent": "Maestro",
                        "reason": "review patch",
                        "ttl_seconds": 600,
                    },
                )
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("under construction by Codex", ctx.exception.message)

    def test_work_claim_complete_requires_peer_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broker.sqlite3"
            live = Path(tmp) / "LIVE_BRIDGE.md"
            work = Path(tmp) / "TWIN_TOWERS.md"
            conn = coop_broker.connect(db)
            claim = coop_broker.create_work_claim(
                conn,
                {
                    "agent": "Codex",
                    "project": "onyx",
                    "goal": "patch voice loop",
                    "paths": ["projects/onyx"],
                    "ttl_seconds": 600,
                },
                work_mirror=work,
            )
            self.assertEqual(claim["status"], "active")
            completed = coop_broker.complete_work_claim(
                conn,
                {
                    "claim_id": claim["claim_id"],
                    "agent": "Codex",
                    "evidence": {"tests": ["pytest tests/onyx -q"], "summary": "green"},
                },
                live_mirror=live,
                work_mirror=work,
            )
            self.assertEqual(completed["status"], "completed")
            review_messages = coop_broker.list_messages(conn, to_agent="Maestro", status="open")
            self.assertEqual(len(review_messages), 1)
            self.assertEqual(review_messages[0]["kind"], "review")
            verified = coop_broker.verify_work_claim(
                conn,
                {
                    "claim_id": claim["claim_id"],
                    "verifier": "Maestro",
                    "verdict": "pass",
                    "verification": {"checks": ["inspected diff", "reran tests"], "summary": "ship"},
                    "signed_verdict": per_agent_envelope.sign_verdict(
                        "Maestro",
                        claim["claim_id"],
                        "pass",
                        {"checks": ["inspected diff", "reran tests"], "summary": "ship"},
                    ),
                },
                live_mirror=live,
                work_mirror=work,
            )
            self.assertEqual(verified["status"], "verified")
            reservations = coop_broker.list_reservations(conn)
            self.assertEqual(reservations[0]["status"], "released")
            receipts = coop_broker.list_messages(conn, to_agent="Codex", status="open")
            self.assertEqual(receipts[0]["kind"], "receipt")
            self.assertIn("completion-claimed-peer-verification-required", work.read_text(encoding="utf-8"))

    def test_gemini_is_first_class_message_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            message = coop_broker.create_message(
                conn,
                {
                    "from_agent": "Codex",
                    "to_agent": "Gemini",
                    "kind": "task",
                    "priority": 8,
                    "target": "three-headed-snake-smoke",
                    "body": {"type": "auto-engage", "thread": "three-headed-snake-smoke"},
                },
                mirror=Path(tmp) / "mirror.md",
            )
            self.assertEqual(message["to_agent"], "Gemini")
            inbox = coop_broker.list_messages(conn, to_agent="Gemini")
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["from_agent"], "Codex")

    def test_architect_is_first_class_message_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            message = coop_broker.create_message(
                conn,
                {
                    "from_agent": "Architect",
                    "to_agent": "Codex",
                    "kind": "task",
                    "priority": 10,
                    "target": "architect-cli-unit",
                    "body": {"type": "architect-prompt", "prompt": "Answer this from the terminal."},
                },
                mirror=Path(tmp) / "mirror.md",
            )
            self.assertEqual(message["from_agent"], "Architect")
            self.assertEqual(message["to_agent"], "Codex")
            inbox = coop_broker.list_messages(conn, to_agent="Codex")
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["body"]["type"], "architect-prompt")

    def test_unsigned_claim_verdict_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            claim = coop_broker.create_work_claim(
                conn,
                {
                    "agent": "Codex",
                    "project": "signed-verdict",
                    "goal": "require per-agent signature",
                    "paths": ["projects/onyx"],
                    "ttl_seconds": 600,
                },
                work_mirror=Path(tmp) / "work.md",
            )
            coop_broker.complete_work_claim(
                conn,
                {
                    "claim_id": claim["claim_id"],
                    "agent": "Codex",
                    "evidence": {"summary": "done"},
                },
                live_mirror=Path(tmp) / "live.md",
                work_mirror=Path(tmp) / "work.md",
            )
            with self.assertRaises(coop_broker.BrokerError) as ctx:
                coop_broker.verify_work_claim(
                    conn,
                    {
                        "claim_id": claim["claim_id"],
                        "verifier": "Maestro",
                        "verdict": "pass",
                        "verification": {"summary": "ship"},
                    },
                    live_mirror=Path(tmp) / "live.md",
                    work_mirror=Path(tmp) / "work.md",
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("signed_verdict", ctx.exception.message)

    def test_claim_breadth_and_ttl_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            with self.assertRaises(coop_broker.BrokerError) as ctx:
                coop_broker.create_work_claim(
                    conn,
                    {
                        "agent": "Codex",
                        "project": "too-wide",
                        "goal": "cover too many paths",
                        "paths": [f"projects/path-{i}" for i in range(coop_broker.MAX_CLAIM_PATHS + 1)],
                    },
                    work_mirror=Path(tmp) / "work.md",
                )
            self.assertEqual(ctx.exception.status, 400)
            with self.assertRaises(coop_broker.BrokerError):
                coop_broker.create_work_claim(
                    conn,
                    {
                        "agent": "Codex",
                        "project": "too-broad",
                        "goal": "claim brain root",
                        "paths": [str(coop_broker.BRAIN_DIR)],
                    },
                    work_mirror=Path(tmp) / "work.md",
                )

    def test_emergency_stop_requires_evidence_and_rate_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            with self.assertRaises(coop_broker.BrokerError) as ctx:
                coop_broker.create_message(
                    conn,
                    {
                        "from_agent": "Codex",
                        "to_agent": "Maestro",
                        "kind": "emergency-stop",
                        "priority": 10,
                        "target": "three-headed-snake",
                        "body": {"reason": "stop now"},
                    },
                    mirror=Path(tmp) / "live.md",
                )
            self.assertEqual(ctx.exception.status, 400)
            first = coop_broker.create_message(
                conn,
                {
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "emergency-stop",
                    "priority": 10,
                    "target": "three-headed-snake",
                    "body": {"reason": "credible halt reason", "evidence": ["test evidence"]},
                },
                mirror=Path(tmp) / "live.md",
            )
            self.assertEqual(first["kind"], "emergency-stop")
            with self.assertRaises(coop_broker.BrokerError) as rate_ctx:
                coop_broker.create_message(
                    conn,
                    {
                        "from_agent": "Codex",
                        "to_agent": "Maestro",
                        "kind": "emergency-stop",
                        "priority": 10,
                        "target": "three-headed-snake",
                        "body": {"reason": "credible halt reason", "evidence": ["test evidence"]},
                    },
                    mirror=Path(tmp) / "live.md",
                )
            self.assertEqual(rate_ctx.exception.status, 429)

    def test_off_limits_targeted_reads_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            envelope = {
                "payload": {
                    "targeted_reads": ["private/sacred.md"],
                }
            }
            with self.assertRaises(coop_broker.BrokerError) as ctx:
                coop_broker.create_message(
                    conn,
                    {
                        "from_agent": "Maestro",
                        "to_agent": "Codex",
                        "kind": "request",
                        "priority": 9,
                        "target": "off-limits-test",
                        "body": {"envelope": envelope},
                    },
                    mirror=Path(tmp) / "live.md",
                )
            self.assertEqual(ctx.exception.status, 403)

    def test_invalid_agent_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = coop_broker.connect(Path(tmp) / "broker.sqlite3")
            with self.assertRaises(coop_broker.BrokerError):
                coop_broker.create_message(
                    conn,
                    {"from_agent": "Mallory", "to_agent": "Codex", "kind": "note", "body": "nope"},
                    mirror=Path(tmp) / "mirror.md",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
