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


class TestOutboxSecurityHardening(unittest.TestCase):
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

    def test_queue_outbox_message_scrubs_credentials(self):
        """Verify queue_outbox_message redacts secrets before writing to pending.jsonl."""
        fake_gh_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        fake_api_key = "sk-testsecretkey1234567890abcdef"
        rec = outbox.queue_outbox_message(
            "lounge",
            f"Diagnostics token={fake_gh_token} key={fake_api_key}",
        )
        self.assertFalse(rec.get("suppressed"))
        self.assertNotIn(fake_gh_token, rec["content"])
        self.assertNotIn(fake_api_key, rec["content"])
        self.assertIn("[REDACTED_GITHUB_TOKEN]", rec["content"])
        self.assertIn("[REDACTED_API_KEY]", rec["content"])

        disk_text = outbox.PENDING_FILE.read_text(encoding="utf-8")
        self.assertNotIn(fake_gh_token, disk_text)
        self.assertNotIn(fake_api_key, disk_text)

    @patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test_bot_token_123"})
    @patch("tools.outbox.record_dispatched_history")
    def test_dispatch_via_rest_scrubs_credentials_and_suppresses_non_home_choices(self, mock_record):
        """Verify dispatch_via_rest scrubs secrets and strips [CHOICES] without attaching buttons on non-home channels."""
        fake_gh_token = "ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZ999999"
        omsg = {
            "id": "outbox-sec-1",
            "channel": "lounge",
            "channel_id": 1534452820995080192,  # Crab Cavern #lounge (non-home)
            "content": f"Status report token={fake_gh_token}\n\n[CHOICES: Deploy | Restart]",
        }
        sent_bodies = []

        class MockResp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def read(self):
                return json.dumps({"id": "msg-nonhome-1"}).encode("utf-8")

        def fake_urlopen(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode("utf-8")))
            return MockResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = outbox.dispatch_via_rest(omsg)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(sent_bodies), 1)
        for body in sent_bodies:
            self.assertNotIn(fake_gh_token, body["content"])
            self.assertIn("[REDACTED_GITHUB_TOKEN]", body["content"])
            self.assertNotIn("[CHOICES:", body["content"])
            self.assertNotIn("components", body)

    @patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test_bot_token_123"})
    @patch("tools.outbox.record_dispatched_history")
    def test_dispatch_via_rest_allows_choices_buttons_on_home_channel(self, mock_record):
        """Verify dispatch_via_rest still attaches choice: buttons for Home Turf channels."""
        from tools.bridge_state import TARGET_CHANNEL_ID
        omsg = {
            "id": "outbox-sec-home-1",
            "channel": "zero-chat",
            "channel_id": TARGET_CHANNEL_ID,
            "content": "Ready to proceed?\n\n[CHOICES: Deploy | Cancel]",
        }
        sent_bodies = []

        class MockResp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def read(self):
                return json.dumps({"id": "msg-home-1"}).encode("utf-8")

        def fake_urlopen(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode("utf-8")))
            return MockResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = outbox.dispatch_via_rest(omsg)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(sent_bodies), 1)
        first_body = sent_bodies[0]
        self.assertNotIn("[CHOICES:", first_body["content"])
        self.assertIn("components", first_body)
        btns = first_body["components"][0]["components"]
        self.assertEqual([b["custom_id"] for b in btns], ["choice:Deploy", "choice:Cancel"])

    @patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test_bot_token_123"})
    @patch("tools.outbox.record_dispatched_history")
    def test_dispatch_via_rest_allows_choices_buttons_on_baseball_channel(self, mock_record):
        """Verify #baseball (Ivy-owned Brock Guild channel) preserves [CHOICES: ...] buttons."""
        baseball_cid = 1548196929308065893
        self.assertTrue(outbox._is_home_turf_channel(baseball_cid))
        self.assertFalse(outbox._is_home_turf_channel(1534452820995080192))  # #lounge stays external

        omsg = {
            "id": "outbox-sec-baseball-1",
            "channel": "baseball",
            "channel_id": baseball_cid,
            "content": "Sabermetric R&D Radar ready.\n\n[CHOICES: Run Full Sweep | Skip Today]",
        }
        sent_bodies = []

        class MockResp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def read(self):
                return json.dumps({"id": "msg-baseball-1"}).encode("utf-8")

        def fake_urlopen(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode("utf-8")))
            return MockResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = outbox.dispatch_via_rest(omsg)

        self.assertTrue(ok)
        self.assertGreaterEqual(len(sent_bodies), 1)
        first_body = sent_bodies[0]
        self.assertNotIn("[CHOICES:", first_body["content"])
        self.assertIn("components", first_body)
        btns = first_body["components"][0]["components"]
        self.assertEqual([b["custom_id"] for b in btns], ["choice:Run Full Sweep", "choice:Skip Today"])


class TestOutboxAtomicPartitioning(unittest.TestCase):
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
        outbox.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.silence_patcher.stop()
        outbox.DATA_DIR = self.orig_data_dir
        outbox.OUTBOX_DIR = self.orig_outbox_dir
        outbox.PENDING_FILE = self.orig_pending_file
        outbox.HISTORY_FILE = self.orig_history_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_flush_pending_messages_channel_filter_preserves_unmatched_raw_jsonl(self):
        """Verify filtered flush extracts only matched channel and preserves unmatched raw JSONL lines verbatim."""
        forge_cid = 1562899941595160657
        zero_cid = 1542081375287640084
        lounge_cid = 1534452820995080192

        raw_zero = json.dumps({
            "id": "outbox-orig-zero-1",
            "channel": "zero-chat",
            "channel_id": zero_cid,
            "content": "Exact **markdown** payload with [CHOICES: A | B]",
            "source": "orig-turn",
            "type": "text",
            "force": True,
            "custom_meta": "keep-me",
        })
        raw_forge = json.dumps({
            "id": "outbox-orig-forge-1",
            "channel": "forge",
            "channel_id": forge_cid,
            "content": "FORGE build status update",
            "source": "vulcan",
            "type": "text",
        })
        raw_lounge = json.dumps({
            "id": "outbox-orig-lounge-1",
            "channel": "lounge",
            "channel_id": lounge_cid,
            "content": "Crab Cavern message during blackout",
            "source": "zero",
            "type": "text",
            "force": True,
        })
        outbox.PENDING_FILE.write_text(f"{raw_zero}\n{raw_forge}\n{raw_lounge}\n", encoding="utf-8")

        # Even if Crab Cavern is silenced, unmatched lines must be preserved verbatim without calling queue_outbox_message
        self.silence_patcher.stop()
        try:
            with patch("tools.bridge_state.is_crab_cavern_silenced", return_value=(True, "Blackout")), \
                 patch("tools.outbox.queue_outbox_message") as mock_queue:
                drained_forge = outbox.flush_pending_messages(channel_filter=forge_cid)
        finally:
            self.silence_patcher.start()

        mock_queue.assert_not_called()
        self.assertEqual(len(drained_forge), 1)
        self.assertEqual(drained_forge[0]["id"], "outbox-orig-forge-1")

        # Unmatched raw lines must remain byte-for-byte intact in pending.jsonl
        remaining_lines = outbox.PENDING_FILE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(remaining_lines, [raw_zero, raw_lounge])

        # Flushing a filter with no matching lines must leave pending.jsonl intact
        self.assertEqual(outbox.flush_pending_messages(channel_filter=forge_cid), [])
        self.assertEqual(outbox.PENDING_FILE.read_text(encoding="utf-8").splitlines(), [raw_zero, raw_lounge])

        # Unfiltered flush drains all remaining messages preserving original IDs and fields
        drained_rest = outbox.flush_pending_messages()
        self.assertEqual([m["id"] for m in drained_rest], ["outbox-orig-zero-1", "outbox-orig-lounge-1"])
        self.assertEqual(drained_rest[0]["custom_meta"], "keep-me")
        self.assertFalse(outbox.PENDING_FILE.exists())

    def test_flush_pending_messages_concurrent_partitioned_flushes_no_message_loss(self):
        """Verify concurrent Vulcan/Zero partitioned flushes + writes never lose or duplicate messages."""
        import threading

        forge_cid = 1562899941595160657
        zero_cid = 1542081375287640084
        total_per_channel = 40

        seen_forge: list[str] = []
        seen_zero: list[str] = []
        errors: list[Exception] = []
        producer_done = threading.Event()

        def producer():
            try:
                for i in range(total_per_channel):
                    outbox.queue_outbox_message("forge", f"forge-concurrent-msg-{i}", force=True)
                    outbox.queue_outbox_message("zero-chat", f"zero-concurrent-msg-{i}", force=True)
            except Exception as exc:
                errors.append(exc)
            finally:
                producer_done.set()

        def consumer(cid: int, bucket: list[str]):
            try:
                while not producer_done.is_set():
                    for msg in outbox.flush_pending_messages(channel_filter=cid):
                        bucket.append(msg["content"])
                # Final drain after producer finishes
                for _ in range(3):
                    for msg in outbox.flush_pending_messages(channel_filter=cid):
                        bucket.append(msg["content"])
            except Exception as exc:
                errors.append(exc)

        t_prod = threading.Thread(target=producer)
        t_forge = threading.Thread(target=consumer, args=(forge_cid, seen_forge))
        t_zero = threading.Thread(target=consumer, args=(zero_cid, seen_zero))

        t_forge.start()
        t_zero.start()
        t_prod.start()
        t_prod.join(timeout=15)
        t_forge.join(timeout=15)
        t_zero.join(timeout=15)

        self.assertEqual(errors, [])
        # Drain any stragglers after threads exit
        for msg in outbox.flush_pending_messages(channel_filter=forge_cid):
            seen_forge.append(msg["content"])
        for msg in outbox.flush_pending_messages(channel_filter=zero_cid):
            seen_zero.append(msg["content"])

        expected_forge = {f"forge-concurrent-msg-{i}" for i in range(total_per_channel)}
        expected_zero = {f"zero-concurrent-msg-{i}" for i in range(total_per_channel)}
        self.assertEqual(len(seen_forge), total_per_channel)
        self.assertEqual(len(seen_zero), total_per_channel)
        self.assertEqual(set(seen_forge), expected_forge)
        self.assertEqual(set(seen_zero), expected_zero)


class TestOutboxCanonicalSafetyGate(unittest.TestCase):
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

    def test_queue_outbox_message_suppresses_silence_sentinels_and_cli_leaks(self):
        for payload, expected_status in (
            ("[NO_REPLY]", "silenced"),
            ("NO_OP", "silenced"),
            ("reply: none", "silenced"),
            ("[REACT: 👍]", "silenced"),
            ("Tool is running as a background task with task id: task-42", "blocked_leak"),
            ("Waiting for task to complete.", "blocked_leak"),
        ):
            res = outbox.queue_outbox_message("lounge", payload, force=True)
            self.assertTrue(res.get("suppressed"), f"Expected {payload!r} to be suppressed")
            self.assertEqual(res.get("status"), expected_status)
        self.assertFalse(outbox.PENDING_FILE.exists())

    def test_queue_outbox_message_strips_react_and_trailing_no_reply_from_substantive_text(self):
        res = outbox.queue_outbox_message("lounge", "[REACT: 🚀]\nBuild succeeded cleanly.\n[NO_REPLY]")
        self.assertFalse(res.get("suppressed"))
        self.assertEqual(res["content"], "Build succeeded cleanly.")

    @patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test_bot_token_123"})
    def test_dispatch_via_rest_blocks_leaks_and_silence_sentinels(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            ok1 = outbox.dispatch_via_rest({
                "id": "outbox-leak-1",
                "channel": "lounge",
                "channel_id": 1534452820995080192,
                "content": "Tool is running as a background task with task id: task-99",
            })
            ok2 = outbox.dispatch_via_rest({
                "id": "outbox-silence-1",
                "channel": "zero-chat",
                "channel_id": 1542081375287640084,
                "content": "[NO_REPLY]",
            })
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
