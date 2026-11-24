"""Strictly allowlisted Google Calendar write actions and credential separation for BRIEF (Step 3)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.brief.config import BriefConfig

logger = logging.getLogger(__name__)

ALLOWED_GATE_ACTIONS = frozenset({"calendar.create", "calendar.update"})
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_GOOGLE_OAUTH_PATH = Path("/secrets/google_oauth.json")

READONLY_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/calendar.readonly"
)
WRITE_OAUTH_SCOPES = "https://www.googleapis.com/auth/calendar.events"

_CREATE_REQUIRED_KEYS = frozenset({"summary", "start", "end"})
_CREATE_OPTIONAL_KEYS = frozenset({"calendar_id", "location", "description", "attendees"})
_CREATE_ALLOWED_KEYS = _CREATE_REQUIRED_KEYS | _CREATE_OPTIONAL_KEYS

_UPDATE_MUTABLE_KEYS = frozenset({"summary", "start", "end", "location", "description", "attendees"})
_UPDATE_ALLOWED_KEYS = frozenset({"event_id", "calendar_id"}) | _UPDATE_MUTABLE_KEYS

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

__all__ = [
    "ALLOWED_GATE_ACTIONS",
    "CALENDAR_API_BASE",
    "DEFAULT_GOOGLE_OAUTH_PATH",
    "GOOGLE_OAUTH_AUTH_URL",
    "GOOGLE_TOKEN_URI",
    "READONLY_OAUTH_SCOPES",
    "WRITE_OAUTH_SCOPES",
    "GoogleCalendarWriteClient",
    "build_oauth_consent_urls",
    "canonical_proposal_hash",
    "execute_action",
    "resolve_gate_write_credentials",
    "validate_action_args",
]


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def canonical_proposal_hash(action: str, args: Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 hex digest over canonical `{action, args}` JSON."""
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string")
    if not isinstance(args, Mapping):
        raise TypeError("args must be a mapping")
    payload = {"action": action, "args": _to_jsonable(args)}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_and_normalize_iso(raw_value: Any, field_name: str, tz: ZoneInfo) -> tuple[datetime, str]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 string")
    text = raw_value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO-8601 timestamp: {raw_value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, dt.isoformat()


def _normalize_optional_text(raw_value: Any, field_name: str) -> str:
    if not isinstance(raw_value, str):
        raise TypeError(f"{field_name} must be a string")
    return raw_value.strip()


def _validate_attendees(raw_attendees: Any) -> list[str]:
    if isinstance(raw_attendees, (str, bytes)) or not isinstance(raw_attendees, Sequence):
        raise TypeError("attendees must be a list of valid non-empty email strings")
    seen: set[str] = set()
    normalized: list[str] = []
    for idx, item in enumerate(raw_attendees):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"attendees[{idx}] must be a non-empty email string")
        raw_str = item.strip()
        _, parsed = parseaddr(raw_str)
        candidate = parsed.strip().lower() if parsed else raw_str.lower()
        if not _EMAIL_RE.match(candidate):
            raise ValueError(f"attendees[{idx}] is not a valid email address: {item!r}")
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def validate_action_args(
    action: str,
    args: Mapping[str, Any],
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Validate and canonicalize write action arguments against the strict Phase 2 allowlist."""
    if not isinstance(action, str) or action not in ALLOWED_GATE_ACTIONS:
        raise ValueError(f"Disallowed gate action: {action!r}")
    if not isinstance(args, Mapping):
        raise TypeError("args must be a mapping")

    tz = ZoneInfo(tz_name)
    raw_keys = set(args.keys())

    if action == "calendar.create":
        extra = raw_keys - _CREATE_ALLOWED_KEYS
        if extra:
            raise ValueError(f"Unsupported argument(s) for calendar.create: {sorted(extra)}")
        missing = [k for k in ("summary", "start", "end") if k not in args]
        if missing:
            raise ValueError(f"Missing required argument(s) for calendar.create: {missing}")

        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")

        start_dt, start_iso = _parse_and_normalize_iso(args.get("start"), "start", tz)
        end_dt, end_iso = _parse_and_normalize_iso(args.get("end"), "end", tz)
        if end_dt <= start_dt:
            raise ValueError("end must be strictly after start")

        cal_id_raw = args.get("calendar_id", "primary")
        if cal_id_raw is None:
            calendar_id = "primary"
        elif not isinstance(cal_id_raw, str) or not cal_id_raw.strip():
            raise ValueError("calendar_id must be a non-empty string")
        else:
            calendar_id = cal_id_raw.strip()

        normalized: dict[str, Any] = {
            "calendar_id": calendar_id,
            "summary": summary.strip(),
            "start": start_iso,
            "end": end_iso,
        }
        if "location" in args and args["location"] is not None:
            normalized["location"] = _normalize_optional_text(args["location"], "location")
        if "description" in args and args["description"] is not None:
            normalized["description"] = _normalize_optional_text(args["description"], "description")
        if "attendees" in args and args["attendees"] is not None:
            normalized["attendees"] = _validate_attendees(args["attendees"])
        return normalized

    # action == "calendar.update"
    extra = raw_keys - _UPDATE_ALLOWED_KEYS
    if extra:
        raise ValueError(f"Unsupported argument(s) for calendar.update: {sorted(extra)}")

    event_id = args.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")

    cal_id_raw = args.get("calendar_id", "primary")
    if cal_id_raw is None:
        calendar_id = "primary"
    elif not isinstance(cal_id_raw, str) or not cal_id_raw.strip():
        raise ValueError("calendar_id must be a non-empty string")
    else:
        calendar_id = cal_id_raw.strip()

    normalized = {
        "calendar_id": calendar_id,
        "event_id": event_id.strip(),
    }

    start_dt: datetime | None = None
    end_dt: datetime | None = None

    if "summary" in args and args["summary"] is not None:
        summary_val = args["summary"]
        if not isinstance(summary_val, str) or not summary_val.strip():
            raise ValueError("summary must be a non-empty string")
        normalized["summary"] = summary_val.strip()
    elif "summary" in args:
        raise ValueError("summary cannot be null")

    if "start" in args and args["start"] is not None:
        start_dt, start_iso = _parse_and_normalize_iso(args["start"], "start", tz)
        normalized["start"] = start_iso
    elif "start" in args:
        raise ValueError("start cannot be null")

    if "end" in args and args["end"] is not None:
        end_dt, end_iso = _parse_and_normalize_iso(args["end"], "end", tz)
        normalized["end"] = end_iso
    elif "end" in args:
        raise ValueError("end cannot be null")

    if start_dt is not None and end_dt is not None and end_dt <= start_dt:
        raise ValueError("end must be strictly after start")

    if "location" in args and args["location"] is not None:
        normalized["location"] = _normalize_optional_text(args["location"], "location")
    elif "location" in args:
        raise ValueError("location cannot be null")

    if "description" in args and args["description"] is not None:
        normalized["description"] = _normalize_optional_text(args["description"], "description")
    elif "description" in args:
        raise ValueError("description cannot be null")

    if "attendees" in args and args["attendees"] is not None:
        normalized["attendees"] = _validate_attendees(args["attendees"])
    elif "attendees" in args:
        raise ValueError("attendees cannot be null")

    if not (set(normalized.keys()) & _UPDATE_MUTABLE_KEYS):
        raise ValueError(
            "calendar.update requires at least one mutable field "
            "(summary, start, end, location, description, attendees)"
        )

    return normalized


def _extract_oauth_client_block(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, Mapping):
        return None
    if "client_id" in data:
        return dict(data)
    for wrapper in ("installed", "web"):
        nested = data.get(wrapper)
        if isinstance(nested, Mapping) and "client_id" in nested:
            merged = dict(nested)
            if "refresh_token" in data and "refresh_token" not in merged:
                merged["refresh_token"] = data["refresh_token"]
            return merged
    return None


def _validate_write_oauth_payload(data: Any) -> dict[str, str] | None:
    block = _extract_oauth_client_block(data)
    if block is None:
        return None
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    refresh_token = block.get("refresh_token")
    if not (
        isinstance(client_id, str)
        and client_id.strip()
        and isinstance(client_secret, str)
        and client_secret.strip()
        and isinstance(refresh_token, str)
        and refresh_token.strip()
    ):
        return None
    token_uri = block.get("token_uri")
    resolved_uri = (
        token_uri.strip()
        if isinstance(token_uri, str) and token_uri.strip()
        else GOOGLE_TOKEN_URI
    )
    return {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": refresh_token.strip(),
        "token_uri": resolved_uri,
    }


def _check_secret_file_permissions(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if (mode & 0o077) != 0:
        logger.warning(
            "BRIEF write credentials file %s has overly permissive mode %04o; expected 0600.",
            path,
            mode & 0o777,
        )


def resolve_gate_write_credentials(
    config: BriefConfig,
    *,
    timeout: float = 15.0,
    now_fn: Callable[[], float] | None = None,
    fallback_oauth_path: Path | str = DEFAULT_GOOGLE_OAUTH_PATH,
) -> dict[str, Any]:
    """Resolve write-scoped Google OAuth credentials and exchange refresh_token.

    Strictly checks `config.gate_write_token_path` first. When absent, refuses
    execution unless `config.gate_allow_shared_oauth_fallback` is explicitly True.
    """
    write_path = Path(config.gate_write_token_path)
    fallback_path = Path(fallback_oauth_path)
    used_fallback = False

    if write_path.is_file():
        chosen_path = write_path
    elif config.gate_allow_shared_oauth_fallback:
        if not fallback_path.is_file():
            raise RuntimeError(
                f"BRIEF write token missing at {write_path} and shared fallback missing at {fallback_path}."
            )
        chosen_path = fallback_path
        used_fallback = True
    else:
        raise RuntimeError(
            f"BRIEF write token missing at {write_path} and gate_allow_shared_oauth_fallback is disabled."
        )

    _check_secret_file_permissions(chosen_path)

    try:
        raw_text = chosen_path.read_text(encoding="utf-8").strip()
        parsed = json.loads(raw_text) if raw_text else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Failed to read Google write OAuth JSON from {chosen_path}.") from exc

    validated = _validate_write_oauth_payload(parsed)
    if validated is None:
        raise RuntimeError(f"Invalid or incomplete Google write OAuth payload in {chosen_path}.")

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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token_payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Google OAuth token exchange failed with HTTP {exc.code}."
        ) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Google OAuth token exchange network error: {exc.reason}."
        ) from None
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Google OAuth token exchange returned invalid JSON.") from exc

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
        "used_shared_fallback": used_fallback,
    }


def build_oauth_consent_urls(
    oauth_client_path: Path | str = DEFAULT_GOOGLE_OAUTH_PATH,
    *,
    redirect_uri: str = "http://localhost",
) -> dict[str, str]:
    """Build copy-paste Google OAuth consent URLs for split read-only and write-only tokens.

    Reads only `client_id` and never includes or outputs `client_secret`.
    """
    client_path = Path(oauth_client_path)
    if not client_path.is_file():
        raise FileNotFoundError(f"Google OAuth client file not found: {client_path}")

    try:
        raw = json.loads(client_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Failed to parse Google OAuth client JSON at {client_path}.") from exc

    block = _extract_oauth_client_block(raw)
    client_id = str((block or {}).get("client_id") or "").strip()
    if not client_id:
        raise ValueError(f"Google OAuth client JSON at {client_path} is missing client_id.")

    def _make_url(scope: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{GOOGLE_OAUTH_AUTH_URL}?{query}"

    return {
        "readonly_url": _make_url(READONLY_OAUTH_SCOPES),
        "write_url": _make_url(WRITE_OAUTH_SCOPES),
    }


def _format_iso_pt(iso_str: str | None, tz_name: str) -> str:
    if not iso_str:
        return ""
    text = str(iso_str).strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    local_dt = dt.astimezone(tz)
    label = "PT" if tz_name == "America/Los_Angeles" else (local_dt.tzname() or "PT")
    return f"{local_dt.strftime('%Y-%m-%d %H:%M')} {label}"


def _format_boundary_pt(boundary: Any, tz_name: str) -> str:
    if not isinstance(boundary, Mapping):
        return ""
    dt_str = boundary.get("dateTime")
    if isinstance(dt_str, str) and dt_str.strip():
        return _format_iso_pt(dt_str, tz_name)
    date_str = boundary.get("date")
    if isinstance(date_str, str) and date_str.strip():
        return f"{date_str.strip()} (all-day)"
    return ""


class GoogleCalendarWriteClient:
    """Strictly calendar-write Google Calendar v3 API client with isolated credentials."""

    def __init__(
        self,
        config: BriefConfig,
        *,
        timeout: float = 15.0,
        now_fn: Callable[[], float] | None = None,
        fallback_oauth_path: Path | str = DEFAULT_GOOGLE_OAUTH_PATH,
    ) -> None:
        self.config = config
        self.timeout = float(timeout)
        self._now_fn = now_fn or time.time
        self._fallback_oauth_path = Path(fallback_oauth_path)
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
            resolved = resolve_gate_write_credentials(
                self.config,
                timeout=self.timeout,
                now_fn=self._now_fn,
                fallback_oauth_path=self._fallback_oauth_path,
            )
            self._access_token = str(resolved["access_token"])
            self._expires_at = float(resolved["expires_at"])
            return self._access_token

    def _request_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        encoded_body = json.dumps(_to_jsonable(body), ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            token = self._get_token(force_refresh=(attempt > 0))
            req = urllib.request.Request(
                url,
                data=encoded_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    continue
                raise RuntimeError(
                    f"Google Calendar {method} request failed with HTTP {exc.code}."
                ) from None
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"Google Calendar {method} network error: {exc.reason}."
                ) from None
        raise RuntimeError(f"Failed to execute Google Calendar {method} request after token refresh.")

    def create_event(self, calendar_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST a new event to Google Calendar v3."""
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        url = f"{CALENDAR_API_BASE}/calendars/{encoded_cal}/events"
        return self._request_json("POST", url, payload)

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """PATCH an existing event in Google Calendar v3."""
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        encoded_ev = urllib.parse.quote(event_id, safe="")
        url = f"{CALENDAR_API_BASE}/calendars/{encoded_cal}/events/{encoded_ev}"
        return self._request_json("PATCH", url, payload)


def _build_calendar_api_payload(
    action: str,
    normalized: Mapping[str, Any],
    tz_name: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if "summary" in normalized:
        payload["summary"] = normalized["summary"]
    if "start" in normalized:
        payload["start"] = {"dateTime": normalized["start"], "timeZone": tz_name}
    if "end" in normalized:
        payload["end"] = {"dateTime": normalized["end"], "timeZone": tz_name}
    if "location" in normalized:
        payload["location"] = normalized["location"]
    if "description" in normalized:
        payload["description"] = normalized["description"]
    if "attendees" in normalized:
        payload["attendees"] = [{"email": addr} for addr in normalized["attendees"]]
    return payload


def execute_action(
    action: str,
    args: Mapping[str, Any],
    config: BriefConfig,
    *,
    dry_run: bool | None = None,
    api_client: Any = None,
) -> dict[str, Any]:
    """Validate and execute an allowlisted BRIEF gate action (or return dry-run receipt)."""
    normalized = validate_action_args(action, args, tz_name=config.timezone)
    proposal_hash = canonical_proposal_hash(action, normalized)
    calendar_id = str(normalized["calendar_id"])
    effective_dry_run = config.dry_run if dry_run is None else bool(dry_run)

    if effective_dry_run:
        synthetic_id = (
            str(normalized["event_id"])
            if action == "calendar.update"
            else f"dryrun-{proposal_hash[:12]}"
        )
        return {
            "dry_run": True,
            "action": action,
            "calendar_id": calendar_id,
            "event_id": synthetic_id,
            "html_link": f"https://calendar.google.com/calendar/event?eid={synthetic_id}",
            "summary": str(normalized.get("summary") or ""),
            "start_pt": _format_iso_pt(normalized.get("start"), config.timezone),
            "end_pt": _format_iso_pt(normalized.get("end"), config.timezone),
            "proposal_hash": proposal_hash,
        }

    client = api_client or GoogleCalendarWriteClient(config)
    api_payload = _build_calendar_api_payload(action, normalized, config.timezone)

    if action == "calendar.create":
        resp = client.create_event(calendar_id, api_payload)
    else:
        resp = client.update_event(calendar_id, str(normalized["event_id"]), api_payload)

    if not isinstance(resp, Mapping):
        resp = {}

    event_id = str(resp.get("id") or normalized.get("event_id") or "")
    html_link = str(resp.get("htmlLink") or "")
    summary = str(resp.get("summary") or normalized.get("summary") or "")
    start_pt = _format_boundary_pt(resp.get("start"), config.timezone) or _format_iso_pt(
        normalized.get("start"), config.timezone
    )
    end_pt = _format_boundary_pt(resp.get("end"), config.timezone) or _format_iso_pt(
        normalized.get("end"), config.timezone
    )

    return {
        "dry_run": False,
        "action": action,
        "calendar_id": calendar_id,
        "event_id": event_id,
        "html_link": html_link,
        "summary": summary,
        "start_pt": start_pt,
        "end_pt": end_pt,
        "proposal_hash": proposal_hash,
    }
