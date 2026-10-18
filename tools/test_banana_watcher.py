import unittest
from unittest.mock import patch, MagicMock
import json
import time

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
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
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
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
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
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
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_concluded_multi_turn_discussion_stays_silent_without_lounge_prompt(self, mock_save, mock_load, mock_post, mock_bstatus):
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
            # Watcher should NOT prompt Zero for #lounge executive summary
            self.assertFalse(any("Summary prompt to Zero" in a for a in actions))
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_parking_closed_topic_does_not_prompt_lounge_summary(self, mock_save, mock_load, mock_post, mock_bstatus):
        raw_msgs = [
            {
                "id": "1002",
                "timestamp": "2026-09-07T02:05:00Z",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '🍌 Parking `banana-sync-hardening`: Task completed.\n\n```handoff\n{"v": 1, "kind": "resolution", "floor": "closed", "reply": "none", "subject": "banana-sync-hardening"}\n```'
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
            self.assertFalse(any("Summary prompt to Zero" in a for a in actions))
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

        # 9. Non-existent / unknown recipient
        c9_single = analyze_envelope_contradictions({"kind": "answer", "to": "fakebot", "subject": "topic-h"})
        self.assertEqual([x["tag"] for x in c9_single], ["unknown_recipient"])

        c9_list = analyze_envelope_contradictions({"kind": "answer", "to": ["amos", "ghostbot"], "subject": "topic-h"})
        self.assertEqual([x["tag"] for x in c9_list], ["unknown_recipient"])

        c9_valid_list = analyze_envelope_contradictions({"kind": "answer", "to": ["amos", "aerial"], "subject": "topic-h"})
        self.assertEqual(c9_valid_list, [])

        c9_wildcard = analyze_envelope_contradictions({"kind": "answer", "to": "team", "subject": "topic-h"})
        self.assertEqual(c9_wildcard, [])

        # 10. Unratified kind (e.g. kind: "task")
        c10_task = analyze_envelope_contradictions({"kind": "task", "to": "amos", "subject": "topic-i"})
        self.assertEqual([x["tag"] for x in c10_task], ["unratified_kind"])

        c10_valid_finding = analyze_envelope_contradictions({"kind": "finding", "to": "amos", "subject": "topic-i"})
        self.assertEqual(c10_valid_finding, [])

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
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
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
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

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {}, "summarized_subjects": {"concluded-morning-standup": 1790000000}})
    @patch("tools.banana_watcher.save_state")
    def test_concluded_topic_does_not_re_stall_on_trailing_open_envelope(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "9999",
                "timestamp": msg_time_str,
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '🍌 Park it. Trailing ACK.\n\n```handoff\n{"v": 1, "kind": "answer", "floor": "open", "subject": "morning-standup"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_parking_text_does_not_stall(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "9998",
                "timestamp": msg_time_str,
                "author": {"id": "1542035925603713086", "username": "Aerial"},
                "content": 'Parking it! Standup is concluded.\n\n```handoff\n{"v": 1, "kind": "answer", "floor": "open", "subject": "arch-discussion"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=True)
            self.assertEqual(actions, [])
            mock_post.assert_not_called()

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_stalled_topic_nudges_single_closer_not_robot(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "9997",
                "timestamp": msg_time_str,
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1, "kind": "proposal", "floor": "open", "subject": "amos-feature"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=False)
            mock_post.assert_called_once()
            call_msg = mock_post.call_args[0][0]
            # Must tag Amos specifically (<@1468012353206354197>), NOT broadcast role @robot
            self.assertIn("<@1468012353206354197>", call_msg)
            self.assertNotIn("<@&1543462881624858624>", call_msg)

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.post_discord", return_value=True)
    @patch("tools.banana_watcher.load_state", return_value={"nudged_stalls": {}, "nudged_handoffs": {}, "warned_contradictions": {"9996": 1790000000}, "summarized_subjects": {}})
    @patch("tools.banana_watcher.save_state")
    def test_max_rounds_auto_concludes_after_idle_buffer(self, mock_save, mock_load, mock_post, mock_bstatus):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        msg_time_str = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_msgs = [
            {
                "id": "9996",
                "timestamp": msg_time_str,
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": 'Round 3 wrap.\n\n```handoff\n{"v": 1, "kind": "answer", "floor": "open", "round": 3, "max_rounds": 3, "subject": "morning-topic-2026-09-26"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=raw_msgs):
            actions = check_channel_and_evaluate(dry_run=False)
            self.assertTrue(any("Discussion concluded on max rounds reached" in a for a in actions))
            mock_post.assert_called_once()
            call_msg = mock_post.call_args[0][0]
            self.assertIn("Discussion Concluded (Max Rounds Reached)", call_msg)


    def test_kind_aliases_normalize_without_unratified_kind_warning(self):
        from tools.banana_watcher import analyze_envelope_contradictions
        for alias in ["resolution", "review", "update"]:
            env = {"v": 1.1, "kind": alias, "reply": "none", "floor": "open", "subject": "test-alias"}
            contradictions = analyze_envelope_contradictions(env)
            unratified = [c for c in contradictions if c["tag"] == "unratified_kind"]
            self.assertEqual(unratified, [], f"Alias {alias} should be accepted without unratified_kind contradiction")

        bad_env = {"v": 1.1, "kind": "unsupported_verb", "reply": "none", "floor": "open", "subject": "test-bad"}
        bad_contradictions = analyze_envelope_contradictions(bad_env)
        unratified = [c for c in bad_contradictions if c["tag"] == "unratified_kind"]
        self.assertEqual(len(unratified), 1)

    def test_normalize_subject(self):
        from tools.banana_watcher import normalize_subject
        self.assertEqual(normalize_subject("morning-topic-2026-10-10"), "morning-topic-2026-10-10")
        self.assertEqual(normalize_subject("morning-topic-2026-10-10: budget throttle"), "morning-topic-2026-10-10")
        self.assertEqual(normalize_subject("MORNING-TOPIC-2026-10-10: foo; bar"), "morning-topic-2026-10-10")
        self.assertEqual(normalize_subject(""), "")
        self.assertEqual(normalize_subject(None), "")

    def test_subjects_match(self):
        from tools.banana_watcher import subjects_match
        s1 = "morning-topic-2026-10-10"
        s2 = "morning-topic-2026-10-10: stamp waiter flag at the wait boundary; filed uw-lembas"
        s3 = "different-topic-2026-10-10"
        self.assertTrue(subjects_match(s1, s2))
        self.assertTrue(subjects_match(s2, s1))
        self.assertFalse(subjects_match(s1, s3))
        self.assertFalse(subjects_match("", s1))
        self.assertFalse(subjects_match(None, s1))

    @patch("os.getenv", return_value="dummy_zero_tok")
    @patch("tools.banana_watcher.get_discord_token", return_value="dummy_bw_tok")
    @patch("urllib.request.urlopen")
    def test_get_recent_messages_rejects_stripped_intent(self, mock_urlopen, mock_bw_tok, mock_getenv):
        from tools.banana_watcher import get_recent_messages
        mock_data = [
            {"id": "1", "author": {"id": "1468012353206354197"}, "content": ""},
            {"id": "2", "author": {"id": "1542285964213358633"}, "content": ""},
            {"id": "3", "author": {"id": "1468012353206354197"}, "content": ""},
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        import json
        mock_resp.read.return_value = json.dumps(mock_data).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = get_recent_messages(channel_id=123, limit=5)
        self.assertEqual(res, [])

    @patch("os.getenv", return_value="dummy_zero_tok")
    @patch("tools.banana_watcher.get_discord_token", return_value=None)
    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_get_recent_messages_retries_on_transient_error(self, mock_urlopen, mock_sleep, mock_bw_tok, mock_getenv):
        import urllib.error
        from tools.banana_watcher import get_recent_messages
        mock_data = [
            {"id": "1", "author": {"id": "1468012353206354197"}, "content": "```handoff\n{}\n```"},
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        import json
        mock_resp.read.return_value = json.dumps(mock_data).encode("utf-8")

        mock_urlopen.side_effect = [
            urllib.error.URLError("The handshake operation timed out"),
            MagicMock(__enter__=MagicMock(return_value=mock_resp))
        ]
        res = get_recent_messages(channel_id=123, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "1")

    @patch("tools.banana.get_status", return_value={"holder": None})
    @patch("tools.banana_watcher.load_state", return_value={})
    def test_check_channel_recognizes_subtitled_resolution(self, mock_load, mock_bstatus):
        from tools.banana_watcher import check_channel_and_evaluate
        messages = [
            {
                "id": "100",
                "timestamp": "2026-10-10T16:30:00.000000+00:00",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '```handoff\n{"v": 1.1, "kind": "handoff", "floor": "open", "subject": "morning-topic-2026-10-10"}\n```'
            },
            {
                "id": "101",
                "timestamp": "2026-10-10T16:35:00.000000+00:00",
                "author": {"id": "1468012353206354197", "username": "Amos"},
                "content": '```handoff\n{"v": 1.1, "kind": "consensus", "floor": "closed", "subject": "morning-topic-2026-10-10: resolution detail"}\n```'
            },
            {
                "id": "102",
                "timestamp": "2026-10-10T16:36:00.000000+00:00",
                "author": {"id": "1542285964213358633", "username": "Zero"},
                "content": '```handoff\n{"v": 1.1, "kind": "answer", "floor": "open", "subject": "morning-topic-2026-10-10"}\n```'
            }
        ]
        with patch("tools.banana_watcher.get_recent_messages", return_value=messages):
            actions = check_channel_and_evaluate(channel_id=123, dry_run=True)
            self.assertFalse(any("Auto-parked" in a for a in actions))
            self.assertFalse(any("Stalled topic nudge" in a for a in actions))

if __name__ == "__main__":
    unittest.main()

