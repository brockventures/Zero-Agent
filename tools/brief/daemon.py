"""Singleton polling/routing daemon and quiet-hours delivery gate for BRIEF."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.config import (
    DEFAULT_SECRETS_PATH,
    BriefConfig,
    ensure_memory_scaffold,
    is_kill_switch_active,
    touch_heartbeat,
)

if TYPE_CHECKING:
    from tools.brief.gate import BriefApprovalGate
from tools.brief.harness import DryRunHarness
from tools.brief.queue import BriefQueue
from tools.brief.router import BriefRouter
from tools.brief.timer_runner import enqueue_daily_review, enqueue_morning_brief
from tools.brief.watchdog import (
    _atomic_write_json,
    _format_pt_timestamp,
    _resolve_discord_bot_token,
    _wrap_urls_in_angles,
    run_watchdog,
)
from tools.brief.watchers import CalendarWatcher, CursorStore, GmailWatcher
from tools.daemon_singleton import (
    acquire_singleton_lock,
    is_lock_held,
    reconcile_live_daemon_pid,
    release_singleton_lock,
    terminate_duplicate_daemons,
)

logger = logging.getLogger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_USER_AGENT = "DiscordBot (https://github.com/brockventures/Zero-Agent, 1.0)"
_MAX_DISCORD_CHARS = 1950
_WATCHDOG_INTERVAL_SECONDS = 900.0
_MAX_ROUTER_DRAIN_PER_TICK = 50
_MORNING_BRIEF_HOUR_PT = 7
_DAILY_REVIEW_HOUR_PT = 18

_IGNORE_PREFIX_RE = re.compile(
    r"^(?:\*{0,2})(?:IGNORE|\[NO_REPLY\]|NO_REPLY)(?:\*{0,2})(?:[:\s\-!.]|$)",
    re.IGNORECASE,
)
_VERDICT_IGNORE_RE = re.compile(
    r"^(?:\*{0,2})(?:VERDICT|OUTCOME|DECISION|ACTION)(?:\*{0,2})\s*:\s*(?:\*{0,2})(?:IGNORE|\[?NO_REPLY\]?)\b",
    re.IGNORECASE,
)
_KIND_PREFIX_RE = re.compile(
    r"^(?:\*{0,2})(INFORM|PROPOSE|TASK)(?:\*{0,2})(?:[:\s\-!]|$)",
    re.IGNORECASE,
)
_VERDICT_KIND_RE = re.compile(
    r"^(?:\*{0,2})(?:VERDICT|OUTCOME|DECISION|ACTION)(?:\*{0,2})\s*:\s*(?:\*{0,2})(INFORM|PROPOSE|TASK|DISPATCH_TASK)\b",
    re.IGNORECASE,
)
_URGENT_WORD_RE = re.compile(r"(?<!not\s)\burgent\b", re.IGNORECASE)


def _resolve_cfg(
    cfg: BriefConfig | None = None,
    *,
    config_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    memory_dir: Path | str | None = None,
) -> BriefConfig:
    if cfg is not None:
        return cfg
    return BriefConfig.load(
        config_path=config_path or os.environ.get("BRIEF_CONFIG"),
        state_dir=state_dir or os.environ.get("BRIEF_STATE_DIR"),
        memory_dir=memory_dir or os.environ.get("BRIEF_MEMORY_DIR"),
    )


def pid_file_path(cfg: BriefConfig) -> Path:
    return cfg.state_dir / "brief_daemon.pid"


def lock_file_path(cfg: BriefConfig) -> Path:
    return cfg.state_dir / "brief_daemon.lock"


def log_file_path(cfg: BriefConfig) -> Path:
    return cfg.state_dir / "brief_daemon.log"


def delivery_budget_path(cfg: BriefConfig) -> Path:
    return cfg.state_dir / "delivery_budget.json"


def deferred_deliveries_path(cfg: BriefConfig) -> Path:
    return cfg.state_dir / "deferred_deliveries.json"


def _resolve_tz_datetime(
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


def _parse_hhmm(value: str) -> int:
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM time specification: {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Out-of-range HH:MM time specification: {value!r}")
    return hour * 60 + minute


def is_quiet_hours(cfg: BriefConfig, now: float | datetime | None = None) -> bool:
    """Return True if `now` falls within `cfg.quiet_hours_start..cfg.quiet_hours_end` in `cfg.timezone`."""
    start_min = _parse_hhmm(cfg.quiet_hours_start)
    end_min = _parse_hhmm(cfg.quiet_hours_end)
    if start_min == end_min:
        return False

    dt = _resolve_tz_datetime(now, tz_name=cfg.timezone)
    cur_min = dt.hour * 60 + dt.minute
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def should_deliver_response(response_text: str) -> tuple[bool, str]:
    """Classify a BRIEF router response into `(should_deliver, kind)`."""
    text = str(response_text or "").strip()
    if not text:
        return False, "ignore"

    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and "verdict" in parsed:
            verdict = str(parsed.get("verdict") or "").strip().upper()
            if verdict in {"IGNORE", "NO_REPLY", "[NO_REPLY]", "NONE", "SILENT"}:
                return False, "ignore"
            if verdict.startswith("PROPOSE"):
                return True, "propose"
            if verdict.startswith("TASK") or verdict == "DISPATCH_TASK":
                return True, "task"
            if verdict in {"INFORM", "URGENT"}:
                return True, "inform"
            return True, verdict.lower() or "summary"

    upper = text.upper()
    if "[NO_REPLY]" in upper:
        return False, "ignore"
    if _IGNORE_PREFIX_RE.match(text) or _VERDICT_IGNORE_RE.match(text):
        return False, "ignore"

    first_line = text.splitlines()[0].strip()
    match = _KIND_PREFIX_RE.match(first_line) or _VERDICT_KIND_RE.match(first_line)
    if match:
        token = match.group(1).upper()
        if token == "DISPATCH_TASK":
            return True, "task"
        return True, token.lower()

    return True, "summary"


def _format_discord_message(response_text: str, max_chars: int = _MAX_DISCORD_CHARS) -> str:
    text = str(response_text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and "verdict" in parsed and "summary" in parsed:
            verdict = str(parsed.get("verdict") or "INFORM").strip().upper()
            session = str(parsed.get("session") or "").strip()
            summary = str(parsed.get("summary") or "").strip()
            prefix = f"**[{verdict}]**"
            if session:
                prefix = f"{prefix} `{session}`"
            text = f"{prefix} {summary}".strip()

    wrapped = _wrap_urls_in_angles(text)
    if len(wrapped) <= max_chars:
        return wrapped
    return wrapped[: max_chars - 3].rstrip() + "..."


def _is_response_urgent(
    response_text: str,
    *,
    urgent: bool = False,
) -> bool:
    if urgent:
        return True
    text = str(response_text or "").strip()
    if not text:
        return False
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            verdict = str(parsed.get("verdict") or "").strip().upper()
            if verdict == "URGENT":
                return True
            if _URGENT_WORD_RE.search(str(parsed.get("summary") or "")):
                return True
    return bool(_URGENT_WORD_RE.search(text))


class BriefDeliveryGate:
    """Enforces quiet hours, IGNORE suppression, and daily proactive noise budget."""

    def __init__(
        self,
        config: BriefConfig,
        *,
        secrets_path: Path | str | None = DEFAULT_SECRETS_PATH,
        now_fn: Callable[[], float] | None = None,
        post_fn: Callable[[int, str], bool] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.config = config
        self.secrets_path = secrets_path
        self._now_fn = now_fn or time.time
        self._post_fn = post_fn
        self.timeout = float(timeout)
        self._lock = threading.Lock()

    def is_quiet_hours(self, now: float | datetime | None = None) -> bool:
        return is_quiet_hours(self.config, now=now)

    @staticmethod
    def should_deliver_response(response_text: str) -> tuple[bool, str]:
        return should_deliver_response(response_text)

    def _pt_date_key(self, now: float | datetime | None = None) -> tuple[str, float]:
        resolved = self._now_fn() if now is None else now
        dt = _resolve_tz_datetime(resolved, tz_name=self.config.timezone)
        return dt.strftime("%Y-%m-%d"), dt.timestamp()

    def _load_budget_unlocked(self) -> dict[str, dict[str, Any]]:
        path = delivery_budget_path(self.config)
        if not path.is_file():
            return {}
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        days = data.get("days")
        if not isinstance(days, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        for key, val in days.items():
            if isinstance(key, str) and isinstance(val, dict):
                cleaned[key] = dict(val)
        return cleaned

    def _save_budget_unlocked(self, days: dict[str, dict[str, Any]], today_key: str) -> None:
        # Retain recent days only to keep state compact.
        kept_keys = sorted(days.keys())[-14:]
        pruned = {k: days[k] for k in kept_keys}
        _atomic_write_json(
            delivery_budget_path(self.config),
            {"today_pt": today_key, "days": pruned},
        )

    def _get_proactive_count_unlocked(self, date_key: str) -> int:
        days = self._load_budget_unlocked()
        entry = days.get(date_key) or {}
        try:
            return max(0, int(entry.get("proactive_sent", 0)))
        except (TypeError, ValueError):
            return 0

    def _record_send_unlocked(
        self,
        date_key: str,
        now_ts: float,
        *,
        proactive: bool,
        urgent: bool,
        owner_initiated: bool,
    ) -> int:
        days = self._load_budget_unlocked()
        entry = dict(days.get(date_key) or {})
        proactive_count = max(0, int(entry.get("proactive_sent", 0)))
        owner_count = max(0, int(entry.get("owner_sent", 0)))
        urgent_count = max(0, int(entry.get("urgent_sent", 0)))

        if proactive:
            proactive_count += 1
        if owner_initiated:
            owner_count += 1
        if urgent:
            urgent_count += 1

        days[date_key] = {
            "proactive_sent": proactive_count,
            "owner_sent": owner_count,
            "urgent_sent": urgent_count,
            "updated_at": now_ts,
        }
        self._save_budget_unlocked(days, date_key)
        return proactive_count

    def _load_deferred_unlocked(self) -> list[dict[str, Any]]:
        path = deferred_deliveries_path(self.config)
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _save_deferred_unlocked(self, items: list[dict[str, Any]]) -> None:
        _atomic_write_json(deferred_deliveries_path(self.config), {"items": items})

    def _dispatch_discord(self, content: str) -> bool:
        if self._post_fn is not None:
            return bool(self._post_fn(int(self.config.channel_id), content))
        if self.config.dry_run:
            return True

        token = _resolve_discord_bot_token(secrets_path=self.secrets_path)
        if not token:
            logger.warning("DISCORD_BOT_TOKEN not configured; cannot deliver BRIEF message.")
            return False

        url = f"{_DISCORD_API_BASE}/channels/{int(self.config.channel_id)}/messages"
        body = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": _DISCORD_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning(
                "Failed to deliver BRIEF message to channel %s: %s",
                self.config.channel_id,
                exc,
            )
            return False

    def deliver(
        self,
        response_text: str,
        *,
        tag: str = "",
        event: Mapping[str, Any] | None = None,
        owner_initiated: bool = False,
        urgent: bool = False,
        now: float | datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate delivery policy and either send, defer, or suppress `response_text`."""
        should_send, kind = should_deliver_response(response_text)
        date_key, now_ts = self._pt_date_key(now=now)
        quiet = self.is_quiet_hours(now=now_ts)
        limit = max(0, int(self.config.max_proactive_messages_per_day))

        if not should_send:
            with self._lock:
                used = self._get_proactive_count_unlocked(date_key)
            return {
                "delivered": False,
                "status": "suppressed_ignore",
                "kind": kind,
                "quiet_hours": quiet,
                "budget_used": used,
                "budget_limit": limit,
            }

        event_source = str((event or {}).get("source") or "").strip().lower()
        is_owner = bool(
            owner_initiated
            or (event_source == "discord" and bool((event or {}).get("trusted", True)))
        )
        is_urgent = _is_response_urgent(response_text, urgent=urgent)
        formatted = _format_discord_message(response_text)

        with self._lock:
            used = self._get_proactive_count_unlocked(date_key)

            if quiet and not is_owner and not is_urgent:
                items = self._load_deferred_unlocked()
                items.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "kind": kind,
                        "tag": tag,
                        "event_id": (event or {}).get("id"),
                        "source": event_source,
                        "content": formatted,
                        "queued_at": now_ts,
                        "queued_at_pt": _format_pt_timestamp(
                            now_ts, tz_name=self.config.timezone
                        ),
                        "reason": "quiet_hours",
                    }
                )
                self._save_deferred_unlocked(items)
                return {
                    "delivered": False,
                    "status": "deferred_quiet_hours",
                    "kind": kind,
                    "quiet_hours": True,
                    "budget_used": used,
                    "budget_limit": limit,
                }

            is_proactive = not is_owner and not is_urgent
            if is_proactive and used >= limit:
                return {
                    "delivered": False,
                    "status": "suppressed_budget",
                    "kind": kind,
                    "quiet_hours": quiet,
                    "budget_used": used,
                    "budget_limit": limit,
                }

            sent = self._dispatch_discord(formatted)
            if not sent:
                return {
                    "delivered": False,
                    "status": "send_failed",
                    "kind": kind,
                    "quiet_hours": quiet,
                    "budget_used": used,
                    "budget_limit": limit,
                }

            new_used = self._record_send_unlocked(
                date_key,
                now_ts,
                proactive=is_proactive,
                urgent=is_urgent,
                owner_initiated=is_owner,
            )
            return {
                "delivered": True,
                "status": "dry_run" if self.config.dry_run and self._post_fn is None else "sent",
                "kind": kind,
                "quiet_hours": quiet,
                "budget_used": new_used,
                "budget_limit": limit,
                "content": formatted,
            }

    def flush_deferred(self, now: float | datetime | None = None) -> dict[str, Any]:
        """Flush queued quiet-hours deliveries once outside quiet hours, honoring daily budget."""
        date_key, now_ts = self._pt_date_key(now=now)
        if self.is_quiet_hours(now=now_ts):
            with self._lock:
                pending = len(self._load_deferred_unlocked())
            return {
                "flushed": 0,
                "remaining": pending,
                "skipped": "quiet_hours",
            }

        limit = max(0, int(self.config.max_proactive_messages_per_day))
        flushed = 0
        failed = 0
        stopped_reason: str | None = None

        with self._lock:
            items = self._load_deferred_unlocked()
            if not items:
                return {"flushed": 0, "remaining": 0, "skipped": None}

            remaining: list[dict[str, Any]] = []
            used = self._get_proactive_count_unlocked(date_key)

            for idx, item in enumerate(items):
                if used >= limit:
                    stopped_reason = "budget_exhausted"
                    remaining.extend(items[idx:])
                    break

                content = _wrap_urls_in_angles(str(item.get("content") or "").strip())
                if not content:
                    continue

                if not self._dispatch_discord(content):
                    failed += 1
                    stopped_reason = "send_failed"
                    remaining.extend(items[idx:])
                    break

                used = self._record_send_unlocked(
                    date_key,
                    now_ts,
                    proactive=True,
                    urgent=False,
                    owner_initiated=False,
                )
                flushed += 1

            self._save_deferred_unlocked(remaining)

        return {
            "flushed": flushed,
            "remaining": len(remaining),
            "failed": failed,
            "skipped": stopped_reason,
        }


class BriefDaemonRunner:
    """Single-process coordinator running BRIEF watchers, timers, router, and watchdog."""

    def __init__(
        self,
        config: BriefConfig,
        *,
        queue: BriefQueue | None = None,
        cursors: CursorStore | None = None,
        gmail_watcher: GmailWatcher | None = None,
        calendar_watcher: CalendarWatcher | None = None,
        router: BriefRouter | None = None,
        delivery_gate: BriefDeliveryGate | None = None,
        approval_gate: BriefApprovalGate | None = None,
        watchdog_fn: Callable[..., dict[str, Any]] | None = None,
        now_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._now_fn = now_fn or time.time
        self._sleep_fn = sleep_fn or time.sleep

        self.queue = queue or BriefQueue(
            config.db_path,
            allowlisted_senders=config.allowlisted_emails,
        )
        self.queue.init_db()
        ensure_memory_scaffold(config.memory_dir)

        self.cursors = cursors or CursorStore(config.cursors_path)
        self.gmail_watcher = gmail_watcher or GmailWatcher(
            self.queue,
            self.cursors,
            config,
            now_fn=self._now_fn,
            sleep_fn=self._sleep_fn,
        )
        self.calendar_watcher = calendar_watcher or CalendarWatcher(
            self.queue,
            self.cursors,
            config,
            now_fn=self._now_fn,
            sleep_fn=self._sleep_fn,
        )
        if router is not None:
            self.router = router
        else:
            harness = DryRunHarness() if config.dry_run else None
            self.router = BriefRouter(
                config,
                queue=self.queue,
                harness=harness,
                now_fn=self._now_fn,
            )

        self.delivery_gate = delivery_gate or BriefDeliveryGate(
            config,
            now_fn=self._now_fn,
        )
        if approval_gate is not None:
            self.approval_gate = approval_gate
        else:
            from tools.brief.gate import BriefApprovalGate

            self.approval_gate = BriefApprovalGate(
                config,
                queue=self.queue,
                now_fn=self._now_fn,
            )
        self._watchdog_fn = watchdog_fn or run_watchdog

        self._last_gmail_at: float | None = None
        self._last_calendar_at: float | None = None
        self._last_router_at: float | None = None
        self._last_watchdog_at: float | None = None
        self._last_morning_brief_date: str | None = None
        self._last_daily_review_date: str | None = None

    @staticmethod
    def _is_due(last_run: float | None, interval_seconds: float, now_ts: float) -> bool:
        if last_run is None:
            return True
        return (now_ts - last_run) >= max(0.0, float(interval_seconds))

    def _run_timers(self, now_ts: float) -> dict[str, int | None]:
        dt = _resolve_tz_datetime(now_ts, tz_name=self.config.timezone)
        date_str = dt.strftime("%Y-%m-%d")
        enqueued: dict[str, int | None] = {}

        if dt.hour >= _MORNING_BRIEF_HOUR_PT and self._last_morning_brief_date != date_str:
            try:
                ev_id = enqueue_morning_brief(
                    self.queue, now=now_ts, tz_name=self.config.timezone
                )
                self._last_morning_brief_date = date_str
                enqueued["morning_brief"] = ev_id
            except Exception:
                logger.exception("BRIEF daemon morning_brief enqueue error")

        if dt.hour >= _DAILY_REVIEW_HOUR_PT and self._last_daily_review_date != date_str:
            try:
                ev_id = enqueue_daily_review(
                    self.queue, now=now_ts, tz_name=self.config.timezone
                )
                self._last_daily_review_date = date_str
                enqueued["daily_review"] = ev_id
            except Exception:
                logger.exception("BRIEF daemon daily_review enqueue error")

        return enqueued

    def run_tick(self, now: float | None = None) -> dict[str, Any]:
        """Execute one scheduled daemon tick across timers, watchers, router, and watchdog."""
        now_ts = float(self._now_fn() if now is None else now)

        if is_kill_switch_active(self.config):
            return {
                "timestamp": now_ts,
                "timestamp_pt": _format_pt_timestamp(now_ts, tz_name=self.config.timezone),
                "paused": True,
                "reason": "kill_switch_active",
                "routed": 0,
                "delivered": 0,
                "errors": {},
            }

        errors: dict[str, str] = {}
        summary: dict[str, Any] = {
            "timestamp": now_ts,
            "timestamp_pt": _format_pt_timestamp(now_ts, tz_name=self.config.timezone),
            "paused": False,
            "timers": self._run_timers(now_ts),
            "gmail": None,
            "calendar": None,
            "flushed": None,
            "gate": None,
            "routed": 0,
            "delivered": 0,
            "outcomes": [],
            "watchdog": None,
            "errors": errors,
        }

        if self._is_due(self._last_gmail_at, self.config.gmail_interval_seconds, now_ts):
            self._last_gmail_at = now_ts
            try:
                summary["gmail"] = self.gmail_watcher.poll_once()
            except Exception as exc:
                logger.exception("GmailWatcher.poll_once failed")
                errors["gmail"] = str(exc)

        if self._is_due(self._last_calendar_at, self.config.calendar_interval_seconds, now_ts):
            self._last_calendar_at = now_ts
            try:
                summary["calendar"] = self.calendar_watcher.poll_once()
            except Exception as exc:
                logger.exception("CalendarWatcher.poll_once failed")
                errors["calendar"] = str(exc)

        if self._is_due(self._last_router_at, self.config.router_interval_seconds, now_ts):
            self._last_router_at = now_ts
            try:
                summary["flushed"] = self.delivery_gate.flush_deferred(now=now_ts)
            except Exception as exc:
                logger.exception("DeliveryGate.flush_deferred failed")
                errors["flush_deferred"] = str(exc)

            outcomes: list[dict[str, Any]] = []
            delivered_count = 0
            for _ in range(_MAX_ROUTER_DRAIN_PER_TICK):
                try:
                    outcome = self.router.process_one(now=now_ts)
                except Exception as exc:
                    logger.exception("BriefRouter.process_one failed")
                    errors["router"] = str(exc)
                    break

                if outcome is None:
                    break

                delivery_res: dict[str, Any] | None = None
                if outcome.get("status") == "done":
                    try:
                        delivery_res = self.delivery_gate.deliver(
                            str(outcome.get("response") or ""),
                            tag=str(outcome.get("tag") or ""),
                            event={
                                "id": outcome.get("event_id"),
                                "source": outcome.get("source"),
                            },
                            now=now_ts,
                        )
                        if delivery_res.get("delivered"):
                            delivered_count += 1
                    except Exception as exc:
                        logger.exception("BriefDeliveryGate.deliver failed")
                        errors["delivery_gate"] = str(exc)

                outcomes.append(
                    {
                        **outcome,
                        "delivery": delivery_res,
                    }
                )

            summary["routed"] = len(outcomes)
            summary["delivered"] = delivered_count
            summary["outcomes"] = outcomes

            try:
                expired_ids = self.approval_gate.expire_stale_proposals(now=now_ts)
                scan_res = self.approval_gate.scan_pending_proposals(now=now_ts)
                touch_heartbeat(self.config.heartbeats_dir, "gate", now=now_ts)
                summary["gate"] = {
                    **scan_res,
                    "expired_staged_ids": expired_ids,
                    "expired_ids": list(
                        dict.fromkeys([*expired_ids, *scan_res.get("expired_ids", [])])
                    ),
                    "expired": len(expired_ids) + int(scan_res.get("expired", 0)),
                }
            except Exception as exc:
                logger.exception("BriefApprovalGate tick failed")
                errors["gate"] = str(exc)

        if self._is_due(self._last_watchdog_at, _WATCHDOG_INTERVAL_SECONDS, now_ts):
            self._last_watchdog_at = now_ts
            try:
                summary["watchdog"] = self._watchdog_fn(
                    self.config,
                    self.queue,
                    dry_run=self.config.dry_run,
                    now=now_ts,
                )
            except Exception as exc:
                logger.exception("Watchdog check failed")
                errors["watchdog"] = str(exc)

        return summary


def _is_daemon_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        status_path = Path(f"/proc/{pid}/status")
        if cmdline_path.exists() and status_path.exists():
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="ignore"
            )
            tgid_lines = [
                line.split()[1]
                for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.startswith("Tgid:") and len(line.split()) >= 2
            ]
            if (tgid_lines and int(tgid_lines[0]) != pid) or "daemon" not in cmdline:
                return False
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def get_daemon_pid(
    cfg: BriefConfig | None = None,
    *,
    state_dir: Path | str | None = None,
    proc_root: str | Path = "/proc",
) -> int | None:
    """Return the live BRIEF daemon PID if running, reconciling or cleaning stale PID files."""
    resolved = _resolve_cfg(cfg, state_dir=state_dir)
    return reconcile_live_daemon_pid(
        pid_file=pid_file_path(resolved),
        lock_path=lock_file_path(resolved),
        script_name="daemon.py",
        subcommands=("run",),
        proc_root=proc_root,
    )


def get_daemon_status(
    cfg: BriefConfig | None = None,
    *,
    state_dir: Path | str | None = None,
    proc_root: str | Path = "/proc",
) -> dict[str, Any]:
    """Return status metadata for the BRIEF background daemon."""
    resolved = _resolve_cfg(cfg, state_dir=state_dir)
    pid = get_daemon_pid(resolved, proc_root=proc_root)
    lock_path = lock_file_path(resolved)
    pid_path = pid_file_path(resolved)
    log_path = log_file_path(resolved)
    return {
        "running": pid is not None,
        "pid": pid,
        "lock_held": is_lock_held(lock_path),
        "pid_file": str(pid_path),
        "lock_file": str(lock_path),
        "log_file": str(log_path),
        "state_dir": str(resolved.state_dir),
        "kill_switch_active": is_kill_switch_active(resolved),
        "dry_run": bool(resolved.dry_run),
        "quiet_hours": is_quiet_hours(resolved),
    }


def start_daemon_background(
    cfg: BriefConfig | None = None,
    *,
    config_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    memory_dir: Path | str | None = None,
) -> bool:
    """Start the singleton BRIEF daemon in a detached background process."""
    resolved = _resolve_cfg(
        cfg,
        config_path=config_path,
        state_dir=state_dir,
        memory_dir=memory_dir,
    )
    lock_path = lock_file_path(resolved)
    pid = get_daemon_pid(resolved)
    if pid or is_lock_held(lock_path):
        terminate_duplicate_daemons("daemon.py", ("run",), keep_pid=pid)
        logger.info("BRIEF daemon already running (PID %s).", pid)
        return True

    terminate_duplicate_daemons("daemon.py", ("run",))
    resolved.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_file_path(resolved)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--state-dir",
        str(resolved.state_dir),
        "--memory-dir",
        str(resolved.memory_dir),
    ]
    if resolved.config_path is not None:
        cmd.extend(["--config", str(resolved.config_path)])

    with log_path.open("a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=str(_REPO_ROOT),
        )

    for _ in range(20):
        if proc.poll() is not None:
            logger.error("BRIEF daemon exited prematurely with code %s.", proc.returncode)
            return False
        if get_daemon_pid(resolved) is not None:
            return True
        time.sleep(0.1)

    return proc.poll() is None


def stop_daemon(
    cfg: BriefConfig | None = None,
    *,
    state_dir: Path | str | None = None,
) -> bool:
    """Stop the running BRIEF daemon gracefully (SIGTERM with SIGKILL fallback)."""
    resolved = _resolve_cfg(cfg, state_dir=state_dir)
    pid_path = pid_file_path(resolved)
    pid = get_daemon_pid(resolved)
    if not pid:
        terminate_duplicate_daemons("daemon.py", ("run",))
        pid_path.unlink(missing_ok=True)
        return True

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            if not _is_daemon_pid_alive(pid):
                break
            time.sleep(0.1)
        if _is_daemon_pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
    except OSError as exc:
        logger.warning("Error while stopping BRIEF daemon PID %s: %s", pid, exc)

    terminate_duplicate_daemons("daemon.py", ("run",), keep_pid=None)
    pid_path.unlink(missing_ok=True)
    return True


def ensure_brief_daemon_running(
    cfg: BriefConfig | None = None,
    *,
    config_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    memory_dir: Path | str | None = None,
) -> bool:
    """Idempotently ensure the singleton BRIEF daemon is running."""
    resolved = _resolve_cfg(
        cfg,
        config_path=config_path,
        state_dir=state_dir,
        memory_dir=memory_dir,
    )
    if get_daemon_pid(resolved) is not None:
        return True
    return start_daemon_background(resolved)


def run_daemon_loop(
    cfg: BriefConfig | None = None,
    max_ticks: int | None = None,
    *,
    runner: BriefDaemonRunner | None = None,
    stop_event: threading.Event | None = None,
    acquire_lock: bool = True,
    tick_sleep_seconds: float | None = None,
) -> bool:
    """Acquire singleton lock and run the BRIEF daemon loop until signaled or `max_ticks`."""
    resolved = _resolve_cfg(cfg)
    lock_handle = None
    pid_path = pid_file_path(resolved)
    lock_path = lock_file_path(resolved)

    if acquire_lock:
        lock_handle = acquire_singleton_lock(lock_path)
        if lock_handle is None:
            logger.info("BRIEF daemon already running (lock held). Exiting.")
            return False
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    local_stop = stop_event or threading.Event()
    prev_sigterm = None
    prev_sigint = None
    installed_signals = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("BRIEF daemon received signal %s; stopping cleanly.", signum)
        local_stop.set()

    if threading.current_thread() is threading.main_thread():
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        installed_signals = True

    active_runner = runner or BriefDaemonRunner(resolved)
    sleep_step = (
        max(0.05, float(tick_sleep_seconds))
        if tick_sleep_seconds is not None
        else max(0.5, min(5.0, float(resolved.router_interval_seconds)))
    )

    ticks = 0
    try:
        while not local_stop.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            ticks += 1

            try:
                active_runner.run_tick()
            except Exception:
                logger.exception("Unhandled exception in BRIEF daemon tick")

            if max_ticks is not None and ticks >= max_ticks:
                break

            local_stop.wait(timeout=sleep_step)
    finally:
        if installed_signals:
            with contextlib.suppress(Exception):
                signal.signal(signal.SIGTERM, prev_sigterm)
                signal.signal(signal.SIGINT, prev_sigint)
        if acquire_lock:
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            release_singleton_lock(lock_handle)

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BRIEF singleton background daemon.")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("run", "start", "stop", "restart", "status", "json"),
        default="status",
        help="Daemon action (default: status).",
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional BriefConfig TOML path.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override state directory.")
    parser.add_argument("--memory-dir", type=Path, default=None, help="Override memory directory.")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode.")
    parser.add_argument("--max-ticks", type=int, default=None, help="Optional max ticks for run.")
    args = parser.parse_args(argv)

    cfg = _resolve_cfg(
        config_path=args.config,
        state_dir=args.state_dir,
        memory_dir=args.memory_dir,
    )
    if args.dry_run:
        cfg.dry_run = True

    if args.action == "run":
        ok = run_daemon_loop(cfg, max_ticks=args.max_ticks)
        return 0 if ok else 1
    if args.action == "start":
        return 0 if start_daemon_background(cfg) else 1
    if args.action == "stop":
        return 0 if stop_daemon(cfg) else 1
    if args.action == "restart":
        stop_daemon(cfg)
        time.sleep(0.5)
        return 0 if start_daemon_background(cfg) else 1
    if args.action == "json":
        print(json.dumps(get_daemon_status(cfg), indent=2, sort_keys=True))
        return 0

    st = get_daemon_status(cfg)
    status_label = "RUNNING" if st["running"] else "STOPPED"
    print(f"BRIEF Daemon: {status_label}")
    if st["pid"] is not None:
        print(f"  PID         : {st['pid']}")
    print(f"  State Dir   : {st['state_dir']}")
    print(f"  Log File    : {st['log_file']}")
    print(f"  Kill Switch : {'ACTIVE' if st['kill_switch_active'] else 'inactive'}")
    print(f"  Quiet Hours : {'active' if st['quiet_hours'] else 'inactive'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
