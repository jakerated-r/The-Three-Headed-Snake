#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAT_SCRIPT = REPO_ROOT / "src" / "chat" / "coop-chat.py"

spec = importlib.util.spec_from_file_location("coop_chat", CHAT_SCRIPT)
assert spec is not None and spec.loader is not None
coop_chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coop_chat)


class CoopChatTests(unittest.TestCase):
    def test_renders_plain_english_im_turn_to_the_second(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 1,
                "message_id": "abc12345",
                "created_at": "2026-05-27T15:12:00Z",
                "created_ns": 1_779_894_720_987_654_321,
                "from_agent": "Codex",
                "to_agent": "Gemini",
                "kind": "task",
                "priority": 8,
                "target": "three-headed-snake-chat",
                "status": "open",
                "body": {
                    "type": "auto-engage",
                    "engagement_mode": "co-builder",
                    "originating_agent": "Codex",
                    "thread": "three-headed-snake-chat",
                    "prompt_summary": "Build the IM console.",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("[15:12:00]", line)
        self.assertNotIn("987654321", line)
        self.assertIn("CODEX", line)
        self.assertIn("→ Gemini", line)
        self.assertIn("ENGAGE [co-builder]", line)
        self.assertIn("Build the IM console.", line)

    def test_strips_control_noise(self) -> None:
        text = coop_chat.strip_control_noise(
            "Read the originating prompt summary. advisory=read+FYI, co-builder=claim parallel lane and build, verifier=stand by for verify-me envelope."
        )
        self.assertEqual(text, "")

    def test_gemini_receipt_shows_stdout_not_only_verdict(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 2,
                "message_id": "gem12345",
                "created_at": "2026-05-27T15:16:34Z",
                "created_ns": 1_779_894_994_000_000_000,
                "from_agent": "Gemini",
                "to_agent": "Codex",
                "kind": "receipt",
                "priority": 8,
                "target": "three-headed-snake-im-console-live-fire",
                "status": "open",
                "body": {
                    "type": "gemini-run-result",
                    "verdict": "PASS",
                    "stdout_tail": "Status: Acknowledged.\nNext Action: Watching the terminal.\nTHREE_HEADED_SNAKE_XXX_RECEIPT: abc\n",
                    "artifacts": "/tmp/run",
                },
            },
            no_color=True,
            width=140,
            show_plumbing=True,
        )
        self.assertIn("PASS", line)
        self.assertIn("Status: Acknowledged.", line)
        self.assertIn("Next Action: Watching the terminal.", line)
        self.assertNotIn("THREE_HEADED_SNAKE_XXX_RECEIPT", line)

    def test_runner_receipt_strips_gemini_update_topic_noise(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 6,
                "message_id": "gem-noise",
                "created_at": "2026-05-27T16:12:00Z",
                "created_ns": 1_779_897_120_000_000_000,
                "from_agent": "Gemini",
                "to_agent": "Architect",
                "kind": "receipt",
                "priority": 10,
                "target": "snake-live-unit",
                "status": "open",
                "body": {
                    "type": "three-headed-snake-agent-run-result",
                    "agent": "Gemini",
                    "verdict": "PASS",
                    "stdout_tail": "update_topic(strategic_intent='x', summary='y')I am Gemini. Handoff acknowledged.\nTHREE_HEADED_SNAKE_XXX_RECEIPT: abc\n",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("I am Gemini. Handoff acknowledged.", line)
        self.assertNotIn("update_topic", line)

    def test_three_headed_snake_agent_receipt_renders_spoken_reply_without_artifacts_by_default(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 4,
                "message_id": "run12345",
                "created_at": "2026-05-27T15:30:00Z",
                "created_ns": 1_779_895_400_000_000_000,
                "from_agent": "Codex",
                "to_agent": "Architect",
                "kind": "receipt",
                "priority": 10,
                "target": "architect-cli-unit",
                "status": "open",
                "body": {
                    "type": "three-headed-snake-agent-run-result",
                    "agent": "Codex",
                    "verdict": "PASS",
                    "stdout_tail": "I see it. Terminal prompts are wired.\nTHREE_HEADED_SNAKE_XXX_RECEIPT: abc\n",
                    "artifacts": "/tmp/run",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("I see it. Terminal prompts are wired.", line)
        self.assertNotIn("THREE_HEADED_SNAKE_XXX_RECEIPT", line)
        self.assertNotIn("artifacts:", line)

    def test_architect_prompt_renders_as_plain_speech(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 3,
                "message_id": "arch12345",
                "created_at": "2026-05-27T15:24:00Z",
                "created_ns": 1_779_895_040_000_000_000,
                "from_agent": "Architect",
                "to_agent": "Gemini",
                "kind": "task",
                "priority": 10,
                "target": "architect-cli-test",
                "status": "open",
                "body": {
                    "type": "architect-prompt",
                    "prompt": "Can the three of you take this next pass?",
                    "thread": "architect-cli-test",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("ARCHITECT", line)
        self.assertIn("→ Gemini", line)
        self.assertIn("Can the three of you take this next pass?", line)

    def test_snake_live_event_renders_without_json_noise(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 5,
                "message_id": "live12345",
                "created_at": "2026-05-27T16:10:00Z",
                "created_ns": 1_779_897_000_000_000_000,
                "from_agent": "Codex",
                "to_agent": "Maestro",
                "kind": "handoff",
                "priority": 10,
                "target": "snake-live-unit",
                "status": "open",
                "body": {
                    "type": "snake-live-event",
                    "conversation_id": "snake-live-unit",
                    "phase": "handoff",
                    "text": "Implementation lane claimed. Maestro, pressure-test the standard.",
                    "latency_ms": 91,
                },
            },
            no_color=True,
            width=140,
            show_plumbing=True,
        )
        self.assertIn("HANDOFF · 91ms · snake-live-unit", line)
        self.assertIn("Implementation lane claimed.", line)
        self.assertNotIn('"type"', line)

    def test_snake_room_line_renders_as_plain_english_dialogue(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 7,
                "message_id": "room12345",
                "created_at": "2026-05-27T16:25:00Z",
                "created_ns": 1_779_897_900_000_000_000,
                "from_agent": "Codex",
                "to_agent": "Maestro",
                "kind": "note",
                "priority": 10,
                "target": "snake-room-unit",
                "status": "open",
                "body": {
                    "type": "snake-room-line",
                    "text": "Codex status: proof is running in the background.",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("CODEX", line)
        self.assertIn("→ Maestro", line)
        self.assertIn("proof is running in the background", line)
        self.assertNotIn("snake-room-line", line)

    def test_peer_verify_string_evidence_does_not_crash_chat_loop(self) -> None:
        line = coop_chat.render_chat_message(
            {
                "id": 70,
                "message_id": "verify-string-evidence",
                "created_at": "2026-06-03T21:06:00Z",
                "created_ns": 1_780_520_760_000_000_000,
                "from_agent": "Maestro",
                "to_agent": "Codex",
                "kind": "review",
                "priority": 10,
                "target": "string-evidence-unit",
                "status": "open",
                "body": {
                    "type": "peer-verification-required",
                    "goal": "Verify the live chat terminal stays up.",
                    "claim_id": "abcdef12-3456-7890",
                    "evidence": "Terminal was open, but renderer crashed on a non-dict evidence payload.",
                },
            },
            no_color=True,
            width=140,
        )
        self.assertIn("VERIFY REQUEST", line)
        self.assertIn("Terminal was open", line)

    def test_latest_message_id_reads_true_sqlite_tail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "broker.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO messages (id) VALUES (?)", [(3,), (91,), (3454,)])
            conn.commit()
            conn.close()

            old_path = coop_chat.SQLITE_PATH
            try:
                coop_chat.SQLITE_PATH = db
                self.assertEqual(coop_chat.latest_message_id(), 3454)
            finally:
                coop_chat.SQLITE_PATH = old_path

    def test_agent_send_payload_is_not_jake_authored(self) -> None:
        thread, payload = coop_chat.build_room_line_message(
            "Codex status: proof is running in the background.",
            "Codex",
            ("Codex", "Maestro", "Gemini"),
            target="agent-line-unit",
        )
        self.assertEqual(thread, "agent-line-unit")
        self.assertEqual(payload["from_agent"], "Codex")
        self.assertEqual(payload["to_agent"], "Architect")
        self.assertEqual(payload["kind"], "note")
        self.assertEqual(payload["body"]["type"], "snake-room-line")
        self.assertIn("background", payload["body"]["text"])

    def test_conversation_view_hides_broker_plumbing(self) -> None:
        seen: set[tuple[str, str]] = set()
        self.assertTrue(
            coop_chat.is_plumbing_message(
                {
                    "id": 8,
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "task",
                    "target": "auto-unit",
                    "body": {"type": "auto-engage", "prompt_summary": "tool lane"},
                },
                seen,
            )
        )
        self.assertFalse(
            coop_chat.is_plumbing_message(
                {
                    "id": 9,
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "note",
                    "target": "room-unit",
                    "body": {"type": "snake-room-line", "text": "Plain English."},
                },
                seen,
            )
        )

    def test_conversation_view_hides_legacy_role_ceremony(self) -> None:
        seen: set[tuple[str, str]] = set()
        self.assertTrue(
            coop_chat.is_plumbing_message(
                {
                    "id": 9,
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "note",
                    "target": "room-unit",
                    "body": {
                        "type": "snake-room-line",
                        "text": "I’ve got the " + "build lane. Maestro, keep Jake’s standard sharp.",
                    },
                },
                seen,
            )
        )

    def test_conversation_view_dedupes_architect_prompt_fanout(self) -> None:
        seen: set[tuple[str, str]] = set()
        original = {
            "id": 10,
            "from_agent": "Architect",
            "to_agent": "Codex",
            "kind": "task",
            "target": "thread-1",
            "body": {"type": "architect-prompt", "prompt": "Coordinate.", "thread": "thread-1"},
        }
        duplicate = {
            "id": 11,
            "from_agent": "Architect",
            "to_agent": "Maestro",
            "kind": "task",
            "target": "thread-1",
            "body": {"type": "architect-prompt", "prompt": "Coordinate.", "thread": "thread-1"},
        }
        fanout = {
            "id": 12,
            "from_agent": "Architect",
            "to_agent": "Gemini",
            "kind": "task",
            "target": "thread-1",
            "body": {
                "type": "architect-prompt",
                "prompt": "Coordinate.",
                "thread": "thread-1",
                "source": "three-headed-snake-live-room-fanout",
            },
        }
        self.assertFalse(coop_chat.is_plumbing_message(original, seen))
        self.assertTrue(coop_chat.is_plumbing_message(duplicate, seen))
        self.assertTrue(coop_chat.is_plumbing_message(fanout, seen))

    def test_plain_text_input_broadcasts_to_three_headed_snake(self) -> None:
        action, recipients, prompt = coop_chat.parse_composer_line("Ship this together.")
        self.assertEqual(action, "send")
        self.assertEqual(recipients, ("Codex", "Maestro", "Gemini"))
        self.assertEqual(prompt, "Ship this together.")

    def test_direct_input_targets_one_agent(self) -> None:
        action, recipients, prompt = coop_chat.parse_composer_line("/to Gemini verify the broker")
        self.assertEqual(action, "send")
        self.assertEqual(recipients, ("Gemini",))
        self.assertEqual(prompt, "verify the broker")

    def test_architect_prompt_payloads_are_first_class_broker_messages(self) -> None:
        thread, payloads = coop_chat.build_architect_prompt_messages(
            "Status from all three.",
            ("Codex", "Maestro", "Gemini"),
            target="architect-cli-unit",
        )
        self.assertEqual(thread, "architect-cli-unit")
        self.assertEqual([p["to_agent"] for p in payloads], ["Codex", "Maestro", "Gemini"])
        for payload in payloads:
            self.assertEqual(payload["from_agent"], "Architect")
            self.assertEqual(payload["kind"], "task")
            self.assertEqual(payload["priority"], 10)
            self.assertEqual(payload["body"]["type"], "architect-prompt")
            self.assertEqual(payload["body"]["prompt"], "Status from all three.")
            self.assertIn("normal group chat", payload["body"]["instructions"])
            self.assertIn("no role ceremony", payload["body"]["instructions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
