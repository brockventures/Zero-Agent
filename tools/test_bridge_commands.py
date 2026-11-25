# ruff: noqa: I001, SIM117, UP041
"""
Unit tests for tools/bridge_commands.py and native Discord slash commands in tools/bridge.py.
Exercises UI choice views, operator commands, model switching, sidecar triggers, and lifecycle hooks.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)
from tools.bridge_commands import (
    MODEL_ALIASES,
    ON_DEMAND_TRIGGERS,
    QuickChoiceView,
    handle_button_choice,
    handle_operator_command,
)
from tools.bridge_state import OWNER_USER_ID, TARGET_CHANNEL_ID


class TestBridgeCommands(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        from tools.bridge_commands import PROCESSED_INTERACTIONS
        PROCESSED_INTERACTIONS.clear()

    def tearDown(self):
        from tools.bridge_commands import PROCESSED_INTERACTIONS
        PROCESSED_INTERACTIONS.clear()

    def test_on_demand_triggers_strictly_slash_prefixed(self):
        """Verify all ON_DEMAND_TRIGGERS use canonical /<job> keys without legacy ! duplicates."""
        self.assertGreater(len(ON_DEMAND_TRIGGERS), 40)
        for k in ON_DEMAND_TRIGGERS:
            self.assertTrue(k.startswith("/"), f"Expected canonical slash key, got {k}")
            self.assertFalse(k.startswith("!"), f"Legacy bang key found: {k}")

    def test_quick_choice_view_structure(self):
        options = ["Option A", "Option B", "Option C", "Option D", "Option E", "Option F"]
        view = QuickChoiceView(options)
        # Max 5 buttons
        self.assertEqual(len(view.children), 5)
        self.assertEqual(view.children[0].label, "Option A")
        self.assertEqual(view.children[0].custom_id, "choice:Option A")

    async def test_handle_button_choice_normal(self):
        turn_queue = asyncio.Queue()
        mock_interaction = MagicMock()
        mock_interaction.id = 99887766
        mock_interaction.user.id = OWNER_USER_ID
        mock_interaction.channel_id = TARGET_CHANNEL_ID
        mock_interaction.channel.id = TARGET_CHANNEL_ID
        mock_interaction.channel.category_id = None
        mock_interaction.channel.parent_id = None
        mock_interaction.channel.parent = None
        mock_interaction.channel.typing = AsyncMock()
        mock_interaction.channel.send = AsyncMock()

        await handle_button_choice("Deploy Staging", mock_interaction, turn_queue)
        self.assertFalse(turn_queue.empty())
        item = await turn_queue.get()
        self.assertEqual(item["prompt"], "Deploy Staging")
        self.assertEqual(item["mode"], "home")

    async def test_handle_button_choice_non_owner_rejected(self):
        from tools.bridge_commands import PROCESSED_INTERACTIONS
        turn_queue = asyncio.Queue()
        mock_interaction = MagicMock()
        mock_interaction.id = 99887767
        mock_interaction.user.id = 123456789  # Not OWNER_USER_ID
        mock_interaction.channel_id = TARGET_CHANNEL_ID
        mock_interaction.channel.id = TARGET_CHANNEL_ID
        mock_interaction.channel.send = AsyncMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.is_done = MagicMock(return_value=False)
        mock_interaction.response.send_message = AsyncMock()
        mock_interaction.followup = MagicMock()
        mock_interaction.followup.send = AsyncMock()

        await handle_button_choice("Restart Zero", mock_interaction, turn_queue)
        self.assertTrue(turn_queue.empty())
        mock_interaction.channel.send.assert_not_awaited()
        mock_interaction.response.send_message.assert_awaited_once()
        self.assertTrue(mock_interaction.response.send_message.call_args[1].get("ephemeral"))
        self.assertNotIn(mock_interaction.id, PROCESSED_INTERACTIONS)

    async def test_handle_button_choice_external_channel_derives_external_mode(self):
        turn_queue = asyncio.Queue()
        mock_interaction = MagicMock()
        mock_interaction.id = 99887768
        mock_interaction.user.id = OWNER_USER_ID
        mock_interaction.channel_id = 1534452820995080192  # #lounge (non-home)
        mock_interaction.channel.id = 1534452820995080192
        mock_interaction.channel.category_id = None
        mock_interaction.channel.parent_id = None
        mock_interaction.channel.parent = None
        mock_interaction.channel.typing = AsyncMock()
        mock_interaction.channel.send = AsyncMock()

        await handle_button_choice("Continue Analysis", mock_interaction, turn_queue)
        self.assertFalse(turn_queue.empty())
        item = await turn_queue.get()
        self.assertEqual(item["prompt"], "Continue Analysis")
        self.assertEqual(item["mode"], "external")

    async def test_handle_operator_command_reset(self):
        msg = MagicMock()
        msg.content = "/reset"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        with patch("tools.bridge_commands.clear_channel_session_id") as mock_clear:
            handled = await handle_operator_command(msg, bot, "/reset", "Ryan", queue)
            self.assertTrue(handled)
            mock_clear.assert_called_once_with(123, "home")
            msg.reply.assert_awaited_once()

    async def test_handle_operator_command_model_switch(self):
        msg = MagicMock()
        msg.content = "/model 3.8"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()
        mock_setter = MagicMock()

        handled = await handle_operator_command(
            msg, bot, "/model 3.8", "Ryan", queue, active_model_setter=mock_setter
        )
        self.assertTrue(handled)
        mock_setter.assert_called_once_with("gemini-3.8-flash-high")
        msg.reply.assert_awaited_once()

    def test_model_aliases_neon(self):
        self.assertEqual(MODEL_ALIASES["neon"], "gemini-4-neon-high")
        self.assertEqual(MODEL_ALIASES["neon-high"], "gemini-4-neon-high")
        self.assertEqual(MODEL_ALIASES["neon-med"], "gemini-4-neon-medium")
        self.assertEqual(MODEL_ALIASES["neon-medium"], "gemini-4-neon-medium")
        self.assertEqual(MODEL_ALIASES["neon-low"], "gemini-4-neon-low")
        self.assertEqual(MODEL_ALIASES["neon-xhigh"], "gemini-4-neon-xhigh")

    async def test_handle_operator_command_model_switch_neon(self):
        msg = MagicMock()
        msg.content = "/model neon-med"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()
        mock_setter = MagicMock()
        live = {
            "gemini-4-neon-high": "Gemini 4 Neon (High)",
            "gemini-4-neon-medium": "Gemini 4 Neon (Medium)",
            "gemini-4-neon-low": "Gemini 4 Neon (Low)",
            "gemini-4-neon-xhigh": "Gemini 4 Neon (Extra High)",
        }

        with patch("tools.bridge_models.list_available_models", return_value=live):
            handled = await handle_operator_command(
                msg, bot, "/model neon-med", "Ryan", queue, active_model_setter=mock_setter
            )
        self.assertTrue(handled)
        mock_setter.assert_called_once_with("gemini-4-neon-medium")
        msg.reply.assert_awaited_once()

    async def test_handle_operator_command_model_switch_argon(self):
        msg = MagicMock()
        msg.content = "/model argon"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()
        mock_setter = MagicMock()

        handled = await handle_operator_command(
            msg, bot, "/model argon", "Ryan", queue, active_model_setter=mock_setter
        )
        self.assertTrue(handled)
        mock_setter.assert_called_once_with("gemini-4-argon-high")
        msg.reply.assert_awaited_once()

    async def test_slash_model_integration(self):
        from tools.bridge import slash_model, slash_model_autocomplete
        interaction = MagicMock()
        interaction.user.id = OWNER_USER_ID
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        with patch("tools.bridge._get_active_model", return_value="gemini-3.8-flash-high"), \
             patch("tools.bridge._set_active_model") as mock_set:
            # Test empty prompt -> returns help with argon
            await slash_model.callback(interaction, "")
            interaction.response.defer.assert_awaited_once()
            interaction.followup.send.assert_awaited_once()
            help_text = interaction.followup.send.call_args[0][0]
            self.assertIn("gemini-4-argon", help_text)
            self.assertIn("argon", help_text)

            # Test switching to argon
            interaction.response.defer.reset_mock()
            interaction.followup.send.reset_mock()
            await slash_model.callback(interaction, "argon")
            interaction.response.defer.assert_awaited_once()
            mock_set.assert_called_once_with("gemini-4-argon-high")
            interaction.followup.send.assert_awaited_once()

        # Test autocomplete returns argon choices
        choices = await slash_model_autocomplete(interaction, "arg")
        self.assertTrue(any(c.value == "argon" for c in choices))

    async def test_slash_run_and_autocomplete(self):
        from tools.bridge import slash_run, slash_run_autocomplete, home_turn_queue
        interaction = MagicMock()
        interaction.user.id = OWNER_USER_ID
        interaction.channel_id = TARGET_CHANNEL_ID
        interaction.channel.id = TARGET_CHANNEL_ID
        interaction.channel.category_id = None
        interaction.channel.parent_id = None
        interaction.channel.parent = None
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        # Autocomplete check
        choices = await slash_run_autocomplete(interaction, "ev9")
        self.assertTrue(any(c.value == "ev9" for c in choices))

        # Kalshi isolation check via /run
        await slash_run.callback(interaction, "kalshi")
        interaction.followup.send.assert_awaited_once()
        self.assertIn("exclusively operate in", interaction.followup.send.call_args[0][0])

        # Valid job execution via /run
        interaction.followup.send.reset_mock()
        with patch.object(home_turn_queue, "put", new_callable=AsyncMock) as mock_put:
            await slash_run.callback(interaction, "heartbeat")
            mock_put.assert_awaited_once()
            queued = mock_put.call_args[0][0]
            self.assertIn("Run the infrastructure heartbeat check", queued["prompt"])
            self.assertEqual(queued["mode"], "home")

    async def test_slash_quota_and_crab(self):
        from tools.bridge import slash_quota, slash_crab
        interaction = MagicMock()
        interaction.user.id = OWNER_USER_ID
        interaction.channel_id = TARGET_CHANNEL_ID
        interaction.channel.id = TARGET_CHANNEL_ID
        interaction.channel.category_id = None
        interaction.channel.parent_id = None
        interaction.channel.parent = None
        interaction.user.display_name = "Ryan"
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        with patch("tools.quota_card.build_quota_card", return_value=(True, "📊 Quota Card", None)):
            await slash_quota.callback(interaction, "card")
            interaction.followup.send.assert_awaited_once_with("📊 Quota Card")

        interaction.followup.send.reset_mock()
        with patch("tools.agora_kill_switch.trigger_kill_switch", return_value={"message": "🛑 Agora trading halted."}) as mock_kill:
            await slash_crab.callback(interaction, "halt-agora", "", 3)
            mock_kill.assert_called_once_with(initiator="Ryan", action="halt", channel_id=TARGET_CHANNEL_ID)
            interaction.followup.send.assert_awaited_once_with("🛑 Agora trading halted.")

    async def test_handle_operator_command_sidecar_trigger(self):
        msg = MagicMock()
        msg.content = "/heartbeat"
        msg.channel.id = 123
        msg.channel.typing = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        handled = await handle_operator_command(msg, bot, "/heartbeat", "Ryan", queue)
        self.assertTrue(handled)
        self.assertFalse(queue.empty())
        item = await queue.get()
        self.assertIn("Run the infrastructure heartbeat check", item["prompt"])

    async def test_handle_operator_command_kalshi_isolation(self):
        msg = MagicMock()
        msg.content = "/kalshi"
        msg.channel.id = 123  # Not VAULT_CHANNEL_ID
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        handled = await handle_operator_command(msg, bot, "/kalshi", "Ryan", queue)
        self.assertTrue(handled)
        self.assertTrue(queue.empty())
        msg.reply.assert_awaited_once()
        self.assertIn("exclusively operate in", msg.reply.call_args[0][0])

    async def test_handle_operator_command_agora_kill_switch(self):
        msg = MagicMock()
        msg.content = "/halt Agora trading emergency"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        with patch("tools.agora_kill_switch.trigger_kill_switch", return_value={"message": "🛑 Agora trading halted."}) as mock_kill:
            handled = await handle_operator_command(msg, bot, "/halt Agora trading emergency", "Ryan", queue)
            self.assertTrue(handled)
            mock_kill.assert_called_once_with(initiator="Ryan", action="halt", channel_id=123)
            msg.reply.assert_awaited_once_with("🛑 Agora trading halted.")

    async def test_execute_container_restart_dispatches_with_resolved_nas(self):
        from tools.bridge_commands import execute_container_restart

        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        with patch("tools.nas_docker_mcp._resolve_nas_config", return_value=("mock-host-1", "mock-host-2", "2222")), \
             patch("tools.bridge_git_sync.sync_git_on_reload", return_value={"synced": True, "clean": True, "sha": "mocksha", "files": [], "message": "Clean", "error": None}), \
             patch("subprocess.run") as mock_run, \
             patch("tools.bridge_commands.record_restart_intent") as mock_record:
            await execute_container_restart(mock_channel, initiator="Ryan", reason="Unit test restart")

            mock_record.assert_called_once_with("Unit test restart", initiator="Ryan")
            mock_channel.send.assert_awaited_once()
            self.assertIn("Restarting Zero Docker container", mock_channel.send.call_args[0][0])
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "ssh")
            self.assertIn("-p", cmd)
            self.assertEqual(cmd[cmd.index("-p") + 1], "2222")
            self.assertTrue(any("mock-host-2" in arg for arg in cmd))

    def test_nas_docker_mcp_ssh_evicts_mux_and_retries_on_timeout_and_rc255(self):
        import subprocess
        from tools.nas_docker_mcp import _ssh

        ok_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="dockhand\tUp 2 days\t3866\n", stderr="")
        fail_255 = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout="",
            stderr="Connection timed out during banner exchange",
        )

        with patch("tools.nas_docker_mcp._resolve_nas_config", return_value=("198.51.100.10", "198.51.100.20", "2222")), \
             patch("tools.nas_docker_mcp.evict_ssh_mux_socket") as mock_evict, \
             patch("tools.nas_docker_mcp.subprocess.run", side_effect=[subprocess.TimeoutExpired(cmd="ssh", timeout=60), ok_proc]) as mock_run:
            res = _ssh("198.51.100.10", "docker ps", timeout=60)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(mock_run.call_count, 2)
            mock_evict.assert_called_once()
            retry_cmd = mock_run.call_args_list[1][0][0]
            self.assertIn("ControlMaster=no", retry_cmd)
            self.assertIn("ControlPath=none", retry_cmd)

        with patch("tools.nas_docker_mcp._resolve_nas_config", return_value=("198.51.100.10", "198.51.100.20", "2222")), \
             patch("tools.nas_docker_mcp.evict_ssh_mux_socket") as mock_evict2, \
             patch("tools.nas_docker_mcp.subprocess.run", side_effect=[fail_255, ok_proc]) as mock_run2:
            res2 = _ssh("198.51.100.10", "docker ps", timeout=60)
            self.assertEqual(res2.returncode, 0)
            self.assertEqual(mock_run2.call_count, 2)
            mock_evict2.assert_called_once()

    def test_subprocess_calls_specify_explicit_timeout_guards(self):
        """Verify every synchronous subprocess call in bridge_git_sync.py, dockhand_update.py, and cubs_notifier.py specifies timeout=."""
        import ast

        tools_dir = Path(__file__).resolve().parent
        target_scripts = ("bridge_git_sync.py", "dockhand_update.py", "cubs_notifier.py")
        missing = []
        for script_name in target_scripts:
            script_path = tools_dir / script_name
            tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "call", "check_call", "check_output")
                ):
                    kw_names = {kw.arg for kw in node.keywords if kw.arg}
                    if "timeout" not in kw_names:
                        missing.append(f"{script_name}:L{node.lineno}: subprocess.{node.func.attr}")
        self.assertEqual(missing, [], f"Unguarded subprocess calls without timeout=: {missing}")

    def test_dockhand_update_get_remote_out_enforces_timeout(self):
        """Verify dockhand_update.get_remote_out passes explicit timeout= and returns empty string on TimeoutExpired."""
        import subprocess
        from tools import dockhand_update

        ssh_stub = ["ssh", "user@198.51.100.10"]
        with patch.object(dockhand_update.subprocess, "check_output", return_value=b"sha256:abc123\n") as mock_co:
            out = dockhand_update.get_remote_out(ssh_stub, "docker inspect dockhand")
            self.assertEqual(out, "sha256:abc123")
            self.assertEqual(mock_co.call_args.kwargs.get("timeout"), dockhand_update.DEFAULT_SSH_CMD_TIMEOUT)

        with patch.object(
            dockhand_update.subprocess,
            "check_output",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30),
        ):
            self.assertEqual(dockhand_update.get_remote_out(ssh_stub, "docker inspect dockhand", timeout=30), "")

    def test_cubs_notifier_cast_enforces_timeout(self):
        """Verify cubs_notifier --cast passes timeout= to subprocess.run and exits 124 on TimeoutExpired."""
        import subprocess
        from tools import cubs_notifier

        with patch.object(sys, "argv", ["cubs_notifier.py", "--cast"]), \
             patch.object(
                 cubs_notifier.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(cmd="mlb_stream_extractor.py", timeout=60),
             ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                cubs_notifier.main()
            self.assertEqual(ctx.exception.code, 124)
            self.assertEqual(mock_run.call_args.kwargs.get("timeout"), cubs_notifier.CAST_SUBPROCESS_TIMEOUT)

    def test_bridge_git_sync_enforces_non_none_timeout_and_aborts_timed_out_rebase(self):
        """Verify sync_git_on_reload enforces positive timeouts even if timeout=None and aborts timed-out rebase without stalling on push."""
        import shutil
        import subprocess
        import tempfile
        from tools import bridge_git_sync

        tmp_dir = tempfile.mkdtemp()
        try:
            repo_path = Path(tmp_dir)
            (repo_path / ".git").mkdir()
            recorded_timeouts = []
            executed_cmds = []

            def fake_run(cmd, *args, **kwargs):
                executed_cmds.append(cmd)
                recorded_timeouts.append(kwargs.get("timeout"))
                if "status" in cmd:
                    return MagicMock(returncode=0, stdout=" M tools/bridge.py\n")
                if "diff" in cmd:
                    return MagicMock(returncode=0, stdout="tools/bridge.py\n")
                if "pull" in cmd:
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 30))
                return MagicMock(returncode=0, stdout="abc1234\n")

            with patch.object(bridge_git_sync.subprocess, "run", side_effect=fake_run):
                res = bridge_git_sync.sync_git_on_reload(repo_dir=repo_path, timeout=None)

            self.assertFalse(res["synced"])
            self.assertIsNotNone(res["error"])
            self.assertTrue(all(t is not None and t > 0 for t in recorded_timeouts))
            self.assertTrue(any(cmd[:3] == ["git", "rebase", "--abort"] for cmd in executed_cmds))
            self.assertFalse(any(cmd[:2] == ["git", "push"] for cmd in executed_cmds))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def test_execute_bridge_reload_shuts_down_warm_classifier_kills_proc_trees_and_offloads_git_sync(self):
        """Verify execute_bridge_reload offloads sync_git_on_reload, shuts down warm_classifier, and kills child process groups with bounded wait."""
        import signal
        import tools.bridge_commands as bc
        import tools.bridge_runner as br

        proc_clean = MagicMock()
        proc_clean.returncode = None
        proc_clean.wait = AsyncMock(return_value=0)

        proc_stubborn = MagicMock()
        proc_stubborn.returncode = None
        proc_stubborn.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])

        br.channel_active_procs[12345] = proc_clean
        orig_active_proc = br.active_proc
        orig_ext_proc = br.ext_active_proc
        br.active_proc = proc_stubborn
        br.ext_active_proc = None

        orig_to_thread = asyncio.to_thread
        offloaded = []

        async def spy_to_thread(fn, *args, **kwargs):
            offloaded.append(fn)
            return await orig_to_thread(fn, *args, **kwargs)

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        try:
            with patch("tools.bridge_commands.record_restart_intent"), \
                 patch("tools.bridge_git_sync.sync_git_on_reload", return_value={"clean": True, "message": "Clean"}) as mock_git_sync, \
                 patch("tools.bridge_daemons.daemon_manager.shutdown_all", new_callable=AsyncMock) as mock_daemons_shutdown, \
                 patch("tools.classifier.warm_classifier.shutdown") as mock_warm_shutdown, \
                 patch("tools.bridge_commands.kill_process_tree") as mock_kill_tree, \
                 patch("tools.bridge_commands.asyncio.to_thread", side_effect=spy_to_thread), \
                 patch("os.execv") as mock_execv:
                await bc.execute_bridge_reload(
                    bot=mock_bot,
                    channel=mock_channel,
                    initiator="user",
                    reason="unit test reload",
                )

            self.assertIn(mock_git_sync, offloaded)
            mock_git_sync.assert_called_once_with(reason="unit test reload", initiator="user")
            mock_daemons_shutdown.assert_awaited_once()
            self.assertIn(mock_warm_shutdown, offloaded)
            mock_warm_shutdown.assert_called_once()

            kill_calls = mock_kill_tree.call_args_list
            self.assertIn(((proc_clean, signal.SIGTERM), {}), [(c.args, c.kwargs) for c in kill_calls])
            self.assertIn(((proc_stubborn, signal.SIGTERM), {}), [(c.args, c.kwargs) for c in kill_calls])
            self.assertIn(((proc_stubborn, signal.SIGKILL), {}), [(c.args, c.kwargs) for c in kill_calls])
            proc_clean.wait.assert_awaited_once()
            mock_bot.close.assert_awaited_once()
            mock_execv.assert_called_once()
        finally:
            br.channel_active_procs.pop(12345, None)
            br.active_proc = orig_active_proc
            br.ext_active_proc = orig_ext_proc

    async def test_execute_bridge_reload_uses_active_agent_display_name(self):
        """Verify execute_bridge_reload and execute_container_restart use Vulcan/Zero dynamically."""
        import os
        import tools.bridge_commands as bc

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        for is_vulc, expected_name, expected_container in (
            (True, "Vulcan", "discord-vulcan-agent"),
            (False, "Zero", "discord-antigravity-agent"),
        ):
            mock_channel = MagicMock()
            mock_channel.send = AsyncMock()
            with patch.dict(os.environ, {}, clear=False), \
                 patch("tools.bridge_commands.is_vulcan_agent", return_value=is_vulc), \
                 patch("tools.bridge_commands.record_restart_intent"), \
                 patch("tools.bridge_git_sync.sync_git_on_reload", return_value={"clean": True, "message": "Clean"}), \
                 patch("tools.bridge_daemons.daemon_manager.shutdown_all", new_callable=AsyncMock), \
                 patch("tools.classifier.warm_classifier.shutdown"), \
                 patch("os.execv"):
                os.environ.pop("AGENT_NAME", None)
                await bc.execute_bridge_reload(bot=mock_bot, channel=mock_channel, announce=True)
            mock_channel.send.assert_awaited_with(f"🔄 Reloading {expected_name} bridge in-place...")

            mock_restart_ch = MagicMock()
            mock_restart_ch.send = AsyncMock()
            with patch.dict(os.environ, {}, clear=False), \
                 patch("tools.bridge_commands.is_vulcan_agent", return_value=is_vulc), \
                 patch("tools.bridge_commands.record_restart_intent"), \
                 patch("tools.bridge_git_sync.sync_git_on_reload", return_value={"clean": True, "message": "Clean"}), \
                 patch("tools.bridge_commands.subprocess.run") as mock_sub:
                os.environ.pop("AGENT_NAME", None)
                await bc.execute_container_restart(channel=mock_restart_ch)
            sent_text = mock_restart_ch.send.call_args.args[0]
            self.assertIn(f"Restarting {expected_name} Docker container", sent_text)
            ssh_cmd = mock_sub.call_args.args[0][-1]
            self.assertIn(f"docker restart {expected_container}", ssh_cmd)

    async def test_execute_bridge_reload_single_flight_and_early_flag_clear(self):
        """Verify concurrent execute_bridge_reload calls execute once, unlink reload_bridge.flag before sync_git_on_reload, and reset _reload_in_progress."""
        import shutil
        import tempfile
        import tools.bridge_commands as bc
        import tools.bridge_state as bs

        tmp_dir = Path(tempfile.mkdtemp())
        flag_file = tmp_dir / "reload_bridge.flag"
        in_flight_file = tmp_dir / "in_flight_turn.json"
        flag_file.write_text("pending", encoding="utf-8")

        flag_exists_during_send = []
        flag_exists_during_git_sync = []
        in_progress_during_git_sync = []

        async def slow_send(_msg):
            flag_exists_during_send.append(flag_file.exists())
            await asyncio.sleep(0.05)

        def check_flag_in_git_sync(**_kwargs):
            flag_exists_during_git_sync.append(flag_file.exists())
            in_progress_during_git_sync.append(bc._reload_in_progress)
            return {"clean": True, "message": "Clean"}

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock(side_effect=slow_send)

        try:
            bc._reload_in_progress = False
            with patch.object(bc, "DATA_DIR", tmp_dir), \
                 patch.object(bs, "DATA_DIR", tmp_dir), \
                 patch.object(bc, "IN_FLIGHT_FILE", in_flight_file), \
                 patch("tools.bridge_commands.record_restart_intent") as mock_record, \
                 patch("tools.bridge_git_sync.sync_git_on_reload", side_effect=check_flag_in_git_sync) as mock_git_sync, \
                 patch("tools.bridge_daemons.daemon_manager.shutdown_all", new_callable=AsyncMock) as mock_daemons_shutdown, \
                 patch("tools.classifier.warm_classifier.shutdown") as mock_warm_shutdown, \
                 patch("os.execv") as mock_execv:
                await asyncio.gather(
                    bc.execute_bridge_reload(
                        bot=mock_bot,
                        channel=mock_channel,
                        initiator="Ryan",
                        reason="Approved Decision Card dec-1",
                    ),
                    bc.execute_bridge_reload(
                        bot=mock_bot,
                        channel=mock_channel,
                        initiator="scheduler",
                        reason="Scheduled flag reload",
                    ),
                )

            self.assertEqual(mock_channel.send.await_count, 1)
            self.assertEqual(mock_git_sync.call_count, 1)
            self.assertEqual(mock_record.call_count, 1)
            self.assertEqual(mock_daemons_shutdown.await_count, 1)
            self.assertEqual(mock_warm_shutdown.call_count, 1)
            self.assertEqual(mock_execv.call_count, 1)
            self.assertEqual(flag_exists_during_send, [False])
            self.assertEqual(flag_exists_during_git_sync, [False])
            self.assertEqual(in_progress_during_git_sync, [True])
            self.assertFalse(flag_file.exists())
            self.assertFalse(bc._reload_in_progress)
        finally:
            bc._reload_in_progress = False
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def test_execute_bridge_reload_resets_guard_on_execv_exception(self):
        """Verify _reload_in_progress resets to False in finally block even if os.execv raises."""
        import shutil
        import tempfile
        import tools.bridge_commands as bc
        import tools.bridge_state as bs

        tmp_dir = Path(tempfile.mkdtemp())
        in_flight_file = tmp_dir / "in_flight_turn.json"
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        try:
            bc._reload_in_progress = False
            with patch.object(bc, "DATA_DIR", tmp_dir), \
                 patch.object(bs, "DATA_DIR", tmp_dir), \
                 patch.object(bc, "IN_FLIGHT_FILE", in_flight_file), \
                 patch("tools.bridge_commands.record_restart_intent"), \
                 patch("tools.bridge_git_sync.sync_git_on_reload", return_value={"clean": True, "message": "Clean"}), \
                 patch("tools.bridge_daemons.daemon_manager.shutdown_all", new_callable=AsyncMock), \
                 patch("tools.classifier.warm_classifier.shutdown"), \
                 patch("os.execv", side_effect=OSError("execv mocked failure")):
                with self.assertRaises(OSError):
                    await bc.execute_bridge_reload(bot=mock_bot, announce=False)

            self.assertFalse(bc._reload_in_progress)
        finally:
            bc._reload_in_progress = False
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def test_on_button_interaction_bridge_reload_clears_flag_and_passes_initiator_reason(self):
        """Verify approving a bridge_reload Decision Card clears reload_bridge.flag before message edit and passes initiator/reason to _execute_reload."""
        import shutil
        import tempfile
        import discord
        import tools.bridge as bridge_mod
        import tools.bridge_commands as bc
        import tools.bridge_state as bs

        tmp_dir = Path(tempfile.mkdtemp())
        flag_file = tmp_dir / "reload_bridge.flag"
        flag_exists_during_edit = []

        def fake_execute_decision(decision_id, user_id):
            flag_file.write_text("1", encoding="utf-8")
            return {
                "ok": True,
                "handler_type": "bridge_reload",
                "formatted_card": "Approved card",
            }

        async def fake_message_edit(**_kwargs):
            flag_exists_during_edit.append(flag_file.exists())

        interaction = MagicMock()
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": "forge_approve:dec-reload-42"}
        interaction.user.id = OWNER_USER_ID
        interaction.user.display_name = None
        interaction.user.name = "Ryan"
        interaction.channel = MagicMock()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock(side_effect=fake_message_edit)

        fake_view = MagicMock()
        fake_view.children = []

        try:
            with patch.object(bc, "DATA_DIR", tmp_dir), \
                 patch.object(bs, "DATA_DIR", tmp_dir), \
                 patch.object(bridge_mod, "DATA_DIR", tmp_dir), \
                 patch("discord.ui.View.from_message", return_value=fake_view), \
                 patch("tools.forge.execute_decision", side_effect=fake_execute_decision), \
                 patch("tools.bridge._execute_reload", new_callable=AsyncMock) as mock_exec_reload:
                await bridge_mod.on_button_interaction(interaction)

            self.assertTrue(len(flag_exists_during_edit) >= 1)
            self.assertTrue(all( exists is False for exists in flag_exists_during_edit ))
            self.assertFalse(flag_file.exists())
            mock_exec_reload.assert_awaited_once_with(
                interaction.channel,
                initiator="Ryan",
                reason="Approved Decision Card dec-reload-42",
                announce=True,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
