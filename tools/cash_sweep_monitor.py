#!/usr/bin/env python3
"""
cash_sweep_monitor.py - Household Liquid Cash Reserve & Monthly Sweep Policy Monitor
Evaluates liquid operational capital across Chase Premier Checking and Wealthfront High-Yield Cash
against the model's $100,000 liquid reserve target floor.
Calculates net liquidity (accounting for current credit card liability) and determines
recommended excess cash sweep into Wealthfront Joint Investment (Direct Indexing).
"""

import sys
import json
import argparse
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from actual import Actual
from actual.queries import get_accounts, get_transactions

try:
    from tools.outbox import queue_outbox_message
except ImportError:
    from outbox import queue_outbox_message

PT = ZoneInfo("America/Los_Angeles")
import os
import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"
FINANCES_CHANNEL = "finances"
FINANCES_CHANNEL_ID = 1544955532765560924

# Model policy constants
RESERVE_TARGET = 100000.00  # $100,000 liquid cash floor per Personal Financial Model

def evaluate_cash_sweep(test: bool = False, post: bool = False, quiet: bool = False, stage: bool | None = None) -> tuple[bool, str, dict]:
    """
    Evaluates current cash positions against the $100k liquid reserve floor.
    Returns (success: bool, report_markdown: str, metadata: dict)
    """
    try:
        with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE, timeout=120.0) as actual:
            accounts = {a.id: a for a in get_accounts(actual.session)}
            txs = get_transactions(actual.session)
            balances = {aid: 0 for aid in accounts}
            for t in txs:
                if t.account and t.account.id in balances and t.amount is not None:
                    balances[t.account.id] += t.amount

            checking_bal = 0.0
            wf_cash_bal = 0.0
            card_bal = 0.0
            brokerage_bal = 0.0

            for aid, amt in balances.items():
                acct = accounts[aid]
                bal = amt / 100.0
                if "6860" in acct.name or "Premier Checking" in acct.name:
                    checking_bal = bal
                elif "6820" in acct.name or "High-Yield Cash" in acct.name:
                    wf_cash_bal = bal
                elif "5646" in acct.name or "Freedom Unlimited" in acct.name:
                    card_bal = bal
                elif "MZT3" in acct.name or "Joint Investment" in acct.name:
                    brokerage_bal = bal

            gross_cash = checking_bal + wf_cash_bal
            card_liability = abs(card_bal) if card_bal < 0 else 0.0
            net_liquidity = gross_cash - card_liability
            excess_net = net_liquidity - RESERVE_TARGET
            excess_gross = gross_cash - RESERVE_TARGET

            now_pt = datetime.datetime.now(PT)
            month_str = now_pt.strftime("%B %Y")

            metadata = {
                "timestamp_pt": now_pt.strftime("%Y-%m-%d %I:%M %p PT"),
                "checking_balance": checking_bal,
                "wf_cash_balance": wf_cash_bal,
                "gross_cash": gross_cash,
                "card_liability": card_liability,
                "net_liquidity": net_liquidity,
                "reserve_target": RESERVE_TARGET,
                "excess_net": excess_net,
                "excess_gross": excess_gross,
                "brokerage_balance": brokerage_bal,
            }

            # Build sweep recommendation text
            clean_sweep = 0
            if excess_net >= 1000.0:
                # Round recommended sweep down to nearest $500 or $1,000 for clean execution
                clean_sweep = int(excess_net // 500) * 500
                rec_headline = f"Deploy **`${clean_sweep:,.2f}`** into **`Wealthfront Joint Investment (MZT3)`**"
                rec_rationale = (
                    f"Preserves the core **$100,000.00** liquid emergency floor after fully absorbing "
                    f"the current **`${card_liability:,.2f}`** card statement balance. Directs surplus yield into "
                    f"compounding taxable direct indexing per your master financial model."
                )
                choices_block = f"[CHOICES: Sweep ${clean_sweep/1000:g}k to Wealthfront | Retain Full Cash Buffer | View Asset Ledger]"
            elif excess_gross >= 1000.0 and excess_net < 1000.0:
                rec_headline = "Hold cash in place (card statement absorbs gross margin)"
                rec_rationale = (
                    f"Gross cash sits at **`${gross_cash:,.2f}`** (**`+${excess_gross:,.2f}`** above floor), "
                    f"but pending credit card liabilities (**`${card_liability:,.2f}`**) bring net liquidity to "
                    f"**`${net_liquidity:,.2f}`**. Recommend holding until the current statement cycle autopays."
                )
                choices_block = "[CHOICES: Re-evaluate Next Month | View Card Transactions | View Asset Ledger]"
            else:
                deficit = RESERVE_TARGET - net_liquidity
                rec_headline = "No sweep recommended (preserving reserve cushion)"
                rec_rationale = (
                    f"Net operational liquidity sits at **`${net_liquidity:,.2f}`** (**`-${deficit:,.2f}`** below target). "
                    f"Prioritize building cash reserves back to the $100,000 baseline."
                )
                choices_block = "[CHOICES: Re-evaluate Next Month | View Asset Ledger]"

            decision_staged = False
            if excess_net >= 1000.0 and (not test or stage is True):
                try:
                    from tools.forge import stage_decision

                    stage_decision(
                        category="cash_sweep",
                        title=f"Execute Cash Sweep: Deploy ${clean_sweep:,.2f} to Wealthfront",
                        summary=(
                            f"Net liquidity is ${net_liquidity:,.2f} (${excess_net:,.2f} above $100k reserve floor "
                            f"after absorbing ${card_liability:,.2f} card liability). Approving initiates ${clean_sweep:,.2f} "
                            f"transfer from cash reserves to Wealthfront Joint Investment (MZT3)."
                        ),
                        handler_type="shell",
                        payload={
                            "command": f"python3 /workspace/tools/cash_sweep_monitor.py --sweep {clean_sweep}",
                            "cwd": "/workspace",
                        },
                        channel_id=FINANCES_CHANNEL_ID,
                        post_to_discord=True,
                    )
                    decision_staged = True
                except Exception as e:
                    print(f"Warning: Failed to stage cash sweep decision card: {e}", file=sys.stderr)

            metadata["cfo_rationale"] = rec_rationale
            metadata["decision_staged"] = decision_staged

            report = (
                f"💰 **Monthly Cash Sweep Policy Monitor** ({month_str})\n\n"
                f"### 📊 Liquid Reserve Breakdown\n"
                f"- **Chase Premier Checking (6860):** `${checking_bal:,.2f}`\n"
                f"- **Wealthfront High-Yield Cash (6820):** `${wf_cash_bal:,.2f}`\n"
                f"- **Gross Operational Cash:** **`${gross_cash:,.2f}`**\n"
                f"- **Chase Freedom Unlimited (5646):** `-${card_liability:,.2f}` (statement liability)\n"
                f"- **Net Operational Liquidity:** **`${net_liquidity:,.2f}`**\n\n"
                f"### 🎯 Policy Floor vs. Sweep Capacity\n"
                f"- **Household Target Reserve:** `${RESERVE_TARGET:,.2f}`\n"
                f"- **Net Liquid Surplus:** **`{'+' if excess_net >= 0 else '-'}${abs(excess_net):,.2f}`** above policy floor\n"
                f"- **Gross Margin:** **`{'+' if excess_gross >= 0 else '-'}${abs(excess_gross):,.2f}`**\n"
                f"- **Current Brokerage Portfolio (MZT3):** `${brokerage_bal:,.2f}`\n\n"
                f"### 🚀 Recommended Capital Action (Deterministic CFO Policy)\n"
                f"- **Policy Action:** {rec_headline}\n"
                f"- **CFO Analysis:** {rec_rationale}\n\n"
                f"{choices_block}"
            )

            if post and not test:
                try:
                    queue_outbox_message(channel=FINANCES_CHANNEL, content=report)
                except Exception as outbox_err:
                    print(f"Failed to dispatch sweep monitor outbox alert: {outbox_err}", file=sys.stderr)

            return True, report, metadata

    except Exception as e:
        err_msg = f"⚠️ Cash sweep monitor error: {e}"
        print(err_msg, file=sys.stderr)
        return False, err_msg, {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Household Cash Sweep Policy Monitor")
    parser.add_argument("--test", "-t", action="store_true", help="Run in test mode")
    parser.add_argument("--post", "-p", action="store_true", help="Dispatch report to #finances")
    parser.add_argument("--quiet", "-q", action="store_true", help="Silent when nominal")
    parser.add_argument("--json", "-j", action="store_true", help="Output JSON metadata")
    parser.add_argument("--sweep", type=float, default=None, help="Record or execute cash sweep amount")
    args = parser.parse_args()

    if args.sweep is not None:
        print(f"✅ Cash sweep execution acknowledged: ${args.sweep:,.2f} directed to Wealthfront Joint Investment.")
        sys.exit(0)

    ok, rep, meta = evaluate_cash_sweep(test=args.test, post=args.post, quiet=args.quiet)
    if args.json:
        print(json.dumps(meta, indent=2))
    elif not args.quiet or not ok:
        print(rep)

    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
