"""SQLite WAL inbox queue and session map for BRIEF."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path
from typing import Any

EVENT_STATUSES = ("pending", "delivering", "done", "dropped", "failed")
TRUSTED_INTERNAL_SOURCES = frozenset({"timer", "internal"})


def _normalize_sender(sender: str) -> str:
    _, addr = parseaddr(str(sender or "").strip())
    candidate = addr.strip().lower()
    if "@" in candidate:
        return candidate
    raw = str(sender or "").strip().lower()
    return raw if "@" in raw else ""


def _row_to_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["trusted"] = bool(item.get("trusted"))
    return item


def _row_to_session_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


class BriefQueue:
    """Persistent SQLite WAL event queue and per-tag session store."""

    def __init__(
        self,
        db_path: Path | str,
        allowlisted_senders: Iterable[str] = (),
    ) -> None:
        self.db_path = Path(db_path)
        self._allowlisted_senders: frozenset[str] = frozenset(
            norm for s in allowlisted_senders if (norm := _normalize_sender(s))
        )

    @property
    def allowlisted_senders(self) -> frozenset[str]:
        return self._allowlisted_senders

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
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
        """Create parent directories, tables, and indexes if they do not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    key TEXT NOT NULL,
                    target_tag TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    trusted INTEGER NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL,
                    claimed_at REAL,
                    updated_at REAL NOT NULL,
                    error TEXT,
                    UNIQUE(source, external_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_claim
                    ON events(status, next_retry_at, received_at, id);

                CREATE INDEX IF NOT EXISTS idx_events_status_updated
                    ON events(status, updated_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    tag TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def _compute_trusted(self, source: str, sender: str) -> bool:
        if source in TRUSTED_INTERNAL_SOURCES:
            return True
        normalized = _normalize_sender(sender)
        return bool(normalized and normalized in self._allowlisted_senders)

    def enqueue_event(
        self,
        source: str,
        key: str,
        target_tag: str,
        external_id: str,
        sender: str = "",
        body: str = "",
        received_at: float | None = None,
    ) -> int | None:
        """Insert a new event idempotently, computing `trusted` strictly server-side.

        Returns the new integer event id, or None if `(source, external_id)` already exists.
        """
        now_ts = time.time() if received_at is None else float(received_at)
        trusted_flag = 1 if self._compute_trusted(source, sender) else 0
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (
                    source, key, target_tag, external_id, sender, trusted,
                    received_at, body, status, attempts, next_retry_at,
                    claimed_at, updated_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL)
                ON CONFLICT(source, external_id) DO NOTHING
                RETURNING id
                """,
                (
                    source,
                    key,
                    target_tag,
                    external_id,
                    sender,
                    trusted_flag,
                    now_ts,
                    body,
                    now_ts,
                ),
            )
            row = cur.fetchone()
            return int(row["id"]) if row is not None else None

    def claim_next_event(
        self,
        *,
        now: float | None = None,
        only_sources: Sequence[str] | None = None,
        exclude_sources: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest eligible pending event for delivery."""
        if only_sources is not None and len(only_sources) == 0:
            return None

        now_ts = time.time() if now is None else float(now)
        where_clauses = [
            "status = 'pending'",
            "(next_retry_at IS NULL OR next_retry_at <= ?)",
        ]
        select_params: list[Any] = [now_ts]

        if only_sources is not None:
            placeholders = ",".join("?" for _ in only_sources)
            where_clauses.append(f"source IN ({placeholders})")
            select_params.extend(only_sources)

        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"source NOT IN ({placeholders})")
            select_params.extend(exclude_sources)

        where_sql = " AND ".join(where_clauses)
        sql = f"""
            UPDATE events
            SET status = 'delivering',
                claimed_at = ?,
                updated_at = ?,
                attempts = attempts + 1
            WHERE id = (
                SELECT id FROM events
                WHERE {where_sql}
                ORDER BY received_at ASC, id ASC
                LIMIT 1
            )
            RETURNING *
        """
        with self._tx() as conn:
            cur = conn.execute(sql, [now_ts, now_ts, *select_params])
            row = cur.fetchone()
            return _row_to_event_dict(row) if row is not None else None

    def mark_done(self, event_id: int, *, now: float | None = None) -> bool:
        """Mark an event as completed."""
        now_ts = time.time() if now is None else float(now)
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE events
                SET status = 'done',
                    claimed_at = NULL,
                    next_retry_at = NULL,
                    updated_at = ?,
                    error = NULL
                WHERE id = ?
                """,
                (now_ts, event_id),
            )
            return cur.rowcount > 0

    def mark_dropped(
        self,
        event_id: int,
        reason: str = "",
        *,
        now: float | None = None,
    ) -> bool:
        """Mark an event as intentionally dropped."""
        now_ts = time.time() if now is None else float(now)
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE events
                SET status = 'dropped',
                    claimed_at = NULL,
                    next_retry_at = NULL,
                    updated_at = ?,
                    error = ?
                WHERE id = ?
                """,
                (now_ts, reason or None, event_id),
            )
            return cur.rowcount > 0

    def mark_failed_or_retry(
        self,
        event_id: int,
        error: str,
        *,
        max_retries: int = 3,
        retry_delay_seconds: float = 30.0,
        backoff_factor: float = 2.0,
        next_retry_at: float | None = None,
        now: float | None = None,
    ) -> str | None:
        """Schedule retry or transition an event to terminal 'failed' when attempts >= max_retries."""
        now_ts = time.time() if now is None else float(now)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT attempts FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None

            attempts = int(row["attempts"])
            if attempts >= max_retries:
                conn.execute(
                    """
                    UPDATE events
                    SET status = 'failed',
                        claimed_at = NULL,
                        next_retry_at = NULL,
                        updated_at = ?,
                        error = ?
                    WHERE id = ?
                    """,
                    (now_ts, error, event_id),
                )
                return "failed"

            if next_retry_at is None:
                exponent = max(0, attempts - 1)
                delay = retry_delay_seconds * (backoff_factor**exponent)
                retry_ts = now_ts + delay
            else:
                retry_ts = float(next_retry_at)

            conn.execute(
                """
                UPDATE events
                SET status = 'pending',
                    claimed_at = NULL,
                    next_retry_at = ?,
                    updated_at = ?,
                    error = ?
                WHERE id = ?
                """,
                (retry_ts, now_ts, error, event_id),
            )
            return "pending"

    def recover_stuck_deliveries(
        self,
        stuck_seconds: float = 300.0,
        *,
        max_retries: int = 3,
        now: float | None = None,
    ) -> int:
        """Recover events stuck in 'delivering' past `stuck_seconds`.

        Events with `attempts >= max_retries` transition to 'failed' so crashing
        deliveries do not loop forever; others return to 'pending'.
        """
        now_ts = time.time() if now is None else float(now)
        cutoff = now_ts - float(stuck_seconds)
        with self._tx() as conn:
            failed_cur = conn.execute(
                """
                UPDATE events
                SET status = 'failed',
                    claimed_at = NULL,
                    next_retry_at = NULL,
                    updated_at = ?,
                    error = COALESCE(error, 'stuck delivery exceeded max_retries')
                WHERE status = 'delivering'
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= ?
                  AND attempts >= ?
                """,
                (now_ts, cutoff, max_retries),
            )
            requeued_cur = conn.execute(
                """
                UPDATE events
                SET status = 'pending',
                    claimed_at = NULL,
                    next_retry_at = ?,
                    updated_at = ?,
                    error = COALESCE(error, 'recovered stuck delivery')
                WHERE status = 'delivering'
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= ?
                  AND attempts < ?
                """,
                (now_ts, now_ts, cutoff, max_retries),
            )
            return int(failed_cur.rowcount) + int(requeued_cur.rowcount)

    def count_done_since(
        self,
        since_ts: float,
        *,
        only_sources: Sequence[str] | None = None,
        exclude_sources: Sequence[str] | None = None,
    ) -> int:
        """Count completed ('done') events updated at or after `since_ts`."""
        if only_sources is not None and len(only_sources) == 0:
            return 0

        clauses = ["status = 'done'", "updated_at >= ?"]
        params: list[Any] = [float(since_ts)]

        if only_sources is not None:
            placeholders = ",".join("?" for _ in only_sources)
            clauses.append(f"source IN ({placeholders})")
            params.extend(only_sources)

        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            clauses.append(f"source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        sql = f"SELECT COUNT(*) AS cnt FROM events WHERE {' AND '.join(clauses)}"
        with self._tx() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row["cnt"]) if row is not None else 0

    def list_failed_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return failed events ordered most-recently-updated first."""
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE status = 'failed'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [_row_to_event_dict(r) for r in rows]

    def get_queue_stats(self) -> dict[str, Any]:
        """Return event counts by status plus total events and active session count."""
        counts: dict[str, int] = {status: 0 for status in EVENT_STATUSES}
        with self._tx() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM events GROUP BY status"
            ).fetchall():
                counts[str(row["status"])] = int(row["cnt"])
            sess_row = conn.execute("SELECT COUNT(*) AS cnt FROM sessions").fetchone()
            sessions_count = int(sess_row["cnt"]) if sess_row is not None else 0

        total = sum(counts.values())
        return {
            **counts,
            "total": total,
            "sessions": sessions_count,
        }

    def get_session(self, tag: str) -> dict[str, Any] | None:
        """Fetch a session record by tag."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE tag = ?",
                (tag,),
            ).fetchone()
            return _row_to_session_dict(row) if row is not None else None

    def upsert_session(
        self,
        tag: str,
        conversation_id: str | None = None,
        *,
        turn_count: int | None = None,
        increment_turns: int = 0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Insert or update a session record keyed by `tag`."""
        now_ts = time.time() if now is None else float(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM sessions WHERE tag = ?",
                (tag,),
            ).fetchone()
            if existing is None:
                initial_turns = (
                    int(turn_count) if turn_count is not None else max(0, int(increment_turns))
                )
                row = conn.execute(
                    """
                    INSERT INTO sessions (tag, conversation_id, turn_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    (tag, conversation_id, initial_turns, now_ts, now_ts),
                ).fetchone()
                return _row_to_session_dict(row)

            new_conv_id = (
                conversation_id if conversation_id is not None else existing["conversation_id"]
            )
            new_turns = (
                int(turn_count)
                if turn_count is not None
                else int(existing["turn_count"]) + int(increment_turns)
            )
            row = conn.execute(
                """
                UPDATE sessions
                SET conversation_id = ?,
                    turn_count = ?,
                    updated_at = ?
                WHERE tag = ?
                RETURNING *
                """,
                (new_conv_id, new_turns, now_ts, tag),
            ).fetchone()
            return _row_to_session_dict(row)

    def clear_session_conversation(
        self,
        tag: str,
        *,
        reset_turn_count: bool = False,
        now: float | None = None,
    ) -> bool:
        """Clear a session's stored conversation_id (optionally resetting turn_count)."""
        now_ts = time.time() if now is None else float(now)
        with self._tx() as conn:
            if reset_turn_count:
                cur = conn.execute(
                    """
                    UPDATE sessions
                    SET conversation_id = NULL,
                        turn_count = 0,
                        updated_at = ?
                    WHERE tag = ?
                    """,
                    (now_ts, tag),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE sessions
                    SET conversation_id = NULL,
                        updated_at = ?
                    WHERE tag = ?
                    """,
                    (now_ts, tag),
                )
            return cur.rowcount > 0

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """List stored sessions ordered by most recently updated."""
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                ORDER BY updated_at DESC, tag ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [_row_to_session_dict(r) for r in rows]


def list_sessions(db_path: Path | str, limit: int = 100) -> list[dict[str, Any]]:
    """Convenience module-level wrapper around BriefQueue.list_sessions."""
    return BriefQueue(db_path).list_sessions(limit=limit)
