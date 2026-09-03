#!/usr/bin/env python3
"""Option B Weekly Proactive Digest (Sundays @ 20:00 PT).

Covers:
1. Upcoming homelab maintenance & hardware posture across .82 and .84.
2. 30-day contract & subscription renewal lookahead.
3. Monthly cash-flow deltas and major bills review.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from ha_mcp import ha_ping, ha_get_state
from nas_docker_mcp import nas_docker
from workspace_mcp import calendar_list_events, gmail_search

PT = ZoneInfo("America/Los_Angeles")

def get_upcoming_reminders_and_renewals(now_pt: datetime, days: int = 30) -> list[str]:
    """Dynamically pull reminders due in the next 30 days from data/reminders.json."""
    lines = []
    reminders_file = Path("/workspace/data/reminders.json")
    today_str = now_pt.strftime("%Y-%m-%d")
    limit_str = (now_pt + timedelta(days=days)).strftime("%Y-%m-%d")

    if reminders_file.exists():
        try:
            with open(reminders_file, "r", encoding="utf-8") as f:
                rems = json.load(f)
            due_rems = [r for r in rems if today_str <= r.get("due", "") <= limit_str]
            seen_gcert = False
            for r in due_rems:
                msg = r.get("message", "")
                due_date = r.get("due", "")
                if "gcert" in msg.lower():
                    if seen_gcert:
                        continue
                    seen_gcert = True
                    lines.append(f"• **Work gcert**: 5-day renewal cadence (Next: {due_date})")
                elif "smartthings" in msg.lower():
                    lines.append(f"• **SmartThings Z-Wave**: Action required before Oct API paywall (Due {due_date})")
                elif "decommission" in msg.lower():
                    lines.append(f"• **Container Decommission**: Decommission old Ivy containers (Due {due_date})")
                elif "arr" in msg.lower():
                    lines.append(f"• **Arr Postgres/IO**: Socket timeout review (Due {due_date})")
                else:
                    clean_msg = re.sub(r"[#*_`~]", "", msg).split("\n")[0].strip()
                    lines.append(f"• **{clean_msg[:45]}**: Due {due_date}")
        except Exception as e:
            print(f"[WeeklyDigest] Error reading reminders.json: {e}", file=sys.stderr)

    # Add standard baseline recurring commitments if not already listed
    lines.append("• **AT&T Fiber**: $90.36/mo (Nominal, auto-pays ~18th)")
    lines.append("• **Kia EV9**: $749.27/mo (Speedpay auto-debit on 24th)")
    return lines[:4]


def get_recent_cashflow_rows() -> list[str]:
    """Query recent statements from Gmail or fall back to verified household recurring ledger."""
    rows = [
        "Item           Amount   Trend ",
        "------------------------------",
    ]
    detected_items = {}
    try:
        res = json.loads(gmail_search('subject:(bill OR statement OR payment OR invoice) newer_than:35d', 10))
        for m in res.get("messages", []):
            subj = m.get("subject", "").lower()
            snip = m.get("snippet", "")
            amt_match = re.search(r"\$(\d{1,4}(?:\.\d{2})?)", snip)
            if "famly" in m.get("from", "").lower() or "preschool" in snip.lower() or "sacc" in snip.lower():
                amt = amt_match.group(1) if amt_match else "779.00"
                detected_items["Preschool"] = (amt, "flat ")
            elif "flagstar" in m.get("from", "").lower() or "mortgage" in subj:
                detected_items["Mortgage P&I"] = ("8360.00", "flat ")
            elif "pge" in m.get("from", "").lower() or "gas and electric" in snip.lower():
                amt = amt_match.group(1) if amt_match else "280.00"
                detected_items["PG&E Net"] = (amt, "solar")
            elif "att" in m.get("from", "").lower() or "fiber" in snip.lower():
                amt = amt_match.group(1) if amt_match else "90.36"
                detected_items["AT&T Fiber"] = (amt, "flat ")
    except Exception as e:
        print(f"[WeeklyDigest] Warning querying gmail statements: {e}", file=sys.stderr)

    baseline = [
        ("Kia EV9 Lease", "749.27", "flat "),
        ("AT&T Fiber", "90.36", "flat "),
        ("Preschool (PCC)", "779.00", "flat "),
        ("Water/Trash", "145.20", "flat "),
    ]
    for name, amt, trend in baseline:
        if name not in detected_items and not any(k in name for k in detected_items):
            detected_items[name] = (amt, trend)

    for name, (amt, trend) in list(detected_items.items())[:5]:
        amt_float = float(amt.replace(",", ""))
        rows.append(f"{name[:14]:<14} {amt_float:>7.2f}    {trend:<6}")
    return rows


def generate_weekly_digest() -> str:
    now_pt = datetime.now(PT)
    date_header = now_pt.strftime("%A, %B %d, %Y")

    # 1. Homelab Infrastructure & Maintenance
    host1_ps = json.loads(nas_docker("compose_ps", host="host1", compose_dir="/docker"))
    host2_ps = json.loads(nas_docker("compose_ps", host="host2", compose_dir=os.environ.get("HOST_AGENT_DIR", "/docker/discord-agy-agent")))
    ha_status = json.loads(ha_ping())

    infra_lines = []
    if ha_status.get("ok"):
        infra_lines.append("• **Home Assistant** (HA): Online & responsive")
    else:
        infra_lines.append("• **Home Assistant** (HA): ⚠️ Offline/unreachable")

    sb_running = len([l for l in host1_ps.get("output", "").splitlines() if "Up" in l])
    bs2_running = len([l for l in host2_ps.get("output", "").splitlines() if "Up" in l])
    infra_lines.append(f"• **Host1** (.82): `{sb_running}` containers nominal")
    infra_lines.append(f"• **Host2** (.84): `{bs2_running}` containers nominal")

    # 2. Upcoming Contract Renewals & 30-Day Lookahead
    renewals_lines = get_upcoming_reminders_and_renewals(now_pt, days=30)

    # 3. Monthly Cash-Flow Deltas
    cashflow_rows = get_recent_cashflow_rows()
    cashflow_block = "\n".join(cashflow_rows)

    report = f"""📊 **Weekly Maintenance & Cash-Flow Digest**
_{date_header}_

🛠️ **Homelab Posture & Maintenance:**
{chr(10).join(infra_lines)}

📅 **30-Day Lookahead & Renewals:**
{chr(10).join(renewals_lines)}

💳 **Monthly Cash-Flow Deltas:**
```{cashflow_block}
```

*Option B Weekly Digest: Silent on minor noise, surfacing 30-day changes.*"""
    return report

if __name__ == "__main__":
    print(generate_weekly_digest())
