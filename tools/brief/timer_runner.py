"""Idempotent, zero-LLM timer event producers for BRIEF."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.config import BriefConfig
from tools.brief.queue import BriefQueue


def _resolve_pt_datetime(
    now: float | datetime | None,
    tz_name: str = "America/Los_Angeles",
) -> datetime:
    tz = ZoneInfo(tz_name)
    if now is None:
        return datetime.now(tz=UTC).astimezone(tz)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)
    return datetime.fromtimestamp(float(now), tz=UTC).astimezone(tz)


def enqueue_morning_brief(
    queue: BriefQueue,
    now: float | datetime | None = None,
    *,
    tz_name: str = "America/Los_Angeles",
) -> int | None:
    """Enqueue today's morning brief timer event idempotently (zero LLM calls)."""
    dt = _resolve_pt_datetime(now, tz_name=tz_name)
    date_str = dt.strftime("%Y-%m-%d")
    ts = dt.timestamp()
    payload = json.dumps(
        {
            "type": "morning_brief",
            "date": date_str,
            "triggered_at_pt": f"{dt.strftime('%Y-%m-%d %H:%M')} PT",
        },
        sort_keys=True,
    )
    return queue.enqueue_event(
        source="timer",
        key="morning-brief",
        target_tag="timer/morning-brief",
        external_id=f"morning-brief-{date_str}",
        sender="brief-timer",
        body=payload,
        received_at=ts,
    )


def enqueue_daily_review(
    queue: BriefQueue,
    now: float | datetime | None = None,
    *,
    tz_name: str = "America/Los_Angeles",
) -> int | None:
    """Enqueue today's internal daily task review event idempotently (zero LLM calls)."""
    dt = _resolve_pt_datetime(now, tz_name=tz_name)
    date_str = dt.strftime("%Y-%m-%d")
    ts = dt.timestamp()
    payload = json.dumps(
        {
            "type": "daily_review",
            "date": date_str,
            "triggered_at_pt": f"{dt.strftime('%Y-%m-%d %H:%M')} PT",
        },
        sort_keys=True,
    )
    return queue.enqueue_event(
        source="internal",
        key="review",
        target_tag="internal/review",
        external_id=f"daily-review-{date_str}",
        sender="brief-timer",
        body=payload,
        received_at=ts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue scheduled BRIEF timer events.")
    parser.add_argument(
        "kind",
        choices=("morning-brief", "daily-review", "both"),
        help="Which timer event to enqueue.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional BriefConfig TOML path.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override state directory.")
    parser.add_argument("--memory-dir", type=Path, default=None, help="Override memory directory.")
    args = parser.parse_args(argv)

    cfg = BriefConfig.load(
        config_path=args.config,
        state_dir=args.state_dir,
        memory_dir=args.memory_dir,
    )
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    now_ts = time.time()

    enqueued: dict[str, int | None] = {}
    if args.kind in ("morning-brief", "both"):
        enqueued["morning_brief"] = enqueue_morning_brief(queue, now=now_ts, tz_name=cfg.timezone)
    if args.kind in ("daily-review", "both"):
        enqueued["daily_review"] = enqueue_daily_review(queue, now=now_ts, tz_name=cfg.timezone)

    print(json.dumps(enqueued, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
