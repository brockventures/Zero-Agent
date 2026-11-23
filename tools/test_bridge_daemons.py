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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        self._ambient_patcher = patch("tools.bridge_pipeline.get_ambient_memory_context", return_value="")
        self._ambient_patcher.start()

    def tearDown(self):
        self._ambient_patcher.stop()
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_meta_file
        if self.orig_testing is None:
            os.environ.pop("TESTING", None)
        else:
            os.environ["TESTING"] = self.orig_testing
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_dedicated_channel_detection(self):
        """Verify dedicated channels (#zero-chat, #zero-ops, #lounge) and BANANA_STAND_CHANNEL_ID exclusion."""
        self.assertTrue(bd.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.ZERO_OPS_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.LOUNGE_CHANNEL_ID))
        self.assertFalse(bd.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))
        self.assertNotIn(bd.BANANA_STAND_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)
        self.assertEqual(bd.BANANA_STAND_CHANNEL_ID, 1534436119888793750)
        self.assertTrue(bd.DEDICATED_CHANNEL_CONFIGS[bs.TARGET_CHANNEL_ID]["always_warm"])
        self.assertEqual(
            bd.DEDICATED_CHANNEL_CONFIGS[bd.ZERO_OPS_CHANNEL_ID]["always_warm"],
            not bs.is_vulcan_agent(),
        )
        self.assertEqual(
            bd.DEDICATED_CHANNEL_CONFIGS[bd.LOUNGE_CHANNEL_ID]["always_warm"],
            not bs.is_vulcan_agent(),
        )

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
            self.assertFalse(mgr.is_dedicated_channel(bd.LOUNGE_CHANNEL_ID))

        with patch("tools.bridge_daemons.is_persistent_daemons_enabled", return_value=True):
            self.assertTrue(mgr.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
            self.assertTrue(mgr.is_dedicated_channel(bd.LOUNGE_CHANNEL_ID))
            self.assertFalse(mgr.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))

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
        """Verify #zero-ops and #lounge execute turns independently and concurrently while #the-banana-stand has no warm daemon."""
        mgr = bd.PersistentDaemonManager()
        self.assertIsNone(mgr.get_worker(bd.BANANA_STAND_CHANNEL_ID))
        worker_ops = mgr.get_worker(bd.ZERO_OPS_CHANNEL_ID)
        worker_lounge = mgr.get_worker(bd.LOUNGE_CHANNEL_ID)

        self.assertIsNotNone(worker_ops)
        self.assertIsNotNone(worker_lounge)

        ops_started = asyncio.Event()
        lounge_started = asyncio.Event()
        ops_can_finish = asyncio.Event()
        lounge_can_finish = asyncio.Event()

        async def mock_ops_turn(*args, **kwargs):
            ops_started.set()
            await ops_can_finish.wait()

        async def mock_lounge_turn(*args, **kwargs):
            lounge_started.set()
            await lounge_can_finish.wait()

        with patch.object(worker_ops, "execute_turn", side_effect=mock_ops_turn), \
             patch.object(worker_lounge, "execute_turn", side_effect=mock_lounge_turn):

            t1 = asyncio.create_task(worker_ops.execute_turn("ops prompt", None, MagicMock(), []))
            t2 = asyncio.create_task(worker_lounge.execute_turn("lounge prompt", None, MagicMock(), []))

            # Both should start concurrently without one blocking the other
            await asyncio.wait_for(asyncio.gather(ops_started.wait(), lounge_started.wait()), timeout=2.0)
            self.assertTrue(ops_started.is_set())
            self.assertTrue(lounge_started.is_set())

            ops_can_finish.set()
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
        """Verify that proactive_nightly_recycle recycles always_warm/running dedicated workers and clears reset keys,
        and execute_turn consumes pending carryforward context without turn recycling."""
        # 1. Dedicated channels verification
        self.assertNotIn(bd.BANANA_STAND_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)
        self.assertIn(bd.TARGET_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)
        self.assertIn(bd.LOUNGE_CHANNEL_ID, bd.DEDICATED_CHANNEL_CONFIGS)

        # 2. Proactive nightly recycle test
        mgr = bd.PersistentDaemonManager()
        mock_worker_home = AsyncMock()
        mock_worker_home.channel_id = bd.TARGET_CHANNEL_ID
        mock_worker_home.always_warm = True
        mock_worker_home.proc = MagicMock(returncode=None)
        mock_worker_home.sess_key = "home"
        mock_worker_home.name = "zero-chat"
        mock_worker_home.conv_id = "old-home-conv"
        mock_worker_home.recycle = AsyncMock()

        mock_worker_lounge = AsyncMock()
        mock_worker_lounge.channel_id = bd.LOUNGE_CHANNEL_ID
        mock_worker_lounge.always_warm = False
        mock_worker_lounge.proc = None
        mock_worker_lounge.mode = "external"
        mock_worker_lounge.sess_key = str(bd.LOUNGE_CHANNEL_ID)
        mock_worker_lounge.name = "lounge"
        mock_worker_lounge.conv_id = "old-lounge-conv"
        mock_worker_lounge.recycle = AsyncMock()
        mock_worker_lounge.shutdown = AsyncMock()

        mgr.workers = {
            bd.TARGET_CHANNEL_ID: mock_worker_home,
            bd.LOUNGE_CHANNEL_ID: mock_worker_lounge,
        }

        with patch("tools.bridge_state.reset_session_meta") as mock_reset_meta, \
             patch("tools.bridge_state.remove_reset_session_key") as mock_rm_key, \
             patch("tools.bridge_runner.reset_session_keys", new={"home", str(bd.LOUNGE_CHANNEL_ID)}) as mock_keys:
            await mgr.proactive_nightly_recycle()

            mock_worker_home.recycle.assert_awaited_once_with(new_conv_id=None)
            mock_worker_lounge.recycle.assert_not_awaited()
            mock_worker_lounge.shutdown.assert_awaited_once()
            self.assertEqual(mock_reset_meta.call_count, 2)
            self.assertEqual(mock_rm_key.call_count, 2)
            self.assertNotIn("home", mock_keys)
            self.assertNotIn(str(bd.LOUNGE_CHANNEL_ID), mock_keys)

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
        """Verify post-turn evaluate_and_settle_turn is retired while preserving is_settle_reinvocation compatibility."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "test-conv-persistent-settle"
        worker.proc = MagicMock()
        worker.proc.pid = 77777
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
        worker.proc.stdout = AsyncMock()
        worker.proc.stdout.readline = AsyncMock(return_value=b"")
        worker.proc.returncode = None
        worker.is_ready = True

        mock_status = AsyncMock()
        mock_status.id = 99993
        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bs.TARGET_CHANNEL_ID

        mock_settle = AsyncMock(return_value=(True, "Should never be called"))

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

            # Post-turn evaluate_and_settle_turn loop is retired
            mock_settle.assert_not_awaited()
            self.assertEqual(result, "Initial turn response")
            mock_deliver.assert_awaited_once()
            self.assertEqual(mock_deliver.call_args.kwargs["output_text"], "Initial turn response")

            # Backwards compatibility: is_settle_reinvocation=True bypasses worker.lock and reconciles pre_settle_text
            mock_deliver.reset_mock()
            async with worker.lock:
                reinv_res = await worker.execute_turn(
                    prompt="Follow-up settle check",
                    status_msg=mock_status,
                    reply_target=mock_target,
                    attachments=[],
                    is_settle_reinvocation=True,
                    pre_settle_text="Initial turn response",
                )
            self.assertEqual(reinv_res, "Initial turn response")
            mock_deliver.assert_awaited_once()


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
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
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

    async def test_17_idle_worker_reaper_and_safety_guards(self):
        """Verify 20m child-safe idle worker reaper behavior across primary and secondary workers."""
        mgr = bd.PersistentDaemonManager()
        w_chat = mgr.get_worker(bs.TARGET_CHANNEL_ID)
        w_ops = mgr.get_worker(bd.ZERO_OPS_CHANNEL_ID)
        w_lounge = mgr.get_worker(bd.LOUNGE_CHANNEL_ID)

        self.assertTrue(w_chat.always_warm)
        self.assertEqual(w_ops.always_warm, not bs.is_vulcan_agent())
        self.assertEqual(w_lounge.always_warm, not bs.is_vulcan_agent())

        # Simulate all three workers running and idle for >1200s
        for idx, w in enumerate((w_chat, w_ops, w_lounge), start=1):
            mproc = MagicMock()
            mproc.pid = 70000 + idx
            mproc.returncode = None
            mproc.wait = AsyncMock(return_value=0)
            w.proc = mproc
            w.is_ready = True
            w.conv_id = f"conv-{w.name}-123"
            w.started_at = 1000.0
            w.last_turn_at = 1000.0

        # (b) always_warm channels (#zero-chat, #zero-ops, #lounge on Zero) are never reaped when idle
        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]):
            self.assertFalse(w_chat.is_idle_reapable(now=5000.0, idle_timeout=1200.0))
            self.assertFalse(await w_chat.reap_if_idle(now=5000.0, idle_timeout=1200.0))
            self.assertIsNotNone(w_chat.proc)
            if not bs.is_vulcan_agent():
                self.assertFalse(w_ops.is_idle_reapable(now=5000.0, idle_timeout=1200.0))
                self.assertFalse(w_lounge.is_idle_reapable(now=5000.0, idle_timeout=1200.0))

        # Disable always_warm on secondary workers to exercise the idle reaper and safety guards
        w_ops.always_warm = False
        w_lounge.always_warm = False

        # Before timeout (e.g. 1199s elapsed), secondary worker is NOT reapable
        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]):
            self.assertFalse(w_ops.is_idle_reapable(now=2199.0, idle_timeout=1200.0))

        # (d) Safety guards block idle reaping even past 1200s:
        # 1. Active child processes
        with patch("tools.bridge_daemons.get_process_children", return_value=[88801]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]):
            self.assertFalse(w_ops.is_idle_reapable(now=3000.0, idle_timeout=1200.0))

        # 2. Pending background tasks
        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=["task-bg-1"]):
            self.assertFalse(w_ops.is_idle_reapable(now=3000.0, idle_timeout=1200.0))

        # 3. Active turn in channel_active_procs
        bd.channel_active_procs[bd.ZERO_OPS_CHANNEL_ID] = w_ops.proc
        try:
            with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
                 patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]):
                self.assertFalse(w_ops.is_idle_reapable(now=3000.0, idle_timeout=1200.0))
        finally:
            bd.channel_active_procs.pop(bd.ZERO_OPS_CHANNEL_ID, None)

        # 4. Locked worker (turn lock or start_lock held)
        async with w_ops.lock:
            with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
                 patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]):
                self.assertFalse(w_ops.is_idle_reapable(now=3000.0, idle_timeout=1200.0))

        # (c) Secondary workers (#zero-ops, #lounge) are reaped after 1200s of inactivity while preserving conv_id
        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_daemons.record_daemon_pids") as mock_rec_pids, \
             patch("time.time", return_value=3000.0):
            reaped = await mgr.reap_idle_workers(idle_timeout=1200.0)
            self.assertCountEqual(reaped, ["zero-ops", "lounge"])
            self.assertIsNotNone(w_chat.proc)
            self.assertIsNone(w_ops.proc)
            self.assertFalse(w_ops.is_ready)
            self.assertEqual(w_ops.conv_id, "conv-zero-ops-123")
            self.assertIsNone(w_lounge.proc)
            self.assertFalse(w_lounge.is_ready)
            self.assertEqual(w_lounge.conv_id, "conv-lounge-123")
            mock_rec_pids.assert_called()

        # Respawning cleanly on the next turn with preserved conv_id
        respawn_proc = MagicMock()
        respawn_proc.pid = 79999
        respawn_proc.returncode = None
        respawn_proc.stdin = MagicMock()
        respawn_proc.stdin.write = MagicMock()
        respawn_proc.stdin.drain = AsyncMock()
        init_and_result = iter([
            b'{"event":"init","conversation_id":"conv-zero-ops-123","init":{}}\n',
            b'{"event":"result","result":{"conversation_id":"conv-zero-ops-123","status":"SUCCESS","response":"Respawned cleanly"}}\n',
            b'',
        ])
        respawn_proc.stdout = AsyncMock()
        respawn_proc.stdout.readline = AsyncMock(side_effect=lambda: next(init_and_result))
        respawn_proc.stderr = AsyncMock()
        respawn_proc.stderr.readline = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", return_value=respawn_proc) as mock_exec, \
             patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock) as mock_deliver:
            await w_ops.execute_turn("Next turn after idle reap", AsyncMock(), AsyncMock(), [])
            mock_exec.assert_called_once()
            spawn_cmd = mock_exec.call_args[0]
            self.assertIn("--conversation=conv-zero-ops-123", spawn_cmd)
            self.assertTrue(w_ops.is_ready)
            self.assertEqual(w_ops.conv_id, "conv-zero-ops-123")
            mock_deliver.assert_awaited_once()
            await w_ops.shutdown()

    async def test_18_fire_presence_non_blocking_and_error_safe(self):
        """Verify apply_presence_fn is scheduled fire-and-forget without blocking turn execution or propagating errors."""
        import tools.bridge_runner as br

        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "conv-presence-fast"
        worker.is_ready = True
        worker.proc = MagicMock()
        worker.proc.pid = 65432
        worker.proc.returncode = None
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
        worker.proc.stdout = AsyncMock()

        presence_gate = asyncio.Event()
        presence_calls = []

        async def slow_presence(*args, **kwargs):
            presence_calls.append((args, kwargs))
            await presence_gate.wait()

        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bs.TARGET_CHANNEL_ID

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="Fast reply"), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock) as mock_deliver:
            # Turn must finish immediately even while slow_presence is still blocked on presence_gate
            res = await asyncio.wait_for(
                worker.execute_turn(
                    prompt="Quick status check",
                    status_msg=None,
                    reply_target=mock_target,
                    attachments=[],
                    apply_presence_fn=slow_presence,
                ),
                timeout=2.0,
            )
            self.assertEqual(res, "Fast reply")
            mock_deliver.assert_awaited_once()
            await asyncio.sleep(0)
            self.assertEqual(len(presence_calls), 2)
            self.assertEqual(presence_calls[0][1].get("status_override"), "dnd")
            self.assertEqual(presence_calls[1], ((), {}))

        presence_gate.set()
        await asyncio.sleep(0.01)

        # Verify both sync and async exceptions inside apply_presence_fn are swallowed quietly
        async def failing_async_presence(*args, **kwargs):
            raise RuntimeError("Discord gateway websocket closed")

        def failing_sync_presence(*args, **kwargs):
            raise RuntimeError("Sync presence error")

        bd._fire_presence(failing_async_presence, status_override="dnd")
        bd._fire_presence(failing_sync_presence)
        br._fire_presence(failing_async_presence, status_override="dnd")
        br._fire_presence(failing_sync_presence)
        await asyncio.sleep(0.01)

    async def test_19_post_turn_compaction_and_turn1_consumption(self):
        """Verify (a) post-turn background compaction after reply & lock release + stashing pending_carryforward (home & external),
        and (b) Turn 1 consuming & clearing pending_carryforward without inline recycle."""
        # --- HOME MODE ---
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "conv-home-precompact"
        worker.is_ready = True
        worker.proc = MagicMock()
        worker.proc.pid = 41001
        worker.proc.returncode = None
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
        worker.proc.stdout = AsyncMock()

        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bs.TARGET_CHANNEL_ID

        events_order = []

        def fake_check_compaction(conv_id, current_turns, high_water_multiplier=1.0):
            if high_water_multiplier > 1.0:
                return (False, None)
            return (True, "Turn limit reached (30/30)")

        async def fake_deliver(**kwargs):
            # When reply is delivered, worker.lock is still held and recycle has NOT run yet
            self.assertTrue(worker.lock.locked())
            self.assertEqual(worker.conv_id, "conv-home-precompact")
            events_order.append("deliver")

        async def fake_recycle(new_conv_id=None, keep_conv_id=False):
            events_order.append("recycle")
            worker.conv_id = "conv-home-fresh"

        with patch("tools.bridge_daemons.check_compaction_needed", side_effect=fake_check_compaction), \
             patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="Turn 30 reply"), \
             patch("tools.bridge_daemons.deliver_turn_output", side_effect=fake_deliver), \
             patch.object(worker, "recycle", side_effect=fake_recycle) as mock_recycle, \
             patch("tools.session_summarizer.generate_summary") as mock_gen_sum, \
             patch("tools.session_summarizer.get_carryforward_context", side_effect=[None, "Stashed home milestones"]) as mock_get_cf:
            res = await worker.execute_turn("Turn 30 prompt", None, mock_target, [])
            self.assertEqual(res, "Turn 30 reply")
            self.assertIsNotNone(worker._post_turn_compaction_task)
            await worker._post_turn_compaction_task

            self.assertEqual(events_order, ["deliver", "recycle"])
            mock_recycle.assert_awaited_once_with(new_conv_id=None)
            mock_gen_sum.assert_called_once_with(conv_id="conv-home-precompact", sess_key="home")
            self.assertEqual(mock_get_cf.call_count, 2)
            self.assertEqual(worker.pending_carryforward, "Stashed home milestones")
            self.assertEqual(worker.turn_count, 0)
            self.assertEqual(worker._last_session_turns, 0)
            self.assertIsNone(worker._post_turn_compaction_task)

        # Turn 1 on fresh session consumes pending_carryforward without inline recycle or disk re-fetch
        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="Turn 1 fresh reply"), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock), \
             patch.object(worker, "recycle", new_callable=AsyncMock) as mock_turn1_recycle, \
             patch("tools.session_summarizer.get_carryforward_context") as mock_turn1_get_cf:
            await worker.execute_turn("First prompt on fresh session", None, mock_target, [])
            if worker._post_turn_compaction_task:
                await worker._post_turn_compaction_task

            self.assertIsNone(worker.pending_carryforward)
            mock_turn1_recycle.assert_not_awaited()
            mock_turn1_get_cf.assert_not_called()
            written_bytes = worker.proc.stdin.write.call_args[0][0]
            written_json = json.loads(written_bytes.decode("utf-8"))
            sent_prompt = written_json["message"]["content"]
            self.assertIn("[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\nStashed home milestones", sent_prompt)
            self.assertIn("First prompt on fresh session", sent_prompt)

        # --- EXTERNAL MODE ---
        ext_worker = bd.PersistentChannelWorker(
            channel_id=bd.LOUNGE_CHANNEL_ID,
            name="lounge",
            mode="external",
            sess_key=str(bd.LOUNGE_CHANNEL_ID),
        )
        ext_worker.conv_id = "conv-ext-precompact"
        ext_worker.is_ready = True
        ext_worker.proc = MagicMock()
        ext_worker.proc.pid = 41002
        ext_worker.proc.returncode = None
        ext_worker.proc.stdin = MagicMock()
        ext_worker.proc.stdin.write = MagicMock()
        ext_worker.proc.stdin.drain = AsyncMock()
        ext_worker.proc.stdout = AsyncMock()
        ext_worker._last_session_turns = 30

        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(True, "Turn limit reached (30/30)")), \
             patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch.object(ext_worker, "recycle", new_callable=AsyncMock) as mock_ext_recycle, \
             patch("tools.session_summarizer.generate_summary") as mock_ext_gen, \
             patch("tools.session_summarizer.get_engineering_carryforward_context", return_value="Sanitized eng delta") as mock_ext_get:
            compacted = await ext_worker._maybe_compact_post_turn()
            self.assertTrue(compacted)
            mock_ext_recycle.assert_awaited_once_with(new_conv_id=None)
            mock_ext_gen.assert_called_once_with(conv_id="conv-ext-precompact", sess_key=str(bd.LOUNGE_CHANNEL_ID))
            mock_ext_get.assert_called_once_with(sess_key=str(bd.LOUNGE_CHANNEL_ID))
            self.assertEqual(ext_worker.pending_eng_carryforward, "Sanitized eng delta")

        # Turn 1 on external worker consumes pending_eng_carryforward without double-fetching overnight rollover
        ext_target = MagicMock()
        ext_target.channel = MagicMock()
        ext_target.channel.id = bd.LOUNGE_CHANNEL_ID
        with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="External reply"), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock), \
             patch.object(ext_worker, "recycle", new_callable=AsyncMock) as mock_ext_t1_recycle, \
             patch("tools.session_summarizer.get_engineering_carryforward_context") as mock_ext_t1_get:
            await ext_worker.execute_turn("Check lounge status", None, ext_target, [], author_name="Amos")
            if ext_worker._post_turn_compaction_task:
                await ext_worker._post_turn_compaction_task

            self.assertIsNone(ext_worker.pending_eng_carryforward)
            mock_ext_t1_recycle.assert_not_awaited()
            mock_ext_t1_get.assert_not_called()
            written_bytes = ext_worker.proc.stdin.write.call_args[0][0]
            written_json = json.loads(written_bytes.decode("utf-8"))
            sent_prompt = written_json["message"]["content"]
            self.assertIn("[PREVIOUS SESSION ENGINEERING DELTA]:\nSanitized eng delta", sent_prompt)

    async def test_20_post_turn_compaction_deferral_on_active_sdk_tasks(self):
        """Verify (c) post-turn compaction defers when active SDK child processes or unsettled tasks exist."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "conv-busy-sdk"
        worker.proc = MagicMock()
        worker.proc.pid = 42001
        worker.proc.returncode = None
        worker._last_session_turns = 32

        # 1. Active child process defers compaction
        with patch("tools.bridge_daemons.get_process_children", return_value=[55001]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_daemons.check_compaction_needed", return_value=(True, "Turn limit")), \
             patch.object(worker, "recycle", new_callable=AsyncMock) as mock_recycle:
            self.assertTrue(worker._has_active_sdk_tasks())
            res = await worker._maybe_compact_post_turn()
            self.assertFalse(res)
            mock_recycle.assert_not_awaited()

        # 2. Unsettled SDK background task defers compaction
        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=["bg-task-99"]), \
             patch("tools.bridge_daemons.check_compaction_needed", return_value=(True, "Turn limit")), \
             patch.object(worker, "recycle", new_callable=AsyncMock) as mock_recycle:
            self.assertTrue(worker._has_active_sdk_tasks())
            res = await worker._maybe_compact_post_turn()
            self.assertFalse(res)
            mock_recycle.assert_not_awaited()

        # 3. Probe exceptions fail open safely (returning False for _has_active_sdk_tasks)
        with patch("tools.bridge_daemons.get_process_children", side_effect=RuntimeError("procfs error")), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", side_effect=RuntimeError("transcript error")):
            self.assertFalse(worker._has_active_sdk_tasks())

    async def test_21_emergency_high_water_and_manual_reset_inline_compaction(self):
        """Verify (d) emergency high-water inline fallback and (e) manual reset_session_keys inline reset."""
        from tools.bridge_runner import reset_session_keys

        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "conv-high-water"
        worker.is_ready = True
        worker.proc = MagicMock()
        worker.proc.pid = 43001
        worker.proc.returncode = None
        worker.proc.stdin = MagicMock()
        worker.proc.stdin.write = MagicMock()
        worker.proc.stdin.drain = AsyncMock()
        worker.proc.stdout = AsyncMock()

        mock_target = MagicMock()
        mock_target.channel = MagicMock()
        mock_target.channel.id = bs.TARGET_CHANNEL_ID

        # Verify 2-arg stub compatibility in _check_high_water_compaction
        with patch("tools.bridge_daemons.check_compaction_needed", new=lambda cid, turns: (True, "2-arg stub")):
            self.assertEqual(worker._check_high_water_compaction(50), (False, ""))

        # (d) Emergency 1.5x high-water triggers inline pre-turn recycle + summary injection
        def high_water_check(conv_id, current_turns, high_water_multiplier=1.0):
            if high_water_multiplier == 1.5:
                return (True, "Turn limit reached (45/45)")
            return (False, None)

        with patch("tools.bridge_daemons.check_compaction_needed", side_effect=high_water_check), \
             patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="High water response"), \
             patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock), \
             patch.object(worker, "recycle", new_callable=AsyncMock) as mock_hw_recycle, \
             patch("tools.session_summarizer.generate_summary") as mock_hw_gen, \
             patch("tools.session_summarizer.get_carryforward_context", return_value="Emergency high-water summary"):
            await worker.execute_turn("Prompt at 1.5x high water", None, mock_target, [])
            if worker._post_turn_compaction_task:
                await worker._post_turn_compaction_task

            mock_hw_recycle.assert_awaited_once_with(new_conv_id=None)
            mock_hw_gen.assert_called_once_with(conv_id="conv-high-water", sess_key="home")
            written_bytes = worker.proc.stdin.write.call_args[0][0]
            written_json = json.loads(written_bytes.decode("utf-8"))
            self.assertIn("Emergency high-water summary", written_json["message"]["content"])

        # (e) Manual reset_session_keys triggers inline pre-turn recycle even when compaction is False
        worker.conv_id = "conv-manual-reset"
        reset_session_keys.add("home")
        try:
            with patch("tools.bridge_daemons.check_compaction_needed", return_value=(False, None)), \
                 patch("tools.bridge_daemons.get_process_children", return_value=[]), \
                 patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
                 patch("tools.bridge_daemons.TurnCoordinator.execute_stream_loop", new_callable=AsyncMock, return_value="Manual reset response"), \
                 patch("tools.bridge_daemons.deliver_turn_output", new_callable=AsyncMock), \
                 patch.object(worker, "recycle", new_callable=AsyncMock) as mock_mr_recycle, \
                 patch("tools.session_summarizer.generate_summary") as mock_mr_gen, \
                 patch("tools.session_summarizer.get_carryforward_context", return_value="Manual reset summary"):
                await worker.execute_turn("Prompt after manual reset", None, mock_target, [])
                if worker._post_turn_compaction_task:
                    await worker._post_turn_compaction_task

                self.assertNotIn("home", reset_session_keys)
                mock_mr_recycle.assert_awaited_once_with(new_conv_id=None)
                mock_mr_gen.assert_called_once_with(conv_id="conv-manual-reset", sess_key="home")
                written_bytes = worker.proc.stdin.write.call_args[0][0]
                written_json = json.loads(written_bytes.decode("utf-8"))
                self.assertIn("Manual reset summary", written_json["message"]["content"])
        finally:
            reset_session_keys.discard("home")

    async def test_22_no_self_cancellation_when_post_turn_compaction_calls_recycle_shutdown(self):
        """Verify (f) _maybe_compact_post_turn calling recycle() -> shutdown() does not cancel itself,
        while external shutdown() cancels a pending _post_turn_compaction_task."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home",
        )
        worker.conv_id = "conv-self-cancel-check"
        worker._last_session_turns = 30
        old_proc = MagicMock()
        old_proc.pid = 44001
        old_proc.returncode = None
        old_proc.wait = AsyncMock(return_value=0)
        worker.proc = old_proc
        worker.is_ready = True

        async def fake_start():
            new_proc = MagicMock()
            new_proc.pid = 44002
            new_proc.returncode = None
            worker.proc = new_proc
            worker.conv_id = "conv-fresh-after-shutdown"
            worker.is_ready = True

        with patch("tools.bridge_daemons.get_process_children", return_value=[]), \
             patch("tools.bridge_daemons.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_daemons.check_compaction_needed", return_value=(True, "Turn limit reached (30/30)")), \
             patch.object(worker, "start", side_effect=fake_start) as mock_start, \
             patch("tools.session_summarizer.generate_summary") as mock_gen, \
             patch("tools.session_summarizer.get_carryforward_context", return_value="Summary after real shutdown"):
            worker._schedule_post_turn_compaction()
            task = worker._post_turn_compaction_task
            self.assertIsNotNone(task)
            result = await task
            self.assertTrue(result)
            old_proc.terminate.assert_called_once()
            mock_start.assert_awaited_once()
            mock_gen.assert_called_once_with(conv_id="conv-self-cancel-check", sess_key="home")
            self.assertEqual(worker.conv_id, "conv-fresh-after-shutdown")
            self.assertEqual(worker.pending_carryforward, "Summary after real shutdown")
            self.assertIsNone(worker._post_turn_compaction_task)

        # Verify external shutdown() cancels an in-flight _post_turn_compaction_task
        block_gate = asyncio.Event()

        async def blocked_compaction():
            await block_gate.wait()

        worker._post_turn_compaction_task = asyncio.create_task(blocked_compaction())
        ext_task = worker._post_turn_compaction_task
        await asyncio.sleep(0)
        await worker.shutdown()
        self.assertIsNone(worker._post_turn_compaction_task)
        await asyncio.sleep(0)
        self.assertTrue(ext_task.cancelled())


if __name__ == "__main__":
    unittest.main()

