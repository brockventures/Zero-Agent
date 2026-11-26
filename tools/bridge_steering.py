"""
bridge_steering.py — Atomic per-channel mid-turn steering queue & routing helpers.

Stdlib-only and fast to import so both the Discord bridge processes and lightweight
Antigravity CLI lifecycle hooks (.agents/hooks/steering_hook.py) can share one
race-free queue without pulling in discord.py or heavy bridge dependencies.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

STEERING_QUEUE_DIR = Path("/workspace/data/steering_queue")
DEFAULT_RUNTIME_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "runtime_rules.json"
DEFAULT_HYBRID_STEERING_ENABLED = True
DEFAULT_HOOK_TIMEOUT_SECONDS = 2.5

_MENTION_PREFIX_RE = re.compile(r"^(?:<@[!&]?\d+>\s*)+")
_HARD_ABORT_SLASH_RE = re.compile(r"^/(?:stop|cancel|abort|halt)(?:\s+(?:now|please|turn|task))?$")
_HARD_ABORT_IMPERATIVE_RE = re.compile(
    r"^(?:(?:wait|hey|yo|no|please|actually|ok|okay)[,\s]+)*"
    r"(?:"
    r"(?:stop|cancel|abort|halt)(?:\s+(?:doing\s+(?:that|this)|that|this|it|everything|all|the\s+turn|this\s+turn|current\s+turn|now|right\s+now|please|immediately))*"
    r"|(?:never\s*mind|forget\s+(?:it|about\s+it|that)|scratch\s+that|belay\s+that)(?:\s+(?:please|then|now))*"
    r")$"
)


def _resolve_queue_dir(queue_dir: Path | str | None = None) -> Path:
    """Resolve the active steering queue directory (honoring ZERO_STEERING_QUEUE_DIR)."""
    if queue_dir is not None:
        return Path(queue_dir)
    env_dir = os.environ.get("ZERO_STEERING_QUEUE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return STEERING_QUEUE_DIR


def _channel_key(channel_id: int | str) -> str:
    """Normalize a Discord channel/thread ID into a safe filename stem."""
    raw = str(channel_id).strip()
    if raw.lstrip("-").isdigit():
        return str(int(raw))
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return sanitized or "unknown"


def _normalize_channel_id(channel_id: int | str) -> int | str:
    """Preserve integer channel IDs when numeric, otherwise return stripped string."""
    if isinstance(channel_id, int):
        return channel_id
    raw = str(channel_id).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _queue_paths(channel_id: int | str, queue_dir: Path | str | None = None) -> tuple[Path, Path, Path, str]:
    """Return (queue_dir, queue_json_path, lock_path, channel_key)."""
    qdir = _resolve_queue_dir(queue_dir)
    ch_key = _channel_key(channel_id)
    return qdir, qdir / f"{ch_key}.json", qdir / f"{ch_key}.lock", ch_key


@contextmanager
def _channel_lock(qdir: Path, ch_key: str) -> Iterator[None]:
    """Acquire an exclusive advisory lock on <channel_id>.lock."""
    qdir.mkdir(parents=True, exist_ok=True)
    lock_path = qdir / f"{ch_key}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_entries_unlocked(queue_path: Path) -> list[dict[str, Any]]:
    """Read queue entries from disk, tolerating missing or corrupt JSON."""
    if not queue_path.exists():
        return []
    try:
        raw = queue_path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        payload = json.loads(raw)
        if isinstance(payload, dict):
            entries = payload.get("entries", [])
        elif isinstance(payload, list):
            entries = payload
        else:
            return []
        return [dict(item) for item in entries if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def _write_entries_atomic_unlocked(
    qdir: Path, queue_path: Path, ch_key: str, entries: list[dict[str, Any]]
) -> None:
    """Write queue entries via a PID/thread-unique temp file and atomic os.replace()."""
    qdir.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{ch_key}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = qdir / tmp_name
    payload = {
        "channel_id": ch_key,
        "updated_at": time.time(),
        "entries": entries,
    }
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, queue_path)
    except Exception:
        with suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()
        raise


def enqueue_soft_steer(
    channel_id: int | str,
    directive: str,
    *,
    author_name: str | None = None,
    is_group: bool = False,
    original_task: str | None = None,
    turn_seq: int | None = None,
    queue_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Atomically append a pending mid-turn soft steer for a channel."""
    qdir, queue_path, _, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    entry: dict[str, Any] = {
        "steer_id": uuid.uuid4().hex,
        "channel_id": _normalize_channel_id(channel_id),
        "directive": str(directive),
        "author_name": author_name,
        "is_group": bool(is_group),
        "original_task": original_task,
        "turn_seq": turn_seq,
        "created_at": time.time(),
        "consumed_at": None,
        "consumer": None,
    }
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        entries.append(entry)
        _write_entries_atomic_unlocked(qdir, queue_path, ch_key, entries)
    return dict(entry)


def peek_pending_steers(
    channel_id: int | str,
    *,
    queue_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return unconsumed steer entries for a channel without marking them consumed."""
    qdir, queue_path, _, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    if not queue_path.exists():
        return []
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        return [dict(e) for e in entries if e.get("consumed_at") is None]


def consume_pending_steers(
    channel_id: int | str,
    *,
    consumer: str = "hook",
    queue_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Atomically mark all pending steers for a channel as consumed and return them."""
    qdir, queue_path, _, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    if not queue_path.exists():
        return []
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        now = time.time()
        consumed: list[dict[str, Any]] = []
        for entry in entries:
            if entry.get("consumed_at") is None:
                entry["consumed_at"] = now
                entry["consumer"] = consumer
                consumed.append(dict(entry))
        if not consumed:
            return []
        _write_entries_atomic_unlocked(qdir, queue_path, ch_key, entries)
        return consumed


def is_steer_pending(
    channel_id: int | str,
    steer_id: str,
    *,
    queue_dir: Path | str | None = None,
) -> bool:
    """Return True iff the given steer_id exists in the queue and has not been consumed."""
    if not steer_id:
        return False
    qdir, queue_path, _, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    if not queue_path.exists():
        return False
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        return any(
            e.get("steer_id") == steer_id and e.get("consumed_at") is None
            for e in entries
        )


def cancel_pending_steer(
    channel_id: int | str,
    steer_id: str,
    *,
    queue_dir: Path | str | None = None,
) -> bool:
    """Atomically remove a specific unconsumed steer (e.g. before SIGINT fallback).

    Returns True only if the steer was still unconsumed and removed, guaranteeing that
    a fallback never double-delivers a steer already consumed by a hook.
    """
    if not steer_id:
        return False
    qdir, queue_path, _, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    if not queue_path.exists():
        return False
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        remaining: list[dict[str, Any]] = []
        removed = False
        for entry in entries:
            if (
                not removed
                and entry.get("steer_id") == steer_id
                and entry.get("consumed_at") is None
            ):
                removed = True
                continue
            remaining.append(entry)
        if not removed:
            return False
        _write_entries_atomic_unlocked(qdir, queue_path, ch_key, remaining)
        return True


def purge_channel_steers(
    channel_id: int | str,
    *,
    queue_dir: Path | str | None = None,
) -> int:
    """Clear all steering entries and remove queue/lock files for a channel."""
    qdir, queue_path, lock_path, ch_key = _queue_paths(channel_id, queue_dir=queue_dir)
    if not qdir.exists():
        return 0
    if not queue_path.exists() and not lock_path.exists():
        return 0
    with _channel_lock(qdir, ch_key):
        entries = _read_entries_unlocked(queue_path)
        count = len(entries)
        with suppress(FileNotFoundError):
            queue_path.unlink()
        with suppress(FileNotFoundError):
            lock_path.unlink()
    return count


def is_hard_abort_directive(text: str) -> bool:
    """Return True if text is an explicit stop/abort command rather than a normal steer."""
    if not text or not isinstance(text, str):
        return False
    cleaned = _MENTION_PREFIX_RE.sub("", text.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    lowered = cleaned.lower().rstrip("!.,?;:")
    if not lowered:
        return False
    if _HARD_ABORT_SLASH_RE.match(lowered):
        return True
    if len(lowered) > 48 or len(lowered.split()) > 6:
        return False
    return bool(_HARD_ABORT_IMPERATIVE_RE.match(lowered))


def _load_runtime_rules(rules_path: Path | None = None) -> dict[str, Any]:
    """Load config/runtime_rules.json safely using stdlib only."""
    path = rules_path or DEFAULT_RUNTIME_RULES_PATH
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return {}


def is_steering_disabled(
    channel_id: int | str | None,
    rules: dict[str, Any] | None = None,
) -> bool:
    """Return True if mid-turn steering is disabled for the given channel ID."""
    if channel_id is None:
        return False
    cfg = rules if isinstance(rules, dict) else _load_runtime_rules()
    disabled = cfg.get("steering_disabled_channels") or []
    target = str(channel_id).strip()
    return any(str(item).strip() == target for item in disabled if item is not None)


def is_hybrid_steering_enabled(rules: dict[str, Any] | None = None) -> bool:
    """Return True when hybrid hook+SIGINT steering is enabled (default True)."""
    cfg = rules if isinstance(rules, dict) else _load_runtime_rules()
    val = cfg.get("hybrid_steering_enabled", DEFAULT_HYBRID_STEERING_ENABLED)
    return DEFAULT_HYBRID_STEERING_ENABLED if val is None else bool(val)


def get_hook_drain_timeout_seconds(rules: dict[str, Any] | None = None) -> float:
    """Return hook-drain wait ceiling before falling back to SIGINT (default 2.5s)."""
    cfg = rules if isinstance(rules, dict) else _load_runtime_rules()
    raw = cfg.get("hybrid_steering_hook_timeout_seconds", DEFAULT_HOOK_TIMEOUT_SECONDS)
    if raw is None:
        return DEFAULT_HOOK_TIMEOUT_SECONDS
    try:
        val = float(raw)
        return max(0.0, val)
    except (TypeError, ValueError):
        return DEFAULT_HOOK_TIMEOUT_SECONDS


def format_hook_steer_message(entries: list[dict[str, Any]]) -> str:
    """Format one or more consumed steer entries into a concise hook injection message."""
    if not entries:
        return ""
    lines = ["[MID-TURN USER STEERING UPDATE]"]
    first_orig = str(entries[0].get("original_task") or "").strip()
    if first_orig:
        snippet = first_orig[:300] + ("..." if len(first_orig) > 300 else "")
        lines.append(f'Original task: "{snippet}"')

    lines.append("New follow-up directive(s) received mid-turn:")
    for entry in entries:
        directive = str(entry.get("directive") or "").strip()
        author = str(entry.get("author_name") or "").strip()
        if author:
            lines.append(f'- {author}: "{directive}"')
        else:
            lines.append(f'- "{directive}"')

    lines.append(
        "Incorporate this update immediately into your current turn without restarting "
        "from scratch. If it modifies, clarifies, or supersedes your current approach, "
        "pivot cleanly."
    )
    return "\n".join(lines)
