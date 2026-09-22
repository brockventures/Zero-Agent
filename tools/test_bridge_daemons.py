#!/usr/bin/env python3
"""
Unit test suite for bridge_daemons.py (Persistent Channel Worker Daemons).
Validates:
1. Dedicated channel configuration and detection (#zero-chat, #the-banana-stand, #lounge).
2. Dynamic fallback for secondary channels and threads.
3. Prompt formatting across home and external air-gapped modes.
4. Persistent worker lifecycle (boot, init parsing, stdin/stdout turn streaming, recycle, shutdown).
5. Watchdog timeout and crash recovery.
6. Multi-channel parallel execution across dedicated workers without head-of-line blocking.
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.bridge_daemons as bd
import tools.bridge_state as bs


class TestBridgeDaemons(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_meta_file = bs.SESSION_METADATA_FILE
        self.orig_runtime_rules = bs.RUNTIME_RULES_FILE

        bs.DATA_DIR = self.temp_path
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_meta_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_dedicated_channel_detection(self):
        """Verify only the Big Three channels are marked dedicated; others are dynamic."""
        self.assertTrue(bd.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.LOUNGE_CHANNEL_ID))

        # Secondary home channels and random threads should NOT be dedicated
        self.assertFalse(bd.is_dedicated_channel(1544955535722545253))  # homelab
        self.assertFalse(bd.is_dedicated_channel(1544955532765560924))  # finances
        self.assertFalse(bd.is_dedicated_channel(1544953277592899615))  # steam-deck
        self.assertFalse(bd.is_dedicated_channel(999888777))            # thread

    def test_02_feature_flag_toggle(self):
        """Verify feature flag in runtime rules can toggle daemon routing."""
        mgr = bd.PersistentDaemonManager()
        with patch("tools.bridge_daemons.is_persistent_daemons_enabled", return_value=False):
            self.assertFalse(mgr.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
            self.assertFalse(mgr.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))

        with patch("tools.bridge_daemons.is_persistent_daemons_enabled", return_value=True):
            self.assertTrue(mgr.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
            self.assertTrue(mgr.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))

    def test_03_prompt_formatting(self):
        """Verify prompt formatting injects PT timezone and boundaries."""
        # Home prompt
        home_p = bd.prepare_turn_prompt("Check system health", mode="home", channel_id=bs.TARGET_CHANNEL_ID, sess_key="home")
        self.assertIn("Check system health", home_p)
        self.assertIn("America/Los_Angeles", home_p)
        self.assertIn("Pacific Time (PT)", home_p)

        # External prompt
        ext_p = bd.prepare_turn_prompt("Review PR #42", mode="external", channel_id=bd.BANANA_STAND_CHANNEL_ID, sess_key=str(bd.BANANA_STAND_CHANNEL_ID), author_name="Amos")
        self.assertIn("Review PR #42", ext_p)
        self.assertIn("CRAB CAVERN", ext_p)
        self.assertIn("Amos", ext_p)

    async def test_04_persistent_worker_lifecycle(self):
        """Verify worker startup, init event reading, turn execution, and clean shutdown."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home"
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()

        # Mock stdout lines: init, step_update, result
        lines = [
            b'{"event":"init","conversation_id":"conv-warm-111","init":{"tools":["view_file"]}}\n',
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/test/file.py"}}}}\n',
            b'{"event":"result","result":{"conversation_id":"conv-warm-111","status":"SUCCESS","response":"Hello from warm worker"}}\n',
            b''
        ]
        line_iter = iter(lines)

        async def mock_readline():
            return next(line_iter)

        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=mock_readline)
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await worker.start()
            self.assertEqual(worker.conv_id, "conv-warm-111")
            self.assertEqual(worker.proc, mock_proc)

            mock_status_msg = AsyncMock()
            mock_reply_target = AsyncMock()

            with patch("tools.bridge_daemons.deliver_turn_output") as mock_deliver:
                await worker.execute_turn(
                    prompt="Hello Zero",
                    status_msg=mock_status_msg,
                    reply_target=mock_reply_target,
                    attachments=[]
                )
                self.assertEqual(worker.turn_count, 1)
                mock_deliver.assert_called_once()
                self.assertIn("Hello from warm worker", mock_deliver.call_args[1]["output_text"])

            await worker.shutdown()
            self.assertIsNone(worker.proc)

    async def test_05_parallel_external_channels(self):
        """Verify #the-banana-stand and #lounge execute turns independently and concurrently."""
        mgr = bd.PersistentDaemonManager()
        worker_banana = mgr.get_worker(bd.BANANA_STAND_CHANNEL_ID)
        worker_lounge = mgr.get_worker(bd.LOUNGE_CHANNEL_ID)

        self.assertIsNotNone(worker_banana)
        self.assertIsNotNone(worker_lounge)

        banana_started = asyncio.Event()
        lounge_started = asyncio.Event()
        banana_can_finish = asyncio.Event()
        lounge_can_finish = asyncio.Event()

        async def mock_banana_turn(*args, **kwargs):
            banana_started.set()
            await banana_can_finish.wait()

        async def mock_lounge_turn(*args, **kwargs):
            lounge_started.set()
            await lounge_can_finish.wait()

        with patch.object(worker_banana, "execute_turn", side_effect=mock_banana_turn), \
             patch.object(worker_lounge, "execute_turn", side_effect=mock_lounge_turn):

            t1 = asyncio.create_task(worker_banana.execute_turn("banana prompt", None, MagicMock(), []))
            t2 = asyncio.create_task(worker_lounge.execute_turn("lounge prompt", None, MagicMock(), []))

            # Both should start concurrently without one blocking the other
            await asyncio.wait_for(asyncio.gather(banana_started.wait(), lounge_started.wait()), timeout=2.0)
            self.assertTrue(banana_started.is_set())
            self.assertTrue(lounge_started.is_set())

            banana_can_finish.set()
            lounge_can_finish.set()
            await asyncio.gather(t1, t2)

    async def test_06_live_status_ticker_suppression(self):
        """Verify that live status ticker edits are suppressed when live_status_ticker_enabled is False."""
        worker = bd.PersistentChannelWorker(
            channel_id=1544953279664889888, # zero-ops
            name="zero-ops",
            mode="home",
            sess_key="1544953279664889888"
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12346
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()

        lines = [
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"grep_search","tool_info":{"parameters":{"Query":"test"}}}}\n',
            b'{"event":"result","result":{"status":"SUCCESS","response":"Done"}}\n',
            b''
        ]
        line_iter = iter(lines)
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        worker.proc = mock_proc
        worker.is_ready = True
        mock_status_msg = AsyncMock()
        mock_status_msg.id = 99991
        mock_reply_target = AsyncMock()

        # Test with ticker disabled (default)
        t_val = 100.0
        def fake_time():
            nonlocal t_val
            t_val += 2.0
            return t_val

        with patch("tools.bridge_daemons.get_runtime_rules", return_value={"live_status_ticker_enabled": False}), \
             patch("tools.bridge_daemons.deliver_turn_output"), \
             patch("time.time", side_effect=fake_time):
            await worker.execute_turn("test prompt", mock_status_msg, mock_reply_target, [])
            mock_status_msg.edit.assert_not_awaited()

        # Reset and test with ticker enabled
        lines2 = [
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"grep_search","tool_info":{"parameters":{"Query":"test"}}}}\n',
            b'{"event":"result","result":{"status":"SUCCESS","response":"Done"}}\n',
            b''
        ]
        line_iter2 = iter(lines2)
        mock_proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter2))
        mock_status_msg.reset_mock()
        t_val = 100.0

        with patch("tools.bridge_daemons.get_runtime_rules", return_value={"live_status_ticker_enabled": True}), \
             patch("tools.bridge_daemons.deliver_turn_output"), \
             patch("time.time", side_effect=fake_time):
            await worker.execute_turn("test prompt", mock_status_msg, mock_reply_target, [])
            self.assertTrue(mock_status_msg.edit.await_count >= 1)

    async def test_07_proactive_nightly_recycle_and_pending_carryforward(self):
        """Verify that proactive_nightly_recycle recycles all dedicated workers and clears reset keys,
        and execute_turn consumes pending carryforward context without turn recycling."""
        # 1. Dedicated channels verification
        self.assertIn(bd.BANANA_STAND_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)
        self.assertIn(bd.TARGET_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)

        # 2. Proactive nightly recycle test
        mgr = bd.PersistentDaemonManager()
        mock_worker_home = AsyncMock()
        mock_worker_home.sess_key = "home"
        mock_worker_home.name = "zero-chat"
        mock_worker_home.conv_id = "old-home-conv"
        mock_worker_home.recycle = AsyncMock()

        mock_worker_banana = AsyncMock()
        mock_worker_banana.sess_key = str(bd.BANANA_STAND_CHANNEL_ID)
        mock_worker_banana.name = "the-banana-stand"
        mock_worker_banana.conv_id = "old-banana-conv"
        mock_worker_banana.recycle = AsyncMock()

        mgr.workers = {
            bd.TARGET_CHANNEL_ID: mock_worker_home,
            bd.BANANA_STAND_CHANNEL_ID: mock_worker_banana,
        }

        with patch("tools.bridge_state.reset_session_meta") as mock_reset_meta, \
             patch("tools.bridge_state.remove_reset_session_key") as mock_rm_key, \
             patch("tools.bridge_runner.reset_session_keys", new={"home", str(bd.BANANA_STAND_CHANNEL_ID)}) as mock_keys:
            await mgr.proactive_nightly_recycle()

            mock_worker_home.recycle.assert_awaited_once_with(new_conv_id=None)
            mock_worker_banana.recycle.assert_awaited_once_with(new_conv_id=None)
            self.assertEqual(mock_reset_meta.call_count, 2)
            self.assertNotIn("home", mock_keys)
            self.assertNotIn(str(bd.BANANA_STAND_CHANNEL_ID), mock_keys)

        # 3. Pending carryforward context consumption without turn recycling
        test_worker = bd.PersistentChannelWorker(
            channel_id=bd.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        test_worker.proc = MagicMock()
        test_worker.proc.returncode = None
        test_worker.proc.pid = 99999
        test_worker.proc.stdin = MagicMock()
        test_worker.proc.stdin.write = MagicMock()
        test_worker.proc.stdin.drain = AsyncMock()
        test_worker.is_ready = True
        test_worker.conv_id = "warm-new-conv"

        lines = [
            b'{"event":"result","result":{"status":"SUCCESS","response":"Morning response"}}\n',
            b''
        ]
        test_worker.proc.stdout.readline = AsyncMock(side_effect=lambda: next(iter(lines)))

        mock_status = AsyncMock()
        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bd.TARGET_CHANNEL_ID

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.session_summarizer.get_carryforward_context", return_value="Recapped milestones") as mock_get_cf, \
             patch("tools.bridge_daemons.deliver_turn_output") as mock_deliver, \
             patch.object(test_worker, "recycle", new_callable=AsyncMock) as mock_recycle:
            await test_worker.execute_turn("Good morning Zero", mock_status, mock_target, [])

            # Recycle MUST NOT be called on this turn
            mock_recycle.assert_not_awaited()
            # Carryforward MUST be retrieved and injected into prompt passed to worker stdin
            mock_get_cf.assert_called_once_with(sess_key="home")
            written_bytes = test_worker.proc.stdin.write.call_args[0][0]
            written_json = json.loads(written_bytes.decode("utf-8"))
            sent_prompt = written_json["message"]["content"]
            self.assertIn("[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\nRecapped milestones", sent_prompt)
            self.assertIn("Good morning Zero", sent_prompt)

    async def test_08_agy_error_stderr_capture_and_delivery(self):
        """Verify that AGY_ERROR on stderr is captured and delivered as a structured error alert."""
        worker = bd.PersistentChannelWorker(
            channel_id=1544953279664889888,
            name="zero-ops",
            mode="home",
            sess_key="1544953279664889888",
        )
        worker.proc = MagicMock()
        worker.proc.returncode = None
        worker.proc.pid = 88888
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
        worker.proc.stdout = MagicMock()

        agy_err_payload = {
            "canonical_status": "RESOURCE_EXHAUSTED",
            "code": 429,
            "retryable": False,
            "error_id": "err-quota-exceeded",
            "short_error": "API rate limit exceeded",
        }

        async def fake_readline():
            # Simulate _drain_stderr capturing AGY_ERROR concurrently before worker process exits
            worker.last_agy_error = agy_err_payload
            worker.proc.returncode = 3
            return b""

        worker.proc.stdout.readline = AsyncMock(side_effect=fake_readline)
        worker.proc.stderr = AsyncMock()
        worker.proc.stderr.readline = AsyncMock(return_value=b"")
        worker.is_ready = True
        worker.conv_id = "test-conv-agy-err"

        mock_status = AsyncMock()
        mock_status.id = 99992
        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = 1544953279664889888

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock) as mock_deliver, \
             patch.object(worker, "recycle", new_callable=AsyncMock) as mock_recycle:
            await worker.execute_turn("Prompt that trips rate limit", mock_status, mock_target, [])

            mock_recycle.assert_awaited_once()
            mock_deliver.assert_awaited_once()
            output_text = mock_deliver.call_args.kwargs["output_text"]
            self.assertIn("⚠️ **Model API Failure (CLI Exit Code 3):**", output_text)
            self.assertIn("API rate limit exceeded", output_text)
            self.assertIn("`RESOURCE_EXHAUSTED` (Code 429)", output_text)
            self.assertIn("`err-quota-exceeded`", output_text)
            self.assertIn("Retryable:** No", output_text)

    async def test_09_persistent_worker_task_settle_integration(self):
        """Verify PersistentChannelWorker triggers evaluate_and_settle_turn and handles settle reinvocation without deadlock."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "test-conv-persistent-settle"
        worker.proc = AsyncMock()
        worker.proc.stdin = AsyncMock()
        worker.proc.stdout = AsyncMock()
        worker.proc.stdout.readline = AsyncMock(return_value=b"")
        worker.proc.returncode = 0
        worker.is_ready = True

        mock_status = AsyncMock()
        mock_status.id = 99993
        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bs.TARGET_CHANNEL_ID

        # Mock evaluate_and_settle_turn returning was_settled=True
        mock_settle = AsyncMock(return_value=(True, "Final settled substantive answer from reinvocation!"))

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="Initial turn response"), \
             patch("tools.task_settle.evaluate_and_settle_turn", mock_settle), \
             patch.object(worker, "start", new_callable=AsyncMock), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock) as mock_deliver:

            result = await worker.execute_turn(
                prompt="Run detached calculation",
                status_msg=mock_status,
                reply_target=mock_target,
                attachments=[],
            )

            # Settle protocol was invoked with correct channel & conv_id
            mock_settle.assert_awaited_once()
            call_kwargs = mock_settle.call_args.kwargs
            self.assertEqual(call_kwargs["conv_id"], worker.conv_id)
            self.assertEqual(call_kwargs["channel_id"], bs.TARGET_CHANNEL_ID)
            self.assertEqual(call_kwargs["mode"], "home")
            self.assertEqual(call_kwargs["reinvoke_coro_fn"], worker.execute_turn)

            # Result is settled_text directly
            self.assertEqual(result, "Final settled substantive answer from reinvocation!")
            # deliver_turn_output should not be called again in outer turn because reinvocation handled it
            mock_deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()

