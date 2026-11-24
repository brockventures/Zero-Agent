"""Read-only Gmail and Google Calendar watermark watchers for BRIEF (Step 2)."""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.brief.config import BriefConfig, touch_heartbeat
from tools.brief.queue import BriefQueue

logger = logging.getLogger(__name__)

DEFAULT_BRIEF_READONLY_TOKEN_PATH = Path("/secrets/brief_readonly_token.json")
DEFAULT_GOOGLE_OAUTH_PATH = Path("/secrets/google_oauth.json")
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

_ALWAYS_DROP_LABELS = ("SENT", "DRAFT", "TRASH", "SPAM")
_CATEGORY_DROP_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

__all__ = [
    "CALENDAR_API_BASE",
    "DEFAULT_BRIEF_READONLY_TOKEN_PATH",
    "DEFAULT_GOOGLE_OAUTH_PATH",
    "GMAIL_API_BASE",
    "GOOGLE_TOKEN_URI",
    "CalendarWatcher",
    "CursorStore",
    "GmailWatcher",
    "GoogleReadonlyApiClient",
    "call_with_backoff",
    "compute_backoff_delay",
    "resolve_readonly_oauth_credentials",
    "should_prefilter_gmail",
    "touch_heartbeat",
]


def _normalize_email(value: str) -> str:
    _, addr = parseaddr(str(value or "").strip())
    candidate = addr.strip().lower()
    if "@" in candidate:
        return candidate
    raw = str(value or "").strip().lower()
    return raw if "@" in raw else ""


def _extract_email_set(values: Iterable[str] | None) -> set[str]:
    if not values:
        return set()
    return {norm for item in values if (norm := _normalize_email(str(item)))}


def _format_pt_timestamp(ts: float | None, tz_name: str = "America/Los_Angeles") -> str:
    epoch = time.time() if ts is None else float(ts)
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(epoch, tz=UTC).astimezone(tz)
    label = "PT" if tz_name == "America/Los_Angeles" else (dt.tzname() or "PT")
    return f"{dt.strftime('%Y-%m-%d %H:%M')} {label}"


def _extract_http_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    for attr in ("status_code", "code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int):
            return val
    return None


def _extract_retry_after(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
        return seconds if seconds >= 0 else None
    except ValueError:
        return None


def compute_backoff_delay(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    jitter_ratio: float = 0.25,
    rng: Callable[[float, float], float] | None = None,
) -> float:
    """Compute bounded exponential backoff delay with positive jitter."""
    exponent = max(0, int(attempt))
    base_delay = min(float(max_seconds), float(base_seconds) * (2**exponent))
    jitter_fn = rng or random.uniform
    jitter = float(jitter_fn(0.0, max(0.0, base_delay * float(jitter_ratio))))
    return min(float(max_seconds), max(0.0, base_delay + jitter))


def call_with_backoff(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 4,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    jitter_ratio: float = 0.25,
    retryable_statuses: frozenset[int] = _RETRYABLE_HTTP_STATUSES,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: Callable[[float, float], float] | None = None,
) -> Any:
    """Execute `fn()` with exponential backoff + jitter on HTTP 429/5xx errors."""
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            status = _extract_http_status(exc)
            is_retryable = status is not None and (status in retryable_statuses or 500 <= status < 600)
            if not is_retryable or attempt >= attempts - 1:
                raise
            delay = compute_backoff_delay(
                attempt,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
                jitter_ratio=jitter_ratio,
                rng=rng,
            )
            retry_after = _extract_retry_after(exc)
            if retry_after is not None:
                delay = min(float(max_seconds), max(delay, retry_after))
            sleep_fn(delay)
    raise RuntimeError("Unreachable retry loop exit")  # pragma: no cover


class CursorStore:
    """Atomic JSON cursor persistence for BRIEF watchers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        """Load cursors JSON from disk, returning `{}` if the file does not yet exist."""
        with self._lock:
            if not self.path.exists():
                return {}
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError(f"Expected JSON object in cursor file {self.path}")
            return dict(data)

    def save(self, data: Mapping[str, Any]) -> Path:
        """Persist `data` atomically via temp file + fsync + os.replace."""
        with self._lock:
            return self._save_unlocked(data)

    def _save_unlocked(self, data: Mapping[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(
            f".{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        payload = json.dumps(dict(data), indent=2, sort_keys=True) + "\n"
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return self.path

    def get(self, key: str, default: Any = None) -> Any:
        """Return a single cursor value by key."""
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> dict[str, Any]:
        """Atomically update a single cursor key and persist."""
        return self.update({key: value})

    def update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically merge `updates` into the cursor file and persist."""
        with self._lock:
            current: dict[str, Any] = {}
            if self.path.exists():
                raw = self.path.read_text(encoding="utf-8").strip()
                if raw:
                    loaded = json.loads(raw)
                    if not isinstance(loaded, dict):
                        raise TypeError(f"Expected JSON object in cursor file {self.path}")
                    current = dict(loaded)
            current.update(updates)
            self._save_unlocked(current)
            return current


def _validate_oauth_payload(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    refresh_token = data.get("refresh_token")
    if not (
        isinstance(client_id, str)
        and client_id.strip()
        and isinstance(client_secret, str)
        and client_secret.strip()
        and isinstance(refresh_token, str)
        and refresh_token.strip()
    ):
        return None
    token_uri = data.get("token_uri")
    resolved_uri = (
        str(token_uri).strip()
        if isinstance(token_uri, str) and token_uri.strip()
        else GOOGLE_TOKEN_URI
    )
    return {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": refresh_token.strip(),
        "token_uri": resolved_uri,
    }


def resolve_readonly_oauth_credentials(
    explicit_path: Path | str | None = None,
    *,
    timeout: float = 15.0,
    now_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Resolve Google OAuth credentials and exchange refresh_token for an access_token.

    Lookup order when `explicit_path` is None:
      1. /secrets/brief_readonly_token.json
      2. /secrets/google_oauth.json
    """
    if explicit_path is not None:
        candidates = [Path(explicit_path)]
    else:
        candidates = [DEFAULT_BRIEF_READONLY_TOKEN_PATH, DEFAULT_GOOGLE_OAUTH_PATH]

    chosen_path: Path | None = None
    validated: dict[str, str] | None = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8").strip()
            if not content:
                continue
            parsed = json.loads(content)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        validated = _validate_oauth_payload(parsed)
        if validated is not None:
            chosen_path = candidate
            break

    if validated is None or chosen_path is None:
        checked = ", ".join(str(p) for p in candidates)
        raise RuntimeError(
            f"No valid Google OAuth credentials found (checked: {checked})."
        )

    form_body = urllib.parse.urlencode(
        {
            "client_id": validated["client_id"],
            "client_secret": validated["client_secret"],
            "refresh_token": validated["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        validated["token_uri"],
        data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        token_payload = json.loads(resp.read().decode("utf-8"))

    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Google OAuth token exchange response missing access_token.")

    now_ts = (now_fn or time.time)()
    expires_in = int(token_payload.get("expires_in", 3600))
    return {
        "access_token": access_token,
        "expires_in": expires_in,
        "expires_at": float(now_ts) + float(expires_in),
        "token_type": str(token_payload.get("token_type") or "Bearer"),
        "scope": str(token_payload.get("scope") or ""),
        "credentials_path": str(chosen_path),
    }


class GoogleReadonlyApiClient:
    """Strictly HTTP GET-only Google API client with cached OAuth token refresh."""

    def __init__(
        self,
        *,
        credentials_path: Path | str | None = None,
        timeout: float = 15.0,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path) if credentials_path is not None else None
        self.timeout = float(timeout)
        self._now_fn = now_fn or time.time
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def _get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now_ts = float(self._now_fn())
            if (
                not force_refresh
                and self._access_token is not None
                and now_ts < (self._expires_at - 60.0)
            ):
                return self._access_token
            resolved = resolve_readonly_oauth_credentials(
                explicit_path=self.credentials_path,
                timeout=self.timeout,
                now_fn=self._now_fn,
            )
            self._access_token = str(resolved["access_token"])
            self._expires_at = float(resolved["expires_at"])
            return self._access_token

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated HTTP GET request and return parsed JSON."""
        full_url = url
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                sep = "&" if "?" in full_url else "?"
                full_url = f"{full_url}{sep}{urllib.parse.urlencode(filtered)}"

        for attempt in range(2):
            token = self._get_token(force_refresh=(attempt > 0))
            req = urllib.request.Request(
                full_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    continue
                raise
        raise RuntimeError("Failed to execute Google GET request after token refresh.")


def _normalize_headers_map(
    headers: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> dict[str, str]:
    if not headers:
        return {}
    if isinstance(headers, Mapping):
        return {
            str(k).strip().lower(): str(v if v is not None else "")
            for k, v in headers.items()
            if str(k).strip()
        }
    result: dict[str, str] = {}
    for item in headers:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip().lower()
            if name:
                result[name] = str(item.get("value") or "")
    return result


def should_prefilter_gmail(
    labels: Iterable[str] | None,
    headers: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    sender: str,
    owner_emails: Iterable[str] | None = None,
    *,
    allowlisted_emails: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Cheap pre-filter dropping bulk/system/own Gmail messages before LLM routing.

    Precedence:
      1. Always drop SENT / DRAFT / TRASH / SPAM labels.
      2. Always drop own sender address (`owner_emails`).
      3. Drop CATEGORY_PROMOTIONS / CATEGORY_SOCIAL / CATEGORY_FORUMS labels.
      4. Drop List-Unsubscribe bulk mail unless sender is in `allowlisted_emails`.
    """
    label_set = {
        str(lbl).strip().upper()
        for lbl in (labels or ())
        if str(lbl).strip()
    }
    for lbl in _ALWAYS_DROP_LABELS:
        if lbl in label_set:
            return True, f"label:{lbl}"

    header_map = _normalize_headers_map(headers)
    effective_sender = str(sender or "").strip() or header_map.get("from", "")
    sender_addr = parseaddr(effective_sender)[1].strip().lower()
    if not sender_addr and "@" in effective_sender:
        sender_addr = effective_sender.lower()

    own_set = _extract_email_set(owner_emails)
    if sender_addr and sender_addr in own_set:
        return True, "own_sender"

    for lbl in _CATEGORY_DROP_LABELS:
        if lbl in label_set:
            return True, f"label:{lbl}"

    list_unsub = header_map.get("list-unsubscribe", "").strip()
    if list_unsub:
        allowlist_set = _extract_email_set(allowlisted_emails)
        if not sender_addr or sender_addr not in allowlist_set:
            return True, "list_unsubscribe"

    return False, ""


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    padded = data + ("=" * (-len(data) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return raw.decode("utf-8", errors="replace")
    except ValueError:
        return ""


def _extract_gmail_body_text(payload: Mapping[str, Any] | None, snippet: str = "") -> str:
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    def _walk(part: Mapping[str, Any] | None) -> None:
        if not isinstance(part, Mapping):
            return
        mime = str(part.get("mimeType") or "").lower()
        body_data = str((part.get("body") or {}).get("data") or "")
        if body_data:
            decoded = _decode_b64url(body_data)
            if mime == "text/plain":
                plain_chunks.append(decoded)
            elif mime == "text/html":
                html_chunks.append(decoded)
            elif not mime.startswith("multipart/"):
                plain_chunks.append(decoded)
        for child in part.get("parts") or ():
            _walk(child)

    _walk(payload)
    if plain_chunks:
        text = "\n".join(c.strip() for c in plain_chunks if c.strip())
    elif html_chunks:
        joined = "\n".join(html_chunks)
        stripped = _HTML_TAG_RE.sub(" ", html.unescape(joined))
        text = _WHITESPACE_RE.sub(" ", stripped).strip()
    else:
        text = str(snippet or "").strip()

    if len(text) > 4000:
        text = text[:3997] + "..."
    return text


def _parse_gmail_received_at(
    msg: Mapping[str, Any],
    header_map: Mapping[str, str],
    fallback_now: float,
) -> float:
    internal_date = msg.get("internalDate")
    if internal_date is not None:
        try:
            return float(internal_date) / 1000.0
        except (TypeError, ValueError):
            pass
    date_hdr = header_map.get("date", "").strip()
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except (TypeError, ValueError):
            pass
    return fallback_now


class _BaseWatcher:
    def __init__(
        self,
        queue: BriefQueue,
        cursors: CursorStore,
        config: BriefConfig,
        api_client: Any = None,
        notify_fn: Callable[[str], None] | None = None,
        *,
        now_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.queue = queue
        self.cursors = cursors
        self.config = config
        self._now_fn = now_fn or time.time
        self._sleep_fn = sleep_fn or time.sleep
        self.api_client = api_client or GoogleReadonlyApiClient(now_fn=self._now_fn)
        self._notify_fn = notify_fn

    def _notify(self, message: str) -> None:
        if self._notify_fn is not None:
            try:
                self._notify_fn(message)
            except Exception as exc:  # noqa: BLE001 - alert failure must not crash watcher
                logger.warning("BRIEF watcher notify_fn failed: %s", exc)
            return

        print(message, file=sys.stderr)
        if not self.config.dry_run:
            try:
                from tools.outbox import queue_outbox_message

                queue_outbox_message(
                    str(self.config.channel_id),
                    message,
                    source_turn="brief-watcher",
                )
            except Exception as exc:  # noqa: BLE001 - outbox failure must not drop state
                logger.warning("Failed to queue BRIEF watcher outbox alert: %s", exc)

    def _api_get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return call_with_backoff(
            lambda: self.api_client.get(url, params=params),
            sleep_fn=self._sleep_fn,
        )


class GmailWatcher(_BaseWatcher):
    """Read-only Gmail historyId watermark watcher."""

    def __init__(
        self,
        queue: BriefQueue,
        cursors: CursorStore,
        config: BriefConfig,
        api_client: Any = None,
        notify_fn: Callable[[str], None] | None = None,
        *,
        own_emails: Iterable[str] | None = None,
        now_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(
            queue,
            cursors,
            config,
            api_client=api_client,
            notify_fn=notify_fn,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        self._explicit_own_emails = _extract_email_set(own_emails)

    def _get_profile(self) -> dict[str, Any]:
        if hasattr(self.api_client, "get_profile"):
            return call_with_backoff(
                self.api_client.get_profile,
                sleep_fn=self._sleep_fn,
            )
        return self._api_get(f"{GMAIL_API_BASE}/users/me/profile")

    def _list_history(
        self,
        start_history_id: str,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        if hasattr(self.api_client, "list_history"):
            return call_with_backoff(
                lambda: self.api_client.list_history(start_history_id, page_token=page_token),
                sleep_fn=self._sleep_fn,
            )
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._api_get(f"{GMAIL_API_BASE}/users/me/history", params=params)

    def _get_message(self, message_id: str) -> dict[str, Any]:
        if hasattr(self.api_client, "get_message"):
            return call_with_backoff(
                lambda: self.api_client.get_message(message_id),
                sleep_fn=self._sleep_fn,
            )
        return self._api_get(
            f"{GMAIL_API_BASE}/users/me/messages/{urllib.parse.quote(message_id, safe='')}",
            params={"format": "full"},
        )

    def _watermark_from_profile(self) -> tuple[str, str]:
        profile = self._get_profile()
        history_id = str(profile.get("historyId") or "").strip()
        if not history_id:
            raise RuntimeError("Gmail users/me/profile response missing historyId.")
        email_addr = _normalize_email(str(profile.get("emailAddress") or ""))
        updates: dict[str, Any] = {"gmail_history_id": history_id}
        if email_addr:
            updates["gmail_email_address"] = email_addr
        self.cursors.update(updates)
        return history_id, email_addr

    def poll_once(self) -> dict[str, Any]:
        """Poll Gmail once, watermarking on startup and advancing cursor only after commit."""
        now_ts = float(self._now_fn())
        cursor_data = self.cursors.load()
        current_history_id = str(cursor_data.get("gmail_history_id") or "").strip()

        if not current_history_id:
            history_id, _ = self._watermark_from_profile()
            touch_heartbeat(self.config.heartbeats_dir, "gmail_watcher", now=now_ts)
            return {
                "status": "watermarked",
                "history_id": history_id,
                "enqueued": 0,
                "filtered": 0,
            }

        own_emails = set(self._explicit_own_emails)
        stored_email = _normalize_email(str(cursor_data.get("gmail_email_address") or ""))
        if stored_email:
            own_emails.add(stored_email)

        message_ids: list[str] = []
        seen_ids: set[str] = set()
        latest_history_id = current_history_id
        page_token: str | None = None

        try:
            while True:
                page = self._list_history(current_history_id, page_token=page_token)
                page_hid = str(page.get("historyId") or "").strip()
                if page_hid:
                    latest_history_id = page_hid

                for record in page.get("history") or ():
                    for added in record.get("messagesAdded") or ():
                        msg_meta = added.get("message") or {}
                        msg_id = str(msg_meta.get("id") or "").strip()
                        if msg_id and msg_id not in seen_ids:
                            seen_ids.add(msg_id)
                            message_ids.append(msg_id)

                next_token = str(page.get("nextPageToken") or "").strip()
                if not next_token:
                    break
                page_token = next_token
        except Exception as exc:
            status = _extract_http_status(exc)
            if status in (404, 410):
                new_hid, _ = self._watermark_from_profile()
                pt_now = _format_pt_timestamp(now_ts, tz_name=self.config.timezone)
                self._notify(
                    f"[BRIEF] Gmail history cursor expired (HTTP {status}) at {pt_now}; "
                    f"re-watermarked to historyId {new_hid}. Check inbox for any gap."
                )
                touch_heartbeat(self.config.heartbeats_dir, "gmail_watcher", now=now_ts)
                return {
                    "status": "rewatermarked",
                    "history_id": new_hid,
                    "enqueued": 0,
                    "filtered": 0,
                }
            raise

        enqueued = 0
        filtered = 0
        duplicates = 0
        missing_404 = 0

        for msg_id in message_ids:
            try:
                msg = self._get_message(msg_id)
            except Exception as exc:
                if _extract_http_status(exc) == 404:
                    missing_404 += 1
                    logger.debug("Skipping deleted Gmail message %s (404 mid-poll)", msg_id)
                    continue
                raise

            actual_id = str(msg.get("id") or msg_id).strip()
            thread_id = str(msg.get("threadId") or actual_id).strip()
            labels = msg.get("labelIds") or []
            payload = msg.get("payload") or {}
            raw_headers = payload.get("headers") or []
            header_map = _normalize_headers_map(raw_headers)
            sender = header_map.get("from", "").strip()

            dropped, _reason = should_prefilter_gmail(
                labels,
                raw_headers,
                sender,
                own_emails,
                allowlisted_emails=self.config.allowlisted_emails,
            )
            if dropped:
                filtered += 1
                continue

            received_at = _parse_gmail_received_at(msg, header_map, now_ts)
            subject = header_map.get("subject", "(no subject)").strip() or "(no subject)"
            body_text = _extract_gmail_body_text(payload, snippet=str(msg.get("snippet") or ""))
            received_pt = _format_pt_timestamp(received_at, tz_name=self.config.timezone)
            envelope_body = (
                f"Subject: {subject}\n"
                f"From: {sender or '(unknown)'}\n"
                f"Date: {received_pt}\n"
                f"Labels: {', '.join(str(lbl) for lbl in labels) or '(none)'}\n\n"
                f"{body_text}"
            ).strip()

            inserted_id = self.queue.enqueue_event(
                source="gmail",
                key=thread_id,
                target_tag=f"intake/gmail/{thread_id}",
                external_id=actual_id,
                sender=sender,
                body=envelope_body,
                received_at=received_at,
            )
            if inserted_id is not None:
                enqueued += 1
            else:
                duplicates += 1

        # Advance cursor ONLY AFTER all surviving queue commits succeed.
        if latest_history_id and latest_history_id != current_history_id:
            self.cursors.set("gmail_history_id", latest_history_id)

        touch_heartbeat(self.config.heartbeats_dir, "gmail_watcher", now=now_ts)
        return {
            "status": "ok",
            "history_id": latest_history_id,
            "enqueued": enqueued,
            "filtered": filtered,
            "duplicates": duplicates,
            "missing_404": missing_404,
        }

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Continuously poll Gmail every `gmail_interval_seconds`."""
        iterations = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            try:
                self.poll_once()
            except Exception:
                logger.exception("Unexpected error in GmailWatcher poll")
                touch_heartbeat(self.config.heartbeats_dir, "gmail_watcher", now=self._now_fn())

            if max_iterations is not None and iterations >= max_iterations:
                break

            sleep_s = max(1.0, float(self.config.gmail_interval_seconds))
            if stop_event is not None:
                if stop_event.wait(timeout=sleep_s):
                    break
            else:
                self._sleep_fn(sleep_s)


def _parse_calendar_iso_datetime(
    raw_dt: str,
    tz_name: str,
) -> datetime | None:
    text = str(raw_dt or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def _parse_calendar_boundary(
    boundary: Mapping[str, Any] | None,
    *,
    tz_name: str,
    is_end: bool,
) -> datetime | None:
    if not isinstance(boundary, Mapping):
        return None
    dt_str = boundary.get("dateTime")
    if isinstance(dt_str, str) and dt_str.strip():
        return _parse_calendar_iso_datetime(dt_str, tz_name)
    date_str = boundary.get("date")
    if isinstance(date_str, str) and date_str.strip():
        try:
            d = date.fromisoformat(date_str.strip())
        except ValueError:
            return None
        tz = ZoneInfo(tz_name)
        dt = datetime(d.year, d.month, d.day, tzinfo=tz)
        # Google Calendar end.date is exclusive; start.date covers that day (add 1 day if end absent).
        if not is_end:
            dt += timedelta(days=1)
        return dt
    return None


def _is_cancelled_past_event(
    event: Mapping[str, Any],
    now_ts: float,
    tz_name: str,
) -> bool:
    if str(event.get("status") or "").strip().lower() != "cancelled":
        return False
    end_dt = _parse_calendar_boundary(event.get("end"), tz_name=tz_name, is_end=True)
    if end_dt is None:
        end_dt = _parse_calendar_boundary(event.get("start"), tz_name=tz_name, is_end=False)
    if end_dt is None:
        return False
    return end_dt.timestamp() <= float(now_ts)


def _format_calendar_when(boundary: Mapping[str, Any] | None, tz_name: str) -> str:
    if not isinstance(boundary, Mapping):
        return "(unspecified)"
    dt_str = boundary.get("dateTime")
    if isinstance(dt_str, str) and dt_str.strip():
        dt = _parse_calendar_iso_datetime(dt_str, tz_name)
        if dt is not None:
            local_dt = dt.astimezone(ZoneInfo(tz_name))
            return f"{local_dt.strftime('%Y-%m-%d %H:%M')} PT"
        return dt_str.strip()
    date_str = boundary.get("date")
    if isinstance(date_str, str) and date_str.strip():
        return f"{date_str.strip()} (all-day)"
    return "(unspecified)"


class CalendarWatcher(_BaseWatcher):
    """Read-only Google Calendar syncToken watermark watcher."""

    def _list_events(
        self,
        *,
        sync_token: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        if hasattr(self.api_client, "list_events"):
            return call_with_backoff(
                lambda: self.api_client.list_events(
                    sync_token=sync_token,
                    page_token=page_token,
                ),
                sleep_fn=self._sleep_fn,
            )
        params: dict[str, Any] = {}
        if sync_token:
            params["syncToken"] = sync_token
            params["showDeleted"] = "true"
        if page_token:
            params["pageToken"] = page_token
        return self._api_get(f"{CALENDAR_API_BASE}/calendars/primary/events", params=params)

    def _fetch_sync_watermark(self) -> str:
        page_token: str | None = None
        sync_token = ""
        while True:
            page = self._list_events(sync_token=None, page_token=page_token)
            next_sync = str(page.get("nextSyncToken") or "").strip()
            if next_sync:
                sync_token = next_sync
            next_page = str(page.get("nextPageToken") or "").strip()
            if not next_page:
                break
            page_token = next_page
        if not sync_token:
            raise RuntimeError("Calendar API did not return nextSyncToken during watermark sync.")
        self.cursors.set("calendar_sync_token", sync_token)
        return sync_token

    def poll_once(self) -> dict[str, Any]:
        """Poll Google Calendar once, watermarking on startup and advancing syncToken post-commit."""
        now_ts = float(self._now_fn())
        cursor_data = self.cursors.load()
        current_sync_token = str(cursor_data.get("calendar_sync_token") or "").strip()

        if not current_sync_token:
            sync_token = self._fetch_sync_watermark()
            touch_heartbeat(self.config.heartbeats_dir, "calendar_watcher", now=now_ts)
            return {
                "status": "watermarked",
                "sync_token": sync_token,
                "enqueued": 0,
                "filtered": 0,
            }

        events: list[Mapping[str, Any]] = []
        next_sync_token = current_sync_token
        page_token: str | None = None

        try:
            while True:
                page = self._list_events(
                    sync_token=current_sync_token,
                    page_token=page_token,
                )
                for item in page.get("items") or ():
                    if isinstance(item, Mapping):
                        events.append(item)
                page_sync = str(page.get("nextSyncToken") or "").strip()
                if page_sync:
                    next_sync_token = page_sync
                next_page = str(page.get("nextPageToken") or "").strip()
                if not next_page:
                    break
                page_token = next_page
        except Exception as exc:
            if _extract_http_status(exc) == 410:
                new_sync = self._fetch_sync_watermark()
                pt_now = _format_pt_timestamp(now_ts, tz_name=self.config.timezone)
                self._notify(
                    f"[BRIEF] Calendar syncToken expired (HTTP 410) at {pt_now}; "
                    "re-watermarked cleanly. Check upcoming calendar events for any gap."
                )
                touch_heartbeat(self.config.heartbeats_dir, "calendar_watcher", now=now_ts)
                return {
                    "status": "rewatermarked",
                    "sync_token": new_sync,
                    "enqueued": 0,
                    "filtered": 0,
                }
            raise

        enqueued = 0
        filtered = 0
        duplicates = 0

        for ev in events:
            if _is_cancelled_past_event(ev, now_ts, self.config.timezone):
                filtered += 1
                continue

            event_id = str(ev.get("id") or "").strip()
            if not event_id:
                continue

            updated_val = str(ev.get("updated") or ev.get("etag") or "").strip()
            external_id = f"{event_id}:{updated_val}"

            organizer = ev.get("organizer") if isinstance(ev.get("organizer"), Mapping) else {}
            creator = ev.get("creator") if isinstance(ev.get("creator"), Mapping) else {}
            sender = str(
                (organizer or {}).get("email")
                or (creator or {}).get("email")
                or ""
            ).strip()

            updated_dt = _parse_calendar_iso_datetime(
                str(ev.get("updated") or ""),
                self.config.timezone,
            )
            received_at = updated_dt.timestamp() if updated_dt is not None else now_ts

            summary = str(ev.get("summary") or "(untitled event)").strip()
            status_val = str(ev.get("status") or "confirmed").strip()
            start_str = _format_calendar_when(ev.get("start"), self.config.timezone)
            end_str = _format_calendar_when(ev.get("end"), self.config.timezone)
            location = str(ev.get("location") or "").strip()
            description = str(ev.get("description") or "").strip()
            if len(description) > 2000:
                description = description[:1997] + "..."

            lines = [
                f"Event: {summary}",
                f"Status: {status_val}",
                f"Start: {start_str}",
                f"End: {end_str}",
                f"Organizer: {sender or '(unknown)'}",
            ]
            if location:
                lines.append(f"Location: {location}")
            if description:
                lines.append(f"\n{description}")

            inserted_id = self.queue.enqueue_event(
                source="calendar",
                key=event_id,
                target_tag=f"intake/calendar/{event_id}",
                external_id=external_id,
                sender=sender,
                body="\n".join(lines),
                received_at=received_at,
            )
            if inserted_id is not None:
                enqueued += 1
            else:
                duplicates += 1

        # Advance syncToken ONLY AFTER all surviving queue commits succeed.
        if next_sync_token and next_sync_token != current_sync_token:
            self.cursors.set("calendar_sync_token", next_sync_token)

        touch_heartbeat(self.config.heartbeats_dir, "calendar_watcher", now=now_ts)
        return {
            "status": "ok",
            "sync_token": next_sync_token,
            "enqueued": enqueued,
            "filtered": filtered,
            "duplicates": duplicates,
        }

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Continuously poll Calendar every `calendar_interval_seconds`."""
        iterations = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            try:
                self.poll_once()
            except Exception:
                logger.exception("Unexpected error in CalendarWatcher poll")
                touch_heartbeat(self.config.heartbeats_dir, "calendar_watcher", now=self._now_fn())

            if max_iterations is not None and iterations >= max_iterations:
                break

            sleep_s = max(1.0, float(self.config.calendar_interval_seconds))
            if stop_event is not None:
                if stop_event.wait(timeout=sleep_s):
                    break
            else:
                self._sleep_fn(sleep_s)
