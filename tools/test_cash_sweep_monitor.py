import sys
from pathlib import Path
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
#!/usr/bin/env python3
"""
test_cash_sweep_monitor.py - Unit tests for Cash Sweep Policy Monitor logic
"""

import unittest
from unittest.mock import patch, MagicMock
from tools.cash_sweep_monitor import evaluate_cash_sweep, RESERVE_TARGET

class TestCashSweepMonitor(unittest.TestCase):

    @patch("tools.cash_sweep_monitor.Actual")
    @patch("tools.cash_sweep_monitor.get_accounts")
    @patch("tools.cash_sweep_monitor.get_transactions")
    def test_evaluate_cash_sweep_surplus(self, mock_get_txs, mock_get_accts, mock_actual_cls):
        # Setup mock actual context manager
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        acct_chk = MagicMock(id="acct-chk")
        acct_chk.name = "Chase Premier Checking (6860)"
        acct_wfc = MagicMock(id="acct-wfc")
        acct_wfc.name = "Wealthfront High-Yield Cash (6820)"
        acct_crd = MagicMock(id="acct-crd")
        acct_crd.name = "Chase Freedom Unlimited (5646)"
        acct_mzt = MagicMock(id="acct-mzt")
        acct_mzt.name = "Wealthfront Joint Investment (MZT3)" 

        mock_get_accts.return_value = [acct_chk, acct_wfc, acct_crd, acct_mzt]

        # Checking: $30,000, WF Cash: $100,000, Card: -$10,000
        # Net liquidity: $120,000 -> Surplus: $20,000
        tx1 = MagicMock(account=acct_chk, amount=3000000)
        tx2 = MagicMock(account=acct_wfc, amount=10000000)
        tx3 = MagicMock(account=acct_crd, amount=-1000000)
        tx4 = MagicMock(account=acct_mzt, amount=35000000)

        mock_get_txs.return_value = [tx1, tx2, tx3, tx4]

        ok, report, meta = evaluate_cash_sweep(test=True, post=False, quiet=True)

        self.assertTrue(ok)
        self.assertEqual(meta["checking_balance"], 30000.0)
        self.assertEqual(meta["wf_cash_balance"], 100000.0)
        self.assertEqual(meta["gross_cash"], 130000.0)
        self.assertEqual(meta["card_liability"], 10000.0)
        self.assertEqual(meta["net_liquidity"], 120000.0)
        self.assertEqual(meta["excess_net"], 20000.0)
        self.assertIn("Deploy **`$20,000.00`** into **`Wealthfront Joint Investment (MZT3)`**", report)

    @patch("tools.cash_sweep_monitor.Actual")
    @patch("tools.cash_sweep_monitor.get_accounts")
    @patch("tools.cash_sweep_monitor.get_transactions")
    def test_evaluate_cash_sweep_deficit(self, mock_get_txs, mock_get_accts, mock_actual_cls):
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        acct_chk = MagicMock(id="acct-chk")
        acct_chk.name = "Chase Premier Checking (6860)"
        acct_wfc = MagicMock(id="acct-wfc")
        acct_wfc.name = "Wealthfront High-Yield Cash (6820)"
        acct_crd = MagicMock(id="acct-crd")
        acct_crd.name = "Chase Freedom Unlimited (5646)"
        acct_mzt = MagicMock(id="acct-mzt")
        acct_mzt.name = "Wealthfront Joint Investment (MZT3)" 

        mock_get_accts.return_value = [acct_chk, acct_wfc, acct_crd, acct_mzt]

        # Checking: $10,000, WF Cash: $70,000, Card: -$5,000
        # Net liquidity: $75,000 -> Deficit: $25,000
        tx1 = MagicMock(account=acct_chk, amount=1000000)
        tx2 = MagicMock(account=acct_wfc, amount=7000000)
        tx3 = MagicMock(account=acct_crd, amount=-500000)

        mock_get_txs.return_value = [tx1, tx2, tx3]

        ok, report, meta = evaluate_cash_sweep(test=True, post=False, quiet=True)

        self.assertTrue(ok)
        self.assertEqual(meta["net_liquidity"], 75000.0)
        self.assertEqual(meta["excess_net"], -25000.0)
        self.assertIn("No sweep recommended", report)

    def test_scheduler_quant_sweep_does_not_trigger_cash_sweep(self):
        import asyncio
        import tools.bridge_scheduler as bsched

        bsched.LAST_SCHEDULED_DISPATCH.clear()
        called_jobs = []

        async def fake_run_sidecar(job_id, job_name, func, *args, **kwargs):
            called_jobs.append(job_id)
            return True, "", {}

        with patch.object(bsched, "_run_sidecar_job_async", side_effect=fake_run_sidecar):
            asyncio.run(
                bsched.dispatch_scheduled_prompt(
                    prompt="Run the multi-venue quantitative paper trading expansion sweep across all 4 tracks: python3 /workspace/tools/sidecars.py quant",
                    job_name="Multi-Venue Quant Paper Trading Sweep",
                )
            )
            self.assertEqual(called_jobs, ["quant_expansion_sweep"])

            called_jobs.clear()
            asyncio.run(
                bsched.dispatch_scheduled_prompt(
                    prompt="Run monthly cash sweep policy evaluation using /workspace/tools/sidecars.py cash_sweep. Post liquid reserve status and recommended sweep amount to #finances.",
                    job_name="Monthly Cash Sweep Policy Monitor",
                )
            )
            self.assertEqual(called_jobs, ["cash_sweep_monitor"])


if __name__ == "__main__":
    unittest.main()
