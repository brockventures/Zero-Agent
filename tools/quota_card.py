#!/usr/bin/env python3
"""Zero (Antigravity) Weekly Quota Card Generator & Sidecar.

Executes `agy -p "/usage" --output-format=json` with zero LLM token overhead,
extracts the Gemini Models weekly remaining percentage and reset timestamp,
computes daily burn rate vs. remaining daily budget across the 7-day cycle,
and renders a mobile-friendly progress bar card in Pacific Time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
WEEK_SECONDS = 7 * 86400  # 604,800 seconds


def fetch_usage_data(timeout: int = 30, retries: int = 1) -> dict:
    """Invoke `agy -p "/usage" --output-format=json` and return parsed JSON."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            res = subprocess.run(
                ["agy", "-p", "/usage", "--output-format=json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = (res.stdout or "").strip()
            if not stdout:
                raise RuntimeError(f"Empty output from agy /usage (rc={res.returncode}, stderr={res.stderr.strip()})")
            # Find the JSON object in stdout
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
            return json.loads(stdout)
        except Exception as e:
            last_err = e
            if attempt < retries:
                continue
            raise last_err


def _get_fallback_weekly_bucket_from_db() -> dict | None:
    """Fallback to the most recent gemini-weekly snapshot in telemetry.db when agy /usage CLI times out."""
    try:
        from tools.quota_poller import get_latest_quota
        latest = get_latest_quota()
        gw = latest.get("gemini-weekly")
        if gw and gw.get("remaining_fraction") is not None and gw.get("reset_time"):
            return {
                "id": "gemini-weekly",
                "name": "Weekly Limit Remaining",
                "window": "weekly",
                "remaining_fraction": float(gw["remaining_fraction"]),
                "reset_time": str(gw["reset_time"]),
            }
    except Exception:
        pass
    return None




def parse_gemini_weekly_bucket(data: dict) -> dict:
    """Extract the Gemini weekly quota bucket from agy /usage JSON payload."""
    groups = data.get("command", {}).get("data", {}).get("groups", [])
    for group in groups:
        if "gemini" in str(group.get("name", "")).lower():
            for bucket in group.get("buckets", []):
                if bucket.get("id") == "gemini-weekly" or bucket.get("window") == "weekly":
                    return bucket

    # Fallback: parse tab-separated `response` string if structured buckets are absent
    resp_text = data.get("response", "")
    for line in resp_text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and "gemini" in parts[0].lower() and "weekly" in parts[1].lower():
            pct_str = parts[2].strip().rstrip("%")
            return {
                "id": "gemini-weekly",
                "name": "Weekly Limit Remaining",
                "window": "weekly",
                "remaining_fraction": float(pct_str) / 100.0,
                "reset_time": parts[3].strip(),
            }

    raise ValueError("Could not locate Gemini weekly quota bucket in /usage response.")


def format_progress_bar(fraction: float, width: int = 10) -> tuple[str, int]:
    """Return a (`[███░░░░░░░]`, rounded_pct) tuple."""
    clamped = max(0.0, min(1.0, float(fraction)))
    pct = int(round(clamped * 100))
    filled = int(round(clamped * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]", pct


def compute_cycle_metrics(
    remaining_fraction: float,
    reset_iso: str,
    now_utc: datetime | None = None,
) -> dict:
    """Compute reset countdown, Pacific Time label, and daily burn vs. available pace."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    iso_clean = reset_iso.strip()
    if iso_clean.endswith("Z"):
        iso_clean = iso_clean[:-1] + "+00:00"
    reset_dt_utc = datetime.fromisoformat(iso_clean)
    if reset_dt_utc.tzinfo is None:
        reset_dt_utc = reset_dt_utc.replace(tzinfo=timezone.utc)

    rem_sec = max(0, int((reset_dt_utc - now_utc).total_seconds()))
    days = rem_sec // 86400
    hours = (rem_sec % 86400) // 3600
    minutes = (rem_sec % 3600) // 60

    if days > 0:
        countdown = f"{days}d {hours}h"
    elif hours > 0:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"

    reset_pt = reset_dt_utc.astimezone(PT)
    hour_12 = reset_pt.strftime("%I").lstrip("0") or "12"
    pt_str = f"{reset_pt.strftime('%a')} {hour_12}:{reset_pt.strftime('%M %p')} PT"

    clamped_rem = max(0.0, min(1.0, float(remaining_fraction)))
    rem_pct_exact = clamped_rem * 100.0
    used_pct_exact = (1.0 - clamped_rem) * 100.0

    days_left = max(rem_sec / 86400.0, 1.0 / 24.0)  # floor at 1 hour to avoid div-by-zero near reset
    days_elapsed = max(7.0 - (rem_sec / 86400.0), 1.0 / 24.0)

    used_per_day = used_pct_exact / days_elapsed
    avail_per_day = rem_pct_exact / days_left

    return {
        "countdown": countdown,
        "reset_pt": pt_str,
        "days_elapsed": round(days_elapsed, 2),
        "days_left": round(days_left, 2),
        "used_per_day_pct": round(used_per_day, 1),
        "avail_per_day_pct": round(avail_per_day, 1),
    }


def build_quota_card(now_utc: datetime | None = None, data: dict | None = None) -> tuple[bool, str, dict]:
    """Fetch live Gemini weekly quota and render the progress bar card."""
    try:
        if data is not None:
            bucket = parse_gemini_weekly_bucket(data)
        else:
            try:
                raw = fetch_usage_data()
                bucket = parse_gemini_weekly_bucket(raw)
            except Exception as cli_err:
                fb = _get_fallback_weekly_bucket_from_db()
                if fb is not None:
                    bucket = fb
                else:
                    raise cli_err
        frac = float(bucket.get("remaining_fraction", 0.0))
        reset_iso = str(bucket.get("reset_time", ""))
        bar, pct = format_progress_bar(frac, width=10)
        metrics = compute_cycle_metrics(frac, reset_iso, now_utc=now_utc)

        card = (
            "```text\n"
            "Gemini Weekly Quota\n"
            f"{bar} {pct}% remaining\n"
            f"Pace: {metrics['used_per_day_pct']:.1f}%/d used | {metrics['avail_per_day_pct']:.1f}%/d left\n"
            f"Resets in {metrics['countdown']} ({metrics['reset_pt']})\n"
            "```"
        )
        meta = {
            "remaining_fraction": frac,
            "remaining_pct": pct,
            "reset_time": reset_iso,
            **metrics,
        }
        return True, card, meta
    except Exception as e:
        return False, f"⚠️ **Failed to retrieve Gemini weekly quota:** `{e}`", {"error": str(e)}


def run_quota_card(test: bool = False, quiet: bool = False) -> tuple[bool, str, dict]:
    """Entry point for sidecars.py and bridge_scheduler.py."""
    return build_quota_card()


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Zero Gemini Weekly Quota progress bar card.")
    parser.add_argument("--test", "-t", action="store_true", help="Run in test mode")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet flag for sidecar compatibility")
    parser.add_argument("--json", action="store_true", help="Output raw JSON metadata along with card")
    args = parser.parse_args()

    ok, card, meta = build_quota_card()
    if args.json:
        print(json.dumps({"ok": ok, "card": card, **meta}, indent=2))
    else:
        print(card)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
