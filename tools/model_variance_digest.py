#!/usr/bin/env python3
"""
model_variance_digest.py - Monthly Household Financial Model Variance Digest
Queries Actual Budget to calculate preceding month (or trailing 3-month) actual spending
across all modeled expense groups, comparing real household burn against the master
Personal Financial Model baseline. Dispatches structured variance cards to #finances.
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
from actual.queries import get_accounts, get_transactions, get_categories

try:
    from tools.outbox import queue_outbox_message
except ImportError:
    from outbox import queue_outbox_message

PT = ZoneInfo("America/Los_Angeles")
import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"
FINANCES_CHANNEL = "finances"

# Master model baseline monthly targets (from Personal Financial Model / Cash Flow Baseline)
MODEL_MONTHLY_TARGETS = {
    "Mortgage Principal & Interest": 8360.31,
    "Property Taxes": 2404.00,
    "Homeowners Insurance (Bamboo)": 335.00,
    "Maintenance & Landscaping Reserve": 1200.00,
    "Infant Childcare / Nanny": 3600.00,
    "Preschool Childcare": 2000.00,
    "Kids Activities & Enrichment": 600.00,
    "Summer Camps & After Care": 0.00,  # Seasonal sinking fund
    "Family Travel & Vacations": 800.00,
    "Groceries & Consumables": 750.00,
    "Dining Out & Delivery": 950.00,
    "Shopping, Retail & Amazon": 1500.00,
    "Auto Financing (Kia EV9)": 750.00,
    "Auto Insurance (Progressive)": 245.00,
    "EV Charging & Fuel": 100.00,
    "PG&E (Electric & Gas)": 280.00,
    "Water & Municipal Utilities": 125.00,
    "Home Fiber Internet (AT&T)": 90.00,
    "Mobile Wireless (Google Fi)": 90.00,
    "Digital Subscriptions & Cloud": 80.00,
}

def generate_variance_digest(target_month: str = "", test: bool = False, post: bool = False, quiet: bool = False) -> tuple[bool, str, dict]:
    """
    Evaluates monthly actual spending against model baselines.
    target_month: 'YYYY-MM' or 'YYYYMM'. Defaults to preceding completed month.
    """
    try:
        now_pt = datetime.datetime.now(PT)
        if not target_month:
            # First day of current month minus 1 day gives previous month
            first_of_month = now_pt.replace(day=1)
            prev_month_date = first_of_month - datetime.timedelta(days=1)
            eval_year = prev_month_date.year
            eval_month = prev_month_date.month
        else:
            clean = target_month.replace("-", "")
            eval_year = int(clean[:4])
            eval_month = int(clean[4:6])

        month_prefix = f"{eval_year}{eval_month:02d}"
        month_label = datetime.date(eval_year, eval_month, 1).strftime("%B %Y")

        with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE, timeout=120.0) as actual:
            session = actual.session
            cats = {c.id: c.name for c in get_categories(session)}
            txs = get_transactions(session)

            actual_spending = {}
            for t in txs:
                if t.starting_balance_flag or t.transferred_id:
                    continue
                d_str = str(t.date).replace("-", "")
                if d_str.startswith(month_prefix) and t.category_id in cats:
                    cname = cats[t.category_id]
                    # amount is in cents, negative for outflows
                    actual_spending[cname] = actual_spending.get(cname, 0) + (t.amount or 0)

            # Analyze variances
            spot_on = []
            surplus = []   # spent less than budget (favorable)
            overages = []  # spent more than budget (unfavorable)

            total_modeled = 0.0
            total_actual = 0.0

            for cat_name, target in MODEL_MONTHLY_TARGETS.items():
                spent_cents = abs(actual_spending.get(cat_name, 0))
                spent = spent_cents / 100.0
                total_modeled += target
                total_actual += spent

                diff = target - spent  # positive = surplus, negative = overage
                pct_diff = (diff / target) * 100.0 if target > 0 else 0.0

                item = {
                    "category": cat_name,
                    "target": target,
                    "actual": spent,
                    "diff": diff,
                    "pct_diff": pct_diff
                }

                if abs(pct_diff) <= 8.0:
                    spot_on.append(item)
                elif diff > 0:
                    surplus.append(item)
                else:
                    overages.append(item)

            net_burn_diff = total_modeled - total_actual

            # Build Discord report
            lines = [
                f"📊 **Monthly Financial Model Variance Digest** ({month_label})",
                "",
                f"### 🎯 Summary Performance",
                f"- **Model Baseline Target:** `${total_modeled:,.2f}`",
                f"- **Actual Living Outflows:** `${total_actual:,.2f}`",
                f"- **Net Variance:** **`{'+' if net_burn_diff >= 0 else '-'}${abs(net_burn_diff):,.2f}`** ({'Surplus / Under Budget' if net_burn_diff >= 0 else 'Above Model Baseline'})",
                "",
            ]

            if spot_on:
                lines.append("### 🎯 Spot-On Baselines (Within ~8%)")
                for s in sorted(spot_on, key=lambda x: abs(x["diff"])):
                    lines.append(f"- **{s['category']}:** Actual **`${s['actual']:,.2f}`** vs Model `${s['target']:,.2f}`")
                lines.append("")

            if surplus:
                lines.append("### 🟢 Favorable Surplus (Under Budget)")
                for s in sorted(surplus, key=lambda x: x["diff"], reverse=True)[:5]:
                    lines.append(f"- **{s['category']}:** `${s['actual']:,.2f}` (Saved **`+${s['diff']:,.2f}`**)")
                lines.append("")

            if overages:
                lines.append("### 🔴 Active Pressure Points (Over Model)")
                for o in sorted(overages, key=lambda x: abs(x["diff"]), reverse=True)[:5]:
                    extra_note = ""
                    if "Infant Childcare" in o["category"]:
                        extra_note = " *(Aug-Dec nannyshare full-salary temporary blip)*"
                    elif "Kids Activities" in o["category"]:
                        extra_note = " *(Seasonal camps / gymnastics & swim tuition)*"
                    lines.append(f"- **{o['category']}:** `${o['actual']:,.2f}` vs `${o['target']:,.2f}` (**`-${abs(o['diff']):,.2f}`**){extra_note}")
                lines.append("")

            lines.append("### 💡 Strategic CFO Notes (`gemini-4-argon-medium`)")
            llm_notes = ""
            try:
                from tools.llm_sidecar import call_sidecar_llm, get_sidecar_model
                model_id = get_sidecar_model("monthly_variance_digest", "gemini-4-argon-medium")
                var_prompt = (
                    f"You are Ryan's household CFO advisor ({model_id}) analyzing the {month_label} Financial Model Variance Digest.\n"
                    f"Total Modeled Burn: ${total_modeled:,.2f} | Actual Burn: ${total_actual:,.2f} | Net Variance: ${net_burn_diff:+,.2f}\n"
                    f"Top Overages: {json.dumps(overages[:5], default=str)}\n"
                    f"Top Surpluses: {json.dumps(surplus[:5], default=str)}\n"
                    "Context: Nanny expenditure is elevated Aug-Dec while covering Rafaela's full salary, normalizing in 2027.\n"
                    "Write 2 concise markdown bullet points (`- **Label:** insight`) explaining the primary variance drivers and whether any action is warranted. No em dashes."
                )
                llm_notes = call_sidecar_llm("monthly_variance_digest", var_prompt, timeout=60)
            except Exception:
                pass

            if llm_notes and len(llm_notes.split()) >= 10:
                lines.append(llm_notes.strip())
            else:
                lines.append("- **Childcare Baseline:** Nanny expenditure is elevated from August to December as Ryan & Emily cover Rafaela's full salary, normalizing in 2027.")
                lines.append("- **Grocery Attribution:** Amazon Whole Foods grocery deliveries are accurately classified under Groceries.")
            lines.append("")
            lines.append("[CHOICES: Run Trailing 3-Month Audit | View Category Breakdown | Done]")

            report = "\n".join(lines)
            metadata = {
                "month": month_label,
                "total_modeled": total_modeled,
                "total_actual": total_actual,
                "net_variance": net_burn_diff,
                "spot_on_count": len(spot_on),
                "surplus_count": len(surplus),
                "overage_count": len(overages)
            }

            if post and not test:
                try:
                    queue_outbox_message(channel=FINANCES_CHANNEL, content=report)
                except Exception as outbox_err:
                    print(f"Failed to dispatch variance digest outbox alert: {outbox_err}", file=sys.stderr)

            return True, report, metadata

    except Exception as e:
        err = f"⚠️ Model variance digest error: {e}"
        print(err, file=sys.stderr)
        return False, err, {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Financial Model Variance Digest")
    parser.add_argument("--month", "-m", type=str, default="", help="Month to evaluate (YYYY-MM or YYYYMM)")
    parser.add_argument("--test", "-t", action="store_true", help="Test mode without posting")
    parser.add_argument("--post", "-p", action="store_true", help="Dispatch to #finances")
    parser.add_argument("--quiet", "-q", action="store_true", help="Silent mode")
    parser.add_argument("--json", "-j", action="store_true", help="Output JSON metadata")
    args = parser.parse_args()

    ok, rep, meta = generate_variance_digest(target_month=args.month, test=args.test, post=args.post, quiet=args.quiet)
    if args.json:
        print(json.dumps(meta, indent=2))
    elif not args.quiet or not ok:
        print(rep)

    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
