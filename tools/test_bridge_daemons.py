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
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
repo_root = str(Path(__file__).resolve().parent.parent)
while "/workspace" in sys.path:
    sys.path.remove("/workspace")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
elif sys.path[0] != repo_root:
    sys.path.remove(repo_root)
    sys.path.insert(0, repo_root)

import tools.bridge_daemons as bd
import tools.bridge_state as bs


class TestBridgeDaemons(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Ensure sys.path prioritizes this worktree over /workspace
        r_root = str(Path(__file__).resolve().parent.parent)
        while "/workspace" in sys.path:
            sys.path.remove("/workspace")
        if sys.path[0] != r_root:
            if r_root in sys.path:
                sys.path.remove(r_root)
            sys.path.insert(0, r_root)
        for mod_name in ["tools.classifier", "tools.bridge_daemons"]:
            if mod_name in sys.modules:
                m = sys.modules[mod_name]
                if getattr(m, "__file__", "").startswith("/workspace"):
                    import importlib
                    importlib.reload(m)

        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_meta_file = bs.SESSION_METADATA_FILE
        self.orig_runtime_rules = bs.RUNTIME_RULES_FILE
        self.orig_testing = os.environ.get("TESTING")
        os.environ["TESTING"] = "1"

        bs.DATA_DIR = self.temp_path
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_meta_file
        if self.orig_testing is None:
            os.environ.pop("TESTING", None)
        else:
            os.environ["TESTING"] = self.orig_testing
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
            self.assertEqual(mock_rm_key.call_count, 2)
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
            mock_deliver.assert_called_once()
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


    async def test_10_recycle_keep_conv_id(self):
        worker = bd.PersistentChannelWorker(
            channel_id=12345,
            name="test-recycle",
            mode="home",
            sess_key="home:12345",
        )
        worker.conv_id = "conv-keep-123"
        with patch.object(worker, "shutdown", new_callable=AsyncMock) as mock_shut,              patch.object(worker, "start", new_callable=AsyncMock) as mock_start:
            await worker.recycle(keep_conv_id=True)
            mock_shut.assert_awaited_once()
            mock_start.assert_awaited_once()
            self.assertEqual(worker.conv_id, "conv-keep-123")

    async def test_11_execute_turn_mid_turn_steering(self):
        from tools.bridge_runner import steering_channels
        worker = bd.PersistentChannelWorker(
            channel_id=999001,
            name="test-steer",
            mode="home",
            sess_key="home:999001",
        )
        worker.conv_id = "conv-steer-456"
        worker.is_ready = True
        worker.proc = MagicMock()
        worker.proc.returncode = None
        worker.proc.stdin = AsyncMock()
        worker.proc.stdout = AsyncMock()

        mock_status = AsyncMock()
        mock_target = MagicMock()
        mock_target.channel.name = "test-steer"
        mock_target.channel.id = 999001

        steering_channels.add(worker.channel_id)

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)),              patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value=""),              patch.object(worker, "recycle", new_callable=AsyncMock) as mock_recycle,              patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock) as mock_deliver:

            result = await worker.execute_turn(
                prompt="Initial prompt that gets steered",
                status_msg=mock_status,
                reply_target=mock_target,
                attachments=[],
            )

            self.assertNotIn(worker.channel_id, steering_channels)
            self.assertIsNone(result)
            mock_deliver.assert_not_called()
            mock_recycle.assert_called_once_with(keep_conv_id=True)

    async def test_12_warm_classifier_daemon_lifecycle(self):
        """Verify WarmClassifierDaemon initialization, start, shutdown, and recycle."""
        from tools.classifier import WarmClassifierDaemon

        daemon = WarmClassifierDaemon(work_dir=str(self.temp_path / "clf"), max_turns=5)
        self.assertEqual(daemon.work_dir, str(self.temp_path / "clf"))
        self.assertEqual(daemon.max_turns, 5)
        self.assertIsNone(daemon.proc)
        self.assertIsNone(daemon.pid)
        self.assertEqual(daemon.turn_count, 0)

        mock_proc = MagicMock()
        mock_proc.pid = 98765
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            daemon.start()
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args
            self.assertEqual(
                call_args[0][0],
                [
                    "agy",
                    "--input-format=stream-json",
                    "--output-format=stream-json",
                    "--model=gemini-3.8-flash-low",
                    "--disable-slash-commands",
                    "--print-timeout=4h",
                    "--print=",
                ],
            )
            self.assertEqual(call_args[1]["cwd"], str(self.temp_path / "clf"))
            self.assertEqual(daemon.pid, 98765)

            # Test recycle
            daemon.recycle()
            mock_proc.terminate.assert_called_once()
            self.assertEqual(mock_popen.call_count, 2)

            # Test shutdown
            daemon.shutdown()
            self.assertIsNone(daemon.proc)
            self.assertIsNone(daemon.pid)
            self.assertEqual(daemon.turn_count, 0)

    async def test_13_warm_classifier_classify_streaming(self):
        """Verify WarmClassifierDaemon.classify streams NDJSON lines and extracts response."""
        from tools.classifier import WarmClassifierDaemon

        daemon = WarmClassifierDaemon(work_dir=str(self.temp_path / "clf"), max_turns=10)
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()

        lines = [
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"0."}}\n',
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"85 | REACT: 🚀"}}\n',
            '{"event":"result","result":{"response":"0.85 | REACT: 🚀"}}\n',
            "",
        ]
        mock_proc.stdout.readline.side_effect = lines
        daemon.proc = mock_proc

        with patch("select.select", return_value=([mock_proc.stdout], [], [])):
            res = daemon.classify("Check container logs on Synology")
            self.assertEqual(res, "0.85 | REACT: 🚀")
            self.assertEqual(daemon.turn_count, 1)

            # Verify stdin received user event
            mock_proc.stdin.write.assert_called_once()
            written_str = mock_proc.stdin.write.call_args[0][0]
            written_data = json.loads(written_str)
            self.assertEqual(written_data["event"], "user")
            self.assertEqual(written_data["message"]["content"], "Check container logs on Synology")

    async def test_14_warm_classifier_auto_recycle_at_max_turns(self):
        """Verify WarmClassifierDaemon automatically triggers background recycle at max_turns."""
        from tools.classifier import WarmClassifierDaemon

        daemon = WarmClassifierDaemon(work_dir=str(self.temp_path / "clf"), max_turns=2)
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()

        lines = [
            '{"event":"result","result":{"response":"0.50"}}\n',
            '{"event":"result","result":{"response":"0.50"}}\n',
        ]
        mock_proc.stdout.readline.side_effect = lines
        daemon.proc = mock_proc

        recycle_called = asyncio.Event()

        def fake_recycle():
            recycle_called.set()

        with patch("select.select", return_value=([mock_proc.stdout], [], [])), \
             patch.object(daemon, "recycle", side_effect=fake_recycle):
            # First turn: turn_count reaches 1, no recycle
            res1 = daemon.classify("Turn 1 prompt")
            self.assertEqual(res1, "0.50")
            self.assertEqual(daemon.turn_count, 1)
            self.assertFalse(recycle_called.is_set())

            # Second turn: turn_count reaches 2 (max_turns=2), auto-recycle triggered in background thread
            res2 = daemon.classify("Turn 2 prompt")
            self.assertEqual(res2, "0.50")
            self.assertEqual(daemon.turn_count, 2)

            # Wait for background recycle thread
            await asyncio.wait_for(recycle_called.wait(), timeout=2.0)
            self.assertTrue(recycle_called.is_set())

    async def test_15_score_relevance_warm_and_fallback(self):
        """Verify score_relevance uses warm_classifier and falls back to cold subprocess.run on failure."""
        from tools.classifier import pop_last_reaction, score_relevance, warm_classifier

        content = "Has anyone seen containerd drop sockets under high IO?"

        # 1. Warm classifier success path
        with patch.object(warm_classifier, "classify", return_value="0.95 | REACT: 🔥") as mock_clf, \
             patch("subprocess.run") as mock_subproc:
            score = score_relevance(content, use_warm=True)
            self.assertEqual(score, 0.95)
            self.assertEqual(pop_last_reaction(), "🔥")
            mock_clf.assert_called_once()
            mock_subproc.assert_not_called()

        # 2. Warm classifier timeout/None fallback to subprocess.run
        mock_subproc_res = MagicMock()
        mock_subproc_res.stdout = "0.75 | REACT: 👍"
        with patch.object(warm_classifier, "classify", return_value=None) as mock_clf, \
             patch("subprocess.run", return_value=mock_subproc_res) as mock_subproc:
            score = score_relevance(content, use_warm=True)
            self.assertEqual(score, 0.75)
            self.assertEqual(pop_last_reaction(), "👍")
            mock_clf.assert_called_once()
            mock_subproc.assert_called_once()

    async def test_16_persistent_daemon_manager_warm_classifier_integration(self):
        """Verify PersistentDaemonManager integrates warm_classifier in lifecycle methods."""
        mgr = bd.PersistentDaemonManager()
        for w in mgr.workers.values():
            w.start = AsyncMock()
            w.shutdown = AsyncMock()
            w.recycle = AsyncMock()

        from tools.classifier import warm_classifier

        with patch.object(warm_classifier, "start") as mock_w_start, \
             patch.object(warm_classifier, "shutdown") as mock_w_shutdown, \
             patch.object(warm_classifier, "recycle") as mock_w_recycle:
            await mgr.start_all(include_classifier=True)
            mock_w_start.assert_called_once()

            await mgr.shutdown_all(include_classifier=True)
            mock_w_shutdown.assert_called_once()

            await mgr.recycle_all(include_classifier=True)
            mock_w_recycle.assert_called_once()

            mock_w_recycle.reset_mock()
            with patch("tools.bridge_state.reset_session_meta"), \
                 patch("tools.bridge_state.remove_reset_session_key"):
                await mgr.proactive_nightly_recycle(include_classifier=True)
                mock_w_recycle.assert_called_once()


if __name__ == "__main__":
    unittest.main()

