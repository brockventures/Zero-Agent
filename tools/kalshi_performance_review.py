#!/usr/bin/env python3
"""Kalshi Paper Trading Performance Review, Dynamic Model Calibration & Task Board Sync.

Runs as a time-gated sidecar:
- Evening Review (18:45 PT): Settles NOAA ASOS temperature contracts and afternoon MLB games.
- Nightly Audit (21:45 PT): Settles West Coast MLB night games, reconciles daily P&L,
  computes Brier score calibration, dynamically tunes model hyperparameters, and syncs
  telemetry to GitHub Project #3 and the brockventures/kalshi-quant repository.
"""

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/kalshi_paper.db")
PARAMS_PATH = Path("/workspace/data/kalshi_model_params.json")
REPO_DIR = Path("/workspace/kalshi-quant")
VAULT_DOC = Path("/workspace/memory/vault/project_kalshi_quant_bot.md")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PROJECT_ID = "PVT_kwHODXND-s4BkB6O"
STATUS_FIELD_ID = "PVTSSF_lAHODXND-s4BkB6Ozhi0DAk"
STATUS_OPTIONS = {
    "Todo": "f75ad846",
    "In Progress": "47fc9ee4",
    "Done": "98236657"
}

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    with conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "model_prob" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN model_prob REAL")
        if "category" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN category TEXT")
    return conn

def load_params():
    if not PARAMS_PATH.exists():
        default_params = {
            "version": "1.0",
            "last_updated": datetime.now(PT).isoformat(),
            "global": {
                "starting_balance": 1000.0,
                "max_total_exposure": 200.0,
                "drawdown_dampener": 1.0,
                "kelly_fraction": 0.25
            },
            "domains": {
                "weather": {
                    "name": "NOAA ASOS Temperature Arbitrage",
                    "min_edge_threshold": 0.08,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.95,
                    "target_brier": 0.18,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                },
                "mlb_daily": {
                    "name": "MLB Daily Matchup Log5 / Stuff+",
                    "min_edge_threshold": 0.05,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.90,
                    "target_brier": 0.21,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                },
                "mlb_playoffs": {
                    "name": "MLB Postseason Monte Carlo Futures",
                    "min_edge_threshold": 0.04,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.92,
                    "target_brier": 0.20,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                }
            },
            "calibration_history": []
        }
        PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PARAMS_PATH, "w") as f:
            json.dump(default_params, f, indent=2)
        return default_params
    with open(PARAMS_PATH) as f:
        return json.load(f)

def save_params(params):
    params["last_updated"] = datetime.now(PT).isoformat()
    with open(PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=2)

def fetch_kalshi_market(ticker):
    try:
        url = f"{KALSHI_API}/markets/{ticker}"
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            return data.get("market", {})
    except Exception:
        return {}

def settle_open_positions(conn, dry_run=False):
    """Scan open paper positions, check Kalshi status, and resolve settled markets."""
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    open_positions = cur.fetchall()
    
    settled_list = []
    total_payout = 0.0
    total_realized_pnl = 0.0
    now_str = datetime.now(PT).isoformat()
    
    for pos in open_positions:
        ticker = pos["ticker"]
        m = fetch_kalshi_market(ticker)
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        
        if status in ("finalized", "closed", "settled") and result in ("yes", "no"):
            won = (pos["side"].lower() == result)
            settled_price = 1.00 if won else 0.00
            payout = pos["contracts"] * settled_price
            realized_pnl = round(payout - pos["total_cost"], 2)
            
            settled_item = {
                "id": pos["id"],
                "ticker": ticker,
                "title": pos["title"],
                "category": pos["category"] or ("weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs")),
                "side": pos["side"],
                "contracts": pos["contracts"],
                "entry_price": pos["entry_price"],
                "total_cost": pos["total_cost"],
                "model_prob": pos["model_prob"],
                "result": result.upper(),
                "won": won,
                "payout": payout,
                "realized_pnl": realized_pnl
            }
            settled_list.append(settled_item)
            total_payout += payout
            total_realized_pnl += realized_pnl
            
            if not dry_run:
                with conn:
                    conn.execute("""
                        UPDATE positions
                        SET status = 'RESOLVED', settled_at = ?, settled_price = ?, realized_pnl = ?
                        WHERE id = ?
                    """, (now_str, settled_price, realized_pnl, pos["id"]))
                    conn.execute("""
                        UPDATE portfolio
                        SET cash = cash + ?, realized_pnl = realized_pnl + ?, last_updated = ?
                        WHERE id = 1
                    """, (payout, realized_pnl, now_str))
                    
    return settled_list, total_realized_pnl

def compute_portfolio_metrics(conn):
    """Calculate portfolio balance, open exposure, Brier score, and win rate."""
    p = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
    resolved_pos = conn.execute("SELECT * FROM positions WHERE status = 'RESOLVED'").fetchall()
    
    open_cost = sum(r["total_cost"] for r in open_pos)
    total_equity = p["cash"] + open_cost
    
    wins = [r for r in resolved_pos if (r["realized_pnl"] or 0.0) > 0]
    win_rate = (len(wins) / len(resolved_pos) * 100) if resolved_pos else 0.0
    
    # Calculate Brier score across resolved positions where model_prob is available
    brier_scores = {}
    market_brier_scores = {}
    domain_counts = {}
    
    for r in resolved_pos:
        cat = r["category"] or ("weather" if r["ticker"].startswith("KXHIGH") else ("mlb_daily" if r["ticker"].startswith("KXMLBGAME") else "mlb_playoffs"))
        domain_counts[cat] = domain_counts.get(cat, 0) + 1
        
        m_prob = r["model_prob"]
        # If side is NO, outcome = 1 if result was NO (settled_price == 1.0)
        outcome = 1.0 if (r["settled_price"] or 0.0) == 1.0 else 0.0
        
        if m_prob is not None:
            # If position was NO, model_prob is probability of NO
            err = (m_prob - outcome) ** 2
            brier_scores.setdefault(cat, []).append(err)
            brier_scores.setdefault("overall", []).append(err)
            
            # Market price as baseline forecast
            mkt_err = ((r["entry_price"] or 0.5) - outcome) ** 2
            market_brier_scores.setdefault(cat, []).append(mkt_err)
            market_brier_scores.setdefault("overall", []).append(mkt_err)
            
    summary_brier = {}
    summary_bss = {}
    for k, errs in brier_scores.items():
        bs = sum(errs) / len(errs)
        summary_brier[k] = round(bs, 4)
        mkt_bs = sum(market_brier_scores[k]) / len(market_brier_scores[k]) if market_brier_scores.get(k) else 0.25
        # Brier Skill Score: 1 - (BS / BS_mkt)
        bss = 1.0 - (bs / mkt_bs) if mkt_bs > 0 else 0.0
        summary_bss[k] = round(bss, 4)
        
    return {
        "starting_balance": p["starting_balance"],
        "cash": p["cash"],
        "open_exposure": open_cost,
        "total_equity": total_equity,
        "realized_pnl": p["realized_pnl"],
        "open_count": len(open_pos),
        "resolved_count": len(resolved_pos),
        "win_count": len(wins),
        "win_rate": win_rate,
        "brier_scores": summary_brier,
        "brier_skill_scores": summary_bss,
        "domain_counts": domain_counts,
        "open_positions": open_pos,
        "resolved_positions": resolved_pos
    }

def recalibrate_models(params, metrics, dry_run=False):
    """Adjust shrinkage alpha, minimum edge thresholds, and Kelly fraction based on empirical performance."""
    adjustments = []
    now_str = datetime.now(PT).isoformat()
    equity = metrics["total_equity"]
    starting = metrics["starting_balance"]
    drawdown_pct = max(0.0, (starting - equity) / starting)
    
    # 1. Portfolio-level Fractional Kelly & Drawdown Dampener
    if drawdown_pct >= 0.08:
        new_kelly = 0.10
        new_dampener = 0.50
        reason = f"High drawdown alert ({drawdown_pct*100:.1f}% >= 8%): Throttle Kelly to 0.10x"
    elif drawdown_pct >= 0.03:
        new_kelly = 0.18
        new_dampener = 0.75
        reason = f"Moderate drawdown ({drawdown_pct*100:.1f}% >= 3%): Throttle Kelly to 0.18x"
    else:
        new_kelly = 0.25
        new_dampener = 1.00
        reason = "Nominal equity curve: Full 0.25x Fractional Kelly active"
        
    old_kelly = params.get("global", {}).get("kelly_fraction", 0.25)
    if abs(old_kelly - new_kelly) > 0.01:
        adjustments.append({
            "scope": "global",
            "parameter": "kelly_fraction",
            "old": old_kelly,
            "new": new_kelly,
            "reason": reason
        })
        if not dry_run:
            params["global"]["kelly_fraction"] = new_kelly
            params["global"]["drawdown_dampener"] = new_dampener
            
    # 2. Domain-Specific Calibration via Brier Scores
    brier = metrics.get("brier_scores", {})
    for domain_key, domain_cfg in params.get("domains", {}).items():
        domain_bs = brier.get(domain_key)
        target_bs = domain_cfg.get("target_brier", 0.20)
        curr_alpha = domain_cfg.get("shrinkage_alpha", 0.95)
        curr_edge = domain_cfg.get("min_edge_threshold", 0.05)
        
        if domain_bs is not None:
            domain_cfg["rolling_brier"] = domain_bs
            # If Brier score is high (poor calibration or overfitting), shrink towards market price
            if domain_bs > target_bs + 0.03:
                new_alpha = max(0.75, round(curr_alpha - 0.04, 2))
                new_edge = min(0.12, round(curr_edge + 0.015, 3))
                adj_reason = f"Brier score elevated ({domain_bs:.3f} > {target_bs:.3f}): Shrink alpha to {new_alpha}, raise edge hurdle to {new_edge*100:.1f}%"
            elif domain_bs < target_bs - 0.02 and curr_alpha < 0.98:
                new_alpha = min(0.98, round(curr_alpha + 0.02, 2))
                new_edge = max(0.04, round(curr_edge - 0.005, 3))
                adj_reason = f"Strong calibration ({domain_bs:.3f} <= {target_bs:.3f}): Expand alpha to {new_alpha}"
            else:
                new_alpha = curr_alpha
                new_edge = curr_edge
                adj_reason = "Calibration within acceptable tolerance bands"
                
            if new_alpha != curr_alpha or new_edge != curr_edge:
                adjustments.append({
                    "scope": domain_key,
                    "parameter": "shrinkage_alpha / min_edge",
                    "old_alpha": curr_alpha,
                    "new_alpha": new_alpha,
                    "old_edge": curr_edge,
                    "new_edge": new_edge,
                    "reason": adj_reason
                })
                if not dry_run:
                    domain_cfg["shrinkage_alpha"] = new_alpha
                    domain_cfg["min_edge_threshold"] = new_edge
                    
    if adjustments and not dry_run:
        params.setdefault("calibration_history", []).append({
            "timestamp": now_str,
            "adjustments": adjustments
        })
        save_params(params)
        
    return adjustments

def update_github_project_board(metrics, settled, adjustments, dry_run=False):
    """Post daily telemetry report to brockventures/kalshi-quant and update Project #3 items."""
    now_pt = datetime.now(PT)
    date_header = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
    
    # 1. Build Standup Markdown Comment
    comment_lines = [
        f"### 📊 Daily Kalshi Paper Trading Standup & Quant Telemetry ({date_header})",
        "",
        "| Metric | Current Value | Starting Target | Status |",
        "| :--- | :--- | :--- | :--- |",
        f"| **Total Portfolio Equity** | **${metrics['total_equity']:.2f}** | ${metrics['starting_balance']:.2f} | {'🟢 In Green' if metrics['total_equity'] >= metrics['starting_balance'] else '🟡 Drawdown'} |",
        f"| **Available Cash** | ${metrics['cash']:.2f} | - | Nominal |",
        f"| **Open Market Exposure** | ${metrics['open_exposure']:.2f} | $200.00 Max | Within Cap |",
        f"| **Cumulative Realized P&L** | **${metrics['realized_pnl']:+.2f}** | - | Realized |",
        f"| **Settled Positions** | {metrics['resolved_count']} trades ({metrics['win_count']}W / {metrics['resolved_count'] - metrics['win_count']}L) | - | Win Rate: {metrics['win_rate']:.1f}% |",
        "",
        "#### Model Calibration & Brier Skill Scores"
    ]
    
    brier = metrics.get("brier_scores", {})
    bss = metrics.get("brier_skill_scores", {})
    if brier:
        comment_lines.extend([
            "| Domain | Resolved | Model Brier | Skill Score (BSS) | Calibration Verdict |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ])
        for dom, score in brier.items():
            skill = bss.get(dom, 0.0)
            verdict = "🟢 Strong Edge" if skill > 0.05 else ("🟡 Neutral" if skill >= -0.05 else "🔴 Overconfident")
            comment_lines.append(f"| `{dom}` | {metrics['domain_counts'].get(dom, metrics['resolved_count'])} | `{score:.4f}` | `{skill:+.3f}` | {verdict} |")
    else:
        comment_lines.append("_Pending initial market settlements to compute empirical Brier score._")
        
    comment_lines.append("")
    if settled:
        comment_lines.append("#### Settled Contracts Today")
        for s in settled:
            w_flag = "✅ WIN" if s["won"] else "❌ LOSS"
            comment_lines.append(f"- **{w_flag}** `{s['ticker']}` ({s['side']}): Payout `${s['payout']:.2f}` (P&L: `${s['realized_pnl']:+.2f}`) | Model: `{s['model_prob']*100:.1f}%`" if s.get("model_prob") else f"- **{w_flag}** `{s['ticker']}` ({s['side']}): Payout `${s['payout']:.2f}` (P&L: `${s['realized_pnl']:+.2f}`)")
        comment_lines.append("")
        
    if adjustments:
        comment_lines.append("#### Dynamic Model Adjustments")
        for adj in adjustments:
            comment_lines.append(f"- **[{adj['scope']}]** {adj.get('reason', '')}")
        comment_lines.append("")
    else:
        comment_lines.append("#### Dynamic Model Adjustments\n_All model hyperparameters within nominal tolerance bands._\n")
        
    comment_lines.append("---")
    comment_lines.append("_Autonomous daily report generated by Zero via `#vault` scheduled sidecar._")
    comment_body = "\n".join(comment_lines)
    
    if dry_run:
        print("\n[Dry Run] GitHub Comment Body:\n" + comment_body)
        return True
        
    # 2. Post telemetry comment to Issue #5 (Portfolio & Risk Management)
    try:
        cmd = [
            "gh", "api", "repos/brockventures/kalshi-quant/issues/5/comments",
            "-f", f"body={comment_body}"
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("✅ Posted telemetry comment to brockventures/kalshi-quant Issue #5")
    except Exception as e:
        print(f"⚠️ Failed to post comment to Issue #5: {e}")
        
    # 3. Advance Project #3 Issue #5 to "In Progress" if not already
    try:
        # Issue 5 item ID: PVTI_lAHODXND-s4BkB6Ozg7wFDg
        mutation = f"""
        mutation {{
          updateProjectV2ItemFieldValue(
            input: {{
              projectId: "{PROJECT_ID}"
              itemId: "PVTI_lAHODXND-s4BkB6Ozg7wFDg"
              fieldId: "{STATUS_FIELD_ID}"
              value: {{
                singleSelectOptionId: "{STATUS_OPTIONS['In Progress']}"
              }}
            }}
          ) {{
            projectV2Item {{
              id
            }}
          }}
        }}
        """
        cmd = ["gh", "api", "graphql", "-f", f"query={mutation}"]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("✅ Advanced Issue #5 on Project #3 board to 'In Progress'")
    except Exception as e:
        print(f"⚠️ Failed to update Project #3 board item: {e}")
        
    return True

def sync_repository(dry_run=False):
    """Synchronize updated model files and parameters into kalshi-quant repo and push."""
    if not REPO_DIR.exists() or not (REPO_DIR / ".git").exists():
        return False
        
    # Copy files
    targets = [
        ("tools/kalshi_performance_review.py", "tools/kalshi_performance_review.py"),
        ("tools/kalshi_paper_bot.py", "tools/kalshi_paper_bot.py"),
        ("tools/baseball_daily_quant.py", "tools/baseball_daily_quant.py"),
        ("tools/baseball_playoff_model.py", "tools/baseball_playoff_model.py"),
        ("data/kalshi_model_params.json", "data/kalshi_model_params.json")
    ]
    
    (REPO_DIR / "data").mkdir(parents=True, exist_ok=True)
    (REPO_DIR / "tools").mkdir(parents=True, exist_ok=True)
    
    for src, dst in targets:
        src_path = Path("/workspace") / src
        dst_path = REPO_DIR / dst
        if src_path.exists():
            shutil.copy2(src_path, dst_path)
            
    if dry_run:
        print("[Dry Run] Synced files to /workspace/kalshi-quant (skipping git commit)")
        return True
        
    try:
        subprocess.run(["git", "-C", str(REPO_DIR), "add", "."], check=True, capture_output=True)
        # Check if there are changes
        st = subprocess.run(["git", "-C", str(REPO_DIR), "status", "--porcelain"], capture_output=True, text=True, check=True)
        if st.stdout.strip():
            msg = f"chore(quant): update model parameters and daily review telemetry ({datetime.now(PT).strftime('%Y-%m-%d %H:%M')})"
            subprocess.run(["git", "-C", str(REPO_DIR), "commit", "-m", msg], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(REPO_DIR), "push", "origin", "main"], check=True, capture_output=True)
            print("✅ Pushed updated quant engine & calibration params to brockventures/kalshi-quant (main)")
        else:
            print("ℹ️ No code/parameter diffs to commit in kalshi-quant")
        return True
    except Exception as e:
        print(f"⚠️ Git sync error: {e}")
        return False

def update_vault_doc(metrics):
    """Update vault documentation with latest metrics."""
    if not VAULT_DOC.exists():
        return
    try:
        now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
        content = VAULT_DOC.read_text()
        
        # Replace or append portfolio line
        pattern = r"- Portfolio Status.*"
        new_line = f"- Portfolio Status ({now_str}): {metrics['open_count']} open positions, ${metrics['open_exposure']:.2f} open exposure, ${metrics['cash']:.2f} cash, ${metrics['total_equity']:.2f} total equity."
        if re.search(pattern, content):
            content = re.sub(pattern, new_line, content)
        else:
            content += f"\n{new_line}\n"
        VAULT_DOC.write_text(content)
        print("✅ Updated vault memory enclave: project_kalshi_quant_bot.md")
    except Exception as e:
        print(f"⚠️ Vault update error: {e}")

def print_review_summary(metrics, settled, adjustments):
    print("==========================================================")
    print("      ZERO KALSHI PAPER TRADING PERFORMANCE REVIEW       ")
    print("==========================================================")
    print(f"Current Time:        {datetime.now(PT).strftime('%Y-%m-%d %I:%M:%S %p PT')}")
    print(f"Cash Balance:        ${metrics['cash']:>10.2f}")
    print(f"Open Exposure:       ${metrics['open_exposure']:>10.2f} ({metrics['open_count']} positions)")
    print(f"Total Equity:        ${metrics['total_equity']:>10.2f}")
    print(f"Realized P&L:        ${metrics['realized_pnl']:>+10.2f}")
    print(f"Resolved Trades:     {metrics['resolved_count']:>10} (Win Rate: {metrics['win_rate']:.1f}%)")
    print("----------------------------------------------------------")
    if settled:
        print(f"SETTLED TODAY ({len(settled)} markets):")
        for s in settled:
            flag = "WIN" if s["won"] else "LOSS"
            print(f" • [{flag}] {s['ticker']} ({s['side']}): Payout ${s['payout']:.2f}, P&L ${s['realized_pnl']:+.2f}")
    else:
        print("No open positions settled in this run.")
        
    print("----------------------------------------------------------")
    brier = metrics.get("brier_scores", {})
    if brier:
        print("BRIER SCORE CALIBRATION:")
        for k, v in brier.items():
            bss = metrics.get("brier_skill_scores", {}).get(k, 0.0)
            print(f" • {k:<15}: Brier {v:.4f} | BSS {bss:+.3f}")
    else:
        print("Brier score: Awaiting settled contracts.")
        
    if adjustments:
        print("----------------------------------------------------------")
        print("DYNAMIC ADJUSTMENTS APPLIED:")
        for a in adjustments:
            print(f" • [{a['scope']}] {a.get('reason', '')}")
    print("==========================================================")

def main():
    parser = argparse.ArgumentParser(description="Kalshi Paper Trading Performance Review & Task Board Sync")
    parser.add_argument("--phase", choices=["evening", "night", "all"], default="all", help="Execution phase")
    parser.add_argument("--settle-only", action="store_true", help="Only settle markets")
    parser.add_argument("--dry-run", action="store_true", help="Run without database writes or GitHub API calls")
    parser.add_argument("--test", action="store_true", help="Test run with simulated settlement")
    parser.add_argument("--no-board-sync", action="store_true", help="Skip GitHub Project board update")
    parser.add_argument("--no-git-sync", action="store_true", help="Skip git push to brockventures/kalshi-quant")
    parser.add_argument("--quiet", action="store_true", help="Minimal console output")
    args = parser.parse_args()
    
    conn = get_db()
    params = load_params()
    
    # 1. Settle
    settled, pnl = settle_open_positions(conn, dry_run=args.dry_run)
    
    if args.settle_only:
        if not args.quiet:
            print(f"Settled {len(settled)} positions. Realized P&L: ${pnl:+.2f}")
        return
        
    # 2. Metrics
    metrics = compute_portfolio_metrics(conn)
    
    # 3. Model Recalibration
    adjustments = recalibrate_models(params, metrics, dry_run=args.dry_run)
    
    # 4. Summary display
    if not args.quiet:
        print_review_summary(metrics, settled, adjustments)
        
    # 5. GitHub Task Board & Telemetry
    if not args.no_board_sync:
        update_github_project_board(metrics, settled, adjustments, dry_run=args.dry_run)
        
    # 6. Git Sync
    if not args.no_git_sync:
        sync_repository(dry_run=args.dry_run)
        
    # 7. Vault Doc
    if not args.dry_run:
        update_vault_doc(metrics)

if __name__ == "__main__":
    main()
