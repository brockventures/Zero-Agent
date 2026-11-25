"""Task handoff validation, YAML frontmatter serialization, and staleness backoff engine for BRIEF."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.brief.config import BriefConfig

_TASK_SLUG_RE = re.compile(r"^[0-9]{8}-[a-z0-9][a-z0-9-]{1,62}$")
VALID_TASK_STATUSES: tuple[str, ...] = ("open", "blocked", "done", "canceled")
ACTIVE_TASK_STATUSES: tuple[str, ...] = ("open", "blocked")
CLOSED_TASK_STATUSES: tuple[str, ...] = ("done", "canceled")


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


def parse_iso_datetime(
    value: str,
    *,
    tz_name: str = "America/Los_Angeles",
) -> datetime:
    """Parse an ISO-8601 timestamp string and normalize to the target timezone."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Empty ISO-8601 timestamp")
    normalized = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def format_pt_iso(
    dt: datetime,
    *,
    tz_name: str = "America/Los_Angeles",
) -> str:
    """Format a datetime as an ISO-8601 string in Pacific Time (seconds precision)."""
    tz = ZoneInfo(tz_name)
    local_dt = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    return local_dt.replace(microsecond=0).isoformat()


def validate_task_slug(slug: str) -> str:
    """Validate task slug against _TASK_SLUG_RE and reject path traversal tokens."""
    if not isinstance(slug, str):
        raise ValueError(f"Invalid task slug: {slug!r}")
    candidate = slug.strip()
    if not candidate or "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"Invalid task slug {slug!r}: path separators or '..' are prohibited")
    if not _TASK_SLUG_RE.fullmatch(candidate):
        raise ValueError(
            f"Invalid task slug {slug!r}: must match {_TASK_SLUG_RE.pattern}"
        )
    return candidate


def resolve_task_path(task_dir: Path | str, slug: str) -> tuple[str, Path]:
    """Validate slug and verify strict path containment inside task_dir."""
    clean_slug = validate_task_slug(slug)
    root = Path(task_dir).resolve()
    target = (root / f"{clean_slug}.md").resolve()
    if target.parent != root:
        raise ValueError(f"Task path escapes task_dir: {slug!r}")
    return clean_slug, target


def validate_task_status(status: str) -> str:
    """Validate that task status is one of open, blocked, done, or canceled."""
    cleaned = str(status or "").strip().lower()
    if cleaned not in VALID_TASK_STATUSES:
        raise ValueError(
            f"Invalid task status {status!r}; expected one of {VALID_TASK_STATUSES}"
        )
    return cleaned


def _normalize_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        raise ValueError(f"Expected list of strings, got {type(raw).__name__}")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _strip_inline_yaml_comment(raw_val: str) -> str:
    in_double = False
    in_single = False
    escaped = False
    for idx, ch in enumerate(raw_val):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == "#" and not in_double and not in_single:
            if idx == 0 or raw_val[idx - 1].isspace():
                return raw_val[:idx].rstrip()
    return raw_val.rstrip()


def _parse_yaml_scalar_or_list(raw_val: str) -> Any:
    cleaned = _strip_inline_yaml_comment(raw_val).strip()
    if not cleaned:
        return ""
    if cleaned in ("~", "null", "Null", "NULL"):
        return ""
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    if len(cleaned) >= 2 and cleaned[0] == "'" and cleaned[-1] == "'":
        return cleaned[1:-1].replace("''", "'")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        inner = cleaned[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        return [
            str(_parse_yaml_scalar_or_list(p))
            for p in parts
            if p
        ]
    if re.fullmatch(r"-?[0-9]+", cleaned):
        return int(cleaned)
    return cleaned


def _split_frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n") and normalized != "---":
        raise ValueError("Task file missing leading YAML frontmatter delimiter '---'")
    lines = normalized.split("\n")
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError("Task file missing closing YAML frontmatter delimiter '---'")

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")

    parsed_fm: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in fm_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^\s*-\s+", line) and current_list_key is not None:
            item_raw = re.sub(r"^\s*-\s+", "", line, count=1)
            item_val = _parse_yaml_scalar_or_list(item_raw)
            if not isinstance(parsed_fm.get(current_list_key), list):
                parsed_fm[current_list_key] = []
            parsed_fm[current_list_key].append(str(item_val))
            continue

        current_list_key = None
        if ":" not in line:
            raise ValueError(f"Malformed YAML frontmatter line: {line!r}")
        key_part, _, val_part = line.partition(":")
        key = key_part.strip()
        if not key:
            raise ValueError(f"Empty YAML frontmatter key in line: {line!r}")
        cleaned_val = _strip_inline_yaml_comment(val_part).strip()
        if cleaned_val == "":
            parsed_fm[key] = ""
            current_list_key = key
        else:
            parsed_fm[key] = _parse_yaml_scalar_or_list(cleaned_val)

    return parsed_fm, body


def _normalize_frontmatter(
    frontmatter: dict[str, Any],
    *,
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be a dict")
    status = validate_task_status(frontmatter.get("status", "open"))
    waiting_on = str(frontmatter.get("waiting_on", "") or "").strip()

    raw_next_check = str(frontmatter.get("next_check", "") or "").strip()
    if raw_next_check:
        dt = parse_iso_datetime(raw_next_check, tz_name=tz_name)
        next_check = format_pt_iso(dt, tz_name=tz_name)
    else:
        next_check = ""

    if status in CLOSED_TASK_STATUSES and not raw_next_check:
        next_check = ""

    raw_entities = frontmatter.get("entities", [])
    entities = [] if raw_entities == "" else _normalize_str_list(raw_entities)

    raw_receipts = frontmatter.get("gate_receipts", [])
    gate_receipts = [] if raw_receipts == "" else _normalize_str_list(raw_receipts)

    raw_nudge_count = frontmatter.get("nudge_count", 0)
    if raw_nudge_count in ("", None):
        nudge_count = 0
    else:
        try:
            nudge_count = max(0, int(raw_nudge_count))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid nudge_count: {raw_nudge_count!r}") from exc

    raw_last_nudged = str(frontmatter.get("last_nudged_at", "") or "").strip()
    if raw_last_nudged:
        nudged_dt = parse_iso_datetime(raw_last_nudged, tz_name=tz_name)
        last_nudged_at = format_pt_iso(nudged_dt, tz_name=tz_name)
    else:
        last_nudged_at = ""

    return {
        "status": status,
        "waiting_on": waiting_on,
        "next_check": next_check,
        "entities": entities,
        "gate_receipts": gate_receipts,
        "nudge_count": nudge_count,
        "last_nudged_at": last_nudged_at,
    }


def render_task_file(frontmatter: dict[str, Any], body: str = "") -> str:
    """Serialize task frontmatter and markdown body to a standard BRIEF task file."""
    norm = _normalize_frontmatter(frontmatter)
    lines = [
        "---",
        f"status: {norm['status']}",
        f"waiting_on: {json.dumps(norm['waiting_on'], ensure_ascii=False)}",
        f"next_check: {json.dumps(norm['next_check'], ensure_ascii=False)}",
        f"entities: {json.dumps(norm['entities'], ensure_ascii=False)}",
        f"gate_receipts: {json.dumps(norm['gate_receipts'], ensure_ascii=False)}",
        f"nudge_count: {norm['nudge_count']}",
        f"last_nudged_at: {json.dumps(norm['last_nudged_at'], ensure_ascii=False)}",
        "---",
    ]
    cleaned_body = str(body or "").strip()
    if cleaned_body:
        lines.append("")
        lines.append(cleaned_body)
    return "\n".join(lines) + "\n"


def parse_task_file(
    path: Path | str,
    *,
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Parse a BRIEF task markdown file into normalized frontmatter and body."""
    file_path = Path(path)
    raw_text = file_path.read_text(encoding="utf-8")
    raw_fm, body = _split_frontmatter_and_body(raw_text)
    norm_fm = _normalize_frontmatter(raw_fm, tz_name=tz_name)
    mtime = file_path.stat().st_mtime if file_path.exists() else 0.0
    return {
        "slug": file_path.stem,
        "path": str(file_path),
        "mtime": mtime,
        "frontmatter": dict(norm_fm),
        "body": body.strip(),
        **norm_fm,
    }


def _atomic_write_text(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.tmp.{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target_path)


def create_or_update_task_handoff(
    task_dir: Path | str,
    slug: str,
    *,
    status: str = "open",
    waiting_on: str = "",
    next_check: str | None = None,
    entities: list[str] | None = None,
    summary: str = "",
    source_session: str = "",
    now: float | None = None,
    tz_name: str = "America/Los_Angeles",
    gate_receipts: list[str] | None = None,
) -> dict[str, Any]:
    """Validate and atomically create or update a task handoff markdown file."""
    clean_slug, target_path = resolve_task_path(task_dir, slug)
    validated_status = validate_task_status(status)
    now_dt = _resolve_pt_datetime(now, tz_name=tz_name)

    existing: dict[str, Any] | None = None
    if target_path.is_file():
        existing = parse_task_file(target_path, tz_name=tz_name)

    if next_check is not None and str(next_check).strip() != "":
        parsed_next_dt = parse_iso_datetime(str(next_check), tz_name=tz_name)
        validated_next_check = format_pt_iso(parsed_next_dt, tz_name=tz_name)
    else:
        validated_next_check = ""

    if validated_status in CLOSED_TASK_STATUSES:
        resolved_next_check = ""
        resolved_waiting_on = ""
    else:
        if validated_next_check:
            resolved_next_check = validated_next_check
        else:
            default_days = 3 if validated_status == "open" else 6
            resolved_next_check = format_pt_iso(
                now_dt + timedelta(days=default_days),
                tz_name=tz_name,
            )
        cleaned_waiting = str(waiting_on or "").strip()
        if cleaned_waiting:
            resolved_waiting_on = cleaned_waiting
        elif existing is not None and validated_status == "blocked":
            resolved_waiting_on = str(existing.get("waiting_on", "") or "").strip()
        else:
            resolved_waiting_on = ""

    existing_entities = existing["entities"] if existing is not None else []
    new_entities = _normalize_str_list(entities) if entities is not None else []
    merged_entities = _normalize_str_list([*existing_entities, *new_entities])

    existing_receipts = existing["gate_receipts"] if existing is not None else []
    new_receipts = _normalize_str_list(gate_receipts) if gate_receipts is not None else []
    merged_receipts = _normalize_str_list([*existing_receipts, *new_receipts])

    nudge_count = int(existing["nudge_count"]) if existing is not None else 0
    last_nudged_at = str(existing["last_nudged_at"]) if existing is not None else ""

    pt_label = f"{now_dt.strftime('%Y-%m-%d %H:%M')} PT"
    meta_parts = [f"status={validated_status}"]
    if source_session and str(source_session).strip():
        meta_parts.append(f"source={str(source_session).strip()}")
    if resolved_waiting_on:
        meta_parts.append(f"waiting_on={resolved_waiting_on}")
    summary_text = str(summary or "").strip() or "Task handoff recorded."
    entry_line = f"- [{pt_label}] ({', '.join(meta_parts)}) {summary_text}"

    if existing is not None and existing["body"]:
        updated_body = f"{existing['body'].rstrip()}\n{entry_line}"
    else:
        updated_body = f"# Task `{clean_slug}`\n\n## Handoff Log\n{entry_line}"

    frontmatter = {
        "status": validated_status,
        "waiting_on": resolved_waiting_on,
        "next_check": resolved_next_check,
        "entities": merged_entities,
        "gate_receipts": merged_receipts,
        "nudge_count": nudge_count,
        "last_nudged_at": last_nudged_at,
    }
    rendered = render_task_file(frontmatter, updated_body)
    _atomic_write_text(target_path, rendered)

    if now is not None:
        ts = float(now)
        os.utime(target_path, (ts, ts))

    parsed = parse_task_file(target_path, tz_name=tz_name)
    parsed["created"] = existing is None
    return parsed


def evaluate_task_staleness(
    task: dict[str, Any],
    *,
    mtime: float,
    now: float,
    open_stale_days: int = 3,
    blocked_stale_days: int = 6,
    backoff_days: Sequence[int] = (3, 7, 14),
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Deterministically evaluate task due-ness, staleness, and day 3/7/14 backoff suppression."""
    fm = task.get("frontmatter") if isinstance(task.get("frontmatter"), dict) else {}
    raw_status = task.get("status", fm.get("status", "open"))
    status = validate_task_status(raw_status)

    clean_backoff = [max(1, int(x)) for x in backoff_days] if backoff_days else [3, 7, 14]
    if not clean_backoff:
        clean_backoff = [3, 7, 14]

    if status in CLOSED_TASK_STATUSES:
        return {
            "due_for_check": False,
            "is_stale": False,
            "should_surface": False,
            "backoff_suppressed": False,
            "reason": f"closed ({status})",
            "next_backoff_days": 0,
            "elapsed_days": 0.0,
        }

    now_ts = float(now)
    mtime_ts = float(mtime)

    raw_nudge_count = task.get("nudge_count", fm.get("nudge_count", 0))
    nudge_count = max(0, int(raw_nudge_count or 0))

    raw_last_nudged = str(
        task.get("last_nudged_at", fm.get("last_nudged_at", "")) or ""
    ).strip()
    last_nudged_ts: float | None = None
    if raw_last_nudged:
        last_nudged_ts = parse_iso_datetime(raw_last_nudged, tz_name=tz_name).timestamp()

    raw_next_check = str(
        task.get("next_check", fm.get("next_check", "")) or ""
    ).strip()
    due_for_check = False
    if raw_next_check:
        next_check_ts = parse_iso_datetime(raw_next_check, tz_name=tz_name).timestamp()
        due_for_check = next_check_ts <= now_ts

    base_threshold = (
        max(1, int(open_stale_days))
        if status == "open"
        else max(1, int(blocked_stale_days))
    )
    if nudge_count == 0:
        required_threshold_days = base_threshold
    elif nudge_count == 1:
        required_threshold_days = (
            clean_backoff[1] if len(clean_backoff) >= 2 else clean_backoff[-1]
        )
    else:
        required_threshold_days = clean_backoff[-1]

    active_backoff_window_days = (
        clean_backoff[0]
        if nudge_count == 0
        else required_threshold_days
    )

    backoff_suppressed = False
    if last_nudged_ts is not None:
        days_since_nudge = max(0.0, (now_ts - last_nudged_ts) / 86400.0)
        if days_since_nudge < active_backoff_window_days:
            backoff_suppressed = True

    if nudge_count > 0 and last_nudged_ts is not None:
        reference_ts = last_nudged_ts
    else:
        reference_ts = mtime_ts

    elapsed_days = max(0.0, (now_ts - reference_ts) / 86400.0)

    if backoff_suppressed:
        is_stale = False
        should_surface = False
        reason = f"backoff_suppressed ({active_backoff_window_days}d window)"
    else:
        is_stale = elapsed_days >= required_threshold_days
        should_surface = bool(due_for_check or is_stale)
        if due_for_check and is_stale:
            reason = "due_and_stale"
        elif due_for_check:
            reason = "due_for_check"
        elif is_stale:
            reason = f"stale ({elapsed_days:.1f}d >= {required_threshold_days}d)"
        else:
            reason = "fresh"

    return {
        "due_for_check": due_for_check,
        "is_stale": is_stale,
        "should_surface": should_surface,
        "backoff_suppressed": backoff_suppressed,
        "reason": reason,
        "next_backoff_days": required_threshold_days,
        "elapsed_days": round(elapsed_days, 3),
    }


def mark_tasks_nudged(
    task_dir: Path | str,
    slugs: Sequence[str],
    *,
    now: float | None = None,
    tz_name: str = "America/Los_Angeles",
) -> list[dict[str, Any]]:
    """Atomically increment nudge_count and set last_nudged_at (ISO-8601 PT) for each task slug."""
    now_dt = _resolve_pt_datetime(now, tz_name=tz_name)
    nudged_iso = format_pt_iso(now_dt, tz_name=tz_name)
    updated: list[dict[str, Any]] = []

    seen: set[str] = set()
    for raw_slug in slugs:
        slug_str = str(raw_slug or "").strip()
        if slug_str.endswith(".md") and "/" not in slug_str and "\\" not in slug_str:
            slug_str = slug_str[:-3]
        clean_slug, target_path = resolve_task_path(task_dir, slug_str)
        if clean_slug in seen or not target_path.is_file():
            continue
        seen.add(clean_slug)
        parsed = parse_task_file(target_path, tz_name=tz_name)
        fm = dict(parsed["frontmatter"])
        fm["nudge_count"] = int(fm.get("nudge_count", 0)) + 1
        fm["last_nudged_at"] = nudged_iso
        rendered = render_task_file(fm, parsed["body"])
        _atomic_write_text(target_path, rendered)
        if now is not None:
            ts = float(now)
            os.utime(target_path, (ts, ts))
        updated.append(parse_task_file(target_path, tz_name=tz_name))

    return updated


def scan_tasks_for_daily_review(
    task_dir: Path | str,
    *,
    now: float | None = None,
    open_stale_days: int = 3,
    blocked_stale_days: int = 6,
    backoff_days: Sequence[int] = (3, 7, 14),
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Scan task_dir/*.md for open/blocked tasks that are due or stale respecting backoff."""
    root = Path(task_dir)
    now_ts = time.time() if now is None else float(now)

    open_count = 0
    blocked_count = 0
    due_tasks: list[dict[str, Any]] = []
    stale_tasks: list[dict[str, Any]] = []
    actionable_tasks: list[dict[str, Any]] = []

    if not root.exists() or not root.is_dir():
        return {
            "open_count": 0,
            "blocked_count": 0,
            "due_tasks": [],
            "stale_tasks": [],
            "actionable_tasks": [],
            "has_actionable_items": False,
        }

    for path in sorted(root.glob("*.md")):
        if path.name.startswith(".") or not path.is_file():
            continue
        try:
            validate_task_slug(path.stem)
            parsed = parse_task_file(path, tz_name=tz_name)
        except ValueError:
            continue

        status = parsed["status"]
        if status == "open":
            open_count += 1
        elif status == "blocked":
            blocked_count += 1
        else:
            continue

        mtime = path.stat().st_mtime
        eval_res = evaluate_task_staleness(
            parsed,
            mtime=mtime,
            now=now_ts,
            open_stale_days=open_stale_days,
            blocked_stale_days=blocked_stale_days,
            backoff_days=backoff_days,
            tz_name=tz_name,
        )
        if not eval_res["should_surface"]:
            continue

        entry = {
            "slug": parsed["slug"],
            "path": str(path),
            "status": status,
            "waiting_on": parsed["waiting_on"],
            "next_check": parsed["next_check"],
            "entities": parsed["entities"],
            "gate_receipts": parsed["gate_receipts"],
            "nudge_count": parsed["nudge_count"],
            "last_nudged_at": parsed["last_nudged_at"],
            **eval_res,
        }
        actionable_tasks.append(entry)
        if eval_res["due_for_check"]:
            due_tasks.append(entry)
        if eval_res["is_stale"]:
            stale_tasks.append(entry)

    return {
        "open_count": open_count,
        "blocked_count": blocked_count,
        "due_tasks": due_tasks,
        "stale_tasks": stale_tasks,
        "actionable_tasks": actionable_tasks,
        "has_actionable_items": bool(due_tasks or stale_tasks),
    }


def _resolve_cli_task_dir(args: argparse.Namespace) -> tuple[Path, BriefConfig]:
    cfg = BriefConfig.load(
        config_path=getattr(args, "config", None),
        memory_dir=getattr(args, "memory_dir", None),
    )
    task_dir = Path(args.task_dir) if getattr(args, "task_dir", None) is not None else cfg.task_dir
    return task_dir, cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="BRIEF task handoff validator and staleness backoff scanner."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_handoff = sub.add_parser("handoff", help="Create or update a task handoff markdown file.")
    p_handoff.add_argument("slug_pos", nargs="?", default=None, help="Task slug (YYYYMMDD-slug).")
    p_handoff.add_argument("--slug", default=None, help="Task slug (YYYYMMDD-slug).")
    p_handoff.add_argument(
        "--status",
        default="open",
        choices=VALID_TASK_STATUSES,
        help="Task status (open|blocked|done|canceled).",
    )
    p_handoff.add_argument("--waiting-on", default="", help="Dependency or person waiting on.")
    p_handoff.add_argument("--next-check", default=None, help="ISO-8601 timestamp in PT.")
    p_handoff.add_argument(
        "--entity",
        "--entities",
        dest="entities",
        action="append",
        default=[],
        help="Related entity (repeatable or comma-separated).",
    )
    p_handoff.add_argument("--summary", default="", help="Concise handoff summary.")
    p_handoff.add_argument("--source-session", default="", help="Originating session tag.")
    p_handoff.add_argument("--task-dir", type=Path, default=None, help="Explicit task directory.")
    p_handoff.add_argument("--memory-dir", type=Path, default=None, help="Override memory directory.")
    p_handoff.add_argument("--config", type=Path, default=None, help="Optional BriefConfig TOML path.")
    p_handoff.add_argument("--tz", default=None, help="Timezone override (default America/Los_Angeles).")

    p_scan = sub.add_parser("scan", help="Scan task_dir for due or stale tasks.")
    p_scan.add_argument("--task-dir", type=Path, default=None, help="Explicit task directory.")
    p_scan.add_argument("--memory-dir", type=Path, default=None, help="Override memory directory.")
    p_scan.add_argument("--config", type=Path, default=None, help="Optional BriefConfig TOML path.")
    p_scan.add_argument("--open-stale-days", type=int, default=None, help="Open staleness threshold.")
    p_scan.add_argument(
        "--blocked-stale-days", type=int, default=None, help="Blocked staleness threshold."
    )
    p_scan.add_argument(
        "--mark-nudged",
        action="store_true",
        help="Increment nudge_count and update last_nudged_at on surfaced tasks.",
    )
    p_scan.add_argument("--tz", default=None, help="Timezone override (default America/Los_Angeles).")

    args = parser.parse_args(argv)
    task_dir, cfg = _resolve_cli_task_dir(args)
    tz_name = args.tz or cfg.timezone or "America/Los_Angeles"

    if args.command == "handoff":
        slug = args.slug or args.slug_pos
        if not slug:
            parser.error("handoff requires a task slug (positional or --slug)")
        flat_entities: list[str] = []
        for raw_ent in args.entities or []:
            for part in str(raw_ent).split(","):
                if part.strip():
                    flat_entities.append(part.strip())
        result = create_or_update_task_handoff(
            task_dir,
            slug,
            status=args.status,
            waiting_on=args.waiting_on,
            next_check=args.next_check,
            entities=flat_entities,
            summary=args.summary,
            source_session=args.source_session,
            tz_name=tz_name,
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "scan":
        open_days = (
            args.open_stale_days
            if args.open_stale_days is not None
            else cfg.task_open_stale_days
        )
        blocked_days = (
            args.blocked_stale_days
            if args.blocked_stale_days is not None
            else cfg.task_blocked_stale_days
        )
        scan_result = scan_tasks_for_daily_review(
            task_dir,
            open_stale_days=open_days,
            blocked_stale_days=blocked_days,
            backoff_days=cfg.task_staleness_backoff_days,
            tz_name=tz_name,
        )
        if args.mark_nudged and scan_result["actionable_tasks"]:
            slugs = [item["slug"] for item in scan_result["actionable_tasks"]]
            mark_tasks_nudged(task_dir, slugs, tz_name=tz_name)
        print(json.dumps(scan_result, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
