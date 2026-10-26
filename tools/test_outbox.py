import os
import sys
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.outbox as outbox


class TestOutboxDedupe(unittest.TestCase):
    def setUp(self):
        self.silence_patcher = patch("tools.bridge_state.is_crab_cavern_silenced", return_value=(False, ""))
        self.silence_patcher.start()

        self.temp_dir = tempfile.mkdtemp()
        self.orig_data_dir = outbox.DATA_DIR
        self.orig_outbox_dir = outbox.OUTBOX_DIR
        self.orig_pending_file = outbox.PENDING_FILE
        self.orig_history_file = outbox.HISTORY_FILE

        outbox.DATA_DIR = Path(self.temp_dir)
        outbox.OUTBOX_DIR = outbox.DATA_DIR / "outbox"
        outbox.PENDING_FILE = outbox.OUTBOX_DIR / "pending.jsonl"
        outbox.HISTORY_FILE = outbox.OUTBOX_DIR / "history.jsonl"

    def tearDown(self):
        self.silence_patcher.stop()
        outbox.DATA_DIR = self.orig_data_dir
        outbox.OUTBOX_DIR = self.orig_outbox_dir
        outbox.PENDING_FILE = self.orig_pending_file
        outbox.HISTORY_FILE = self.orig_history_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_summary_topic(self):
        msg = "📋 **Executive Summary: Agent Credential Discovery vs. Instruction Gating**\nDetails"
        self.assertEqual(outbox.extract_summary_topic(msg), "agent credential discovery vs. instruction gating")

        msg2 = "🍌 **Executive Summary: Market Sandbox Standup**\nDetails"
        self.assertEqual(outbox.extract_summary_topic(msg2), "market sandbox standup")

        msg3 = "Regular chat message without summary header"
        self.assertIsNone(outbox.extract_summary_topic(msg3))

    def test_topic_match(self):
        t1 = "Agent Credential Discovery vs. Instruction Gating"
        t2 = "Agent Credential Discovery vs. Instruction Guardrails"
        t3 = "Market Sandbox Standup & Volatility Fix"
        self.assertTrue(outbox.topic_match(t1, t2))
        self.assertFalse(outbox.topic_match(t1, t3))

    def test_pending_queue_deduplication(self):
        s1 = "📋 **Executive Summary: Agent Credential Discovery vs. Instruction Gating**\n• Problem: foo\n• Resolution: bar"
        s2 = "📋 **Executive Summary: Agent Credential Discovery vs. Instruction Guardrails**\n• Problem: foo\n• Resolution: bar"

        r1 = outbox.queue_outbox_message("lounge", s1)
        self.assertFalse(r1.get("suppressed"))
        self.assertEqual(r1.get("status"), "queued")

        # Second summary should be suppressed by dedupe guard
        r2 = outbox.queue_outbox_message("lounge", s2)
        self.assertTrue(r2.get("suppressed"))
        self.assertEqual(r2.get("status"), "deduplicated")
        self.assertIn("already pending in queue", r2.get("reason", ""))

        # Force flag should bypass deduplication
        r3 = outbox.queue_outbox_message("lounge", s2, force=True)
        self.assertFalse(r3.get("suppressed"))
        self.assertEqual(r3.get("status"), "queued")

    def test_history_deduplication(self):
        s1 = "📋 **Executive Summary: Afternoon Security Review**\nDetails"
        s2 = "🍌 **Executive Summary: Afternoon Security Review**\nDetails updated"

        # Record s1 to history as if dispatched
        outbox.record_dispatched_history({
            "id": "test-hist-1",
            "channel": "lounge",
            "channel_id": 1534452820995080192,
            "content": s1,
            "source": "unit-test"
        })

        # Attempting to queue s2 to lounge within window should be suppressed
        r = outbox.queue_outbox_message("lounge", s2, dedupe_window=300)
        self.assertTrue(r.get("suppressed"))
        self.assertEqual(r.get("status"), "deduplicated")

        # Different channel should not be suppressed
        r_other = outbox.queue_outbox_message("zero-chat", s2, dedupe_window=300)
        self.assertFalse(r_other.get("suppressed"))

    def test_hard_duplicate_guard_blocks_forced_identical(self):
        msg = "Identical payload test"
        # 1. Queue first message
        res1 = outbox.queue_outbox_message("lounge", msg)
        self.assertFalse(res1.get("suppressed"))

        # 2. Simulate dispatch to history
        outbox.record_dispatched_history(res1)

        # 3. Attempt forced re-queue within 120s
        res2 = outbox.queue_outbox_message("lounge", msg, force=True)
        self.assertTrue(res2.get("suppressed"))
        self.assertIn("Hard duplicate guard", res2.get("reason", ""))

    def test_is_duplicate_recent_alias(self):
        msg = "Alias test payload"
        outbox.queue_outbox_message("lounge", msg)
        is_dup, _ = outbox.is_duplicate_recent("lounge", msg)
        self.assertTrue(is_dup)

    def test_outbox_crab_cavern_silenced_suppresses_queue(self):
        self.silence_patcher.stop()
        try:
            with patch("tools.bridge_state.is_crab_cavern_silenced", return_value=(True, "Quota reset hold")):
                msg = "Test dispatch during blackout"
                res = outbox.queue_outbox_message("the-banana-stand", msg)
                self.assertTrue(res.get("suppressed"))
                self.assertEqual(res.get("status"), "silenced")
                self.assertIn("Quota reset hold", res.get("reason"))
        finally:
            self.silence_patcher.start()


class TestOutboxZeroOpsFallback(unittest.TestCase):
    def test_get_zero_fallback_token_from_env(self):
        with tempfile.TemporaryDirectory() as td:
            fake_env = Path(td) / ".env"
            fake_env.write_text('DISCORD_BOT_TOKEN="token_from_env"\n', encoding="utf-8")
            with patch("tools.outbox.Path") as mock_path:
                def fake_path(p):
                    if str(p) == "/opt/zero/.env":
                        return fake_env
                    return Path(p)
                mock_path.side_effect = fake_path
                token = outbox._get_zero_fallback_token()
                self.assertEqual(token, "token_from_env")

    def test_get_zero_fallback_token_from_json(self):
        with tempfile.TemporaryDirectory() as td:
            fake_json = Path(td) / "env.json"
            fake_json.write_text(json.dumps({"DISCORD_BOT_TOKEN": "token_from_json"}), encoding="utf-8")
            with patch("tools.outbox.Path") as mock_path:
                def fake_path(p):
                    if str(p) == "/opt/zero/.env":
                        return Path("/nonexistent/.env")
                    if str(p) == "/opt/zero/secrets/env.json":
                        return fake_json
                    return Path(p)
                mock_path.side_effect = fake_path
                token = outbox._get_zero_fallback_token()
                self.assertEqual(token, "token_from_json")

    @patch("tools.outbox._get_zero_fallback_token", return_value="zero_fallback_token_xyz")
    @patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "ivy_primary_token_abc"})
    @patch("tools.outbox.record_dispatched_history")
    def test_dispatch_via_rest_zero_ops_403_fallback(self, mock_record, mock_fallback_token):
        omsg = {
            "id": "outbox-test-1",
            "channel": "zero-ops",
            "channel_id": 1544953279664889888,
            "content": "Host level failure report",
        }

        call_requests = []

        class MockResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def read(self):
                return json.dumps({"id": "discord_msg_999"}).encode("utf-8")

        def fake_urlopen(req, timeout=15):
            call_requests.append(req)
            if len(call_requests) == 1:
                # First attempt with primary Ivy token returns 403 Forbidden
                raise urllib.error.HTTPError(
                    url=req.full_url,
                    code=403,
                    msg="Forbidden",
                    hdrs={},
                    fp=None,
                )
            # Second attempt with fallback Zero token succeeds
            return MockResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            success = outbox.dispatch_via_rest(omsg)

        self.assertTrue(success)
        self.assertEqual(len(call_requests), 2)

        # First request used Ivy primary token and unprefixed text
        first_req = call_requests[0]
        self.assertEqual(first_req.headers.get("Authorization"), "Bot ivy_primary_token_abc")
        first_body = json.loads(first_req.data.decode("utf-8"))
        self.assertFalse(first_body["content"].startswith("📡 **[Ivy Escalation from #baseball]:**\n"))

        # Second request used Zero fallback token and prefixed escalation text
        retry_req = call_requests[1]
        self.assertEqual(retry_req.headers.get("Authorization"), "Bot zero_fallback_token_xyz")
        retry_body = json.loads(retry_req.data.decode("utf-8"))
        self.assertTrue(retry_body["content"].startswith("📡 **[Ivy Escalation from #baseball]:**\n"))
        self.assertIn("Host level failure report", retry_body["content"])
        mock_record.assert_called_once_with(omsg)


if __name__ == "__main__":
    unittest.main()
