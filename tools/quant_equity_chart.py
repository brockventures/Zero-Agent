#!/usr/bin/env python3
"""
Quantitative Portfolio Equity vs. Time Trend Chart Generator (Active Books 4–5).

Reconstructs net-of-fee equity trajectories for the active strategy books (Book 4:
Hyperliquid Spot-Perp Carry and Book 5: Crypto Factor L/S & Macro Regime) starting
from the October 27, 2026 real-world execution rebuild ($1,000.00 baseline per book,
$2,000.00 consolidated active capital), persists live mark-to-market snapshots in
`quant_equity_snapshots`, and renders a high-DPI dark-mode 3-panel equity trend
chart for Discord (#vault).
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
QUANT_DB_PATH = Path("/workspace/data/quant_paper.db")
DEFAULT_CHART_PATH = Path("/workspace/data/quant_equity_chart.png")

# October 27, 2026 rebuild epoch when Books 1–3 were decommissioned and Books 4–5 reset to $1,000 each
ACTIVE_REBUILD_START_PT = datetime(2026, 10, 27, 22, 21, 0, tzinfo=PT)
ACTIVE_REBUILD_ISO = "2026-10-27T22:21:00"
HL_ONE_WAY_FRICTION_RATE = 0.00125


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
    t3_eq: float,
    t4_eq: float,
) -> None:
    """Persist a live mark-to-market equity snapshot across active Books 4 and 5."""
    if not QUANT_DB_PATH.exists():
        return
    now_pt = datetime.now(PT)
    ts_str = now_pt.isoformat()
    day_str = now_pt.strftime("%Y-%m-%d")
    tot_eq = round(t3_eq + t4_eq, 2)

    conn = sqlite3.connect(str(QUANT_DB_PATH))
    try:
        ensure_snapshot_table(conn)
        last = conn.execute(
            "SELECT timestamp, total_equity, hyperliquid_equity, trend_equity "
            "FROM quant_equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            last_dt = _parse_iso_pt(last[0])
            same_vals = (
                abs(float(last[1]) - tot_eq) < 0.01
                and abs(float(last[2]) - t3_eq) < 0.01
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
                ) VALUES (?, ?, 0.0, 0.0, 0.0, 0.0, ?, ?, ?)
                """,
                (
                    ts_str,
                    day_str,
                    round(t3_eq, 2),
                    round(t4_eq, 2),
                    round(tot_eq, 2),
                ),
            )
    finally:
        conn.close()


def build_book_timeseries(start_pt: datetime = ACTIVE_REBUILD_START_PT) -> dict:
    """Reconstruct timestamped net-of-fee equity curves for active Books 4 and 5 since start_pt."""
    now_pt = datetime.now(PT)
    start_iso = start_pt.strftime("%Y-%m-%dT%H:%M:%S")

    t3_dates, t3_vals = [start_pt], [1000.0]
    t4_dates, t4_vals = [start_pt], [1000.0]

    t3_curr = 1000.0
    t4_curr = 1000.0

    if QUANT_DB_PATH.exists():
        qconn = sqlite3.connect(str(QUANT_DB_PATH))
        qconn.row_factory = sqlite3.Row
        try:
            ensure_snapshot_table(qconn)

            # --- Book 4 (Track 3): Hyperliquid Forward Spot-Perp Carry (Net of Entry/Exit Fees + Funding) ---
            hl_events = defaultdict(float)

            for r in qconn.execute(
                "SELECT opened_at, closed_at, allocated_usd, status FROM hyperliquid_positions "
                "WHERE opened_at >= ? AND status != 'ARCHIVED_PRE_AUDIT' ORDER BY opened_at LIMIT 500",
                (start_iso,),
            ):
                alloc = float(r["allocated_usd"] or 0.0)
                fee = round(alloc * HL_ONE_WAY_FRICTION_RATE, 4)
                odt = _parse_iso_pt(r["opened_at"])
                if odt and odt >= start_pt:
                    hl_events[odt.replace(second=0, microsecond=0)] -= fee
                if r["status"] == "CLOSED" and r["closed_at"]:
                    cdt = _parse_iso_pt(r["closed_at"])
                    if cdt and cdt >= start_pt:
                        hl_events[cdt.replace(second=0, microsecond=0)] -= fee

            for r in qconn.execute(
                "SELECT timestamp, accrual_usd FROM hyperliquid_funding_accruals "
                "WHERE timestamp >= ? ORDER BY timestamp LIMIT 2000",
                (start_iso,),
            ):
                dt = _parse_iso_pt(r["timestamp"])
                if dt and dt >= start_pt:
                    dt_min = dt.replace(second=0, microsecond=0)
                    hl_events[dt_min] += float(r["accrual_usd"] or 0.0)

            cum_t3 = 1000.0
            for dt_min in sorted(hl_events.keys()):
                cum_t3 += hl_events[dt_min]
                if dt_min > t3_dates[-1]:
                    t3_dates.append(dt_min)
                    t3_vals.append(round(cum_t3, 2))
                else:
                    t3_vals[-1] = round(cum_t3, 2)

            t3_port = qconn.execute(
                "SELECT total_equity FROM hyperliquid_portfolio WHERE id = 1 LIMIT 1"
            ).fetchone()
            t3_curr = round(float(t3_port["total_equity"]), 2) if t3_port else round(cum_t3, 2)

            # --- Book 5 (Track 4): Crypto Factor L/S & Macro Regime (Mark-to-Market Snapshots + Live) ---
            for snap in qconn.execute(
                "SELECT timestamp, trend_equity FROM quant_equity_snapshots "
                "WHERE timestamp >= ? ORDER BY timestamp LIMIT 2000",
                (start_iso,),
            ):
                sdt = _parse_iso_pt(snap["timestamp"])
                if sdt and sdt >= start_pt:
                    sdt_min = sdt.replace(second=0, microsecond=0)
                    if sdt_min > t4_dates[-1]:
                        t4_dates.append(sdt_min)
                        t4_vals.append(round(float(snap["trend_equity"]), 2))
                    else:
                        t4_vals[-1] = round(float(snap["trend_equity"]), 2)

            t4_port = qconn.execute(
                "SELECT total_equity FROM trend_portfolio WHERE id = 1 LIMIT 1"
            ).fetchone()
            t4_open = qconn.execute(
                "SELECT unrealized_pnl FROM trend_positions WHERE status = 'OPEN' LIMIT 100"
            ).fetchall()
            t4_unreal = sum(float(r["unrealized_pnl"] or 0.0) for r in t4_open)
            t4_curr = round(
                (float(t4_port["total_equity"]) if t4_port else 1000.0) + t4_unreal,
                2,
            )
        finally:
            qconn.close()

    for d_list, v_list, curr_v in [
        (t3_dates, t3_vals, t3_curr),
        (t4_dates, t4_vals, t4_curr),
    ]:
        if d_list[-1] < now_pt:
            d_list.append(now_pt)
            v_list.append(round(curr_v, 2))
        else:
            v_list[-1] = round(curr_v, 2)

    record_equity_snapshot(t3_curr, t4_curr)

    all_event_dts = sorted(set(t3_dates + t4_dates))
    t3_ts = np.array([d.timestamp() for d in t3_dates], dtype=float)
    t4_ts = np.array([d.timestamp() for d in t4_dates], dtype=float)
    t3_arr = np.array(t3_vals, dtype=float)
    t4_arr = np.array(t4_vals, dtype=float)

    total_dates = []
    total_vals = []
    for dt in all_event_dts:
        ts = dt.timestamp()
        v_3 = float(np.interp(ts, t3_ts, t3_arr))
        v_4 = float(np.interp(ts, t4_ts, t4_arr))
        total_dates.append(dt)
        total_vals.append(round(v_3 + v_4, 2))

    return {
        "now_pt": now_pt,
        "start_pt": start_pt,
        "total": {"dates": total_dates, "vals": total_vals, "baseline": 2000.0},
        "track3": {"dates": t3_dates, "vals": t3_vals, "baseline": 1000.0},
        "track4": {"dates": t4_dates, "vals": t4_vals, "baseline": 1000.0},
    }


def render_quant_equity_chart(output_path: str | Path = DEFAULT_CHART_PATH) -> Path:
    """Render a 3-panel dark-mode equity vs. time chart for active Books 4 and 5."""
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
    amber_bright = "#d29922"

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.4), dpi=160)
    fig.patch.set_facecolor(bg_color)

    now_str = data["now_pt"].strftime("%b %d, %Y • %I:%M %p PT")
    tot_curr = data["total"]["vals"][-1]
    tot_delta = tot_curr - 2000.0
    tot_pct = (tot_delta / 2000.0) * 100.0
    sign_str = "+" if tot_delta >= 0 else "-"

    fig.text(
        0.02,
        0.935,
        "ZERO QUANT • ACTIVE BOOKS (4–5)",
        fontsize=13.5,
        fontweight="bold",
        color=text_primary,
        ha="left",
        va="center",
    )
    fig.text(
        0.98,
        0.935,
        f"Active Capital: ${tot_curr:,.2f} ({sign_str}${abs(tot_delta):,.2f} / {tot_pct:+.2f}%)  •  Since Oct 27  •  {now_str}",
        fontsize=11.0,
        fontweight="bold",
        color=green_bright if tot_delta >= 0 else red_bright,
        ha="right",
        va="center",
    )

    panels = [
        {
            "ax": axes[0],
            "key": "total",
            "title": "Active Portfolio (Books 4–5)",
            "subtitle": "Consolidated Net-of-Fee Mark-to-Market",
            "color": green_bright if tot_delta >= 0 else red_bright,
        },
        {
            "ax": axes[1],
            "key": "track3",
            "title": "Book 4 • Hyperliquid Spot-Perp Carry",
            "subtitle": "Forward Basis (SHORT_PERP) • Net of Fees",
            "color": amber_bright,
        },
        {
            "ax": axes[2],
            "key": "track4",
            "title": "Book 5 • Crypto Factor L/S & Macro",
            "subtitle": "Market-Neutral L/S & Slow-Cadence Macro",
            "color": cyan_bright if data["track4"]["vals"][-1] >= 1000.0 else red_bright,
        },
    ]

    # Shared Y-axis limits across single-book panels (Books 4 & 5)
    book_keys = ["track3", "track4"]
    global_book_min = min(min(min(data[k]["vals"]), 1000.0) for k in book_keys)
    global_book_max = max(max(max(data[k]["vals"]), 1000.0) for k in book_keys)
    global_book_span = max(global_book_max - global_book_min, 16.0)
    shared_book_ylim = (
        global_book_min - global_book_span * 0.22,
        global_book_max + global_book_span * 0.25,
    )

    span_days = max(
        0.1,
        (data["now_pt"] - data["start_pt"]).total_seconds() / 86400.0,
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
            linewidth=2.5,
            zorder=4,
        )
        ax.scatter(
            [dts[-1]],
            [curr_val],
            color=line_color,
            s=46,
            zorder=6,
            edgecolors=bg_color,
            linewidths=1.2,
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
            span = max(y_max - y_min, 16.0)
            ax.set_ylim(y_min - span * 0.22, y_max + span * 0.25)
        else:
            ax.set_ylim(*shared_book_ylim)

        ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=6))
        ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("${x:,.0f}"))

        # Two-row card header: Left (Title / Subtitle), Right (Equity / Signed Delta)
        ax.text(
            0.0,
            1.09,
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
            1.022,
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
            1.09,
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
            1.022,
            f"{d_sign}${abs(delta):,.2f} ({pct:+.2f}%)",
            transform=ax.transAxes,
            fontsize=9.0,
            fontweight="bold",
            color=badge_color,
            ha="right",
            va="bottom",
        )

        ax.grid(True, color=grid_color, linestyle="-", linewidth=0.8, alpha=0.8)

        if span_days <= 3.0:
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=PT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%I%p", tz=PT))
        elif span_days <= 14.0:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1, tz=PT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=PT))
        else:
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7, tz=PT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=PT))

        ax.tick_params(axis="both", colors=text_muted, labelsize=8.5)

    plt.tight_layout(rect=[0.012, 0.02, 0.988, 0.88])
    plt.subplots_adjust(wspace=0.18)
    fig.savefig(output_path, facecolor=bg_color, dpi=160)
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    out = render_quant_equity_chart()
    print(f"Rendered chart to {out}")
