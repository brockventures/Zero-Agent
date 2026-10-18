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

# Model policy constants
RESERVE_TARGET = 100000.00  # $100,000 liquid cash floor per Personal Financial Model

def evaluate_cash_sweep(test: bool = False, post: bool = False, quiet: bool = False) -> tuple[bool, str, dict]:
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

            try:
                from tools.llm_sidecar import call_sidecar_llm, get_sidecar_model
                model_id = get_sidecar_model("cash_sweep_monitor", "gemini-4-argon-medium")
                cfo_prompt = (
                    f"You are Ryan's household CFO advisor ({model_id}) reviewing the monthly liquid cash sweep position.\n"
                    f"Metrics:\n"
                    f"- Chase Checking: ${checking_bal:,.2f}\n"
                    f"- Wealthfront HY Cash: ${wf_cash_bal:,.2f}\n"
                    f"- Gross Cash: ${gross_cash:,.2f}\n"
                    f"- Credit Card Statement Liability: -${card_liability:,.2f}\n"
                    f"- Net Liquidity: ${net_liquidity:,.2f} (Target Floor: ${RESERVE_TARGET:,.2f})\n"
                    f"- Recommended Action: {rec_headline}\n\n"
                    "Write a crisp 2-sentence CFO commentary explaining the net liquidity posture and capital deployment trade-off. "
                    "Do not use bullet points or em dashes. Output ONLY the 2 sentences."
                )
                llm_rationale = call_sidecar_llm("cash_sweep_monitor", cfo_prompt, timeout=60)
                if llm_rationale and len(llm_rationale.split()) >= 10:
                    rec_rationale = llm_rationale.strip()
            except Exception:
                pass

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
                f"### 🚀 Recommended Capital Action (`gemini-4-argon-medium`)\n"
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
    args = parser.parse_args()

    ok, rep, meta = evaluate_cash_sweep(test=args.test, post=args.post, quiet=args.quiet)
    if args.json:
        print(json.dumps(meta, indent=2))
    elif not args.quiet or not ok:
        print(rep)

    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
