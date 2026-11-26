"""Operator CLI for BRIEF (`python3 -m tools.brief.cli <cmd>`)."""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.actions import (
    DEFAULT_GOOGLE_OAUTH_PATH,
    READONLY_OAUTH_SCOPES,
    WRITE_OAUTH_SCOPES,
    build_oauth_consent_urls,
    exchange_oauth_authorization_code,
)
from tools.brief.config import (
    BriefConfig,
    ensure_memory_scaffold,
    is_kill_switch_active,
    set_kill_switch,
)
from tools.brief.daemon import (
    get_daemon_status,
    is_quiet_hours,
    run_daemon_loop,
    start_daemon_background,
    stop_daemon,
)
from tools.brief.gate import GATE_STATUSES, BriefApprovalGate, BriefGateStore
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


def _count_pending_proposal_files(pending_dir: Path) -> int:
    if not pending_dir.is_dir():
        return 0
    return sum(
        1
        for entry in pending_dir.iterdir()
        if entry.is_file()
        and not entry.name.startswith(".")
        and not entry.name.endswith(".tmp")
        and entry.suffix.lower() == ".json"
    )


def _inspect_gate_summary(cfg: BriefConfig) -> dict[str, Any]:
    gate_db_exists = cfg.gate_db_path.is_file()
    if gate_db_exists:
        counts = BriefGateStore(cfg.gate_db_path).get_status_counts()
    else:
        counts = {status: 0 for status in GATE_STATUSES}
    return {
        "db_exists": gate_db_exists,
        "pending_files": _count_pending_proposal_files(cfg.proposals_pending_dir),
        **counts,
        "total": sum(counts.values()),
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
    gate_summary = _inspect_gate_summary(cfg)
    kill_active = is_kill_switch_active(cfg)
    daemon_st = get_daemon_status(cfg)

    payload: dict[str, Any] = {
        "channel_id": cfg.channel_id,
        "state_dir": str(cfg.state_dir),
        "memory_dir": str(cfg.memory_dir),
        "db_exists": db_exists,
        "daemon": {
            "running": daemon_st["running"],
            "pid": daemon_st["pid"],
            "lock_held": daemon_st["lock_held"],
            "log_file": daemon_st["log_file"],
        },
        "kill_switch": {
            "active": kill_active,
            "path": str(cfg.kill_switch_path),
        },
        "queue": queue_stats,
        "gate": gate_summary,
        "sessions": sessions,
        "cursors": cursors,
        "health": health,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        daemon_label = (
            f"RUNNING (PID {daemon_st['pid']})" if daemon_st["running"] else "stopped"
        )
        print(f"BRIEF Status ({health['checked_at_pt']})")
        print(f"  Channel ID   : {cfg.channel_id}")
        print(f"  State Dir    : {cfg.state_dir}")
        print(f"  Daemon       : {daemon_label}")
        print(f"  Kill Switch  : {'ACTIVE' if kill_active else 'inactive'}")
        print(
            "  Queue        : "
            f"pending={queue_stats['pending']} delivering={queue_stats['delivering']} "
            f"done={queue_stats['done']} dropped={queue_stats['dropped']} "
            f"failed={queue_stats['failed']} total={queue_stats['total']}"
        )
        print(
            "  Gate         : "
            f"staged={gate_summary['staged']} executed={gate_summary['executed']} "
            f"edited_executed={gate_summary['edited_executed']} "
            f"dismissed={gate_summary['dismissed']} expired={gate_summary['expired']} "
            f"tampered={gate_summary['tampered']} pending_files={gate_summary['pending_files']}"
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
    event_id = enqueue_morning_brief(
        queue,
        now=time.time(),
        tz_name=cfg.timezone,
        task_dir=cfg.task_dir,
        personal_tasks_path=cfg.personal_tasks_path,
        config=cfg,
    )
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
    event_id = enqueue_daily_review(
        queue,
        now=time.time(),
        tz_name=cfg.timezone,
        task_dir=cfg.task_dir,
        personal_tasks_path=cfg.personal_tasks_path,
        config=cfg,
    )
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


def _cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if getattr(args, "dry_run", False):
        cfg.dry_run = True

    action = args.action
    if action == "status":
        st = get_daemon_status(cfg)
        if args.json:
            print(json.dumps(st, indent=2, sort_keys=True))
        else:
            state_label = f"RUNNING (PID {st['pid']})" if st["running"] else "STOPPED"
            print(f"BRIEF Daemon : {state_label}")
            print(f"  State Dir  : {st['state_dir']}")
            print(f"  Lock Held  : {st['lock_held']}")
            print(f"  Log File   : {st['log_file']}")
            print(f"  Quiet Hours: {'active' if st['quiet_hours'] else 'inactive'}")
            print(f"  Kill Switch: {'ACTIVE' if st['kill_switch_active'] else 'inactive'}")
        return 0
    if action == "start":
        ok = start_daemon_background(cfg)
        st = get_daemon_status(cfg)
        print(
            f"BRIEF daemon started (PID {st['pid']})."
            if ok
            else "Failed to start BRIEF daemon."
        )
        return 0 if ok else 1
    if action == "stop":
        ok = stop_daemon(cfg)
        print("BRIEF daemon stopped." if ok else "Failed to stop BRIEF daemon.")
        return 0 if ok else 1
    if action == "restart":
        stop_daemon(cfg)
        time.sleep(0.5)
        ok = start_daemon_background(cfg)
        st = get_daemon_status(cfg)
        print(
            f"BRIEF daemon restarted (PID {st['pid']})."
            if ok
            else "Failed to restart BRIEF daemon."
        )
        return 0 if ok else 1
    if action == "run":
        ok = run_daemon_loop(cfg, max_ticks=args.max_ticks)
        return 0 if ok else 1
    return 1


def _cmd_gate_status(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if cfg.gate_db_path.is_file():
        status_payload = BriefApprovalGate(cfg).get_gate_status()
    else:
        counts = {status: 0 for status in GATE_STATUSES}
        status_payload = {
            **counts,
            "total": 0,
            "counts": counts,
            "pending_files": _count_pending_proposal_files(cfg.proposals_pending_dir),
            "staged_proposals": [],
            "quiet_hours": is_quiet_hours(cfg),
            "dry_run": bool(cfg.dry_run),
            "readonly_token_configured": Path(cfg.readonly_token_path).is_file(),
            "readonly_token_path": str(cfg.readonly_token_path),
            "allow_shared_readonly_fallback": bool(cfg.allow_shared_readonly_fallback),
            "allow_shared_oauth_fallback": bool(cfg.gate_allow_shared_oauth_fallback),
            "write_token_configured": Path(cfg.gate_write_token_path).is_file(),
            "write_token_path": str(cfg.gate_write_token_path),
        }

    if args.json:
        print(json.dumps(status_payload, indent=2, sort_keys=True))
    else:
        readonly_state = "configured" if status_payload["readonly_token_configured"] else "missing"
        write_state = "configured" if status_payload["write_token_configured"] else "missing"
        print("BRIEF Approval Gate Status")
        print(f"  Gate DB        : {cfg.gate_db_path} ({'present' if cfg.gate_db_path.is_file() else 'missing'})")
        print(f"  Pending Files  : {status_payload['pending_files']}")
        print(
            "  Counts         : "
            f"staged={status_payload['staged']} executed={status_payload['executed']} "
            f"edited_executed={status_payload['edited_executed']} "
            f"dismissed={status_payload['dismissed']} expired={status_payload['expired']} "
            f"rejected={status_payload['rejected']} tampered={status_payload['tampered']} "
            f"failed={status_payload['failed']}"
        )
        print(f"  Read-Only Token: {readonly_state} ({status_payload['readonly_token_path']})")
        print(
            f"  RO Fallback    : {'enabled' if status_payload['allow_shared_readonly_fallback'] else 'disabled'}"
        )
        print(f"  Write Token    : {write_state} ({status_payload['write_token_path']})")
        print(f"  Shared Fallback: {'enabled' if status_payload['allow_shared_oauth_fallback'] else 'disabled'}")
        print(f"  Quiet Hours    : {'active' if status_payload['quiet_hours'] else 'inactive'}")
        for item in status_payload["staged_proposals"]:
            print(
                f"    - {item['id']} [{item['action']}] session={item['from_session']} "
                f"decision={item.get('decision_id') or '-'} expires={item['expires_at_pt']}"
            )
    return 0


def _cmd_gate_scan(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if args.dry_run:
        cfg.dry_run = True

    if is_kill_switch_active(cfg):
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "kill_switch_active",
                    "expired": [],
                    "scanned": 0,
                    "staged": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    gate = BriefApprovalGate(cfg)
    expired_ids = gate.expire_stale_proposals()
    scan_res = gate.scan_pending_proposals()
    print(
        json.dumps(
            {
                "status": "ok",
                "expired_ids": expired_ids,
                **scan_res,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cmd_gate_approve(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    if args.dry_run:
        cfg.dry_run = True
    gate = BriefApprovalGate(cfg)
    res = gate.approve_proposal(args.proposal_id)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 1


def _cmd_gate_dismiss(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    gate = BriefApprovalGate(cfg)
    res = gate.dismiss_proposal(args.proposal_id, note=args.note or "")
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 1


def _cmd_mint_oauth_urls(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    try:
        urls = build_oauth_consent_urls(
            args.oauth_client_path,
            redirect_uri=args.redirect_uri,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error building Google OAuth consent URLs: {exc}", file=sys.stderr)
        return 1

    payload = {
        "oauth_client_path": str(args.oauth_client_path),
        "redirect_uri": args.redirect_uri,
        "readonly_scopes": READONLY_OAUTH_SCOPES,
        "readonly_token_path": str(cfg.readonly_token_path),
        "write_scopes": WRITE_OAUTH_SCOPES,
        "write_token_path": str(cfg.gate_write_token_path),
        **urls,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("BRIEF Google OAuth Split-Credential Consent URLs")
        print(f"  Client File      : {args.oauth_client_path}")
        print(f"  Read-Only Scopes : {READONLY_OAUTH_SCOPES}")
        print(f"  Read-Only Dest   : {cfg.readonly_token_path}")
        print(f"  Read-Only URL    : {urls['readonly_url']}")
        print(f"  Write-Only Scopes: {WRITE_OAUTH_SCOPES}")
        print(f"  Write Token Dest : {cfg.gate_write_token_path}")
        print(f"  Write-Only URL   : {urls['write_url']}")
    return 0


def _cmd_exchange_oauth_code(args: argparse.Namespace) -> int:
    cfg = _load_runtime_config(args)
    default_output = (
        Path(cfg.readonly_token_path)
        if args.role == "readonly"
        else Path(cfg.gate_write_token_path)
    )
    target_output = Path(args.output) if args.output is not None else default_output

    try:
        result = exchange_oauth_authorization_code(
            args.code,
            role=args.role,
            oauth_client_path=args.oauth_client_path,
            output_path=target_output,
            redirect_uri=args.redirect_uri,
        )
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (
            errno.EROFS,
            errno.EACCES,
            errno.EPERM,
        ):
            hint = (
                f"Cannot write {args.role} OAuth token to {target_output} ({exc.strerror or exc}). "
                "If /secrets is mounted read-only, re-run with `--output <writable_path>` "
                f"to stage the mode-0600 token file and move it to {default_output} on the host."
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": str(exc),
                            "role": args.role,
                            "output_path": str(target_output),
                            "hint": hint,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"Error: {hint}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error exchanging Google OAuth authorization code: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error exchanging Google OAuth authorization code: {exc}", file=sys.stderr)
        return 1

    safe_payload = {
        "ok": True,
        **{
            k: v
            for k, v in result.items()
            if k not in {"client_secret", "refresh_token", "access_token"}
        },
    }
    if args.json:
        print(json.dumps(safe_payload, indent=2, sort_keys=True))
    else:
        print(f"BRIEF Google OAuth {safe_payload['role']} token saved")
        print(f"  Role   : {safe_payload['role']}")
        print(f"  Output : {safe_payload['output_path']}")
        print(f"  Scopes : {safe_payload['scope']}")
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

    p_daemon = subparsers.add_parser(
        "daemon",
        parents=[common_subparser_opts],
        help="Start, stop, restart, inspect, or run the BRIEF singleton daemon.",
    )
    p_daemon.add_argument(
        "action",
        nargs="?",
        choices=("start", "stop", "restart", "status", "run"),
        default="status",
        help="Daemon lifecycle action (default: status).",
    )
    p_daemon.add_argument("--json", action="store_true", help="Output JSON (for status).")
    p_daemon.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode when running in foreground (`daemon run`).",
    )
    p_daemon.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Maximum ticks to execute in `daemon run` before exiting.",
    )
    p_daemon.set_defaults(func=_cmd_daemon)

    p_gate_status = subparsers.add_parser(
        "gate-status",
        parents=[common_subparser_opts],
        help="Inspect BRIEF Approval Gate proposal counts and staged proposals.",
    )
    p_gate_status.add_argument("--json", action="store_true", help="Output JSON.")
    p_gate_status.set_defaults(func=_cmd_gate_status)

    p_gate_scan = subparsers.add_parser(
        "gate-scan",
        parents=[common_subparser_opts],
        help="Expire stale proposals and scan proposals/pending/*.json to stage Decision Cards.",
    )
    p_gate_scan.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage cards without posting to Discord.",
    )
    p_gate_scan.set_defaults(func=_cmd_gate_scan)

    p_gate_approve = subparsers.add_parser(
        "gate-approve",
        parents=[common_subparser_opts],
        help="Approve and execute a staged BRIEF proposal after SHA-256 verification.",
    )
    p_gate_approve.add_argument("proposal_id", help="Proposal ID to approve and execute.")
    p_gate_approve.add_argument(
        "--dry-run",
        action="store_true",
        help="Return synthetic dry-run receipt without calling Google Calendar API.",
    )
    p_gate_approve.set_defaults(func=_cmd_gate_approve)

    p_gate_dismiss = subparsers.add_parser(
        "gate-dismiss",
        parents=[common_subparser_opts],
        help="Dismiss a staged BRIEF proposal and emit an internal receipt.",
    )
    p_gate_dismiss.add_argument("proposal_id", help="Proposal ID to dismiss.")
    p_gate_dismiss.add_argument("--note", default="", help="Optional owner preference note.")
    p_gate_dismiss.set_defaults(func=_cmd_gate_dismiss)

    p_mint_oauth = subparsers.add_parser(
        "mint-oauth-urls",
        parents=[common_subparser_opts],
        help="Print narrow-scoped Google OAuth consent URLs (read-only vs calendar.events + gmail.compose write).",
    )
    p_mint_oauth.add_argument(
        "--oauth-client-path",
        type=Path,
        default=DEFAULT_GOOGLE_OAUTH_PATH,
        help="Path to Google OAuth client JSON (default: /secrets/google_oauth.json).",
    )
    p_mint_oauth.add_argument(
        "--redirect-uri",
        default="http://localhost",
        help="OAuth redirect URI (default: http://localhost).",
    )
    p_mint_oauth.add_argument("--json", action="store_true", help="Output JSON.")
    p_mint_oauth.set_defaults(func=_cmd_mint_oauth_urls)

    p_exchange_oauth = subparsers.add_parser(
        "exchange-oauth-code",
        parents=[common_subparser_opts],
        help="Exchange a Google OAuth authorization code or redirect URL for a split token file.",
    )
    p_exchange_oauth.add_argument(
        "--role",
        required=True,
        choices=("readonly", "write"),
        help="Target split OAuth credential role ('readonly' or 'write').",
    )
    p_exchange_oauth.add_argument(
        "--code",
        required=True,
        help="Authorization code or full redirect URL containing ?code=...",
    )
    p_exchange_oauth.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output token JSON path (defaults to configured readonly or write token path).",
    )
    p_exchange_oauth.add_argument(
        "--oauth-client-path",
        type=Path,
        default=DEFAULT_GOOGLE_OAUTH_PATH,
        help="Path to Google OAuth client JSON (default: /secrets/google_oauth.json).",
    )
    p_exchange_oauth.add_argument(
        "--redirect-uri",
        default="http://localhost",
        help="OAuth redirect URI (default: http://localhost).",
    )
    p_exchange_oauth.add_argument("--json", action="store_true", help="Output JSON.")
    p_exchange_oauth.set_defaults(func=_cmd_exchange_oauth_code)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for `python3 -m tools.brief.cli`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
