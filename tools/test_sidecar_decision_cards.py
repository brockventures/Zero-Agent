#!/usr/bin/env python3
"""
test_sidecar_decision_cards.py - Unit tests for sidecar Decision Card staging & deterministic CFO policy logic.
Covers:
  - tools/cash_sweep_monitor.py: deterministic CFO rationale & SQLite Decision Card staging
  - tools/model_variance_digest.py: LLM gating (< $150 overages) & Decision Card staging
  - tools/meal_planner_proposal.py: Decision Card staging for weekly dinner lock-in
"""

import os
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Prioritize worktree directory over /workspace in sys.path
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, REPO_ROOT)

# Ensure worktree tools package is loaded rather than /workspace
for _m in list(sys.modules.keys()):
    if _m == "tools" or _m.startswith("tools."):
        del sys.modules[_m]

import tools
tools.__path__ = [os.path.join(REPO_ROOT, "tools")]

from tools.cash_sweep_monitor import evaluate_cash_sweep, FINANCES_CHANNEL_ID
from tools.model_variance_digest import generate_variance_digest

tools.__path__ = [os.path.join(REPO_ROOT, "tools")]

from tools.meal_planner_proposal import (
    stage_meal_proposal_decision,
    generate_proposal,
    SHOPPING_CHANNEL_ID,
)


# ==============================================================================
# 1. CASH SWEEP MONITOR TESTS
# ==============================================================================

class TestCashSweepDecisionCards:
    """Tests deterministic CFO policy and Decision Card staging for cash_sweep_monitor."""

    @patch("tools.cash_sweep_monitor.Actual")
    @patch("tools.cash_sweep_monitor.get_accounts")
    @patch("tools.cash_sweep_monitor.get_transactions")
    @patch("tools.forge.stage_decision")
    def test_cash_sweep_surplus_stages_decision_card(
        self, mock_stage, mock_get_txs, mock_get_accts, mock_actual_cls
    ):
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        acct_chk = MagicMock(id="chk")
        acct_chk.name = "Chase Premier Checking (6860)"
        acct_wfc = MagicMock(id="wfc")
        acct_wfc.name = "Wealthfront High-Yield Cash (6820)"
        acct_crd = MagicMock(id="crd")
        acct_crd.name = "Chase Freedom Unlimited (5646)"
        acct_mzt = MagicMock(id="mzt")
        acct_mzt.name = "Wealthfront Joint Investment (MZT3)"
        mock_get_accts.return_value = [acct_chk, acct_wfc, acct_crd, acct_mzt]

        # Checking: $25,000, WF Cash: $110,000, Card: -$5,000 -> Gross: $135k, Net: $130k, Surplus: $30k
        txs = [
            MagicMock(account=acct_chk, amount=2500000),
            MagicMock(account=acct_wfc, amount=11000000),
            MagicMock(account=acct_crd, amount=-500000),
            MagicMock(account=acct_mzt, amount=50000000),
        ]
        mock_get_txs.return_value = txs
        mock_stage.return_value = {"ok": True, "decision_id": "dec-sweep-1"}

        ok, report, meta = evaluate_cash_sweep(test=False, post=False, quiet=True)

        assert ok is True
        assert meta["excess_net"] == 30000.0
        assert meta["decision_staged"] is True
        assert "Deterministic CFO Policy" in report
        assert "Preserves the core **$100,000.00** liquid emergency floor" in meta["cfo_rationale"]

        # Verify Decision Card staged with correct payload and target channel
        mock_stage.assert_called_once()
        kws = mock_stage.call_args.kwargs
        assert kws["category"] == "cash_sweep"
        assert kws["channel_id"] == FINANCES_CHANNEL_ID
        assert kws["channel_id"] == 1544955532765560924
        assert kws["handler_type"] == "shell"
        assert kws["post_to_discord"] is True
        assert "python3 /workspace/tools/cash_sweep_monitor.py --sweep 30000" in kws["payload"]["command"]

    @patch("tools.cash_sweep_monitor.Actual")
    @patch("tools.cash_sweep_monitor.get_accounts")
    @patch("tools.cash_sweep_monitor.get_transactions")
    @patch("tools.forge.stage_decision")
    def test_cash_sweep_deficit_skips_decision_card(
        self, mock_stage, mock_get_txs, mock_get_accts, mock_actual_cls
    ):
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        acct_chk = MagicMock(id="chk")
        acct_chk.name = "Chase Premier Checking (6860)"
        acct_wfc = MagicMock(id="wfc")
        acct_wfc.name = "Wealthfront High-Yield Cash (6820)"
        acct_crd = MagicMock(id="crd")
        acct_crd.name = "Chase Freedom Unlimited (5646)"
        mock_get_accts.return_value = [acct_chk, acct_wfc, acct_crd]

        # Checking: $10,000, WF Cash: $70,000, Card: -$5,000 -> Net: $75k, Deficit: $25k
        txs = [
            MagicMock(account=acct_chk, amount=1000000),
            MagicMock(account=acct_wfc, amount=7000000),
            MagicMock(account=acct_crd, amount=-500000),
        ]
        mock_get_txs.return_value = txs

        ok, report, meta = evaluate_cash_sweep(test=False, post=False, quiet=True)

        assert ok is True
        assert meta["excess_net"] == -25000.0
        assert meta["decision_staged"] is False
        assert "No sweep recommended" in report
        assert "Prioritize building cash reserves back to the $100,000 baseline" in meta["cfo_rationale"]
        mock_stage.assert_not_called()

    @patch("tools.cash_sweep_monitor.Actual")
    @patch("tools.cash_sweep_monitor.get_accounts")
    @patch("tools.cash_sweep_monitor.get_transactions")
    def test_cash_sweep_never_calls_llm(
        self, mock_get_txs, mock_get_accts, mock_actual_cls
    ):
        """Verifies cash_sweep_monitor no longer imports or calls call_sidecar_llm."""
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        acct_chk = MagicMock(id="chk")
        acct_chk.name = "Chase Premier Checking (6860)"
        mock_get_accts.return_value = [acct_chk]
        mock_get_txs.return_value = [MagicMock(account=acct_chk, amount=12000000)]

        with patch("tools.llm_sidecar.call_sidecar_llm") as mock_llm:
            ok, report, meta = evaluate_cash_sweep(test=True, post=False, quiet=True)
            assert ok is True
            mock_llm.assert_not_called()


# ==============================================================================
# 2. MODEL VARIANCE DIGEST TESTS
# ==============================================================================

class TestModelVarianceDigestDecisionCards:
    """Tests LLM gating and Decision Card staging for model_variance_digest."""

    @patch("tools.model_variance_digest.Actual")
    @patch("tools.model_variance_digest.get_categories")
    @patch("tools.model_variance_digest.get_transactions")
    @patch("tools.forge.stage_decision")
    def test_model_variance_nominal_gates_llm_and_skips_decision_card(
        self, mock_stage, mock_get_txs, mock_get_cats, mock_actual_cls
    ):
        """When category overages do not exceed $150, LLM call is bypassed and no Decision Card is staged."""
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        cat_groceries = MagicMock(id="cat-groc")
        cat_groceries.name = "Groceries & Consumables"
        mock_get_cats.return_value = [cat_groceries]

        # Target for Groceries is $750. Actual spend is $800 ($50 overage, <= $150 threshold).
        tx = MagicMock(
            starting_balance_flag=False,
            transferred_id=None,
            date="2026-09-15",
            category_id="cat-groc",
            amount=-80000,
        )
        mock_get_txs.return_value = [tx]

        with patch("tools.llm_sidecar.call_sidecar_llm") as mock_llm:
            ok, report, meta = generate_variance_digest(
                target_month="2026-09", test=False, post=False, quiet=True
            )

            assert ok is True
            assert meta["actionable_overages_count"] == 0
            assert meta["decision_staged"] is False
            assert meta["llm_called"] is False
            mock_llm.assert_not_called()
            mock_stage.assert_not_called()
            assert "Deterministic Baseline" in report
            assert "All living categories remained within the $150 review threshold" in report

    @patch("tools.model_variance_digest.Actual")
    @patch("tools.model_variance_digest.get_categories")
    @patch("tools.model_variance_digest.get_transactions")
    @patch("tools.forge.stage_decision")
    def test_model_variance_material_overage_calls_llm_and_stages_decision_card(
        self, mock_stage, mock_get_txs, mock_get_cats, mock_actual_cls
    ):
        """When category overage exceeds $150, LLM is called and SQLite Decision Card is staged in #finances."""
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        cat_dining = MagicMock(id="cat-dining")
        cat_dining.name = "Dining Out & Delivery"
        mock_get_cats.return_value = [cat_dining]

        # Target for Dining Out is $950. Actual spend is $1,600 ($650 overage, > $150).
        tx = MagicMock(
            starting_balance_flag=False,
            transferred_id=None,
            date="2026-09-20",
            category_id="cat-dining",
            amount=-160000,
        )
        mock_get_txs.return_value = [tx]
        mock_stage.return_value = {"ok": True, "decision_id": "dec-var-1"}

        custom_notes = "- **Dining Pressure:** Birthday dinner party drove one-time surplus spend in Dining Out."
        with patch("tools.llm_sidecar.call_sidecar_llm", return_value=custom_notes) as mock_llm:
            ok, report, meta = generate_variance_digest(
                target_month="2026-09", test=False, post=False, quiet=True
            )

            assert ok is True
            assert meta["actionable_overages_count"] >= 1
            assert meta["decision_staged"] is True
            assert meta["llm_called"] is True
            mock_llm.assert_called_once()

            # Verify Decision Card was staged with appropriate metadata
            mock_stage.assert_called_once()
            kws = mock_stage.call_args.kwargs
            assert kws["category"] == "budget_variance"
            assert kws["channel_id"] == FINANCES_CHANNEL_ID
            assert kws["channel_id"] == 1544955532765560924
            assert kws["handler_type"] == "shell"
            assert kws["post_to_discord"] is True
            assert "python3 /workspace/tools/model_variance_digest.py --audit" in kws["payload"]["command"]


# ==============================================================================
# 3. MEAL PLANNER PROPOSAL TESTS
# ==============================================================================

class TestMealPlannerProposalDecisionCards:
    """Tests SQLite Decision Card staging for weekly meal planner proposals."""

    @patch("tools.forge.stage_decision")
    def test_stage_meal_proposal_decision_payload(self, mock_stage):
        mock_stage.return_value = {"ok": True, "decision_id": "dec-meal-1"}
        sample_plan = {
            "meals": {
                "tuesday": {"recipe": {"name": "Crispy Sheet Pan Salmon Bowls"}},
                "thursday": {"recipe": {"name": "Kid-Friendly Chicken Quesadillas"}},
                "sunday": {"recipe": {"name": "Slow-Braised Pot Roast"}},
            }
        }

        res = stage_meal_proposal_decision(sample_plan)
        assert res is not None
        assert res["ok"] is True

        mock_stage.assert_called_once()
        kws = mock_stage.call_args.kwargs
        assert kws["category"] == "meal_plan"
        assert kws["title"] == "Lock In Weekly 3-Dinner Menu"
        assert kws["channel_id"] == SHOPPING_CHANNEL_ID
        assert kws["channel_id"] == 1544955538033348618
        assert kws["handler_type"] == "shell"
        assert kws["payload"]["command"] == "python3 /workspace/tools/meal_planner_proposal.py lock-in"
        assert kws["payload"]["cwd"] == "/workspace"
        assert kws["post_to_discord"] is True

    @patch("tools.meal_planner_proposal.stage_meal_proposal_decision")
    def test_generate_proposal_skips_stage_in_pytest_by_default(self, mock_stage_meal):
        """Under test runner, generate_proposal skips staging decision unless stage=True is explicitly passed."""
        # Active plan file mock
        with patch("tools.meal_planner_proposal.load_active_plan", return_value=None):
            with patch("tools.meal_planner_proposal.load_recipes", return_value=[
                {"name": "Dinner 1", "slug": "d1", "tags": ["Gluten-Free", "Quick"]},
                {"name": "Dinner 2", "slug": "d2", "tags": ["Gluten-Free", "Kid-Friendly"]},
                {"name": "Dinner 3", "slug": "d3", "tags": ["Gluten-Free", "Comfort Food"]},
            ]):
                with patch("tools.meal_planner_proposal.save_active_plan"):
                    # Default in pytest: stage is skipped
                    plan = generate_proposal(force=True)
                    assert plan is not None
                    mock_stage_meal.assert_not_called()

                    # Explicit stage=True: stages decision
                    generate_proposal(force=True, stage=True)
                    mock_stage_meal.assert_called_once()
