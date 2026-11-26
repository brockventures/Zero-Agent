"""Pure-Python heartbeat and failed-event watchdog for BRIEF (zero LLM calls)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.brief.config import (
    DEFAULT_CHANNEL_ID,
    DEFAULT_SECRETS_PATH,
    DEFAULT_STATE_DIR,
    BriefConfig,
    is_kill_switch_active,
)
from tools.brief.queue import BriefQueue

logger = logging.getLogger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_USER_AGENT = "DiscordBot (https://github.com/brockventures/Zero-Agent, 1.0)"
_MAX_ALERT_CHARS = 1900
_BARE_URL_RE = re.compile(r"(?<!<)(https?://[^\s<>]+)")

_KNOWN_COMPONENTS = ("gmail_watcher", "calendar_watcher", "router")

__all__ = [
    "check_brief_health",
    "post_brief_alert",
    "run_watchdog",
]


def _format_pt_timestamp(ts: float | None, tz_name: str = "America/Los_Angeles") -> str:
    epoch = time.time() if ts is None else float(ts)
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(epoch, tz=UTC).astimezone(tz)
    label = "PT" if tz_name == "America/Los_Angeles" else (dt.tzname() or "PT")
    return f"{dt.strftime('%Y-%m-%d %H:%M')} {label}"


def _wrap_urls_in_angles(text: str) -> str:
    return _BARE_URL_RE.sub(r"<\1>", text)


def _format_plain_alert(message: str, max_chars: int = _MAX_ALERT_CHARS) -> str:
    wrapped = _wrap_urls_in_angles(str(message or "").strip())
    if len(wrapped) <= max_chars:
        return wrapped
    return wrapped[: max_chars - 3].rstrip() + "..."


def _resolve_discord_bot_token(secrets_path: Path | str | None = DEFAULT_SECRETS_PATH) -> str:
    env_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    if secrets_path is None:
        return ""
    path = Path(secrets_path)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("DISCORD_BOT_TOKEN") or "").strip()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    serialized = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_cursors_safely(cursors_path: Path) -> dict[str, Any]:
    if not cursors_path.is_file():
        return {}
    try:
        raw = cursors_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return dict(data) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _discover_heartbeat_files(heartbeats_dir: Path) -> dict[str, list[Path]]:
    discovered: dict[str, list[Path]] = {}
    if not heartbeats_dir.is_dir():
        return discovered
    try:
        entries = list(heartbeats_dir.iterdir())
    except OSError:
        return discovered

    for entry in entries:
        if entry.name.startswith(".") or not entry.is_file():
            continue
        if entry.suffix not in {".hb", ".ts"}:
            continue
        discovered.setdefault(entry.stem, []).append(entry)

    # Sort each component's candidate files newest-mtime first (.hb preferred on tie).
    for name, paths in discovered.items():
        paths.sort(
            key=lambda p: (
                p.stat().st_mtime if p.exists() else 0.0,
                1 if p.suffix == ".hb" else 0,
            ),
            reverse=True,
        )
        discovered[name] = paths
    return discovered


def _parse_heartbeat_file(path: Path) -> tuple[float | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"unreadable heartbeat file {path.name}: {exc}"
    if not raw:
        return None, f"empty heartbeat file {path.name}"
    try:
        ts = float(raw)
    except ValueError:
        return None, f"corrupt heartbeat timestamp in {path.name}: {raw[:40]!r}"
    if math.isnan(ts) or math.isinf(ts):
        return None, f"non-finite heartbeat timestamp in {path.name}"
    return ts, None


def _list_failed_safely(queue: BriefQueue) -> list[dict[str, Any]]:
    if isinstance(queue, BriefQueue) and not queue.db_path.exists():
        return []
    return list(queue.list_failed_events())


def _is_router_expected(queue: BriefQueue, now_ts: float, router_interval: float) -> bool:
    if isinstance(queue, BriefQueue):
        if not queue.db_path.exists():
            return False
        try:
            with queue._tx() as conn:
                row = conn.execute(
                    """
                    SELECT MIN(received_at) AS oldest_ts
                    FROM events
                    WHERE status IN ('pending', 'delivering')
                    """
                ).fetchone()
            if row is not None and row["oldest_ts"] is not None:
                return (now_ts - float(row["oldest_ts"])) > (3.0 * router_interval)
        except Exception:  # noqa: BLE001
            return False
    return False


def check_brief_health(
    config: BriefConfig,
    queue: BriefQueue,
    now: float | None = None,
) -> dict[str, Any]:
    """Inspect heartbeats (`*.hb` / `*.ts`) and failed inbox rows without calling any LLM."""
    now_ts = time.time() if now is None else float(now)

    component_intervals: dict[str, float] = {
        "gmail_watcher": float(config.gmail_interval_seconds),
        "calendar_watcher": float(config.calendar_interval_seconds),
        "router": max(float(config.router_interval_seconds), 60.0),
    }

    discovered = _discover_heartbeat_files(config.heartbeats_dir)
    cursors = _load_cursors_safely(config.cursors_path)
    expected_components: set[str] = set()
    if str(cursors.get("gmail_history_id") or "").strip():
        expected_components.add("gmail_watcher")
    if str(cursors.get("calendar_sync_token") or "").strip():
        expected_components.add("calendar_watcher")
    if _is_router_expected(queue, now_ts, component_intervals["router"]):
        expected_components.add("router")

    all_names = list(_KNOWN_COMPONENTS) + sorted(
        name for name in discovered if name not in _KNOWN_COMPONENTS
    )

    heartbeats: dict[str, dict[str, Any]] = {}
    stale_heartbeats: list[str] = []
    corrupt_heartbeats: list[str] = []
    missing_heartbeats: list[str] = []
    issues: list[str] = []

    for name in all_names:
        interval = component_intervals.get(
            name,
            max(float(config.router_interval_seconds), 60.0),
        )
        max_age = 3.0 * interval
        candidates = discovered.get(name, [])

        if not candidates:
            is_expected = name in expected_components
            if is_expected:
                missing_heartbeats.append(name)
                issues.append(
                    f"Heartbeat missing for expected component '{name}' "
                    f"(threshold {max_age:.0f}s)."
                )
            heartbeats[name] = {
                "status": "missing" if is_expected else "not_started",
                "expected": is_expected,
                "healthy": not is_expected,
                "interval_seconds": interval,
                "max_age_seconds": max_age,
                "timestamp": None,
                "age_seconds": None,
                "path": None,
            }
            continue

        chosen_path = candidates[0]
        ts, parse_err = _parse_heartbeat_file(chosen_path)
        if parse_err is not None or ts is None:
            corrupt_heartbeats.append(name)
            issues.append(f"Corrupt heartbeat for '{name}': {parse_err}")
            heartbeats[name] = {
                "status": "corrupt",
                "expected": name in expected_components,
                "healthy": False,
                "interval_seconds": interval,
                "max_age_seconds": max_age,
                "timestamp": None,
                "age_seconds": None,
                "path": str(chosen_path),
                "error": parse_err,
            }
            continue

        age_seconds = max(0.0, now_ts - ts)
        is_stale = age_seconds > max_age
        if is_stale:
            stale_heartbeats.append(name)
            last_pt = _format_pt_timestamp(ts, tz_name=config.timezone)
            issues.append(
                f"Stale heartbeat for '{name}': age {age_seconds:.0f}s > "
                f"{max_age:.0f}s (3x {interval:.0f}s), last seen {last_pt}."
            )

        heartbeats[name] = {
            "status": "stale" if is_stale else "ok",
            "expected": True,
            "healthy": not is_stale,
            "interval_seconds": interval,
            "max_age_seconds": max_age,
            "timestamp": ts,
            "timestamp_pt": _format_pt_timestamp(ts, tz_name=config.timezone),
            "age_seconds": age_seconds,
            "path": str(chosen_path),
        }

    failed_events = _list_failed_safely(queue)
    if failed_events:
        issues.append(f"{len(failed_events)} failed event(s) in BRIEF inbox queue.")

    healthy = (
        not stale_heartbeats
        and not corrupt_heartbeats
        and not missing_heartbeats
        and not failed_events
    )

    return {
        "healthy": healthy,
        "checked_at": now_ts,
        "checked_at_pt": _format_pt_timestamp(now_ts, tz_name=config.timezone),
        "kill_switch_active": is_kill_switch_active(config),
        "heartbeats": heartbeats,
        "stale_heartbeats": stale_heartbeats,
        "corrupt_heartbeats": corrupt_heartbeats,
        "missing_heartbeats": missing_heartbeats,
        "failed_events": failed_events,
        "failed_count": len(failed_events),
        "issues": issues,
    }


def _load_alert_state(state_path: Path) -> dict[str, dict[str, Any]]:
    if not state_path.is_file():
        return {}
    try:
        raw = state_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    alerts = data.get("alerts")
    if not isinstance(alerts, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, val in alerts.items():
        if isinstance(key, str) and isinstance(val, dict):
            cleaned[key] = dict(val)
    return cleaned


def post_brief_alert(
    message: str,
    channel_id: int = DEFAULT_CHANNEL_ID,
    dry_run: bool = False,
    state_path: Path | str | None = None,
    cooldown_seconds: int = 1800,
    *,
    dedupe_key: str | None = None,
    now: float | None = None,
    secrets_path: Path | str | None = DEFAULT_SECRETS_PATH,
    timeout: float = 15.0,
) -> bool:
    """Post a plain-text alert to #brief via Discord REST API v10 with cooldown dedupe."""
    formatted = _format_plain_alert(message, max_chars=_MAX_ALERT_CHARS)
    if not formatted:
        return False

    now_ts = time.time() if now is None else float(now)
    resolved_state = (
        Path(state_path)
        if state_path is not None
        else (DEFAULT_STATE_DIR / "watchdog_alert_state.json")
    )

    key_source = f"{int(channel_id)}:{dedupe_key if dedupe_key is not None else formatted}"
    alert_hash = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:24]

    alerts = _load_alert_state(resolved_state)
    previous = alerts.get(alert_hash)
    if previous is not None and cooldown_seconds > 0:
        try:
            last_sent = float(previous.get("last_sent_at", 0.0))
        except (TypeError, ValueError):
            last_sent = 0.0
        if (now_ts - last_sent) < float(cooldown_seconds):
            return False

    if not dry_run:
        token = _resolve_discord_bot_token(secrets_path=secrets_path)
        if not token:
            logger.warning("DISCORD_BOT_TOKEN not configured; cannot post BRIEF alert.")
            return False

        url = f"{_DISCORD_API_BASE}/channels/{int(channel_id)}/messages"
        body = json.dumps({"content": formatted}).encode("utf-8")
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning("Failed to post BRIEF alert to channel %s: %s", channel_id, exc)
            return False

    retention = max(float(cooldown_seconds) * 2.0, 86400.0)
    pruned: dict[str, dict[str, Any]] = {}
    for key, entry in alerts.items():
        try:
            ts = float(entry.get("last_sent_at", 0.0))
        except (TypeError, ValueError):
            continue
        if (now_ts - ts) <= retention:
            pruned[key] = entry

    pruned[alert_hash] = {
        "last_sent_at": now_ts,
        "channel_id": int(channel_id),
        "preview": formatted[:120],
    }
    _atomic_write_json(resolved_state, {"alerts": pruned})
    return True


def _build_watchdog_alert(health: Mapping[str, Any], tz_name: str) -> tuple[str, str]:
    checked_pt = str(health.get("checked_at_pt") or _format_pt_timestamp(None, tz_name=tz_name))
    lines = [f"[BRIEF Watchdog] Health issue detected at {checked_pt}"]

    hb_map: Mapping[str, Any] = health.get("heartbeats") or {}
    for name in health.get("stale_heartbeats") or ():
        info = hb_map.get(name, {})
        age = info.get("age_seconds")
        limit = info.get("max_age_seconds")
        age_str = f"{float(age):.0f}s" if age is not None else "unknown"
        limit_str = f"{float(limit):.0f}s" if limit is not None else "unknown"
        lines.append(f"- Stale heartbeat: {name} (age {age_str} > {limit_str})")

    for name in health.get("corrupt_heartbeats") or ():
        info = hb_map.get(name, {})
        lines.append(f"- Corrupt heartbeat: {name} ({info.get('error', 'invalid')})")

    for name in health.get("missing_heartbeats") or ():
        lines.append(f"- Missing expected heartbeat: {name}")

    failed_events = list(health.get("failed_events") or ())
    if failed_events:
        lines.append(f"- Failed queue events: {len(failed_events)}")
        for ev in failed_events[:3]:
            ev_id = ev.get("id")
            tag = str(ev.get("target_tag") or ev.get("source") or "unknown")
            err = str(ev.get("error") or "unknown error").replace("\n", " ").strip()
            if len(err) > 120:
                err = err[:117] + "..."
            lines.append(f"  * event #{ev_id} [{tag}]: {err}")

    failed_ids = sorted(int(ev["id"]) for ev in failed_events if ev.get("id") is not None)
    dedupe_payload = json.dumps(
        {
            "stale": sorted(health.get("stale_heartbeats") or ()),
            "corrupt": sorted(health.get("corrupt_heartbeats") or ()),
            "missing": sorted(health.get("missing_heartbeats") or ()),
            "failed_ids": failed_ids,
        },
        sort_keys=True,
    )
    return _format_plain_alert("\n".join(lines)), dedupe_payload


def run_watchdog(
    config: BriefConfig,
    queue: BriefQueue,
    dry_run: bool = False,
    now: float | None = None,
    *,
    cooldown_seconds: int = 1800,
    state_path: Path | str | None = None,
    secrets_path: Path | str | None = DEFAULT_SECRETS_PATH,
) -> dict[str, Any]:
    """Run health check and dispatch a deduplicated alert to #brief if unhealthy."""
    now_ts = time.time() if now is None else float(now)
    health = check_brief_health(config, queue, now=now_ts)
    if health["healthy"]:
        return {
            "healthy": True,
            "health": health,
            "alert_dispatched": False,
            "alert_suppressed": False,
            "alert_message": None,
        }

    alert_message, dedupe_key = _build_watchdog_alert(health, tz_name=config.timezone)
    resolved_state = (
        Path(state_path)
        if state_path is not None
        else (config.state_dir / "watchdog_alert_state.json")
    )
    dispatched = post_brief_alert(
        alert_message,
        channel_id=config.channel_id,
        dry_run=dry_run,
        state_path=resolved_state,
        cooldown_seconds=cooldown_seconds,
        dedupe_key=dedupe_key,
        now=now_ts,
        secrets_path=secrets_path,
    )
    return {
        "healthy": False,
        "health": health,
        "alert_dispatched": dispatched,
        "alert_suppressed": not dispatched,
        "alert_message": alert_message,
    }
