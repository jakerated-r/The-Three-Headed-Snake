#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH_SCRIPT = REPO_ROOT / "src" / "orchestrator" / "three-headed-snake-orchestrator.py"

spec = importlib.util.spec_from_file_location("three_headed_snake_orchestrator", ORCH_SCRIPT)
assert spec is not None and spec.loader is not None
orchestrator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = orchestrator
spec.loader.exec_module(orchestrator)


class ThreeHeadedSnakeOrchestratorTests(unittest.TestCase):
    def architect_message(self, to_agent: str = "Codex") -> dict:
        return {
            "id": 10,
            "message_id": f"msg-{to_agent}",
            "created_at": "2026-05-27T16:00:00Z",
            "created_ns": 1_779_896_400_000_000_000,
            "from_agent": "Architect",
            "to_agent": to_agent,
            "kind": "task",
            "priority": 10,
            "target": "snake-unit",
            "body": {
                "type": "architect-prompt",
                "prompt": "Show me all three coordinating live.",
                "thread": "snake-unit",
                "recipients": [to_agent],
            },
        }

    def test_grouping_prompts_one_head_wakes_all_three(self) -> None:
        groups = orchestrator.group_messages([self.architect_message("Codex")])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.requested_recipients, ("Codex",))
        self.assertEqual(group.effective_agents, ("Codex", "Maestro", "Gemini"))
        self.assertEqual(group.source_kind, "architect-prompt")

    def test_grouping_dedupes_broadcast_messages_by_thread_and_prompt(self) -> None:
        messages = [self.architect_message("Codex"), self.architect_message("Maestro"), self.architect_message("Gemini")]
        for index, message in enumerate(messages, start=10):
            message["id"] = index
        groups = orchestrator.group_messages(messages)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].requested_recipients, ("Codex", "Maestro", "Gemini"))

    def test_auto_engage_group_also_wakes_whole_room(self) -> None:
        groups = orchestrator.group_messages(
            [
                {
                    "id": 20,
                    "message_id": "auto-1",
                    "from_agent": "Codex",
                    "to_agent": "Maestro",
                    "kind": "task",
                    "target": "auto-unit",
                    "body": {
                        "type": "auto-engage",
                        "originating_agent": "Codex",
                        "thread": "auto-unit",
                        "prompt_summary": "Coordinate on this.",
                    },
                }
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].source_kind, "auto-engage")
        self.assertEqual(groups[0].effective_agents, ("Codex", "Maestro", "Gemini"))

    def test_live_events_include_ack_handoff_and_working_lines(self) -> None:
        group = orchestrator.group_messages([self.architect_message("Codex")])[0]
        events = orchestrator.build_live_events(group)
        phases = [event.phase for event in events]
        self.assertEqual(phases.count("ack"), 3)
        self.assertEqual(phases.count("handoff"), 3)
        self.assertEqual(phases.count("working"), 3)
        self.assertIn(("Codex", "Maestro"), [(event.from_agent, event.to_agent) for event in events])
        self.assertIn("Got you", events[0].text)
        self.assertIn("real answer", events[0].text)
        self.assertNotIn("Build lane", events[0].text)

    def test_room_dialogue_posts_plain_english_between_agents(self) -> None:
        group = orchestrator.group_messages([self.architect_message("Codex")])[0]
        events = orchestrator.build_room_dialogue(group)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].from_agent, "Codex")
        self.assertEqual(events[0].to_agent, "Architect")
        self.assertEqual(events[0].kind, "note")
        self.assertEqual(events[0].phase, "dialogue")
        self.assertIn("real answer", events[0].text)
        self.assertNotIn("build lane", events[0].text)

    def test_state_store_dedupes_processed_group_keys(self) -> None:
        group = orchestrator.group_messages([self.architect_message("Codex")])[0]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = orchestrator.StateStore(state_path)
            self.assertFalse(state.has_processed(group.key))
            state.mark_processed(group)
            state.save()
            reloaded = orchestrator.StateStore(state_path)
            self.assertTrue(reloaded.has_processed(group.key))

    def test_runner_launch_acks_failures_to_prevent_duplicate_blocker_storms(self) -> None:
        group = orchestrator.group_messages([self.architect_message("Codex")])[0]
        with patch.object(orchestrator.subprocess, "Popen") as popen:
            orchestrator.launch_architect_runner(group, timeout=90)
        cmd = popen.call_args.args[0]
        self.assertIn("--ack-failures", cmd)
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "90")


if __name__ == "__main__":
    unittest.main(verbosity=2)
