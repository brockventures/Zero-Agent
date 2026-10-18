import unittest
import time
from unittest.mock import patch
from pathlib import Path
import tempfile
import json
import shutil

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
        res = outbox.queue_outbox_message("lounge", msg)
        is_dup, reason = outbox.is_duplicate_recent("lounge", msg)
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


if __name__ == "__main__":
    unittest.main()
