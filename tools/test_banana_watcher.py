import unittest
from unittest.mock import patch, MagicMock
import json
import time

from tools.banana_watcher import check_channel_and_evaluate, STALL_THRESHOLD_SECONDS

class TestBananaWatcher(unittest.TestCase):
    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_status_ping_does_not_stall(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "12345",
                "timestamp": msg_time_str,
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 0, "kind": "status", "reply": "none", "subject": "hestia-leg-check"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            # Should NOT trigger stalled topic nudge
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_proposal_stalls_when_idle(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "12346",
                "timestamp": msg_time_str,
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1, "kind": "proposal", "reply": "optional", "subject": "banana-cache-spec"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertTrue(any("Stalled topic nudge: banana-cache-spec" in a for a in actions))

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_single_turn_closure_does_not_prompt_executive_summary_or_warn(self, mock_save, mock_load, mock_post, mock_bstatus):
        raw_msgs = [
            {
                "id": "12347",
                "timestamp": "2026-09-06T05:00:00Z",
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1, "kind": "status", "floor": "closed", "subject": "hestia-leg-check"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_multi_turn_premature_closure_triggers_contradiction_warning(self, mock_save, mock_load, mock_post, mock_bstatus):
        # Simulates the exact Marvin 19:03:40 incident on agent-collaborative-project (newest first from Discord API)
        raw_msgs = [
            {
                "id": "1003",
                "timestamp": "2026-09-07T02:03:40Z",
                "author": {"id": "1492043459618537492", "username": "Marvin"},
                "content": '```handoff\n{"v": 1, "kind": "status", "reply": "none", "floor": "closed", "subject": "agent-collaborative-project"}\n```'
            },
            {
                "id": "1002",
                "timestamp": "2026-09-07T02:01:01Z",
                "author": {"id": "1492043459618537492", "username": "Marvin"},
                "content": '```handoff\n{"v": 1, "kind": "answer", "floor": "open", "subject": "agent-collaborative-project"}\n```'
            },
            {
                "id": "1001",
                "timestamp": "2026-09-07T02:00:20Z",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '```handoff\n{"v": 1, "kind": "status", "floor": "open", "subject": "agent-collaborative-project", "round": 1}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertTrue(any("Contradiction warning to Marvin on agent-collaborative-project: [premature_closure]" in a for a in actions))

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_concluded_multi_turn_discussion_triggers_summary_prompt(self, mock_save, mock_load, mock_post, mock_bstatus):
        raw_msgs = [
            {
                "id": "2002",
                "timestamp": "2026-09-07T03:00:00Z",
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1, "kind": "consensus", "reply": "none", "floor": "closed", "subject": "banana-sync-hardening"}\n```'
            },
            {
                "id": "2001",
                "timestamp": "2026-09-07T02:55:00Z",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '```handoff\n{"v": 1, "kind": "proposal", "reply": "required", "to": "amos", "floor": "open", "subject": "banana-sync-hardening"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertTrue(any("Summary prompt to Zero on banana-sync-hardening" in a for a in actions))

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_already_dispatched_summary_suppresses_prompt(self, mock_save, mock_load, mock_post, mock_bstatus):
        raw_msgs = [
            {
                "id": "1002",
                "timestamp": "2026-09-07T02:05:00Z",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '🍌 Parking `banana-sync-hardening`: Executive summary dispatched to #lounge.\n\n```handoff\n{"v": 1, "kind": "resolution", "floor": "closed", "reply": "none", "subject": "banana-sync-hardening"}\n```'
            },
            {
                "id": "1001",
                "timestamp": "2026-09-07T02:00:20Z",
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1, "kind": "proposal", "floor": "open", "subject": "banana-sync-hardening", "round": 1}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertTrue(any("Summary already dispatched for banana-sync-hardening; suppressing prompt to Zero" in a for a in actions))
            mock_post.assert_not_called()

    def test_analyze_contradictions_direct(self):
        from tools.banana_watcher import analyze_envelope_contradictions

        # 1. Premature closure on multi-turn
        c1 = analyze_envelope_contradictions({"kind": "status", "floor": "closed", "subject": "topic-a"}, subject_turns=2)
        self.assertEqual([x["tag"] for x in c1], ["premature_closure"])

        # Single-turn status closed floor is valid (not a multi-turn discussion)
        c1_single = analyze_envelope_contradictions({"kind": "status", "floor": "closed", "subject": "topic-a"}, subject_turns=1)
        self.assertEqual(c1_single, [])

        # 2. Deadlock handoff: reply required + floor closed
        c2 = analyze_envelope_contradictions({"kind": "answer", "floor": "closed", "reply": "required", "to": "zero", "subject": "topic-b"})
        self.assertIn("deadlock_handoff", [x["tag"] for x in c2])

        # 3. Open terminal: consensus + floor open
        c3 = analyze_envelope_contradictions({"kind": "consensus", "floor": "open", "reply": "none", "subject": "topic-c"})
        self.assertEqual([x["tag"] for x in c3], ["open_terminal"])

        # 4. Unaddressed baton: reply required without target
        c4 = analyze_envelope_contradictions({"kind": "proposal", "reply": "required", "floor": "open", "subject": "topic-d"})
        self.assertEqual([x["tag"] for x in c4], ["unaddressed_baton"])

        # 5. Unanswerable question: question + reply none
        c5 = analyze_envelope_contradictions({"kind": "question", "reply": "none", "floor": "open", "subject": "topic-e"})
        self.assertEqual([x["tag"] for x in c5], ["unanswerable_question"])

        # 6. Yielded baton pass: handoff + reply none
        c6 = analyze_envelope_contradictions({"kind": "handoff", "reply": "none", "floor": "open", "to": "amos", "subject": "topic-f"})
        self.assertEqual([x["tag"] for x in c6], ["yielded_baton_pass"])

        # 7. Governor limit breach without terminal clamp
        c7 = analyze_envelope_contradictions({"kind": "answer", "reply": "optional", "floor": "open", "round": 10, "max_rounds": 10, "subject": "topic-g"})
        self.assertEqual([x["tag"] for x in c7], ["governor_breach"])

        # Clean terminal envelope produces NO contradictions
        c_clean = analyze_envelope_contradictions({"kind": "consensus", "reply": "none", "floor": "closed", "round": 3, "max_rounds": 10, "subject": "topic-clean"}, subject_turns=3)
        self.assertEqual(c_clean, [])

        # 8. Game topic floor closed triggers game_floor_closed contradiction
        c8 = analyze_envelope_contradictions({"kind": "status", "floor": "closed", "subject": "agora-trading-floor"})
        self.assertEqual([x["tag"] for x in c8], ["game_floor_closed"])

        # Game topic with round == max_rounds does NOT trigger governor breach
        c_game_rounds = analyze_envelope_contradictions({"kind": "status", "floor": "open", "round": 5, "max_rounds": 5, "subject": "agora-trading-floor"})
        self.assertEqual(c_game_rounds, [])

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_game_topic_exempt_from_loop_warning(self, mock_save, mock_load, mock_post, mock_bstatus):
        # 12 turns on agora-trading-floor must NOT trigger loop warning
        raw_msgs = [
            {
                "id": str(1000 + i),
                "timestamp": "2026-09-20T20:50:00Z",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": f'```handoff\n{{"v": 1, "kind": "status", "floor": "open", "subject": "agora-trading-floor", "round": {i}}}\n```'
            }
            for i in range(12)
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_game_topic_exempt_from_stall_and_autoclose(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "12349",
                "timestamp": msg_time_str,
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '```handoff\n{"v": 1, "kind": "status", "floor": "open", "subject": "operation-agon"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

if __name__ == "__main__":
    unittest.main()
