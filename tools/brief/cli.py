"""Operator CLI for BRIEF (`python3 -m tools.brief.cli <cmd>`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.config import (
    BriefConfig,
    ensure_memory_scaffold,
    is_kill_switch_active,
    set_kill_switch,
)
from tools.brief.harness import DryRunHarness
from tools.brief.queue import BriefQueue
from tools.brief.replay import run_replay
from tools.brief.router import BriefRouter
from tools.brief.timer_runner import enqueue_daily_review, enqueue_morning_brief
from tools.brief.watchdog import check_brief_health, run_watchdog
from tools.brief.watchers import CalendarWatcher, CursorStore, GmailWatcher


def _load_runtime_config(args: argparse.Namespace) -> BriefConfig:
    config_opt = getattr(args, "config", None) or os.environ.get("BRIEF_CONFIG")
    state_opt = getattr(args, "state_dir", None) or os.environ.get("BRIEF_STATE_DIR")
    memory_opt = getattr(args, "memory_dir", None) or os.environ.get("BRIEF_MEMORY_DIR")
    return BriefConfig.load(
        config_path=config_opt,
        state_dir=state_opt,
        memory_dir=memory_opt,
    )


def _inspect_cursors(cursors_path: Path) -> dict[str, Any]:
    if not cursors_path.is_file():
        return {
            "exists": False,
            "path": str(cursors_path),
            "gmail_history_id_present": False,
            "calendar_sync_token_present": False,
            "keys": [],
        }
    try:
        data = CursorStore(cursors_path).load()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "exists": True,
            "path": str(cursors_path),
            "gmail_history_id_present": False,
            "calendar_sync_token_present": False,
            "keys": [],
            "error": str(exc),
        }
    return {
        "exists": True,
        "path": str(cursors_path),
        "gmail_history_id_present": bool(str(data.get("gmail_history_id") or "").strip()),
        "calendar_sync_token_present": bool(str(data.get("calendar_sync_token") or "").strip()),
        "keys": sorted(str(k) for k in data),
    }


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    db_exists = cfg.db_path.is_file()
    if db_exists:
        queue_stats = queue.get_queue_stats()
        sessions = queue.list_sessions()
    else:
        queue_stats = {
            "pending": 0,
            "delivering": 0,
            "done": 0,
            "dropped": 0,
            "failed": 0,
            "total": 0,
            "sessions": 0,
        }
        sessions = []

    health = check_brief_health(cfg, queue)
    cursors = _inspect_cursors(cfg.cursors_path)
    kill_active = is_kill_switch_active(cfg)

    payload: dict[str, Any] = {
        "channel_id": cfg.channel_id,
        "state_dir": str(cfg.state_dir),
        "memory_dir": str(cfg.memory_dir),
        "db_exists": db_exists,
        "kill_switch": {
            "active": kill_active,
            "path": str(cfg.kill_switch_path),
        },
        "queue": queue_stats,
        "sessions": sessions,
        "cursors": cursors,
        "health": health,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"BRIEF Status ({health['checked_at_pt']})")
        print(f"  Channel ID   : {cfg.channel_id}")
        print(f"  State Dir    : {cfg.state_dir}")
        print(f"  Kill Switch  : {'ACTIVE' if kill_active else 'inactive'}")
        print(
            "  Queue        : "
            f"pending={queue_stats['pending']} delivering={queue_stats['delivering']} "
            f"done={queue_stats['done']} dropped={queue_stats['dropped']} "
            f"failed={queue_stats['failed']} total={queue_stats['total']}"
        )
        print(f"  Sessions     : {len(sessions)}")
        print(
            "  Cursors      : "
            f"gmail={cursors['gmail_history_id_present']} "
            f"calendar={cursors['calendar_sync_token_present']}"
        )
        print(f"  Health       : {'HEALTHY' if health['healthy'] else 'UNHEALTHY'}")
        for comp, info in health["heartbeats"].items():
            age = info.get("age_seconds")
            age_txt = f" ({float(age):.0f}s ago)" if age is not None else ""
            print(f"    - {comp}: {info['status']}{age_txt}")
        for issue in health["issues"]:
            print(f"    ! {issue}")
    return 0


def _cmd_poll_once(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if args.dry_run:
        cfg.dry_run = True

    if is_kill_switch_active(cfg):
        msg = f"BRIEF kill switch is active ({cfg.kill_switch_path}); skipping poll-once."
        print(msg)
        return 0

    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    ensure_memory_scaffold(cfg.memory_dir)
    cursors = CursorStore(cfg.cursors_path)

    results: dict[str, Any] = {}
    if args.source in ("gmail", "all"):
        results["gmail"] = GmailWatcher(queue, cursors, cfg).poll_once()
    if args.source in ("calendar", "all"):
        results["calendar"] = CalendarWatcher(queue, cursors, cfg).poll_once()

    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


def _cmd_route_once(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if args.dry_run:
        cfg.dry_run = True

    if is_kill_switch_active(cfg):
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "kill_switch_active",
                    "processed": 0,
                    "outcomes": [],
                },
                sort_keys=True,
            )
        )
        return 0

    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    harness = DryRunHarness() if args.dry_run else None
    router = BriefRouter(cfg, queue=queue, harness=harness)

    max_events = max(1, int(args.max_events))
    outcomes: list[dict[str, Any]] = []
    for _ in range(max_events):
        outcome = router.process_one()
        if outcome is None:
            break
        outcomes.append(outcome)

    print(
        json.dumps(
            {
                "status": "ok",
                "processed": len(outcomes),
                "outcomes": outcomes,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cmd_morning_brief(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    event_id = enqueue_morning_brief(queue, now=time.time(), tz_name=cfg.timezone)
    print(
        json.dumps(
            {
                "kind": "morning_brief",
                "event_id": event_id,
                "duplicate": event_id is None,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_daily_review(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    event_id = enqueue_daily_review(queue, now=time.time(), tz_name=cfg.timezone)
    print(
        json.dumps(
            {
                "kind": "daily_review",
                "event_id": event_id,
                "duplicate": event_id is None,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_watchdog(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    dry_run = bool(args.dry_run or cfg.dry_run)
    result = run_watchdog(cfg, queue, dry_run=dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    passed, results = run_replay()
    if args.json:
        print(json.dumps({"passed": passed, "results": results}, indent=2, sort_keys=True))
    else:
        print("BRIEF Dry-Run Replay Summary")
        print("=" * 40)
        for item in results:
            label = "PASS" if item["passed"] else "FAIL"
            verdict = item.get("verdict", "-")
            print(f"[{label}] {item['name']} (verdict={verdict})")
            for err in item.get("errors", []):
                print(f"       - {err}")
        print("-" * 40)
        print(f"Overall: {'PASS' if passed else 'FAIL'} ({len(results)} checks)")
    return 0 if passed else 1


def _cmd_kill_switch(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    action = args.action
    if action == "on":
        flag_path = set_kill_switch(cfg, True, reason=args.reason or "")
        active = True
    elif action == "off":
        flag_path = set_kill_switch(cfg, False)
        active = False
    else:
        flag_path = cfg.kill_switch_path
        active = is_kill_switch_active(cfg)

    payload = {
        "action": action,
        "active": active,
        "path": str(flag_path),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        state_word = "ACTIVE" if active else "inactive"
        print(f"BRIEF kill switch is {state_word} ({flag_path})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the BRIEF operator CLI argument parser."""
    common_subparser_opts = argparse.ArgumentParser(add_help=False)
    common_subparser_opts.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="Optional BriefConfig TOML path.",
    )
    common_subparser_opts.add_argument(
        "--state-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override BRIEF state directory.",
    )
    common_subparser_opts.add_argument(
        "--memory-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override BRIEF memory directory.",
    )

    parser = argparse.ArgumentParser(
        prog="tools.brief.cli",
        description="BRIEF (Background Routing, Inbox & Execution Filter) operator CLI.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional BriefConfig TOML path.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override BRIEF state directory.")
    parser.add_argument("--memory-dir", type=Path, default=None, help="Override BRIEF memory directory.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_status = subparsers.add_parser(
        "status",
        parents=[common_subparser_opts],
        help="Show BRIEF queue, sessions, cursors, kill switch, and heartbeat health.",
    )
    p_status.add_argument("--json", action="store_true", help="Output JSON.")
    p_status.set_defaults(func=_cmd_status)

    p_poll = subparsers.add_parser(
        "poll-once",
        parents=[common_subparser_opts],
        help="Run one read-only Gmail/Calendar watcher poll.",
    )
    p_poll.add_argument(
        "--source",
        choices=("gmail", "calendar", "all"),
        default="all",
        help="Which watcher source to poll.",
    )
    p_poll.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode without dispatching Discord notifications.",
    )
    p_poll.set_defaults(func=_cmd_poll_once)

    p_route = subparsers.add_parser(
        "route-once",
        parents=[common_subparser_opts],
        help="Drain claimable pending queue items.",
    )
    p_route.add_argument(
        "--dry-run",
        action="store_true",
        help="Force DryRunHarness instead of AgyCliHarness.",
    )
    p_route.add_argument(
        "--max-events",
        type=int,
        default=50,
        help="Maximum pending events to process in one invocation.",
    )
    p_route.set_defaults(func=_cmd_route_once)

    p_morning = subparsers.add_parser(
        "morning-brief",
        parents=[common_subparser_opts],
        help="Enqueue today's timer/morning-brief event.",
    )
    p_morning.set_defaults(func=_cmd_morning_brief)

    p_review = subparsers.add_parser(
        "daily-review",
        parents=[common_subparser_opts],
        help="Enqueue today's internal/review event.",
    )
    p_review.set_defaults(func=_cmd_daily_review)

    p_watchdog = subparsers.add_parser(
        "watchdog",
        parents=[common_subparser_opts],
        help="Run pure-Python heartbeat and failed-event watchdog.",
    )
    p_watchdog.add_argument(
        "--dry-run",
        action="store_true",
        help="Check health and record cooldown without posting to Discord.",
    )
    p_watchdog.set_defaults(func=_cmd_watchdog)

    p_replay = subparsers.add_parser(
        "replay",
        parents=[common_subparser_opts],
        help="Run the offline dry-run fixture replay suite.",
    )
    p_replay.add_argument("--json", action="store_true", help="Output JSON.")
    p_replay.set_defaults(func=_cmd_replay)

    p_ks = subparsers.add_parser(
        "kill-switch",
        parents=[common_subparser_opts],
        help="Toggle or inspect the BRIEF kill_switch.flag.",
    )
    p_ks.add_argument(
        "action",
        nargs="?",
        choices=("on", "off", "status"),
        default="status",
        help="Kill switch action (default: status).",
    )
    p_ks.add_argument("--reason", default="", help="Optional reason recorded when turning on.")
    p_ks.add_argument("--json", action="store_true", help="Output JSON.")
    p_ks.set_defaults(func=_cmd_kill_switch)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for `python3 -m tools.brief.cli`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
