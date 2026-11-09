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
        with patch("tools.bridge_state.get_runtime_rules", return_value={}):
            # Direct #forge channel ID and object
            self.assertTrue(bs.is_home_channel(bs.FORGE_CHANNEL_ID))
            self.assertTrue(bs.is_home_channel(str(bs.FORGE_CHANNEL_ID)))
            self.assertTrue(bs.is_home_channel(DummyChannel(bs.FORGE_CHANNEL_ID)))
            # Thread inside #forge
            self.assertTrue(bs.is_home_channel(DummyChannel(999999999999, parent_id=bs.FORGE_CHANNEL_ID)))
            # Zero's other home channels must return False for Vulcan
            self.assertFalse(bs.is_home_channel(bs.TARGET_CHANNEL_ID))
            self.assertFalse(bs.is_home_channel(bs.HOMELAB_CHANNEL_ID))
            self.assertFalse(bs.is_home_channel(DummyChannel(bs.HOMELAB_CHANNEL_ID, category_id=bs.OPERATIONS_CATEGORY_ID)))

    def test_is_home_channel_for_zero_when_vulcan_delegated(self):
        os.environ["AGENT_ROLE"] = "zero"
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
            self.assertTrue(bs.is_home_channel(bs.TARGET_CHANNEL_ID))
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
        self.assertIn(f"DISCORD_CHANNEL_ID={bs.FORGE_CHANNEL_ID}", compose)
        self.assertIn("/workspace:/workspace", compose)
        self.assertIn("/secrets:/secrets", compose)
        self.assertIn("/app/bridge.py:/app/bridge.py", compose)
        self.assertIn("/app/entrypoint.sh:/app/entrypoint.sh", compose)
        self.assertIn("agents.md:/root/.gemini/AGENTS.md:ro", compose)

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


if __name__ == "__main__":
    unittest.main()
