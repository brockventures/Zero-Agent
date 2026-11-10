#!/usr/bin/env python3
"""
Unit tests for Vulcan standalone agent mode, channel scoping, ForgeSupervisor delegation,
scheduler lean loop, and Host 2 container provisioner.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.bridge_scheduler as bshed
import tools.bridge_state as bs
import tools.forge as fg
import tools.provision_vulcan as pv


class DummyChannel:
    def __init__(self, cid: int, parent_id: int | None = None, category_id: int | None = None):
        self.id = cid
        self.parent_id = parent_id
        self.category_id = category_id
        self.parent = None


class TestVulcanAgentScoping(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        os.environ.pop("AGENT_ROLE", None)
        os.environ.pop("DISCORD_CHANNEL_ID", None)
        os.environ.pop("VULCAN_DELEGATED", None)
        os.environ.pop("VULCAN_DISCORD_TOKEN", None)
        os.environ.pop("VULCAN_BOT_TOKEN", None)
        os.environ.pop("FORGE_BOT_TOKEN", None)

    def tearDown(self):
        self.env_patcher.stop()

    def test_is_vulcan_agent_detection(self):
        self.assertFalse(bs.is_vulcan_agent())
        os.environ["AGENT_ROLE"] = "vulcan"
        self.assertTrue(bs.is_vulcan_agent())
        os.environ["AGENT_ROLE"] = "  VULCAN  "
        self.assertTrue(bs.is_vulcan_agent())
        os.environ["AGENT_ROLE"] = "zero"
        self.assertFalse(bs.is_vulcan_agent())
        os.environ.pop("AGENT_ROLE", None)
        os.environ["DISCORD_CHANNEL_ID"] = str(bs.FORGE_CHANNEL_ID)
        self.assertTrue(bs.is_vulcan_agent())
        os.environ["DISCORD_CHANNEL_ID"] = "1542081375287640084"
        self.assertFalse(bs.is_vulcan_agent())

    def test_session_key_isolation_between_zero_and_vulcan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sess_file = tmp_path / "sessions.json"
            meta_file = tmp_path / "session_metadata.json"
            zero_chat_id = 1542081375287640084
            forge_id = bs.FORGE_CHANNEL_ID

            with patch.object(bs, "SESSIONS_FILE", sess_file), \
                 patch.object(bs, "SESSION_METADATA_FILE", meta_file):
                # 1. Zero persists its home session
                os.environ["AGENT_ROLE"] = "zero"
                os.environ["DISCORD_CHANNEL_ID"] = str(zero_chat_id)
                self.assertEqual(bs.get_home_session_key(), "home")
                bs.set_channel_session_id(zero_chat_id, "home", "zero-conv-111", target_channel_id=zero_chat_id)
                self.assertEqual(bs.get_channel_session_id(zero_chat_id, "home", target_channel_id=zero_chat_id), "zero-conv-111")

                # 2. Vulcan persists its home session in #forge (with target_channel_id == FORGE_CHANNEL_ID)
                os.environ["AGENT_ROLE"] = "vulcan"
                os.environ["DISCORD_CHANNEL_ID"] = str(forge_id)
                self.assertEqual(bs.get_home_session_key(), "vulcan-home")
                bs.set_channel_session_id(forge_id, "home", "vulcan-conv-222", target_channel_id=forge_id)
                self.assertEqual(bs.get_channel_session_id(forge_id, "home", target_channel_id=forge_id), "vulcan-conv-222")

                # Verify Zero's 'home' key in sessions.json was NOT overwritten by Vulcan
                raw_sessions = json.loads(sess_file.read_text(encoding="utf-8"))
                self.assertEqual(raw_sessions.get("home"), "zero-conv-111")
                self.assertEqual(raw_sessions.get(str(forge_id)), "vulcan-conv-222")

                # 3. Clearing Vulcan's session leaves Zero's 'home' key intact
                bs.clear_channel_session_id(forge_id, "home", target_channel_id=forge_id)
                self.assertIsNone(bs.get_channel_session_id(forge_id, "home", target_channel_id=forge_id))
                os.environ["AGENT_ROLE"] = "zero"
                os.environ["DISCORD_CHANNEL_ID"] = str(zero_chat_id)
                self.assertEqual(bs.get_channel_session_id(zero_chat_id, "home", target_channel_id=zero_chat_id), "zero-conv-111")

    def test_stage_decision_card_uses_for_forge_channel_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "forge.db"
            fg.init_forge_db(db_path)

            with patch("tools.forge._get_discord_token", return_value="vulcan-bot-token") as mock_get_token, \
                 patch("tools.forge._post_decision_card_rest", return_value=987654321) as mock_post_rest:
                res = fg.stage_decision_card(
                    category="ops",
                    title="Reload Vulcan Bridge",
                    summary="Reload Vulcan in #forge",
                    handler_type="bridge_reload",
                    payload={"action": "Trigger in-place bridge reload"},
                    channel_id=fg.FORGE_CHANNEL_ID,
                    db_path=db_path,
                    post_to_discord=True,
                )
                self.assertTrue(res["ok"])
                self.assertEqual(res["channel_id"], fg.FORGE_CHANNEL_ID)
                self.assertFalse(res["redirected_to_zero_chat"])
                mock_get_token.assert_called_with(for_forge_channel=True)
                mock_post_rest.assert_called_once()

    def test_is_vulcan_delegated_env_and_runtime_rules(self):
        with patch("tools.bridge_state.get_runtime_rules", return_value={}):
            self.assertFalse(bs.is_vulcan_delegated())
            os.environ["VULCAN_DELEGATED"] = "true"
            self.assertTrue(bs.is_vulcan_delegated())
            os.environ["VULCAN_DELEGATED"] = "1"
            self.assertTrue(bs.is_vulcan_delegated())
            os.environ["VULCAN_DELEGATED"] = "0"
            self.assertFalse(bs.is_vulcan_delegated())

        with patch("tools.bridge_state.get_runtime_rules", return_value={"vulcan_delegated": True}):
            self.assertTrue(bs.is_vulcan_delegated())

    def test_is_home_channel_for_vulcan_agent(self):
        os.environ["AGENT_ROLE"] = "vulcan"
        zero_chat_id = 1542081375287640084
        with patch("tools.bridge_state.get_runtime_rules", return_value={}):
            # Direct #forge channel ID and object
            self.assertTrue(bs.is_home_channel(bs.FORGE_CHANNEL_ID))
            self.assertTrue(bs.is_home_channel(str(bs.FORGE_CHANNEL_ID)))
            self.assertTrue(bs.is_home_channel(DummyChannel(bs.FORGE_CHANNEL_ID)))
            # Thread inside #forge
            self.assertTrue(bs.is_home_channel(DummyChannel(999999999999, parent_id=bs.FORGE_CHANNEL_ID)))
            # Zero's other home channels must return False for Vulcan
            self.assertFalse(bs.is_home_channel(zero_chat_id))
            self.assertFalse(bs.is_home_channel(bs.HOMELAB_CHANNEL_ID))
            self.assertFalse(bs.is_home_channel(DummyChannel(bs.HOMELAB_CHANNEL_ID, category_id=bs.OPERATIONS_CATEGORY_ID)))

    def test_is_home_channel_for_zero_when_vulcan_delegated(self):
        os.environ["AGENT_ROLE"] = "zero"
        zero_chat_id = 1542081375287640084
        with patch("tools.bridge_state.get_runtime_rules", return_value={}):
            # When not delegated, #forge is part of Zero's home channels
            os.environ["VULCAN_DELEGATED"] = "0"
            self.assertTrue(bs.is_home_channel(bs.FORGE_CHANNEL_ID))
            self.assertTrue(bs.is_home_channel(DummyChannel(bs.FORGE_CHANNEL_ID, category_id=bs.OPERATIONS_CATEGORY_ID)))

            # When delegated to Vulcan, Zero excludes #forge (and threads inside #forge)
            os.environ["VULCAN_DELEGATED"] = "1"
            self.assertFalse(bs.is_home_channel(bs.FORGE_CHANNEL_ID))
            self.assertFalse(bs.is_home_channel(DummyChannel(bs.FORGE_CHANNEL_ID, category_id=bs.OPERATIONS_CATEGORY_ID)))
            self.assertFalse(bs.is_home_channel(DummyChannel(888888888888, parent_id=bs.FORGE_CHANNEL_ID, category_id=bs.OPERATIONS_CATEGORY_ID)))
            self.assertTrue(bs.is_excluded_channel(bs.FORGE_CHANNEL_ID))
            # Zero still owns #zero-chat and #homelab
            self.assertTrue(bs.is_home_channel(zero_chat_id) or bs.is_home_channel(bs.HOMELAB_CHANNEL_ID))
            self.assertTrue(bs.is_home_channel(bs.HOMELAB_CHANNEL_ID))

    def test_forge_supervisor_should_execute_builds_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "forge.db"
            sup = fg.ForgeSupervisor(db_path=db_path)

            with patch("tools.forge.is_ivy_container", return_value=False), \
                 patch("tools.bridge_state.get_runtime_rules", return_value={}):
                # Default Zero (not delegated) -> True
                os.environ["AGENT_ROLE"] = "zero"
                os.environ["VULCAN_DELEGATED"] = "0"
                self.assertTrue(sup.should_execute_builds())

                # Delegated Zero -> False
                os.environ["VULCAN_DELEGATED"] = "1"
                self.assertFalse(sup.should_execute_builds())
                self.assertIsNone(sup.maybe_spawn_next_build_worker())

                # Vulcan container -> True even when VULCAN_DELEGATED=1
                os.environ["AGENT_ROLE"] = "vulcan"
                self.assertTrue(sup.should_execute_builds())

                with patch("tools.forge.DATA_DIR", tmp_path), \
                     patch("tools.forge.expire_stale_decisions", return_value=[]), \
                     patch("tools.forge.prune_completed_build_widgets", return_value=0), \
                     patch("tools.forge.sync_forge_channel_header", return_value=False), \
                     patch.object(sup, "maybe_spawn_next_build_worker", return_value=None):
                    sup.tick_sync()
                    hb_file = tmp_path / "vulcan_heartbeat.json"
                    self.assertTrue(hb_file.exists())
                    hb_data = json.loads(hb_file.read_text(encoding="utf-8"))
                    self.assertEqual(hb_data["agent_role"], "vulcan")
                    self.assertEqual(hb_data["channel_id"], fg.FORGE_CHANNEL_ID)

    def test_forge_token_resolution_for_forge_vs_cross_channel(self):
        os.environ["AGENT_ROLE"] = "vulcan"
        os.environ["VULCAN_DISCORD_TOKEN"] = "vulcan-token-123"
        with patch("tools.deliver_image.get_discord_token", return_value="zero-primary-token-456"):
            self.assertEqual(fg._get_discord_token(for_forge_channel=True), "vulcan-token-123")
            self.assertEqual(fg._get_discord_token(for_channel_id=fg.FORGE_CHANNEL_ID), "vulcan-token-123")
            self.assertEqual(fg._get_discord_token(for_forge_channel=False), "zero-primary-token-456")

    def test_provision_vulcan_compose_and_dry_run(self):
        compose = pv.render_vulcan_compose_yaml(docker_root="/opt/docker")
        self.assertIn("image: discord-agy-agent-antigravity-agent:latest", compose)
        self.assertIn("container_name: discord-vulcan-agent", compose)
        self.assertIn("mem_limit: 2000m", compose)
        self.assertIn("cpu_shares: 256", compose)
        self.assertIn("AGENT_ROLE=vulcan", compose)
        self.assertIn("AGENT_NAME=Vulcan", compose)
        self.assertIn(f"DISCORD_CHANNEL_ID={bs.FORGE_CHANNEL_ID}", compose)
        self.assertIn("/workspace:/workspace", compose)
        self.assertIn("/secrets:/secrets", compose)
        self.assertIn("/app/bridge.py:/app/bridge.py", compose)
        self.assertIn("/app/entrypoint.sh:/app/entrypoint.sh", compose)
        self.assertIn("agents.md:/root/.gemini/AGENTS.md:ro", compose)

        staging = pv.build_remote_staging_Payload(docker_root="/opt/docker")
        self.assertIn("AGENT_NAME=Vulcan", staging["/opt/docker/discord-vulcan-agent/.env.defaults"])

        res = pv.provision_vulcan_on_host2(dry_run=True, docker_root="/opt/docker")
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertIn("/opt/docker/discord-vulcan-agent/docker-compose.yml", res["files"])
        self.assertIn("/opt/docker/discord-vulcan-agent/agents.md", res["files"])


class TestVulcanSchedulerLeanMode(unittest.IsolatedAsyncioTestCase):
    async def test_vulcan_scheduler_loop_skips_household_jobs(self):
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        scheduler._running = True

        def stop_after_one_sleep(_):
            scheduler._running = False
            raise asyncio.CancelledError()

        with patch.dict(os.environ, {"AGENT_ROLE": "vulcan"}, clear=False), \
             patch.object(scheduler, "_evaluate_and_dispatch_jobs", new_callable=AsyncMock) as mock_eval_jobs, \
             patch.object(scheduler, "flush_outbox_queue", new_callable=AsyncMock) as mock_flush, \
             patch("tools.forge.forge_supervisor.tick_sync") as mock_tick, \
             patch("tools.process_probe.reap_stale_agy_processes") as mock_reap, \
             patch("asyncio.sleep", side_effect=stop_after_one_sleep):
            try:
                await scheduler._loop()
            except asyncio.CancelledError:
                pass

        mock_eval_jobs.assert_not_awaited()
        mock_reap.assert_not_called()
        mock_tick.assert_called_once()
        mock_flush.assert_awaited_once_with(channel_filter=bs.FORGE_CHANNEL_ID)

    async def test_vulcan_runtime_hygiene_syncs_bridge_and_kills_zero_sidecars(self):
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            ws_tools = tmp_path / "tools"
            app_dir = tmp_path / "app"
            ws_tools.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            (ws_tools / "bridge.py").write_text("# fresh bridge\n", encoding="utf-8")
            (ws_tools / "bridge_scheduler.py").write_text("# fresh scheduler\n", encoding="utf-8")
            (app_dir / "bridge.py").write_text("# stale\n", encoding="utf-8")

            res = scheduler._ensure_vulcan_runtime_hygiene(workspace_tools_dir=ws_tools, app_dir=app_dir)
            self.assertIn("bridge.py", res["synced_files"])
            self.assertIn("bridge_scheduler.py", res["synced_files"])
            self.assertEqual((app_dir / "bridge.py").read_text(encoding="utf-8"), "# fresh bridge\n")
            self.assertEqual((app_dir / "bridge_scheduler.py").read_text(encoding="utf-8"), "# fresh scheduler\n")

    async def test_vulcan_scheduler_loop_recovers_from_tick_sync_exception(self):
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        scheduler._running = True

        def stop_after_one_sleep(_):
            scheduler._running = False
            raise asyncio.CancelledError()

        with patch.dict(os.environ, {"AGENT_ROLE": "vulcan"}, clear=False), \
             patch.object(scheduler, "_ensure_vulcan_runtime_hygiene", return_value={"synced_files": [], "killed_pids": []}), \
             patch.object(scheduler, "flush_outbox_queue", new_callable=AsyncMock) as mock_flush, \
             patch("tools.forge.forge_supervisor.tick_sync", side_effect=RuntimeError("transient db error")), \
             patch("importlib.reload"), \
             patch("tools.forge.init_forge_db") as mock_init_db, \
             patch("tools.forge.forge_supervisor.maybe_spawn_next_build_worker", return_value="fg-123") as mock_spawn, \
             patch("asyncio.sleep", side_effect=stop_after_one_sleep):
            try:
                await scheduler._loop()
            except asyncio.CancelledError:
                pass

        mock_init_db.assert_called_once()
        mock_spawn.assert_called_once()
        mock_flush.assert_awaited_once_with(channel_filter=bs.FORGE_CHANNEL_ID)


class TestForgeSupervisorResilience(unittest.TestCase):
    def test_get_db_connection_heals_broken_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "forge.db"
            db_path.symlink_to(tmp_path / "nonexistent_dir" / "missing.db")
            self.assertTrue(db_path.is_symlink())
            self.assertFalse(db_path.exists())

            fg.init_forge_db(db_path)
            self.assertFalse(db_path.is_symlink())
            self.assertTrue(db_path.exists())

    def test_tick_sync_isolates_substep_errors_and_still_spawns_builds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "forge.db"
            sup = fg.ForgeSupervisor(db_path=db_path)

            with patch.dict(os.environ, {"AGENT_ROLE": "vulcan"}, clear=False), \
                 patch("tools.forge.DATA_DIR", tmp_path), \
                 patch("tools.forge.expire_stale_decisions", side_effect=RuntimeError("decision error")), \
                 patch("tools.forge.prune_completed_build_widgets", side_effect=RuntimeError("prune error")), \
                 patch("tools.forge.sync_forge_channel_header", side_effect=RuntimeError("header error")), \
                 patch.object(sup, "_effective_max_builds", return_value=1), \
                 patch.object(sup, "_count_active_build_workers", side_effect=[set(), set(), {"fg-recovered"}]), \
                 patch.object(sup, "maybe_spawn_next_build_worker", return_value="fg-recovered") as mock_spawn:
                res = sup.tick_sync()
                self.assertEqual(res["spawned_build"], "fg-recovered")
                self.assertEqual(res["spawned_builds"], ["fg-recovered"])
                mock_spawn.assert_called_once()


class TestVulcanStartupAndPresence(unittest.IsolatedAsyncioTestCase):
    async def test_apply_bot_presence_defaults_to_vulcan(self):
        import tools.bridge_handlers as bh

        bot = AsyncMock()
        bot.is_ready = lambda: True

        with patch.dict(os.environ, {"AGENT_ROLE": "vulcan"}, clear=False), \
             patch("tools.bridge_engine.format_active_presence_text", return_value=None), \
             patch("tools.set_status.get_status", return_value={"activity_text": "Zero is online and ready.", "is_custom": False}):
            os.environ.pop("AGENT_NAME", None)
            await bh.apply_bot_presence(bot)
            bot.change_presence.assert_awaited_once()
            _, kwargs = bot.change_presence.call_args
            self.assertEqual(kwargs["activity"].name, "Vulcan is online and ready.")

    async def test_handle_on_ready_queues_vulcan_startup_prompt_without_agent_name_env(self):
        import contextlib
        import tools.bridge_handlers as bh

        bot = AsyncMock()
        bot.user = AsyncMock()
        bot.user.id = 123456789
        fake_channel = DummyChannel(bs.FORGE_CHANNEL_ID)
        bot.get_channel = lambda cid: fake_channel
        turn_queue = asyncio.Queue()
        ext_turn_queue = asyncio.Queue()

        orig_has_notified = bh.has_notified_ready
        bh.has_notified_ready = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"AGENT_ROLE": "vulcan"}, clear=False))
                stack.enter_context(patch("tools.bridge_handlers.sync_credentials"))
                stack.enter_context(patch("tools.bridge_handlers.update_beacon"))
                stack.enter_context(patch("tools.mcp_daemon.ensure_mcp_daemon_running"))
                stack.enter_context(patch("tools.mcp_daemon.get_status", return_value={}))
                stack.enter_context(patch("tools.zero_mail_listener.ensure_mail_listener_running"))
                stack.enter_context(patch("tools.zero_mail_listener.get_status", return_value={}))
                stack.enter_context(patch("tools.amazon_order_listener.ensure_amazon_listener_running"))
                stack.enter_context(patch("tools.amazon_order_listener.get_status", return_value={}))
                stack.enter_context(patch("tools.zero_health_server.ensure_health_server_running"))
                stack.enter_context(patch("tools.zero_health_server.get_status", return_value={}))
                stack.enter_context(patch("tools.banana_watcher.ensure_banana_watcher_running"))
                stack.enter_context(patch("tools.banana_watcher.get_daemon_status", return_value={}))
                stack.enter_context(patch("tools.forge.reconcile_forge_on_boot", return_value={}))
                stack.enter_context(patch("tools.bridge_daemons.warmup_persistent_daemons", new_callable=AsyncMock))
                mock_inflight = stack.enter_context(patch("tools.bridge_handlers.IN_FLIGHT_FILE"))
                mock_restart = stack.enter_context(patch("tools.bridge_handlers.RESTART_INTENT_FILE"))
                mock_sessions = stack.enter_context(patch("tools.bridge_handlers.SESSIONS_FILE"))
                stack.enter_context(patch("tools.bridge_handlers.get_runtime_rules", return_value={"home_channel_ids": []}))
                stack.enter_context(patch("tools.bridge_handlers.warm_channel_history", new_callable=AsyncMock))
                stack.enter_context(patch("tools.bridge_handlers.detect_recent_container_oom", return_value=None, create=True))

                os.environ.pop("AGENT_NAME", None)
                mock_inflight.exists.return_value = False
                mock_restart.exists.return_value = False
                mock_sessions.exists.return_value = False

                await bh.handle_on_ready(
                    bot=bot,
                    turn_queue=turn_queue,
                    ext_turn_queue=ext_turn_queue,
                    start_workers_fn=None,
                    start_scheduler_fn=None,
                )

                self.assertFalse(turn_queue.empty())
                queued = await turn_queue.get()
                prompt = queued["prompt"]
                self.assertIn("You (Vulcan) just completed a reboot/reload", prompt)
                self.assertIn("🟢 **Vulcan is online and ready.**", prompt)
                self.assertIn("#forge", prompt)
                self.assertNotIn("Zero is online and ready.", prompt)
        finally:
            bh.has_notified_ready = orig_has_notified

    async def test_handle_message_ignores_discord_system_pin_events(self):
        import discord
        import tools.bridge_handlers as bh
        from unittest.mock import MagicMock

        bot = AsyncMock()
        bot.user = MagicMock()
        bot.user.id = 999999999

        msg = MagicMock()
        msg.id = 777777777
        msg.type = discord.MessageType.pins_add
        msg.content = ""
        msg.author = MagicMock()
        msg.author.id = 179407724335988736
        msg.author.bot = False
        msg.author.display_name = "Ryan"
        msg.author.name = "ryan"
        msg.channel = DummyChannel(bs.FORGE_CHANNEL_ID)
        msg.reference = MagicMock()
        msg.reference.message_id = 1563755730161631280
        msg.reference.resolved = None

        home_turn_queue = asyncio.Queue()
        ext_turn_queue = asyncio.Queue()

        with patch("tools.channel_history.record_message"), \
             patch("tools.channel_history.get_recent_messages", return_value=[{}, {}]):
            await bh.handle_message(
                msg=msg,
                bot=bot,
                home_turn_queue=home_turn_queue,
                ext_turn_queue=ext_turn_queue,
            )

        self.assertTrue(home_turn_queue.empty())
        self.assertTrue(ext_turn_queue.empty())

    def test_sync_forge_channel_header_recovers_and_migrates_on_403(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "forge.db"
            fg.init_forge_db(db_path)
            id_cache = tmp_path / ".forge_header_msg_id.txt"

            with patch("tools.forge.DATA_DIR", tmp_path), \
                 patch("tools.forge.FORGE_HEADER_ID_CACHE_FILE", id_cache), \
                 patch("tools.forge._is_prod_forge_env", return_value=True), \
                 patch("tools.forge._get_discord_token", return_value="vulcan-tok-123"), \
                 patch("tools.forge._get_vulcan_webhook", return_value=None), \
                 patch("tools.forge.format_forge_channel_header", return_value="### ⚒️ VULCAN Header"), \
                 patch("tools.forge._merge_remote_active_embeds_into_header", side_effect=lambda c, *a: c), \
                 patch("tools.forge._patch_forge_channel_message", return_value=(False, 403)) as mock_patch, \
                 patch("tools.forge._post_forge_channel_message", return_value=222233334444) as mock_post, \
                 patch("tools.forge.requests.put") as mock_put, \
                 patch("tools.forge.requests.delete") as mock_delete:
                ok = fg.sync_forge_channel_header(
                    db_path=db_path,
                    forge_channel_id=fg.FORGE_CHANNEL_ID,
                    header_message_id=fg.FORGE_HEADER_MESSAGE_ID,
                    force=True,
                )
                self.assertTrue(ok)
                mock_patch.assert_called_once()
                self.assertEqual(mock_patch.call_args[0][0], fg.FORGE_HEADER_MESSAGE_ID)
                mock_post.assert_called_once()
                self.assertTrue(id_cache.exists())
                self.assertEqual(id_cache.read_text(encoding="utf-8").strip(), "222233334444")
                self.assertEqual(fg.get_forge_header_message_id(), 222233334444)
                mock_put.assert_called_once_with(
                    f"https://discord.com/api/v10/channels/{fg.FORGE_CHANNEL_ID}/pins/222233334444",
                    headers={"Authorization": "Bot vulcan-tok-123"},
                    timeout=10,
                )
                mock_delete.assert_called_once_with(
                    f"https://discord.com/api/v10/channels/{fg.FORGE_CHANNEL_ID}/pins/{fg.FORGE_HEADER_MESSAGE_ID}",
                    headers={"Authorization": "Bot vulcan-tok-123"},
                    timeout=10,
                )


if __name__ == "__main__":
    unittest.main()


def test_run_build_pipeline_reclaims_orphaned_running_chunks(tmp_path, monkeypatch):
    from tools import forge
    db_path = tmp_path / "forge.db"
    forge.init_forge_db(db_path)
    with forge.get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO forge_builds (id, title, objective, repo, tier, origin_channel_id, status, created_at, updated_at) "
            "VALUES ('fg-orphan-1', 'Orphan Test', 'Obj', ?, 2, 123, 'running', 1.0, 1.0)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO forge_chunks (id, build_id, chunk_index, title, repo, target_files_json, instructions, verification_cmd, status) "
            "VALUES ('fg-orphan-1-c0', 'fg-orphan-1', 0, 'Chunk 0', ?, ?, 'do it', 'true', 'running')",
            (str(tmp_path), '["a.py"]'),
        )
    sup = forge.ForgeSupervisor(db_path=db_path)
    executed = []

    def fake_exec(build_id, chunk_row):
        executed.append(chunk_row["id"])
        with forge.get_db_connection(db_path) as conn:
            conn.execute("UPDATE forge_chunks SET status = 'merged' WHERE id = ?", (chunk_row["id"],))
        return True

    monkeypatch.setattr(sup, "execute_chunk_in_worktree", fake_exec)
    monkeypatch.setattr(forge, "_prepare_clean_repo_and_sync_base", lambda *a, **kw: "HEAD")
    monkeypatch.setattr(forge, "_propagate_build_branches", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_completion_to_origin", lambda *a, **kw: None)

    assert sup.run_build_pipeline("fg-orphan-1") is True
    assert executed == ["fg-orphan-1-c0"]
