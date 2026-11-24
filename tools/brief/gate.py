"""Hash-verified Approval Gate state machine and proposal lifecycle for BRIEF (Step 3)."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.brief.actions import (
    ALLOWED_GATE_ACTIONS,
    canonical_proposal_hash,
    execute_action,
    validate_action_args,
)
from tools.brief.config import BriefConfig
from tools.brief.daemon import BriefDeliveryGate, is_quiet_hours
from tools.brief.queue import BriefQueue
from tools.brief.watchdog import _format_pt_timestamp, post_brief_alert

logger = logging.getLogger(__name__)

GATE_STATUSES = (
    "staged",
    "executing",
    "executed",
    "edited_executed",
    "dismissed",
    "expired",
    "rejected",
    "tampered",
    "failed",
)

_SAFE_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_DEFAULT_RECEIPT_TAG = "intake/discord/brief"

__all__ = [
    "GATE_STATUSES",
    "BriefApprovalGate",
    "BriefGateStore",
    "handle_forge_decision_approve",
    "handle_forge_decision_dismiss",
    "write_proposal_atomic",
]


def _validate_proposal_id(raw_id: Any) -> str:
    """Validate proposal_id against strict safe path-segment rules."""
    if not isinstance(raw_id, str):
        raise TypeError("proposal id must be a string")
    pid = raw_id.strip()
    if (
        not pid
        or pid in {".", ".."}
        or pid.startswith(".")
        or ".." in pid
        or "/" in pid
        or "\\" in pid
        or "\x00" in pid
        or not _SAFE_PROPOSAL_ID_RE.fullmatch(pid)
    ):
        raise ValueError(f"Unsafe or invalid proposal id: {raw_id!r}")
    return pid


def _safe_archive_stem(stem: str) -> str:
    """Sanitize an arbitrary file stem for safe archiving under proposals/archive."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(stem or "").strip())
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.lstrip(".")
    return cleaned or "malformed_proposal"


def _parse_expires_at(raw_value: Any, tz_name: str) -> tuple[float, str]:
    """Parse an ISO-8601 expiration timestamp and return (epoch_seconds, normalized_iso)."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("expires_at must be a non-empty ISO-8601 string")
    text = raw_value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"expires_at is not a valid ISO-8601 timestamp: {raw_value!r}") from exc
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return float(dt.timestamp()), dt.isoformat()


def write_proposal_atomic(
    proposals_pending_dir: Path | str,
    proposal_dict: Mapping[str, Any],
) -> Path:
    """Atomically write a proposal JSON to `<id>.json.tmp` -> fsync -> `<id>.json`."""
    if not isinstance(proposal_dict, Mapping):
        raise TypeError("proposal_dict must be a mapping")
    proposal_id = _validate_proposal_id(proposal_dict.get("id"))
    pending_dir = Path(proposals_pending_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)

    target_path = pending_dir / f"{proposal_id}.json"
    tmp_path = pending_dir / f"{proposal_id}.json.tmp"
    serialized = json.dumps(dict(proposal_dict), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target_path


def _row_to_proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["urgent"] = bool(item.get("urgent"))
    return item


class BriefGateStore:
    """Persistent SQLite WAL store for SHA-256 snapshotted gate proposals."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                with conn:
                    yield conn
            finally:
                conn.close()

    def init_db(self) -> None:
        """Create parent directories, gate_proposals table, and indexes if missing."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gate_proposals (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    args_sha256 TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    from_session TEXT NOT NULL,
                    urgent INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    decision_id TEXT,
                    source_path TEXT,
                    archived_path TEXT,
                    receipt_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_gate_proposals_status_expires
                    ON gate_proposals(status, expires_at);
                """
            )

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM gate_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            return _row_to_proposal_dict(row) if row is not None else None

    def has_proposal(self, proposal_id: str) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM gate_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            return row is not None

    def insert_proposal(
        self,
        *,
        proposal_id: str,
        action: str,
        args_json: str,
        args_sha256: str,
        reason: str,
        from_session: str,
        urgent: bool,
        expires_at: float,
        status: str,
        source_path: str | None = None,
        archived_path: str | None = None,
        receipt_json: str | None = None,
        error: str | None = None,
        created_at: float | None = None,
        resolved_at: float | None = None,
    ) -> bool:
        """Insert a proposal row idempotently. Returns False if `id` already exists."""
        if status not in GATE_STATUSES:
            raise ValueError(f"Unsupported gate status: {status!r}")
        now_ts = time.time() if created_at is None else float(created_at)
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO gate_proposals (
                    id, action, args_json, args_sha256, reason, from_session,
                    urgent, expires_at, status, decision_id, source_path,
                    archived_path, receipt_json, error, created_at, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                RETURNING id
                """,
                (
                    proposal_id,
                    action,
                    args_json,
                    args_sha256,
                    reason,
                    from_session,
                    1 if urgent else 0,
                    float(expires_at),
                    status,
                    source_path,
                    archived_path,
                    receipt_json,
                    error,
                    now_ts,
                    resolved_at,
                ),
            )
            return cur.fetchone() is not None

    def delete_unstaged(self, proposal_id: str) -> bool:
        """Remove a newly inserted row only if no Decision Card was linked yet."""
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM gate_proposals WHERE id = ? AND status = 'staged' AND decision_id IS NULL",
                (proposal_id,),
            )
            return cur.rowcount > 0

    def update_decision_id(self, proposal_id: str, decision_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE gate_proposals SET decision_id = ? WHERE id = ?",
                (decision_id, proposal_id),
            )
            return cur.rowcount > 0

    def claim_for_execution(self, proposal_id: str) -> dict[str, Any] | None:
        """Atomically transition a proposal from 'staged' to 'executing'."""
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE gate_proposals
                SET status = 'executing'
                WHERE id = ? AND status = 'staged'
                RETURNING *
                """,
                (proposal_id,),
            )
            row = cur.fetchone()
            return _row_to_proposal_dict(row) if row is not None else None

    def complete_from_executing(
        self,
        proposal_id: str,
        *,
        status: str,
        archived_path: str | None = None,
        receipt_json: str | None = None,
        error: str | None = None,
        resolved_at: float | None = None,
    ) -> bool:
        """Finalize an 'executing' row into a terminal status."""
        if status not in GATE_STATUSES or status in {"staged", "executing"}:
            raise ValueError(f"Invalid terminal execution status: {status!r}")
        ts = time.time() if resolved_at is None else float(resolved_at)
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE gate_proposals
                SET status = ?,
                    archived_path = COALESCE(?, archived_path),
                    receipt_json = COALESCE(?, receipt_json),
                    error = ?,
                    resolved_at = ?
                WHERE id = ? AND status = 'executing'
                """,
                (status, archived_path, receipt_json, error, ts, proposal_id),
            )
            return cur.rowcount > 0

    def transition_staged(
        self,
        proposal_id: str,
        *,
        new_status: str,
        archived_path: str | None = None,
        receipt_json: str | None = None,
        error: str | None = None,
        resolved_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically transition a 'staged' proposal (e.g. dismissed/expired)."""
        if new_status not in {"dismissed", "expired", "tampered", "failed"}:
            raise ValueError(f"Unsupported staged transition status: {new_status!r}")
        ts = time.time() if resolved_at is None else float(resolved_at)
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE gate_proposals
                SET status = ?,
                    archived_path = COALESCE(?, archived_path),
                    receipt_json = COALESCE(?, receipt_json),
                    error = ?,
                    resolved_at = ?
                WHERE id = ? AND status = 'staged'
                RETURNING *
                """,
                (new_status, archived_path, receipt_json, error, ts, proposal_id),
            )
            row = cur.fetchone()
            return _row_to_proposal_dict(row) if row is not None else None

    def set_archived_path(self, proposal_id: str, archived_path: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE gate_proposals SET archived_path = ? WHERE id = ?",
                (archived_path, proposal_id),
            )
            return cur.rowcount > 0

    def list_expired_staged(self, now_ts: float) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gate_proposals
                WHERE status = 'staged' AND expires_at <= ?
                ORDER BY expires_at ASC, created_at ASC
                """,
                (float(now_ts),),
            ).fetchall()
            return [_row_to_proposal_dict(r) for r in rows]

    def list_by_status(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gate_proposals
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
            return [_row_to_proposal_dict(r) for r in rows]

    def get_status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {status: 0 for status in GATE_STATUSES}
        with self._tx() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM gate_proposals GROUP BY status"
            ).fetchall():
                counts[str(row["status"])] = int(row["cnt"])
        return counts


def _default_stage_card(
    *,
    category: str,
    title: str,
    summary: str,
    handler_type: str,
    payload: dict[str, Any],
    channel_id: int,
    ttl_seconds: float,
    is_private: bool,
    post_to_discord: bool,
) -> dict[str, Any]:
    from tools.forge import stage_decision

    return stage_decision(
        category=category,
        title=title,
        summary=summary,
        handler_type=handler_type,
        payload=payload,
        channel_id=channel_id,
        ttl_seconds=ttl_seconds,
        is_private=is_private,
        post_to_discord=post_to_discord,
    )


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_readable_pt(raw_value: Any, tz_name: str) -> tuple[datetime | None, bool]:
    """Parse an ISO-8601 datetime or YYYY-MM-DD date string into local PT datetime."""
    if not isinstance(raw_value, str):
        return None, False
    text = raw_value.strip()
    if not text:
        return None, False
    tz = ZoneInfo(tz_name)
    if _DATE_ONLY_RE.fullmatch(text):
        try:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            return None, False
        return dt, True
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz), False


def _fmt_readable_date(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}"


def _fmt_readable_time(dt: datetime) -> str:
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt:%M} {ampm}"


def _format_readable_pt_range(
    start_iso: Any,
    end_iso: Any,
    tz_name: str = "America/Los_Angeles",
) -> str:
    """Format ISO-8601 start/end timestamps into concise, readable Pacific Time."""
    start_dt, start_date_only = _parse_readable_pt(start_iso, tz_name)
    end_dt, end_date_only = _parse_readable_pt(end_iso, tz_name)

    if start_dt is None and end_dt is None:
        parts = [str(v).strip() for v in (start_iso, end_iso) if isinstance(v, str) and v.strip()]
        return " - ".join(parts)

    ref_dt = start_dt or end_dt
    assert ref_dt is not None
    tz_label = "PT" if tz_name == "America/Los_Angeles" else (ref_dt.tzname() or "PT")

    if start_dt is not None and end_dt is not None:
        if start_date_only and end_date_only:
            if start_dt.date() == end_dt.date():
                return f"{_fmt_readable_date(start_dt)} (All day)"
            return f"{_fmt_readable_date(start_dt)} - {_fmt_readable_date(end_dt)} (All day)"
        if not start_date_only and not end_date_only:
            if start_dt.date() == end_dt.date():
                return (
                    f"{_fmt_readable_date(start_dt)}, "
                    f"{_fmt_readable_time(start_dt)} - {_fmt_readable_time(end_dt)} {tz_label}"
                )
            return (
                f"{_fmt_readable_date(start_dt)}, {_fmt_readable_time(start_dt)} - "
                f"{_fmt_readable_date(end_dt)}, {_fmt_readable_time(end_dt)} {tz_label}"
            )
        start_part = (
            _fmt_readable_date(start_dt)
            if start_date_only
            else f"{_fmt_readable_date(start_dt)}, {_fmt_readable_time(start_dt)}"
        )
        end_part = (
            _fmt_readable_date(end_dt)
            if end_date_only
            else f"{_fmt_readable_date(end_dt)}, {_fmt_readable_time(end_dt)}"
        )
        return f"{start_part} - {end_part} {tz_label}"

    if start_dt is not None:
        if start_date_only:
            return f"{_fmt_readable_date(start_dt)} (All day)"
        return f"{_fmt_readable_date(start_dt)}, {_fmt_readable_time(start_dt)} {tz_label}"

    assert end_dt is not None
    if end_date_only:
        return f"Ends {_fmt_readable_date(end_dt)}"
    return f"Ends {_fmt_readable_date(end_dt)}, {_fmt_readable_time(end_dt)} {tz_label}"


def _build_decision_card_copy(
    *,
    proposal_id: str,
    action: str,
    normalized_args: Mapping[str, Any],
    reason: str,
    expires_ts: float,
    urgent: bool,
    tz_name: str,
) -> tuple[str, str]:
    """Build mobile-friendly PT summary and explicit '**Approve Action:**' line."""
    del proposal_id, expires_ts
    summary_text = str(normalized_args.get("summary") or "").strip()
    when_str = _format_readable_pt_range(
        normalized_args.get("start"),
        normalized_args.get("end"),
        tz_name,
    )
    when_suffix = f" ({when_str})" if when_str else ""

    if action == "calendar.create":
        title_label = summary_text or "New Event"
        title = f"Add to Calendar: {title_label[:64]}"
        approve_line = f'Add "{title_label}" to your calendar{when_suffix}'
    else:
        title_label = summary_text or "Calendar Event"
        title = f"Update Calendar: {title_label[:64]}"
        approve_line = f'Update "{title_label}" on your calendar{when_suffix}'

    lines: list[str] = []
    if urgent:
        lines.append("**[URGENT]**")
    if when_str:
        lines.append(f"**When:** {when_str}")
    if normalized_args.get("location"):
        lines.append(f"**Where:** {normalized_args['location']}")
    if normalized_args.get("attendees"):
        lines.append(f"**With:** {', '.join(normalized_args['attendees'])}")
    lines.append(f"**Why:** {reason}")
    lines.append("")
    lines.append(f"**Approve Action:** {approve_line}")

    return title, "\n".join(lines)


class BriefApprovalGate:
    """Code-enforced, SHA-256 hash-verified Approval Gate for BRIEF Calendar mutations."""

    def __init__(
        self,
        config: BriefConfig,
        *,
        queue: BriefQueue | None = None,
        gate_store: BriefGateStore | None = None,
        stage_card_fn: Callable[..., dict[str, Any]] | None = None,
        api_client: Any = None,
        now_fn: Callable[[], float] | None = None,
        budget_check_fn: Callable[[float], bool] | None = None,
        alert_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config
        self._now_fn = now_fn or time.time
        self.queue = queue or BriefQueue(
            config.db_path,
            allowlisted_senders=config.allowlisted_emails,
        )
        self.queue.init_db()
        self.gate_store = gate_store or BriefGateStore(config.gate_db_path)
        self.gate_store.init_db()
        self._stage_card_fn = stage_card_fn or _default_stage_card
        self.api_client = api_client
        self._budget_check_fn = budget_check_fn
        self._alert_fn = alert_fn
        self._delivery_gate = BriefDeliveryGate(config, now_fn=self._now_fn)

    def _archive_file(self, src_path: Path | None, archive_name: str) -> str | None:
        if src_path is None or not src_path.is_file():
            return None
        self.config.proposals_archive_dir.mkdir(parents=True, exist_ok=True)
        dst_path = self.config.proposals_archive_dir / archive_name
        if dst_path.exists():
            stem = dst_path.stem
            suffix = dst_path.suffix
            dst_path = self.config.proposals_archive_dir / f"{stem}.{int(self._now_fn() * 1000)}{suffix}"
        os.replace(src_path, dst_path)
        return str(dst_path)

    def _emit_receipt(
        self,
        *,
        proposal_id: str,
        status: str,
        action: str,
        from_session: str,
        reason: str,
        now_ts: float,
        receipt: Mapping[str, Any] | None = None,
        note: str = "",
        error: str = "",
    ) -> int | None:
        target_tag = str(from_session or "").strip() or _DEFAULT_RECEIPT_TAG
        pt_str = _format_pt_timestamp(now_ts, tz_name=self.config.timezone)
        payload: dict[str, Any] = {
            "type": "gate_receipt",
            "proposal_id": proposal_id,
            "status": status,
            "action": action,
            "timestamp_pt": pt_str,
            "reason": reason,
        }
        if receipt is not None:
            payload["receipt"] = dict(receipt)
        if note:
            payload["note"] = note
        if error:
            payload["error"] = error

        body = (
            f"[GATE RECEIPT] proposal_id={proposal_id} status={status} action={action} at {pt_str}\n"
            + json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        )
        return self.queue.enqueue_event(
            source="internal",
            key=f"gate-receipt:{proposal_id}",
            target_tag=target_tag,
            external_id=f"gate-receipt:{proposal_id}:{status}",
            sender="brief-gate",
            body=body,
            received_at=now_ts,
        )

    def _send_tamper_alert(self, proposal_id: str, detail: str, now_ts: float) -> bool:
        pt_ts = _format_pt_timestamp(now_ts, tz_name=self.config.timezone)
        message = (
            f"[BRIEF Gate] Tamper detected for proposal `{proposal_id}` at {pt_ts}: "
            f"{detail} Execution refused and proposal quarantined."
        )
        if self._alert_fn is not None:
            return bool(self._alert_fn(message))
        return post_brief_alert(
            message,
            channel_id=self.config.channel_id,
            dry_run=self.config.dry_run,
            state_path=self.config.state_dir / "gate_alert_state.json",
            cooldown_seconds=60,
            dedupe_key=f"gate-tamper:{proposal_id}",
            now=now_ts,
        )

    def _can_send_proactive(self, now_ts: float) -> bool:
        if self._budget_check_fn is not None:
            return bool(self._budget_check_fn(now_ts))
        limit = max(0, int(self.config.max_proactive_messages_per_day))
        if limit <= 0:
            return False
        date_key, _ = self._delivery_gate._pt_date_key(now=now_ts)
        with self._delivery_gate._lock:
            used = self._delivery_gate._get_proactive_count_unlocked(date_key)
        return used < limit

    def _record_proactive_send(self, now_ts: float, *, urgent: bool) -> None:
        date_key, _ = self._delivery_gate._pt_date_key(now=now_ts)
        with self._delivery_gate._lock:
            self._delivery_gate._record_send_unlocked(
                date_key,
                now_ts,
                proactive=not urgent,
                urgent=urgent,
                owner_initiated=False,
            )

    def _handle_invalid_pending(
        self,
        entry: Path,
        *,
        proposal_id: str | None,
        action: str,
        args_raw: Any,
        reason: str,
        from_session: str,
        urgent: bool,
        expires_ts: float,
        status: str,
        error_msg: str,
        now_ts: float,
    ) -> None:
        safe_id = proposal_id or _safe_archive_stem(entry.stem)
        archive_name = f"{safe_id}.{status}.json"
        archived_path = self._archive_file(entry, archive_name)
        safe_tag = from_session.strip() if isinstance(from_session, str) and from_session.strip() else _DEFAULT_RECEIPT_TAG

        if proposal_id is not None:
            try:
                args_json = json.dumps(args_raw if isinstance(args_raw, Mapping) else {}, sort_keys=True)
            except (TypeError, ValueError):
                args_json = "{}"
            self.gate_store.insert_proposal(
                proposal_id=proposal_id,
                action=action or "invalid",
                args_json=args_json,
                args_sha256="",
                reason=reason or "validation failed",
                from_session=safe_tag,
                urgent=urgent,
                expires_at=expires_ts,
                status=status,
                source_path=str(entry),
                archived_path=archived_path,
                error=error_msg,
                created_at=now_ts,
                resolved_at=now_ts,
            )

        self._emit_receipt(
            proposal_id=safe_id,
            status=status,
            action=action or "invalid",
            from_session=safe_tag,
            reason=reason or error_msg,
            now_ts=now_ts,
            error=error_msg,
        )

    def scan_pending_proposals(self, now: float | None = None) -> dict[str, Any]:
        """Scan proposals/pending/*.json, validate schemas, snapshot hashes, and stage cards."""
        now_ts = float(self._now_fn() if now is None else now)
        pending_dir = self.config.proposals_pending_dir

        summary: dict[str, Any] = {
            "scanned": 0,
            "staged": 0,
            "skipped_existing": 0,
            "rejected": 0,
            "expired": 0,
            "deferred_quiet_hours": 0,
            "deferred_budget": 0,
            "staged_ids": [],
            "rejected_ids": [],
            "expired_ids": [],
            "errors": [],
        }
        if not pending_dir.is_dir():
            return summary

        entries = sorted(pending_dir.iterdir(), key=lambda p: p.name)
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.startswith(".") or entry.name.endswith(".tmp") or entry.suffix.lower() != ".json":
                continue

            summary["scanned"] += 1
            try:
                raw_text = entry.read_text(encoding="utf-8")
                data = json.loads(raw_text)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self._handle_invalid_pending(
                    entry,
                    proposal_id=None,
                    action="invalid",
                    args_raw={},
                    reason="malformed proposal JSON",
                    from_session=_DEFAULT_RECEIPT_TAG,
                    urgent=False,
                    expires_ts=now_ts,
                    status="rejected",
                    error_msg=f"Malformed proposal JSON: {exc}",
                    now_ts=now_ts,
                )
                summary["rejected"] += 1
                summary["rejected_ids"].append(_safe_archive_stem(entry.stem))
                continue

            if not isinstance(data, Mapping):
                self._handle_invalid_pending(
                    entry,
                    proposal_id=None,
                    action="invalid",
                    args_raw={},
                    reason="proposal JSON root must be an object",
                    from_session=_DEFAULT_RECEIPT_TAG,
                    urgent=False,
                    expires_ts=now_ts,
                    status="rejected",
                    error_msg="Proposal JSON root must be an object.",
                    now_ts=now_ts,
                )
                summary["rejected"] += 1
                summary["rejected_ids"].append(_safe_archive_stem(entry.stem))
                continue

            raw_from_session = data.get("from_session")
            from_session = raw_from_session.strip() if isinstance(raw_from_session, str) else ""
            raw_reason = data.get("reason")
            reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
            raw_action = data.get("action")
            action = raw_action.strip() if isinstance(raw_action, str) else ""
            urgent = bool(data.get("urgent", False))
            raw_args = data.get("args")

            try:
                proposal_id = _validate_proposal_id(data.get("id"))
            except (TypeError, ValueError) as exc:
                self._handle_invalid_pending(
                    entry,
                    proposal_id=None,
                    action=action or "invalid",
                    args_raw=raw_args,
                    reason=reason or "invalid proposal id",
                    from_session=from_session or _DEFAULT_RECEIPT_TAG,
                    urgent=urgent,
                    expires_ts=now_ts,
                    status="rejected",
                    error_msg=str(exc),
                    now_ts=now_ts,
                )
                summary["rejected"] += 1
                summary["rejected_ids"].append(_safe_archive_stem(entry.stem))
                continue

            if self.gate_store.has_proposal(proposal_id):
                summary["skipped_existing"] += 1
                continue

            # Parse and validate expires_at first so expired proposals get .expired.json
            try:
                expires_ts, _ = _parse_expires_at(data.get("expires_at"), self.config.timezone)
            except ValueError as exc:
                self._handle_invalid_pending(
                    entry,
                    proposal_id=proposal_id,
                    action=action or "invalid",
                    args_raw=raw_args,
                    reason=reason or "invalid expires_at",
                    from_session=from_session or _DEFAULT_RECEIPT_TAG,
                    urgent=urgent,
                    expires_ts=now_ts,
                    status="rejected",
                    error_msg=str(exc),
                    now_ts=now_ts,
                )
                summary["rejected"] += 1
                summary["rejected_ids"].append(proposal_id)
                continue

            if expires_ts <= now_ts:
                self._handle_invalid_pending(
                    entry,
                    proposal_id=proposal_id,
                    action=action or "invalid",
                    args_raw=raw_args,
                    reason=reason or "proposal expired before staging",
                    from_session=from_session or _DEFAULT_RECEIPT_TAG,
                    urgent=urgent,
                    expires_ts=expires_ts,
                    status="expired",
                    error_msg="Proposal expired before gate staging.",
                    now_ts=now_ts,
                )
                summary["expired"] += 1
                summary["expired_ids"].append(proposal_id)
                continue

            # Validate remaining schema & strict action allowlist
            validation_error: str | None = None
            normalized_args: dict[str, Any] | None = None
            max_ttl = float(self.config.gate_max_proposal_ttl_seconds)

            if (expires_ts - now_ts) > max_ttl:
                validation_error = (
                    f"expires_at exceeds gate_max_proposal_ttl_seconds ({int(max_ttl)}s)."
                )
            elif not from_session:
                validation_error = "from_session must be a non-empty string."
            elif not reason:
                validation_error = "reason must be a non-empty string."
            elif action not in ALLOWED_GATE_ACTIONS:
                validation_error = f"Disallowed gate action: {action!r}"
            else:
                try:
                    normalized_args = validate_action_args(
                        action,
                        raw_args,  # type: ignore[arg-type]
                        tz_name=self.config.timezone,
                    )
                except (TypeError, ValueError) as exc:
                    validation_error = str(exc)

            if validation_error is not None or normalized_args is None:
                self._handle_invalid_pending(
                    entry,
                    proposal_id=proposal_id,
                    action=action or "invalid",
                    args_raw=raw_args,
                    reason=reason or "schema validation failed",
                    from_session=from_session or _DEFAULT_RECEIPT_TAG,
                    urgent=urgent,
                    expires_ts=expires_ts,
                    status="rejected",
                    error_msg=validation_error or "Invalid proposal arguments.",
                    now_ts=now_ts,
                )
                summary["rejected"] += 1
                summary["rejected_ids"].append(proposal_id)
                continue

            # Valid non-urgent proposals wait untouched in pending/ during quiet hours or daily cap.
            if not urgent:
                if is_quiet_hours(self.config, now=now_ts):
                    summary["deferred_quiet_hours"] += 1
                    continue
                if not self._can_send_proactive(now_ts):
                    summary["deferred_budget"] += 1
                    continue

            args_sha256 = canonical_proposal_hash(action, normalized_args)
            args_json = json.dumps(normalized_args, sort_keys=True, ensure_ascii=False)
            inserted = self.gate_store.insert_proposal(
                proposal_id=proposal_id,
                action=action,
                args_json=args_json,
                args_sha256=args_sha256,
                reason=reason,
                from_session=from_session,
                urgent=urgent,
                expires_at=expires_ts,
                status="staged",
                source_path=str(entry),
                created_at=now_ts,
            )
            if not inserted:
                summary["skipped_existing"] += 1
                continue

            title, card_summary = _build_decision_card_copy(
                proposal_id=proposal_id,
                action=action,
                normalized_args=normalized_args,
                reason=reason,
                expires_ts=expires_ts,
                urgent=urgent,
                tz_name=self.config.timezone,
            )
            card_payload: dict[str, Any] = {
                "proposal_id": proposal_id,
                "action": action,
                "args": normalized_args,
                "args_sha256": args_sha256,
                "reason": reason,
                "from_session": from_session,
                "state_dir": str(self.config.state_dir),
                "memory_dir": str(self.config.memory_dir),
                "config_path": str(self.config.config_path) if self.config.config_path else None,
                "dry_run": bool(self.config.dry_run),
            }
            ttl_seconds = max(1.0, expires_ts - now_ts)

            try:
                stage_res = self._stage_card_fn(
                    category="brief",
                    title=title,
                    summary=card_summary,
                    handler_type="brief_proposal",
                    payload=card_payload,
                    channel_id=int(self.config.channel_id),
                    ttl_seconds=ttl_seconds,
                    is_private=True,
                    post_to_discord=not bool(self.config.dry_run),
                )
            except Exception as exc:  # noqa: BLE001 - staging failure must not abort scan loop
                self.gate_store.delete_unstaged(proposal_id)
                summary["errors"].append(f"{proposal_id}: {exc}")
                continue

            decision_id = (
                str(stage_res.get("decision_id") or "").strip()
                if isinstance(stage_res, Mapping)
                else ""
            )
            if decision_id:
                self.gate_store.update_decision_id(proposal_id, decision_id)

            self._record_proactive_send(now_ts, urgent=urgent)
            summary["staged"] += 1
            summary["staged_ids"].append(proposal_id)

        return summary

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        edited_args: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically claim, verify SHA-256 against disk, and execute an approved proposal."""
        now_ts = float(self._now_fn() if now is None else now)
        try:
            pid = _validate_proposal_id(proposal_id)
        except ValueError as exc:
            return {
                "ok": False,
                "proposal_id": str(proposal_id),
                "status": "invalid",
                "error": str(exc),
            }

        claimed = self.gate_store.claim_for_execution(pid)
        if claimed is None:
            existing = self.gate_store.get_proposal(pid)
            if existing is None:
                return {
                    "ok": False,
                    "proposal_id": pid,
                    "status": "not_found",
                    "error": f"Proposal {pid} not found in gate.db.",
                }
            return {
                "ok": False,
                "proposal_id": pid,
                "status": existing["status"],
                "error": f"Proposal {pid} cannot be executed (current status: {existing['status']}).",
            }

        action = str(claimed["action"])
        from_session = str(claimed["from_session"])
        reason = str(claimed["reason"])
        expected_hash = str(claimed["args_sha256"])
        pending_path = (
            Path(claimed["source_path"])
            if claimed.get("source_path")
            else (self.config.proposals_pending_dir / f"{pid}.json")
        )
        if not pending_path.is_file():
            fallback_pending = self.config.proposals_pending_dir / f"{pid}.json"
            pending_path = fallback_pending if fallback_pending.is_file() else pending_path

        try:
            # 1. Check expiration before anything else
            if now_ts > float(claimed["expires_at"]):
                err = f"Proposal {pid} expired before approval."
                archived_path = self._archive_file(pending_path, f"{pid}.expired.json")
                self.gate_store.complete_from_executing(
                    pid,
                    status="expired",
                    archived_path=archived_path,
                    error=err,
                    resolved_at=now_ts,
                )
                receipt_ev = self._emit_receipt(
                    proposal_id=pid,
                    status="expired",
                    action=action,
                    from_session=from_session,
                    reason=reason,
                    now_ts=now_ts,
                    error=err,
                )
                return {
                    "ok": False,
                    "proposal_id": pid,
                    "status": "expired",
                    "error": err,
                    "archived_path": archived_path,
                    "receipt_event_id": receipt_ev,
                }

            # 2. If source file still exists on disk, re-hash via the exact staging pipeline
            if pending_path.is_file():
                tamper_detail: str | None = None
                try:
                    disk_data = json.loads(pending_path.read_text(encoding="utf-8"))
                    if not isinstance(disk_data, Mapping):
                        tamper_detail = "pending proposal file is no longer a JSON object."
                    elif str(disk_data.get("id") or "").strip() != pid:
                        tamper_detail = "pending proposal id mismatch on disk."
                    elif str(disk_data.get("action") or "").strip() != action:
                        tamper_detail = (
                            f"pending proposal action changed on disk "
                            f"({disk_data.get('action')!r} != {action!r})."
                        )
                    else:
                        disk_norm = validate_action_args(
                            action,
                            disk_data.get("args"),  # type: ignore[arg-type]
                            tz_name=self.config.timezone,
                        )
                        disk_hash = canonical_proposal_hash(action, disk_norm)
                        if disk_hash != expected_hash:
                            tamper_detail = (
                                f"SHA-256 mismatch on disk ({disk_hash[:12]} != {expected_hash[:12]})."
                            )
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    tamper_detail = f"pending proposal modified or invalid on disk ({exc})."

                if tamper_detail is not None:
                    archived_path = self._archive_file(pending_path, f"{pid}.tampered.json")
                    self.gate_store.complete_from_executing(
                        pid,
                        status="tampered",
                        archived_path=archived_path,
                        error=tamper_detail,
                        resolved_at=now_ts,
                    )
                    receipt_ev = self._emit_receipt(
                        proposal_id=pid,
                        status="tampered",
                        action=action,
                        from_session=from_session,
                        reason=reason,
                        now_ts=now_ts,
                        error=tamper_detail,
                    )
                    self._send_tamper_alert(pid, tamper_detail, now_ts)
                    return {
                        "ok": False,
                        "proposal_id": pid,
                        "status": "tampered",
                        "error": tamper_detail,
                        "archived_path": archived_path,
                        "receipt_event_id": receipt_ev,
                    }

            # 3. Resolve exact snapshotted args or validated edited args
            snapshotted_args = json.loads(claimed["args_json"])
            if edited_args is not None:
                exec_args = validate_action_args(
                    action,
                    edited_args,
                    tz_name=self.config.timezone,
                )
                final_status = "edited_executed"
            else:
                exec_args = validate_action_args(
                    action,
                    snapshotted_args,
                    tz_name=self.config.timezone,
                )
                final_status = "executed"

            exec_hash = canonical_proposal_hash(action, exec_args)

            # 4. Execute allowlisted calendar action
            exec_receipt = execute_action(
                action,
                exec_args,
                self.config,
                api_client=self.api_client,
            )

            archived_path = self._archive_file(pending_path, f"{pid}.{final_status}.json")
            receipt_json = json.dumps(exec_receipt, sort_keys=True, ensure_ascii=False)
            self.gate_store.complete_from_executing(
                pid,
                status=final_status,
                archived_path=archived_path,
                receipt_json=receipt_json,
                error=None,
                resolved_at=now_ts,
            )
            receipt_ev = self._emit_receipt(
                proposal_id=pid,
                status=final_status,
                action=action,
                from_session=from_session,
                reason=reason,
                now_ts=now_ts,
                receipt=exec_receipt,
            )
            return {
                "ok": True,
                "proposal_id": pid,
                "status": final_status,
                "action": action,
                "args": exec_args,
                "args_sha256": exec_hash,
                "original_args_sha256": expected_hash,
                "receipt": exec_receipt,
                "archived_path": archived_path,
                "receipt_event_id": receipt_ev,
            }

        except Exception as exc:  # noqa: BLE001 - transition executing row to failed instead of leaving stuck
            err = str(exc) or exc.__class__.__name__
            archived_path = self._archive_file(pending_path, f"{pid}.failed.json")
            self.gate_store.complete_from_executing(
                pid,
                status="failed",
                archived_path=archived_path,
                error=err,
                resolved_at=now_ts,
            )
            receipt_ev = self._emit_receipt(
                proposal_id=pid,
                status="failed",
                action=action,
                from_session=from_session,
                reason=reason,
                now_ts=now_ts,
                error=err,
            )
            return {
                "ok": False,
                "proposal_id": pid,
                "status": "failed",
                "error": err,
                "archived_path": archived_path,
                "receipt_event_id": receipt_ev,
            }

    def dismiss_proposal(
        self,
        proposal_id: str,
        *,
        note: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Dismiss a staged proposal, archive it, and emit an internal receipt with optional note."""
        now_ts = float(self._now_fn() if now is None else now)
        try:
            pid = _validate_proposal_id(proposal_id)
        except ValueError as exc:
            return {
                "ok": False,
                "proposal_id": str(proposal_id),
                "status": "invalid",
                "error": str(exc),
            }

        clean_note = str(note or "").strip()
        receipt_json = json.dumps({"dismissed": True, "note": clean_note}, sort_keys=True)
        row = self.gate_store.transition_staged(
            pid,
            new_status="dismissed",
            receipt_json=receipt_json,
            error=None,
            resolved_at=now_ts,
        )
        if row is None:
            existing = self.gate_store.get_proposal(pid)
            if existing is None:
                return {
                    "ok": False,
                    "proposal_id": pid,
                    "status": "not_found",
                    "error": f"Proposal {pid} not found in gate.db.",
                }
            return {
                "ok": False,
                "proposal_id": pid,
                "status": existing["status"],
                "error": f"Proposal {pid} cannot be dismissed (current status: {existing['status']}).",
            }

        pending_path = (
            Path(row["source_path"])
            if row.get("source_path")
            else (self.config.proposals_pending_dir / f"{pid}.json")
        )
        if not pending_path.is_file():
            fallback_pending = self.config.proposals_pending_dir / f"{pid}.json"
            pending_path = fallback_pending if fallback_pending.is_file() else pending_path

        archived_path = self._archive_file(pending_path, f"{pid}.dismissed.json")
        if archived_path is not None:
            self.gate_store.set_archived_path(pid, archived_path)

        receipt_ev = self._emit_receipt(
            proposal_id=pid,
            status="dismissed",
            action=str(row["action"]),
            from_session=str(row["from_session"]),
            reason=str(row["reason"]),
            now_ts=now_ts,
            note=clean_note,
        )
        return {
            "ok": True,
            "proposal_id": pid,
            "status": "dismissed",
            "note": clean_note,
            "archived_path": archived_path,
            "receipt_event_id": receipt_ev,
        }

    def expire_stale_proposals(self, now: float | None = None) -> list[str]:
        """Expire staged proposals past expires_at, archive them, and emit internal receipts."""
        now_ts = float(self._now_fn() if now is None else now)
        expired_rows = self.gate_store.list_expired_staged(now_ts)
        expired_ids: list[str] = []

        for candidate in expired_rows:
            pid = str(candidate["id"])
            updated = self.gate_store.transition_staged(
                pid,
                new_status="expired",
                error="Proposal expired without owner action.",
                resolved_at=now_ts,
            )
            if updated is None:
                continue

            pending_path = (
                Path(updated["source_path"])
                if updated.get("source_path")
                else (self.config.proposals_pending_dir / f"{pid}.json")
            )
            if not pending_path.is_file():
                fallback_pending = self.config.proposals_pending_dir / f"{pid}.json"
                pending_path = fallback_pending if fallback_pending.is_file() else pending_path

            archived_path = self._archive_file(pending_path, f"{pid}.expired.json")
            if archived_path is not None:
                self.gate_store.set_archived_path(pid, archived_path)

            self._emit_receipt(
                proposal_id=pid,
                status="expired",
                action=str(updated["action"]),
                from_session=str(updated["from_session"]),
                reason=str(updated["reason"]),
                now_ts=now_ts,
                error="Proposal expired without owner action.",
            )
            expired_ids.append(pid)

        return expired_ids

    def get_gate_status(self, now: float | None = None) -> dict[str, Any]:
        """Return aggregate gate counts, pending file count, and active staged proposals."""
        now_ts = float(self._now_fn() if now is None else now)
        counts = self.gate_store.get_status_counts()
        staged_rows = self.gate_store.list_by_status("staged", limit=100)

        pending_files = 0
        pending_dir = self.config.proposals_pending_dir
        if pending_dir.is_dir():
            for entry in pending_dir.iterdir():
                if (
                    entry.is_file()
                    and not entry.name.startswith(".")
                    and not entry.name.endswith(".tmp")
                    and entry.suffix.lower() == ".json"
                ):
                    pending_files += 1

        staged_summaries = [
            {
                "id": row["id"],
                "action": row["action"],
                "from_session": row["from_session"],
                "decision_id": row["decision_id"],
                "urgent": row["urgent"],
                "expires_at": row["expires_at"],
                "expires_at_pt": _format_pt_timestamp(row["expires_at"], tz_name=self.config.timezone),
            }
            for row in staged_rows
        ]
        return {
            **counts,
            "total": sum(counts.values()),
            "counts": counts,
            "pending_files": pending_files,
            "staged_proposals": staged_summaries,
            "quiet_hours": is_quiet_hours(self.config, now=now_ts),
            "dry_run": bool(self.config.dry_run),
            "allow_shared_oauth_fallback": bool(self.config.gate_allow_shared_oauth_fallback),
            "write_token_configured": Path(self.config.gate_write_token_path).is_file(),
        }


def _config_from_decision_payload(payload: Mapping[str, Any]) -> BriefConfig:
    state_dir = payload.get("state_dir")
    memory_dir = payload.get("memory_dir")
    config_path = payload.get("config_path")
    cfg = BriefConfig.load(
        config_path=str(config_path) if config_path else None,
        state_dir=str(state_dir) if state_dir else None,
        memory_dir=str(memory_dir) if memory_dir else None,
    )
    if "dry_run" in payload and payload["dry_run"] is not None:
        cfg.dry_run = bool(payload["dry_run"])
    return cfg


def handle_forge_decision_approve(
    payload: Mapping[str, Any],
    edited_payload: Mapping[str, Any] | None = None,
    *,
    api_client: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """FORGE Decision Card entrypoint for approving a staged `brief_proposal`."""
    if not isinstance(payload, Mapping):
        return {"ok": False, "status": "invalid", "error": "Decision payload must be a mapping."}
    proposal_id = str(payload.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "status": "invalid", "error": "Decision payload missing proposal_id."}

    edited_args: Mapping[str, Any] | None = None
    if edited_payload is not None:
        if not isinstance(edited_payload, Mapping):
            return {
                "ok": False,
                "proposal_id": proposal_id,
                "status": "invalid",
                "error": "edited_payload must be a mapping.",
            }
        nested_args = edited_payload.get("args")
        edited_args = nested_args if isinstance(nested_args, Mapping) else edited_payload

    cfg = _config_from_decision_payload(payload)
    gate = BriefApprovalGate(cfg, api_client=api_client)
    return gate.approve_proposal(proposal_id, edited_args=edited_args, now=now)


def handle_forge_decision_dismiss(
    payload: Mapping[str, Any],
    note: str = "",
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """FORGE Decision Card entrypoint for dismissing a staged `brief_proposal`."""
    if not isinstance(payload, Mapping):
        return {"ok": False, "status": "invalid", "error": "Decision payload must be a mapping."}
    proposal_id = str(payload.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "status": "invalid", "error": "Decision payload missing proposal_id."}

    cfg = _config_from_decision_payload(payload)
    gate = BriefApprovalGate(cfg)
    return gate.dismiss_proposal(proposal_id, note=note, now=now)
