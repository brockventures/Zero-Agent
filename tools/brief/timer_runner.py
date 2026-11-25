"""Idempotent, zero-LLM timer event producers for BRIEF."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.config import BriefConfig
from tools.brief.queue import BriefQueue
from tools.brief.tasks import scan_tasks_for_daily_review


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
    task_dir: Path | str | None = None,
    gate_store: Any | None = None,
    config: BriefConfig | None = None,
    skip_if_empty: bool = False,
) -> int | None:
    """Enqueue today's internal daily task review event idempotently (zero LLM calls)."""
    effective_tz = tz_name or (config.timezone if config is not None else "America/Los_Angeles")
    dt = _resolve_pt_datetime(now, tz_name=effective_tz)
    date_str = dt.strftime("%Y-%m-%d")
    ts = dt.timestamp()

    payload_dict: dict[str, Any] = {
        "type": "daily_review",
        "date": date_str,
        "triggered_at_pt": f"{dt.strftime('%Y-%m-%d %H:%M')} PT",
    }

    has_scan_context = (
        task_dir is not None
        or gate_store is not None
        or config is not None
        or skip_if_empty
    )
    three_strike_candidates: list[dict[str, Any]] = []
    resolved_gate_store = gate_store

    if has_scan_context:
        resolved_task_dir = (
            Path(task_dir)
            if task_dir is not None
            else (config.task_dir if config is not None else None)
        )
        if resolved_task_dir is not None:
            open_stale_days = config.task_open_stale_days if config is not None else 3
            blocked_stale_days = config.task_blocked_stale_days if config is not None else 6
            backoff_days = (
                config.task_staleness_backoff_days if config is not None else (3, 7, 14)
            )
            task_scan = scan_tasks_for_daily_review(
                resolved_task_dir,
                now=ts,
                open_stale_days=open_stale_days,
                blocked_stale_days=blocked_stale_days,
                backoff_days=backoff_days,
                tz_name=effective_tz,
            )
        else:
            task_scan = {
                "open_count": 0,
                "blocked_count": 0,
                "due_tasks": [],
                "stale_tasks": [],
                "actionable_tasks": [],
                "has_actionable_items": False,
            }

        if (
            resolved_gate_store is None
            and config is not None
            and config.gate_db_path.is_file()
        ):
            from tools.brief.gate import BriefGateStore

            resolved_gate_store = BriefGateStore(config.gate_db_path)
            resolved_gate_store.init_db()

        if resolved_gate_store is not None and hasattr(
            resolved_gate_store, "list_three_strike_candidates"
        ):
            threshold = config.dismissal_mute_strikes if config is not None else 3
            window_days = config.dismissal_window_days if config is not None else 30
            muted_domains = config.blocked_domains if config is not None else ()
            three_strike_candidates = resolved_gate_store.list_three_strike_candidates(
                threshold=threshold,
                window_days=window_days,
                now=ts,
                muted_domains=muted_domains,
            )

        has_actionable_items = bool(
            task_scan.get("has_actionable_items") or three_strike_candidates
        )
        if skip_if_empty and not has_actionable_items:
            return None

        payload_dict["task_scan"] = task_scan
        payload_dict["three_strike_mute_candidates"] = three_strike_candidates
        payload_dict["has_actionable_items"] = has_actionable_items

    payload = json.dumps(payload_dict, sort_keys=True)
    ev_id = queue.enqueue_event(
        source="internal",
        key="review",
        target_tag="internal/review",
        external_id=f"daily-review-{date_str}",
        sender="brief-timer",
        body=payload,
        received_at=ts,
    )
    if (
        ev_id is not None
        and three_strike_candidates
        and resolved_gate_store is not None
        and hasattr(resolved_gate_store, "mark_strikes_surfaced")
    ):
        resolved_gate_store.mark_strikes_surfaced(three_strike_candidates, now=ts)
    return ev_id


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
        gate_store = None
        if cfg.gate_db_path.is_file():
            from tools.brief.gate import BriefGateStore

            gate_store = BriefGateStore(cfg.gate_db_path)
            gate_store.init_db()
        enqueued["daily_review"] = enqueue_daily_review(
            queue,
            now=now_ts,
            tz_name=cfg.timezone,
            task_dir=cfg.task_dir,
            gate_store=gate_store,
            config=cfg,
            skip_if_empty=cfg.skip_empty_daily_review,
        )

    print(json.dumps(enqueued, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
