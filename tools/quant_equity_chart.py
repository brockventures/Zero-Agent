#!/usr/bin/env python3
"""
Quantitative Portfolio Equity vs. Time Trend Chart Generator.

Reconstructs historical equity trajectories across all 5 strategy books from SQLite ledgers,
persists live mark-to-market snapshots in `quant_equity_snapshots`, and renders a high-DPI
dark-mode multi-book equity trend chart for Discord (#vault).
"""

import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

plt.rcParams["text.parse_math"] = False

PT = ZoneInfo("America/Los_Angeles")
KALSHI_DB_PATH = Path("/workspace/data/kalshi_paper.db")
QUANT_DB_PATH = Path("/workspace/data/quant_paper.db")
DEFAULT_CHART_PATH = Path("/workspace/data/quant_equity_chart.png")


def _parse_iso_pt(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PT)
        else:
            dt = dt.astimezone(PT)
        return dt
    except Exception:
        return None


def ensure_snapshot_table(qconn: sqlite3.Connection) -> None:
    with qconn:
        qconn.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                day_pt TEXT NOT NULL,
                kalshi_equity REAL NOT NULL,
                cross_venue_equity REAL NOT NULL,
                cross_venue_realized REAL NOT NULL,
                polymarket_equity REAL NOT NULL,
                hyperliquid_equity REAL NOT NULL,
                trend_equity REAL NOT NULL,
                total_equity REAL NOT NULL
            )
            """
        )


def record_equity_snapshot(
    kalshi_eq: float,
    t1_eq: float,
    t1_realized_eq: float,
    t2_eq: float,
    t3_eq: float,
    t4_eq: float,
) -> None:
    """Persist a live mark-to-market equity snapshot across all 5 books."""
    if not QUANT_DB_PATH.exists():
        return
    now_pt = datetime.now(PT)
    ts_str = now_pt.isoformat()
    day_str = now_pt.strftime("%Y-%m-%d")
    tot_eq = kalshi_eq + t1_eq + t2_eq + t3_eq + t4_eq

    conn = sqlite3.connect(str(QUANT_DB_PATH))
    try:
        ensure_snapshot_table(conn)
        last = conn.execute(
            "SELECT timestamp, total_equity, kalshi_equity, trend_equity "
            "FROM quant_equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            last_dt = _parse_iso_pt(last[0])
            same_vals = (
                abs(float(last[1]) - tot_eq) < 0.01
                and abs(float(last[2]) - kalshi_eq) < 0.01
                and abs(float(last[3]) - t4_eq) < 0.01
            )
            if last_dt and (now_pt - last_dt).total_seconds() < 900 and same_vals:
                return
        with conn:
            conn.execute(
                """
                INSERT INTO quant_equity_snapshots (
                    timestamp, day_pt, kalshi_equity, cross_venue_equity,
                    cross_venue_realized, polymarket_equity, hyperliquid_equity,
                    trend_equity, total_equity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_str,
                    day_str,
                    round(kalshi_eq, 2),
                    round(t1_eq, 2),
                    round(t1_realized_eq, 2),
                    round(t2_eq, 2),
                    round(t3_eq, 2),
                    round(t4_eq, 2),
                    round(tot_eq, 2),
                ),
            )
    finally:
        conn.close()


def build_book_timeseries() -> dict:
    """Reconstruct timestamped equity curves for each of the 5 books and total portfolio."""
    now_pt = datetime.now(PT)

    # --- 1. Kalshi Weather & Events ---
    kalshi_daily = defaultdict(float)
    kalshi_curr_eq = 1000.0
    if KALSHI_DB_PATH.exists():
        kconn = sqlite3.connect(str(KALSHI_DB_PATH))
        kconn.row_factory = sqlite3.Row
        try:
            p = kconn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
            open_pos = kconn.execute("SELECT total_cost FROM positions WHERE status='OPEN'").fetchall()
            if p:
                kalshi_curr_eq = float(p["cash"]) + sum(float(r["total_cost"]) for r in open_pos)
            for r in kconn.execute(
                "SELECT settled_at, realized_pnl FROM positions WHERE status='RESOLVED' ORDER BY settled_at"
            ):
                dt = _parse_iso_pt(r["settled_at"])
                if dt:
                    day_key = dt.strftime("%Y-%m-%d")
                    kalshi_daily[day_key] += float(r["realized_pnl"] or 0.0)
        finally:
            kconn.close()

    kalshi_dates = [datetime(2026, 9, 19, 9, 0, tzinfo=PT)]
    kalshi_vals = [1000.0]
    cum_k = 1000.0
    for d_str in sorted(kalshi_daily.keys()):
        cum_k += kalshi_daily[d_str]
        dt = datetime.strptime(d_str, "%Y-%m-%d").replace(hour=18, minute=45, tzinfo=PT)
        kalshi_dates.append(dt)
        kalshi_vals.append(round(cum_k, 2))
    if kalshi_dates[-1] < now_pt:
        kalshi_dates.append(now_pt)
        kalshi_vals.append(round(kalshi_curr_eq, 2))
    else:
        kalshi_vals[-1] = round(kalshi_curr_eq, 2)

    # --- Tracks 1–4 from quant_paper.db ---
    exp_start = datetime(2026, 10, 11, 8, 0, tzinfo=PT)
    t1_dates, t1_vals = [exp_start], [1000.0]
    t1_real_dates, t1_real_vals = [exp_start], [1000.0]
    t2_dates, t2_vals = [exp_start], [1000.0]
    t3_dates, t3_vals = [exp_start], [1000.0]
    t4_dates, t4_vals = [exp_start], [1000.0]

    t1_curr = 1000.0
    t1_real_curr = 1000.0
    t2_curr = 1000.0
    t3_curr = 1000.0
    t4_curr = 1000.0

    if QUANT_DB_PATH.exists():
        qconn = sqlite3.connect(str(QUANT_DB_PATH))
        qconn.row_factory = sqlite3.Row
        try:
            ensure_snapshot_table(qconn)

            # Track 1: Cross-Venue Arbitrage (locked equity + settled realized equity)
            cum_t1 = 1000.0
            for r in qconn.execute(
                "SELECT timestamp, locked_profit FROM cross_venue_trades "
                "WHERE status IN ('OPEN', 'SETTLED') ORDER BY timestamp"
            ):
                dt = _parse_iso_pt(r["timestamp"])
                if dt:
                    cum_t1 += float(r["locked_profit"] or 0.0)
                    t1_dates.append(dt)
                    t1_vals.append(round(cum_t1, 2))
            t1_curr = cum_t1

            cum_t1_r = 1000.0
            for r in qconn.execute(
                "SELECT settled_at, realized_pnl FROM cross_venue_trades "
                "WHERE status = 'SETTLED' AND settled_at IS NOT NULL ORDER BY settled_at"
            ):
                dt = _parse_iso_pt(r["settled_at"])
                if dt:
                    cum_t1_r += float(r["realized_pnl"] or 0.0)
                    t1_real_dates.append(dt)
                    t1_real_vals.append(round(cum_t1_r, 2))
            t1_real_curr = cum_t1_r
            t1_real_dates.append(now_pt)
            t1_real_vals.append(round(t1_real_curr, 2))

            # Track 2: Polymarket Standalone
            cum_t2 = 1000.0
            for r in qconn.execute(
                "SELECT timestamp, locked_profit FROM polymarket_standalone_trades "
                "WHERE status IN ('OPEN', 'SETTLED') ORDER BY timestamp"
            ):
                dt = _parse_iso_pt(r["timestamp"])
                if dt:
                    cum_t2 += float(r["locked_profit"] or 0.0)
                    t2_dates.append(dt)
                    t2_vals.append(round(cum_t2, 2))
            t2_curr = cum_t2

            # Track 3: Hyperliquid Basis (group by timestamp batch so curve is clean)
            hl_batches = defaultdict(float)
            for r in qconn.execute(
                "SELECT timestamp, accrual_usd FROM hyperliquid_funding_accruals ORDER BY timestamp"
            ):
                dt = _parse_iso_pt(r["timestamp"])
                if dt:
                    dt_min = dt.replace(second=0, microsecond=0)
                    hl_batches[dt_min] += float(r["accrual_usd"] or 0.0)
            cum_t3 = 1000.0
            for dt_min in sorted(hl_batches.keys()):
                cum_t3 += hl_batches[dt_min]
                t3_dates.append(dt_min)
                t3_vals.append(round(cum_t3, 2))
            t3_curr = cum_t3

            # Track 4: Trend Momentum
            cum_t4 = 1000.0
            for r in qconn.execute(
                "SELECT closed_at, realized_pnl FROM trend_positions "
                "WHERE status = 'CLOSED' AND closed_at IS NOT NULL ORDER BY closed_at"
            ):
                dt = _parse_iso_pt(r["closed_at"])
                if dt:
                    cum_t4 += float(r["realized_pnl"] or 0.0)
                    t4_dates.append(dt)
                    t4_vals.append(round(cum_t4, 2))

            t4_port = qconn.execute("SELECT total_equity FROM trend_portfolio WHERE id=1").fetchone()
            t4_open = qconn.execute("SELECT unrealized_pnl FROM trend_positions WHERE status='OPEN'").fetchall()
            t4_unreal = sum(float(r["unrealized_pnl"] or 0.0) for r in t4_open)
            t4_curr = (float(t4_port["total_equity"]) if t4_port else cum_t4) + t4_unreal

            for snap in qconn.execute(
                "SELECT timestamp, trend_equity FROM quant_equity_snapshots ORDER BY timestamp"
            ):
                sdt = _parse_iso_pt(snap["timestamp"])
                if sdt and sdt > t4_dates[-1]:
                    t4_dates.append(sdt)
                    t4_vals.append(round(float(snap["trend_equity"]), 2))
        finally:
            qconn.close()

    for d_list, v_list, curr_v in [
        (t1_dates, t1_vals, t1_curr),
        (t2_dates, t2_vals, t2_curr),
        (t3_dates, t3_vals, t3_curr),
        (t4_dates, t4_vals, t4_curr),
    ]:
        if d_list[-1] < now_pt:
            d_list.append(now_pt)
            v_list.append(round(curr_v, 2))
        else:
            v_list[-1] = round(curr_v, 2)

    record_equity_snapshot(
        kalshi_curr_eq,
        t1_curr,
        t1_real_curr,
        t2_curr,
        t3_curr,
        t4_curr,
    )

    all_event_dts = sorted(
        set(kalshi_dates + t1_dates + t2_dates + t3_dates + t4_dates)
    )

    def _interp_step(dts, vals, target_dt):
        val = vals[0]
        for d, v in zip(dts, vals):
            if d <= target_dt:
                val = v
            else:
                break
        return val

    total_dates = []
    total_vals = []
    for dt in all_event_dts:
        v_k = _interp_step(kalshi_dates, kalshi_vals, dt)
        v_1 = _interp_step(t1_dates, t1_vals, dt)
        v_2 = _interp_step(t2_dates, t2_vals, dt)
        v_3 = _interp_step(t3_dates, t3_vals, dt)
        v_4 = _interp_step(t4_dates, t4_vals, dt)
        total_dates.append(dt)
        total_vals.append(round(v_k + v_1 + v_2 + v_3 + v_4, 2))

    return {
        "now_pt": now_pt,
        "total": {"dates": total_dates, "vals": total_vals, "baseline": 5000.0},
        "kalshi": {"dates": kalshi_dates, "vals": kalshi_vals, "baseline": 1000.0},
        "track1": {
            "dates": t1_dates,
            "vals": t1_vals,
            "real_dates": t1_real_dates,
            "real_vals": t1_real_vals,
            "baseline": 1000.0,
        },
        "track2": {"dates": t2_dates, "vals": t2_vals, "baseline": 1000.0},
        "track3": {"dates": t3_dates, "vals": t3_vals, "baseline": 1000.0},
        "track4": {"dates": t4_dates, "vals": t4_vals, "baseline": 1000.0},
    }


def render_quant_equity_chart(output_path: str | Path = DEFAULT_CHART_PATH) -> Path:
    """Render a 6-panel dark-mode equity vs. time chart across all 5 strategy books."""
    data = build_book_timeseries()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    bg_color = "#0d1117"
    card_color = "#161b22"
    grid_color = "#21262d"
    border_color = "#30363d"
    text_primary = "#f0f6fc"
    text_muted = "#8b949e"
    green_bright = "#3fb950"
    red_bright = "#f85149"
    cyan_bright = "#58a6ff"
    purple_bright = "#bc8cff"
    amber_bright = "#d29922"
    teal_bright = "#39d353"

    fig, axes = plt.subplots(2, 3, figsize=(16, 9.8), dpi=160)
    fig.patch.set_facecolor(bg_color)

    now_str = data["now_pt"].strftime("%b %d, %Y • %I:%M %p PT")
    tot_curr = data["total"]["vals"][-1]
    tot_delta = tot_curr - 5000.0
    tot_pct = (tot_delta / 5000.0) * 100.0
    sign_str = "+" if tot_delta >= 0 else "-"

    fig.text(
        0.025,
        0.96,
        "ZERO QUANT • MULTI-BOOK EQUITY TRAJECTORY",
        fontsize=15.5,
        fontweight="bold",
        color=text_primary,
        ha="left",
        va="center",
    )
    fig.text(
        0.975,
        0.96,
        f"Total Capital: ${tot_curr:,.2f} ({sign_str}${abs(tot_delta):,.2f} / {tot_pct:+.2f}%)   •   {now_str}",
        fontsize=12,
        fontweight="bold",
        color=green_bright if tot_delta >= 0 else red_bright,
        ha="right",
        va="center",
    )

    panels = [
        {
            "ax": axes[0, 0],
            "key": "total",
            "title": "Total Portfolio (5 Books)",
            "subtitle": "Consolidated Mark-to-Market + Locked Arb",
            "color": green_bright,
            "is_multi_window": False,
        },
        {
            "ax": axes[0, 1],
            "key": "kalshi",
            "title": "Book 1 • Kalshi Weather & Events",
            "subtitle": "NOAA ASOS High-Temp & MLB CLOB",
            "color": cyan_bright,
            "is_multi_window": False,
        },
        {
            "ax": axes[0, 2],
            "key": "track1",
            "title": "Book 2 • Cross-Venue Arbitrage",
            "subtitle": "Kalshi × Polymarket Locked Spreads",
            "color": teal_bright,
            "is_multi_window": True,
        },
        {
            "ax": axes[1, 0],
            "key": "track2",
            "title": "Book 3 • Polymarket Standalone",
            "subtitle": "3-Way Soccer Maker Underround",
            "color": purple_bright,
            "is_multi_window": True,
        },
        {
            "ax": axes[1, 1],
            "key": "track3",
            "title": "Book 4 • Hyperliquid Basis",
            "subtitle": "Delta-Neutral Perp Funding Carry",
            "color": amber_bright,
            "is_multi_window": True,
        },
        {
            "ax": axes[1, 2],
            "key": "track4",
            "title": "Book 5 • Trend Momentum",
            "subtitle": "Cross-Asset Donchian / MA Swings",
            "color": red_bright if data["track4"]["vals"][-1] < 1000.0 else green_bright,
            "is_multi_window": True,
        },
    ]

    # Compute shared Y-axis limits across all 5 single-book panels (Books 1–5)
    book_keys = ["kalshi", "track1", "track2", "track3", "track4"]
    global_book_min = min(min(min(data[k]["vals"]), 1000.0) for k in book_keys)
    global_book_max = max(max(max(data[k]["vals"]), 1000.0) for k in book_keys)
    global_book_span = max(global_book_max - global_book_min, 20.0)
    shared_book_ylim = (
        global_book_min - global_book_span * 0.14,
        global_book_max + global_book_span * 0.18,
    )

    for p in panels:
        ax = p["ax"]
        series = data[p["key"]]
        dts = series["dates"]
        vals = series["vals"]
        baseline = series["baseline"]
        curr_val = vals[-1]
        delta = curr_val - baseline
        pct = (delta / baseline) * 100.0
        line_color = p["color"]

        ax.set_facecolor(card_color)
        for spine in ax.spines.values():
            spine.set_color(border_color)
            spine.set_linewidth(1.1)

        ax.axhline(
            baseline,
            color="#6e7681",
            linestyle="--",
            linewidth=1.1,
            alpha=0.75,
            zorder=2,
        )

        x_nums = mdates.date2num(dts)
        y_arr = np.array(vals, dtype=float)

        ax.plot(
            dts,
            vals,
            color=line_color,
            linewidth=2.4,
            zorder=4,
            label="Total Expected" if p["key"] == "track1" else None,
        )
        ax.scatter(
            [dts[-1]],
            [curr_val],
            color=line_color,
            s=42,
            zorder=6,
            edgecolors=bg_color,
            linewidths=1.2,
        )

        if p["key"] == "track1" and "real_dates" in series:
            ax.plot(
                series["real_dates"],
                series["real_vals"],
                color="#58a6ff",
                linestyle=":",
                linewidth=1.8,
                alpha=0.9,
                zorder=3,
                label=f"Settled Cash (${series['real_vals'][-1]:,.2f})",
            )
            ax.legend(
                loc="lower right",
                frameon=True,
                facecolor=bg_color,
                edgecolor=border_color,
                fontsize=8.5,
                labelcolor=text_primary,
            )

        ax.fill_between(
            x_nums,
            y_arr,
            baseline,
            where=(y_arr >= baseline),
            interpolate=True,
            color=line_color if delta >= 0 else green_bright,
            alpha=0.16,
            zorder=1,
        )
        ax.fill_between(
            x_nums,
            y_arr,
            baseline,
            where=(y_arr < baseline),
            interpolate=True,
            color=red_bright,
            alpha=0.18,
            zorder=1,
        )

        if p["key"] == "total":
            y_min = min(min(vals), baseline)
            y_max = max(max(vals), baseline)
            span = max(y_max - y_min, 20.0)
            ax.set_ylim(y_min - span * 0.16, y_max + span * 0.20)
        else:
            ax.set_ylim(*shared_book_ylim)
            ax.yaxis.set_major_locator(mticker.MultipleLocator(50))

        # Two-row card header: Left (Title / Subtitle), Right (Equity / Signed Delta)
        ax.text(
            0.0,
            1.085,
            p["title"],
            transform=ax.transAxes,
            fontsize=11.0,
            fontweight="bold",
            color=text_primary,
            ha="left",
            va="bottom",
        )
        ax.text(
            0.0,
            1.02,
            p["subtitle"],
            transform=ax.transAxes,
            fontsize=8.5,
            color=text_muted,
            ha="left",
            va="bottom",
        )

        d_sign = "+" if delta >= 0 else "-"
        badge_color = green_bright if delta >= 0 else red_bright
        ax.text(
            1.0,
            1.085,
            f"${curr_val:,.2f}",
            transform=ax.transAxes,
            fontsize=11.5,
            fontweight="bold",
            color=text_primary,
            ha="right",
            va="bottom",
        )
        ax.text(
            1.0,
            1.02,
            f"{d_sign}${abs(delta):,.2f} ({pct:+.1f}%)",
            transform=ax.transAxes,
            fontsize=9.0,
            fontweight="bold",
            color=badge_color,
            ha="right",
            va="bottom",
        )

        ax.grid(True, color=grid_color, linestyle="-", linewidth=0.8, alpha=0.8)
        ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("${x:,.0f}"))

        if p["is_multi_window"]:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=PT))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=5))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=PT))

        ax.tick_params(axis="both", colors=text_muted, labelsize=8.5)

    plt.tight_layout(rect=[0.015, 0.015, 0.985, 0.92])
    plt.subplots_adjust(hspace=0.38, wspace=0.20)
    fig.savefig(output_path, facecolor=bg_color, dpi=160)
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    out = render_quant_equity_chart()
    print(f"Rendered chart to {out}")
