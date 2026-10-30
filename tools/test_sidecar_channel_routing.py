"""Unit tests for Sidecar Channel Routing, Silent-When-Nominal Suppression,

and Plex Audiobook Interceptor Fast-Path.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.bridge_scheduler as bshed
from tools.bridge_scheduler import (
    FINANCES_CHANNEL_ID,
    HOMELAB_CHANNEL_ID,
    SHOPPING_CHANNEL_ID,
    VAULT_CHANNEL_ID,
    ZERO_OPS_CHANNEL_ID,
)


class TestSidecarScheduleJson(unittest.TestCase):
    """Verify data/schedule.json has proper domain channel_ids configured."""

    def test_schedule_json_domain_channel_routing(self):
        sched_path = REPO_ROOT / "data" / "schedule.json"
        self.assertTrue(sched_path.exists(), f"schedule.json not found at {sched_path}")
        with open(sched_path, "r") as f:
            jobs = json.load(f)

        job_map = {j["id"]: j.get("channel_id") for j in jobs}

        # #homelab
        self.assertEqual(job_map.get("heartbeat_sweep"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("nas_storage_check"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("plex_cleanup"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("host1_backup"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("host2_backup"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("dockhand_update"), HOMELAB_CHANNEL_ID)
        self.assertEqual(job_map.get("plex_audiobook_enricher"), HOMELAB_CHANNEL_ID)

        # #zero-ops
        self.assertEqual(job_map.get("session_rollover"), ZERO_OPS_CHANNEL_ID)
        self.assertEqual(job_map.get("memory_doctor"), ZERO_OPS_CHANNEL_ID)
        self.assertEqual(job_map.get("antigravity_check"), ZERO_OPS_CHANNEL_ID)
        self.assertEqual(job_map.get("bridge_watchdog"), ZERO_OPS_CHANNEL_ID)
        self.assertEqual(job_map.get("monthly_hardcode_regex_audit"), ZERO_OPS_CHANNEL_ID)
        self.assertEqual(job_map.get("dreaming"), ZERO_OPS_CHANNEL_ID)

        # #finances
        self.assertEqual(job_map.get("actual_bank_sync"), FINANCES_CHANNEL_ID)
        self.assertEqual(job_map.get("cash_sweep_monitor"), FINANCES_CHANNEL_ID)
        self.assertEqual(job_map.get("monthly_variance_digest"), FINANCES_CHANNEL_ID)

        # #shopping
        self.assertEqual(job_map.get("ev9_monitor"), SHOPPING_CHANNEL_ID)

        # #vault
        self.assertEqual(job_map.get("kalshi_paper_bot"), VAULT_CHANNEL_ID)
        self.assertEqual(job_map.get("quant_daily_sweep"), VAULT_CHANNEL_ID)
        self.assertEqual(job_map.get("kalshi_night_audit"), VAULT_CHANNEL_ID)
        self.assertEqual(job_map.get("kalshi_evening_review"), VAULT_CHANNEL_ID)
        self.assertEqual(job_map.get("kalshi_midday_heating"), VAULT_CHANNEL_ID)
        self.assertEqual(job_map.get("quant_pregame_sweep"), VAULT_CHANNEL_ID)


class TestSidecarChannelRoutingAndSuppression(unittest.IsolatedAsyncioTestCase):
    """Verify dispatch_scheduled_prompt routes to domain channels and silences nominal reports."""

    def setUp(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        self.mock_bot = MagicMock()
        self.channels = {}

        def get_channel(cid):
            if cid not in self.channels:
                ch = AsyncMock()
                ch.id = cid
                self.channels[cid] = ch
            return self.channels[cid]

        self.mock_bot.get_channel.side_effect = get_channel
        self.mock_bot.fetch_channel = AsyncMock(side_effect=get_channel)
        self.mock_queue = AsyncMock()

    # --- #homelab tests ---

    async def test_heartbeat_sweep_degraded_routes_to_homelab(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "🚨 Host 1 degraded", {})):
            await bshed.dispatch_scheduled_prompt(
                "Run the infrastructure heartbeat check",
                job_id="heartbeat_sweep",
                job_name="Heartbeat Sweep",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("🚨 Host 1 degraded")

    async def test_nas_storage_check_warning_routes_to_homelab(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "⚠️ Volume 1 > 88% full", None)):
            await bshed.dispatch_scheduled_prompt(
                "Check NAS storage",
                job_id="nas_storage_check",
                job_name="NAS Storage Check",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("⚠️ Volume 1 > 88% full")

    async def test_plex_cleanup_error_routes_to_homelab(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "⚠️ Failed pruning cache", None)):
            await bshed.dispatch_scheduled_prompt(
                "Run Plex cleanup",
                job_id="plex_cleanup",
                job_name="Plex Transcode Cleanup",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("⚠️ Failed pruning cache")

    async def test_host_backups_error_route_to_homelab(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "🚨 USB disk full", None)):
            await bshed.dispatch_scheduled_prompt(
                "backup_host1",
                job_id="host1_backup",
                job_name="Host 1 Local USB Backup",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("🚨 USB disk full")

        bshed.LAST_SCHEDULED_DISPATCH.clear()
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "🚨 Backup failed", None)):
            await bshed.dispatch_scheduled_prompt(
                "backup_host2",
                job_id="host2_backup",
                job_name="Host 2 Local USB Backup",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertEqual(ch.send.await_count, 2)

    async def test_dockhand_update_routes_to_homelab(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "📦 2 container updates available", None)):
            await bshed.dispatch_scheduled_prompt(
                "dockhand_update.py",
                job_id="dockhand_update",
                job_name="Dockhand Image Check",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once()

    # --- #zero-ops tests ---

    async def test_session_rollover_error_routes_to_zero_ops(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "⚠️ Rollover failed", {})):
            await bshed.dispatch_scheduled_prompt(
                "[INTERNAL_SESSION_ROLLOVER]",
                job_id="session_rollover",
                job_name="Daily Session Rollover",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("⚠️ Rollover failed")

    async def test_memory_doctor_with_issues_routes_to_zero_ops(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "🚨 Security Air-Gap Violations", None)):
            await bshed.dispatch_scheduled_prompt(
                "Run memory doctor",
                job_id="memory_doctor",
                job_name="Memory Doctor Audit",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("🚨 Security Air-Gap Violations")

    async def test_antigravity_check_update_routes_to_zero_ops(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "🚀 Antigravity CLI v2.5 available", None)):
            await bshed.dispatch_scheduled_prompt(
                "update_antigravity.py",
                job_id="antigravity_check",
                job_name="Antigravity CLI Check",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once()

    async def test_bridge_watchdog_error_routes_to_zero_ops(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "⚠️ Bridge lag high", None)):
            await bshed.dispatch_scheduled_prompt(
                "bridge_watchdog",
                job_id="bridge_watchdog",
                job_name="Bridge Liveness Watchdog",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("⚠️ Bridge lag high")

    async def test_monthly_hardcode_regex_audit_findings_route_to_zero_ops(self):
        rep = "🔍 **Hardcoded Rule & Brittle Regex Audit**\n⚠️ **Findings Detected:** 1 total (1 High)\n### 🔴 [HIGH] `tools/test.py:10`"
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, rep, None)):
            await bshed.dispatch_scheduled_prompt(
                "code_audit",
                job_id="monthly_hardcode_regex_audit",
                job_name="Monthly Hardcoded Rule & Regex Audit",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with(rep)

    # --- #shopping tests ---

    async def test_ev9_monitor_digest_routes_to_shopping(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "🚗 **Weekly EV9 Digest**", None)):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py ev9",
                job_id="ev9_monitor",
                job_name="EV9 Listing Monitor",
                bot=self.mock_bot,
            )
            ch = self.channels.get(SHOPPING_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("🚗 **Weekly EV9 Digest**", file=None)

    def test_amazon_order_listener_fallback_to_shopping(self):
        from tools.amazon_order_listener import load_env
        env = load_env()
        channel_id = env.get("SHOPPING_CHANNEL") or env.get("SHOPPING_CHANNEL_ID") or env.get("AMAZON_ORDER_CHANNEL") or "1544955538033348618"
        self.assertEqual(channel_id, "1544955538033348618")

    # --- #vault tests (including kalshi_midday_heating & quant_pregame_sweep) ---

    async def test_kalshi_sweeps_route_to_vault_and_stay_silent_on_success(self):
        for jid, jname in [
            ("kalshi_paper_bot", "Kalshi Weather Quant Paper Bot"),
            ("kalshi_midday_heating", "Kalshi Midday Heating & Euro Pre-Kickoff Sweep"),
            ("quant_pregame_sweep", "Quant US Pre-Game Lineup & Afternoon Peak Sweep"),
        ]:
            bshed.LAST_SCHEDULED_DISPATCH.clear()
            with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "Orders executed", None)):
                await bshed.dispatch_scheduled_prompt(
                    "sidecars.py kalshi",
                    job_id=jid,
                    job_name=jname,
                    bot=self.mock_bot,
                )
                ch = self.channels.get(VAULT_CHANNEL_ID)
                if ch:
                    ch.send.assert_not_called()

    async def test_kalshi_sweeps_alert_vault_on_error(self):
        for jid, jname in [
            ("kalshi_midday_heating", "Kalshi Midday Heating & Euro Pre-Kickoff Sweep"),
            ("quant_pregame_sweep", "Quant US Pre-Game Lineup & Afternoon Peak Sweep"),
        ]:
            bshed.LAST_SCHEDULED_DISPATCH.clear()
            with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "NOAA timeout", None)):
                await bshed.dispatch_scheduled_prompt(
                    "sidecars.py kalshi",
                    job_id=jid,
                    job_name=jname,
                    bot=self.mock_bot,
                )
                ch = self.channels.get(VAULT_CHANNEL_ID)
                self.assertIsNotNone(ch)
                self.assertTrue(ch.send.awaited)

    # --- Silent-When-Nominal Suppression Tests ---

    async def test_nas_logs_silent_when_total_issues_zero(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "0 errors across all containers", {"total_issues": 0})):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py nas_logs",
                job_id="nas_logs",
                job_name="NAS Log Review",
                bot=self.mock_bot,
                turn_queue=self.mock_queue,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            if ch:
                ch.send.assert_not_called()
            self.mock_queue.put.assert_not_called()

    async def test_memory_doctor_silent_when_nominal(self):
        report = "Memory store is healthy and air-gapped. ✅\n- **Public Engineering Documents:** `10`\n- **Private Homelab Documents:** `20`"
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, report, None)):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py doctor",
                job_id="memory_doctor",
                job_name="Memory Doctor Audit",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            if ch:
                ch.send.assert_not_called()

    async def test_monthly_hardcode_regex_audit_silent_when_nominal(self):
        report = (
            "🔍 **Hardcoded Rule & Brittle Regex Audit** (`/workspace/tools`)\n"
            "✅ **Zero Fragile Static Heuristics Detected.** Codebase adheres to dynamic state and LLM intent routing protocols."
        )
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, report, None)):
            await bshed.dispatch_scheduled_prompt(
                "code_audit",
                job_id="monthly_hardcode_regex_audit",
                job_name="Monthly Hardcoded Rule & Regex Audit",
                bot=self.mock_bot,
            )
            ch = self.channels.get(ZERO_OPS_CHANNEL_ID)
            if ch:
                ch.send.assert_not_called()

    async def test_marketing_sweep_silent_when_zero_senders(self):
        msg = "📬 **Biweekly Marketing Report** — Oct 30\nNo promotional emails in the last 2 weeks. ✅"
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, msg, {"should_post": False})):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py marketing",
                job_id="marketing_sweep",
                job_name="Biweekly Marketing Sweep",
                bot=self.mock_bot,
            )
            for ch in self.channels.values():
                ch.send.assert_not_called()

    # --- Plex Audiobook Enricher Interceptor Fast-Path Tests ---

    async def test_plex_audiobook_enricher_silent_when_nominal(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, "", {"newly_enriched": []})):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py plex_audiobook_enricher --quiet",
                job_id="plex_audiobook_enricher",
                job_name="Weekly Plex Audiobook Enricher & Cover Badges",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            if ch:
                ch.send.assert_not_called()

    async def test_plex_audiobook_enricher_posts_to_homelab_when_books_enriched(self):
        report = "Plex Audiobooks verified: 50/50 books badged & locked (2 newly enriched, 0 split sub-albums merged).\nNewly Enriched: Book A, Book B"
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(True, report, {"newly_enriched": ["Book A", "Book B"]})):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py plex_audiobook_enricher --quiet",
                job_id="plex_audiobook_enricher",
                job_name="Weekly Plex Audiobook Enricher & Cover Badges",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with(report)

    async def test_plex_audiobook_enricher_posts_to_homelab_on_error(self):
        with patch("tools.bridge_scheduler._run_sidecar_job_async", return_value=(False, "⚠️ Plex server unreachable", None)):
            await bshed.dispatch_scheduled_prompt(
                "sidecars.py plex_audiobook_enricher --quiet",
                job_id="plex_audiobook_enricher",
                job_name="Weekly Plex Audiobook Enricher & Cover Badges",
                bot=self.mock_bot,
            )
            ch = self.channels.get(HOMELAB_CHANNEL_ID)
            self.assertIsNotNone(ch)
            ch.send.assert_awaited_once_with("⚠️ Plex server unreachable")


if __name__ == "__main__":
    unittest.main()
