"""Strictly allowlisted Google Calendar & Gmail Draft write actions and split OAuth for BRIEF (Step 3)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.brief.config import BriefConfig

logger = logging.getLogger(__name__)

ALLOWED_GATE_ACTIONS = frozenset({"calendar.create", "calendar.update", "gmail.create_draft"})
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
DEFAULT_GOOGLE_OAUTH_PATH = Path("/secrets/google_oauth.json")

READONLY_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/calendar.readonly"
)
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
WRITE_OAUTH_SCOPES = f"{CALENDAR_WRITE_SCOPE} {GMAIL_COMPOSE_SCOPE}"

_FORBIDDEN_OAUTH_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    }
)

_CREATE_REQUIRED_KEYS = frozenset({"summary", "start", "end"})
_CREATE_OPTIONAL_KEYS = frozenset({"calendar_id", "location", "description", "attendees"})
_CREATE_ALLOWED_KEYS = _CREATE_REQUIRED_KEYS | _CREATE_OPTIONAL_KEYS

_UPDATE_MUTABLE_KEYS = frozenset({"summary", "start", "end", "location", "description", "attendees"})
_UPDATE_ALLOWED_KEYS = frozenset({"event_id", "calendar_id"}) | _UPDATE_MUTABLE_KEYS

_DRAFT_REQUIRED_KEYS = frozenset({"to", "subject", "body"})
_DRAFT_OPTIONAL_KEYS = frozenset({"cc", "bcc", "thread_id", "in_reply_to", "references"})
_DRAFT_ALLOWED_KEYS = _DRAFT_REQUIRED_KEYS | _DRAFT_OPTIONAL_KEYS

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_MAX_SUBJECT_LEN = 500
_MAX_BODY_LEN = 50000
_MAX_IN_REPLY_TO_LEN = 500
_MAX_REFERENCES_LEN = 2000

__all__ = [
    "ALLOWED_GATE_ACTIONS",
    "CALENDAR_API_BASE",
    "CALENDAR_WRITE_SCOPE",
    "DEFAULT_GOOGLE_OAUTH_PATH",
    "GMAIL_API_BASE",
    "GMAIL_COMPOSE_SCOPE",
    "GOOGLE_OAUTH_AUTH_URL",
    "GOOGLE_TOKEN_URI",
    "READONLY_OAUTH_SCOPES",
    "WRITE_OAUTH_SCOPES",
    "GoogleCalendarWriteClient",
    "GoogleGmailDraftWriteClient",
    "build_oauth_consent_urls",
    "canonical_proposal_hash",
    "exchange_oauth_authorization_code",
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


def _reject_crlf(value: str, field_name: str) -> None:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{field_name} must not contain CR, LF, or NUL characters")


def _validate_email_sequence(
    raw_value: Any,
    field_name: str,
    *,
    allow_single_str: bool = False,
    required: bool = False,
) -> list[str]:
    items: list[Any]
    if allow_single_str and isinstance(raw_value, str):
        _reject_crlf(raw_value, field_name)
        items = [raw_value]
    elif isinstance(raw_value, (str, bytes)) or not isinstance(raw_value, Sequence):
        expected = (
            "an email string or list of valid non-empty email strings"
            if allow_single_str
            else "a list of valid non-empty email strings"
        )
        raise TypeError(f"{field_name} must be {expected}")
    else:
        items = list(raw_value)

    seen: set[str] = set()
    normalized: list[str] = []
    for idx, item in enumerate(items):
        if not isinstance(item, str):
            raise TypeError(f"{field_name}[{idx}] must be a non-empty email string")
        _reject_crlf(item, f"{field_name}[{idx}]")
        raw_str = item.strip()
        if not raw_str:
            raise ValueError(f"{field_name}[{idx}] must be a non-empty email string")
        _, parsed = parseaddr(raw_str)
        candidate = parsed.strip().lower() if parsed else raw_str.lower()
        _reject_crlf(candidate, f"{field_name}[{idx}]")
        if not _EMAIL_RE.match(candidate):
            raise ValueError(f"{field_name}[{idx}] is not a valid email address: {item!r}")
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)

    if required and not normalized:
        raise ValueError(f"{field_name} must contain at least one valid email address")
    return normalized


def _validate_attendees(raw_attendees: Any) -> list[str]:
    return _validate_email_sequence(raw_attendees, "attendees", allow_single_str=False, required=False)


def _validate_header_text(raw_value: Any, field_name: str, *, max_len: int) -> str:
    if not isinstance(raw_value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    _reject_crlf(raw_value, field_name)
    cleaned = raw_value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} must be at most {max_len} characters")
    return cleaned


def validate_action_args(
    action: str,
    args: Mapping[str, Any],
    tz_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Validate and canonicalize write action arguments against the strict gate allowlist."""
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

    if action == "gmail.create_draft":
        extra = raw_keys - _DRAFT_ALLOWED_KEYS
        if extra:
            raise ValueError(f"Unsupported argument(s) for gmail.create_draft: {sorted(extra)}")
        missing = [k for k in ("to", "subject", "body") if k not in args]
        if missing:
            raise ValueError(f"Missing required argument(s) for gmail.create_draft: {missing}")

        to_list = _validate_email_sequence(args.get("to"), "to", allow_single_str=True, required=True)
        subject = _validate_header_text(args.get("subject"), "subject", max_len=_MAX_SUBJECT_LEN)

        raw_body = args.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            raise ValueError("body must be a non-empty string")
        if "\x00" in raw_body:
            raise ValueError("body must not contain NUL characters")
        body = raw_body.strip()
        if len(body) > _MAX_BODY_LEN:
            raise ValueError(f"body must be at most {_MAX_BODY_LEN} characters")

        normalized = {
            "to": to_list,
            "subject": subject,
            "body": body,
        }
        if "cc" in args and args["cc"] is not None:
            normalized["cc"] = _validate_email_sequence(
                args["cc"], "cc", allow_single_str=True, required=False
            )
        if "bcc" in args and args["bcc"] is not None:
            normalized["bcc"] = _validate_email_sequence(
                args["bcc"], "bcc", allow_single_str=True, required=False
            )
        if "thread_id" in args and args["thread_id"] is not None:
            raw_tid = args["thread_id"]
            if not isinstance(raw_tid, str):
                raise ValueError("thread_id must match ^[A-Za-z0-9_-]{1,128}$")
            _reject_crlf(raw_tid, "thread_id")
            tid_clean = raw_tid.strip()
            if not _THREAD_ID_RE.fullmatch(tid_clean):
                raise ValueError("thread_id must match ^[A-Za-z0-9_-]{1,128}$")
            normalized["thread_id"] = tid_clean
        if "in_reply_to" in args and args["in_reply_to"] is not None:
            normalized["in_reply_to"] = _validate_header_text(
                args["in_reply_to"], "in_reply_to", max_len=_MAX_IN_REPLY_TO_LEN
            )
        if "references" in args and args["references"] is not None:
            normalized["references"] = _validate_header_text(
                args["references"], "references", max_len=_MAX_REFERENCES_LEN
            )
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


def _post_oauth_token_form(
    token_uri: str,
    form_fields: Mapping[str, str],
    *,
    timeout: float,
    context_label: str,
) -> dict[str, Any]:
    form_body = urllib.parse.urlencode(dict(form_fields)).encode("utf-8")
    req = urllib.request.Request(
        token_uri,
        data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token_payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{context_label} failed with HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{context_label} network error: {exc.reason}.") from None
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{context_label} returned invalid JSON.") from exc
    if isinstance(token_payload, Mapping):
        return dict(token_payload)
    raise RuntimeError(f"{context_label} returned unexpected JSON payload.")


def resolve_gate_write_credentials(
    config: BriefConfig,
    *,
    action: str | None = None,
    timeout: float = 15.0,
    now_fn: Callable[[], float] | None = None,
    fallback_oauth_path: Path | str = DEFAULT_GOOGLE_OAUTH_PATH,
) -> dict[str, Any]:
    """Resolve write-scoped Google OAuth credentials and exchange refresh_token.

    Strictly checks `config.gate_write_token_path` first. For any `gmail.*` action,
    shared `/secrets/google_oauth.json` fallback is strictly prohibited even when
    `config.gate_allow_shared_oauth_fallback` is True. `gmail.send` is always banned.
    """
    action_str: str | None = None
    if action is not None:
        if not isinstance(action, str) or not action.strip():
            raise ValueError(f"Disallowed gate action: {action!r}")
        action_str = action.strip()
        if action_str == "gmail.send" or (
            action_str.startswith("gmail.") and action_str not in ALLOWED_GATE_ACTIONS
        ):
            raise RuntimeError(f"Disallowed gate action {action_str!r}; strictly prohibited.")

    write_path = Path(config.gate_write_token_path)
    fallback_path = Path(fallback_oauth_path)
    used_fallback = False
    is_gmail_action = bool(action_str and action_str.startswith("gmail."))
    allow_shared_fallback = bool(config.gate_allow_shared_oauth_fallback) and not is_gmail_action

    if write_path.is_file():
        chosen_path = write_path
    elif allow_shared_fallback:
        if not fallback_path.is_file():
            raise RuntimeError(
                f"BRIEF write token missing at {write_path} and shared fallback missing at {fallback_path}."
            )
        chosen_path = fallback_path
        used_fallback = True
    elif is_gmail_action:
        raise RuntimeError(
            f"BRIEF write token missing at {write_path}; shared fallback is strictly prohibited for {action_str}."
        )
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

    token_payload = _post_oauth_token_form(
        validated["token_uri"],
        {
            "client_id": validated["client_id"],
            "client_secret": validated["client_secret"],
            "refresh_token": validated["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=timeout,
        context_label="Google OAuth token exchange",
    )

    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Google OAuth token exchange response missing access_token.")

    scope_str = str(token_payload.get("scope") or "").strip()
    if scope_str and action_str:
        granted_scopes = set(scope_str.split())
        if action_str == "gmail.create_draft" and GMAIL_COMPOSE_SCOPE not in granted_scopes:
            raise RuntimeError(
                f"Google OAuth token is missing required scope {GMAIL_COMPOSE_SCOPE} for {action_str}."
            )
        if action_str.startswith("calendar.") and CALENDAR_WRITE_SCOPE not in granted_scopes:
            raise RuntimeError(
                f"Google OAuth token is missing required scope {CALENDAR_WRITE_SCOPE} for {action_str}."
            )

    now_ts = (now_fn or time.time)()
    expires_in = int(token_payload.get("expires_in", 3600))
    return {
        "access_token": access_token,
        "expires_in": expires_in,
        "expires_at": float(now_ts) + float(expires_in),
        "token_type": str(token_payload.get("token_type") or "Bearer"),
        "scope": scope_str,
        "credentials_path": str(chosen_path),
        "used_shared_fallback": used_fallback,
    }


def _extract_auth_code(code_or_url: str) -> str:
    if not isinstance(code_or_url, str) or not code_or_url.strip():
        raise ValueError("code_or_url must be a non-empty string")
    _reject_crlf(code_or_url, "code_or_url")
    raw = code_or_url.strip()

    if "://" in raw or raw.startswith(("?", "code=")):
        if "://" in raw:
            query_str = urllib.parse.urlparse(raw).query
        else:
            query_str = raw.removeprefix("?")
        qs = urllib.parse.parse_qs(query_str)
        if qs.get("error"):
            raise RuntimeError(f"Google OAuth authorization error: {qs['error'][0]}")
        codes = qs.get("code")
        if not codes or not codes[0].strip():
            raise ValueError("Redirect URL does not contain a non-empty 'code' query parameter.")
        return codes[0].strip()

    return raw


def _write_json_atomic_0600(target_path: Path, payload: Mapping[str, Any]) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(
        f".{target_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    serialized = json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def exchange_oauth_authorization_code(
    code_or_url: str,
    *,
    role: str,
    oauth_client_path: Path | str = DEFAULT_GOOGLE_OAUTH_PATH,
    output_path: Path | str | None = None,
    redirect_uri: str = "http://localhost",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Exchange a Google OAuth authorization code or redirect URL for a split token file.

    Validates granted scopes against `role` ('readonly' vs 'write'), atomically writes
    mode-0600 JSON when `output_path` is provided, and never returns `client_secret`
    or `refresh_token`.
    """
    normalized_role = role.strip().lower() if isinstance(role, str) else ""
    if normalized_role not in {"readonly", "write"}:
        raise ValueError(f"Unsupported OAuth role {role!r}; expected 'readonly' or 'write'.")

    auth_code = _extract_auth_code(code_or_url)

    client_path = Path(oauth_client_path)
    if not client_path.is_file():
        raise FileNotFoundError(f"Google OAuth client file not found: {client_path}")

    try:
        raw = json.loads(client_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Failed to parse Google OAuth client JSON at {client_path}.") from exc

    block = _extract_oauth_client_block(raw)
    client_id = str((block or {}).get("client_id") or "").strip()
    client_secret = str((block or {}).get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            f"Google OAuth client JSON at {client_path} is missing client_id or client_secret."
        )

    raw_token_uri = (block or {}).get("token_uri")
    token_uri = (
        raw_token_uri.strip()
        if isinstance(raw_token_uri, str) and raw_token_uri.strip()
        else GOOGLE_TOKEN_URI
    )

    token_payload = _post_oauth_token_form(
        token_uri,
        {
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=timeout,
        context_label="Google OAuth authorization code exchange",
    )

    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError(
            "Google OAuth authorization code exchange response missing refresh_token."
        )

    scope_str = str(token_payload.get("scope") or "").strip()
    granted_scopes = set(scope_str.split())
    if not granted_scopes:
        raise RuntimeError("Google OAuth authorization code exchange response missing scope.")

    required_scopes = (
        set(READONLY_OAUTH_SCOPES.split())
        if normalized_role == "readonly"
        else set(WRITE_OAUTH_SCOPES.split())
    )
    missing_scopes = sorted(required_scopes - granted_scopes)
    if missing_scopes:
        raise RuntimeError(
            f"Google OAuth token for role {normalized_role!r} is missing required scope(s): {missing_scopes}"
        )

    forbidden = granted_scopes & _FORBIDDEN_OAUTH_SCOPES
    if normalized_role == "readonly":
        forbidden = forbidden | (
            granted_scopes
            & {
                CALENDAR_WRITE_SCOPE,
                GMAIL_COMPOSE_SCOPE,
                "https://www.googleapis.com/auth/calendar",
            }
        )
    if forbidden:
        raise RuntimeError(
            f"Google OAuth token for role {normalized_role!r} includes prohibited scope(s): {sorted(forbidden)}"
        )

    out_path = Path(output_path) if output_path is not None else None
    if out_path is not None:
        _write_json_atomic_0600(
            out_path,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "token_uri": token_uri,
                "scope": scope_str,
                "role": normalized_role,
            },
        )

    return {
        "role": normalized_role,
        "scope": scope_str,
        "scopes": sorted(granted_scopes),
        "token_type": str(token_payload.get("token_type") or "Bearer"),
        "expires_in": int(token_payload.get("expires_in", 3600)),
        "output_path": str(out_path) if out_path is not None else None,
        "written": out_path is not None,
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


class _BaseGoogleWriteClient:
    """Shared thread-safe OAuth token cache and single-401-retry JSON client."""

    _SERVICE_NAME = "Google API"
    _DEFAULT_ACTION: str | None = None

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

    def _get_token(
        self,
        *,
        force_refresh: bool = False,
        action: str | None = None,
    ) -> str:
        target_action = action if action is not None else self._DEFAULT_ACTION
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
                action=target_action,
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
        *,
        action: str | None = None,
    ) -> dict[str, Any]:
        encoded_body = json.dumps(_to_jsonable(body), ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            token = self._get_token(force_refresh=(attempt > 0), action=action)
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
                    f"{self._SERVICE_NAME} {method} request failed with HTTP {exc.code}."
                ) from None
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"{self._SERVICE_NAME} {method} network error: {exc.reason}."
                ) from None
        raise RuntimeError(
            f"Failed to execute {self._SERVICE_NAME} {method} request after token refresh."
        )


class GoogleCalendarWriteClient(_BaseGoogleWriteClient):
    """Strictly calendar-write Google Calendar v3 API client with isolated credentials."""

    _SERVICE_NAME = "Google Calendar"
    _DEFAULT_ACTION = "calendar.create"

    def create_event(self, calendar_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST a new event to Google Calendar v3."""
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        url = f"{CALENDAR_API_BASE}/calendars/{encoded_cal}/events"
        return self._request_json("POST", url, payload, action="calendar.create")

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
        return self._request_json("PATCH", url, payload, action="calendar.update")


class GoogleGmailDraftWriteClient(_BaseGoogleWriteClient):
    """Strictly draft-create Gmail v1 API client using isolated split write credentials."""

    _SERVICE_NAME = "Google Gmail"
    _DEFAULT_ACTION = "gmail.create_draft"

    @staticmethod
    def build_raw_mime_message(
        to: Sequence[str] | str,
        subject: str,
        body: str,
        *,
        cc: Sequence[str] | str | None = None,
        bcc: Sequence[str] | str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str:
        """Build an RFC 2822 UTF-8 plain-text EmailMessage encoded as base64url."""
        to_list = _validate_email_sequence(to, "to", allow_single_str=True, required=True)
        clean_subject = _validate_header_text(subject, "subject", max_len=_MAX_SUBJECT_LEN)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body must be a non-empty string")
        if "\x00" in body:
            raise ValueError("body must not contain NUL characters")

        cc_list = (
            _validate_email_sequence(cc, "cc", allow_single_str=True, required=False)
            if cc is not None
            else []
        )
        bcc_list = (
            _validate_email_sequence(bcc, "bcc", allow_single_str=True, required=False)
            if bcc is not None
            else []
        )

        msg = EmailMessage()
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        if bcc_list:
            msg["Bcc"] = ", ".join(bcc_list)
        msg["Subject"] = clean_subject
        if in_reply_to is not None:
            msg["In-Reply-To"] = _validate_header_text(
                in_reply_to, "in_reply_to", max_len=_MAX_IN_REPLY_TO_LEN
            )
        if references is not None:
            msg["References"] = _validate_header_text(
                references, "references", max_len=_MAX_REFERENCES_LEN
            )
        msg.set_content(body.strip(), subtype="plain", charset="utf-8")
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    def create_draft(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST a new draft to `{GMAIL_API_BASE}/users/me/drafts` (never sends email)."""
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if "message" in payload and isinstance(payload["message"], Mapping):
            body = dict(payload)
        else:
            normalized = validate_action_args("gmail.create_draft", payload)
            body = _build_gmail_draft_api_payload(normalized)
        url = f"{GMAIL_API_BASE}/users/me/drafts"
        return self._request_json("POST", url, body, action="gmail.create_draft")


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


def _build_gmail_draft_api_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    raw_b64 = GoogleGmailDraftWriteClient.build_raw_mime_message(
        to=normalized["to"],
        subject=normalized["subject"],
        body=normalized["body"],
        cc=normalized.get("cc"),
        bcc=normalized.get("bcc"),
        in_reply_to=normalized.get("in_reply_to"),
        references=normalized.get("references"),
    )
    message_obj: dict[str, Any] = {"raw": raw_b64}
    thread_id = normalized.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        message_obj["threadId"] = thread_id
    return {"message": message_obj}


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
    effective_dry_run = config.dry_run if dry_run is None else bool(dry_run)

    if action == "gmail.create_draft":
        to_list = list(normalized["to"])
        cc_list = list(normalized.get("cc") or [])
        subject_str = str(normalized["subject"])
        req_thread_id = str(normalized.get("thread_id") or "")

        if effective_dry_run:
            synthetic_draft_id = f"dryrun-{proposal_hash[:12]}"
            synthetic_msg_id = f"dryrun-msg-{proposal_hash[:12]}"
            return {
                "dry_run": True,
                "action": action,
                "draft_id": synthetic_draft_id,
                "message_id": synthetic_msg_id,
                "thread_id": req_thread_id,
                "to": to_list,
                "cc": cc_list,
                "subject": subject_str,
                "gmail_url": f"https://mail.google.com/mail/u/0/#drafts?compose={synthetic_msg_id}",
                "proposal_hash": proposal_hash,
            }

        client = api_client or GoogleGmailDraftWriteClient(config)
        api_payload = _build_gmail_draft_api_payload(normalized)
        resp = client.create_draft(api_payload)
        if not isinstance(resp, Mapping):
            resp = {}
        msg_obj = resp.get("message") if isinstance(resp.get("message"), Mapping) else {}
        draft_id = str(resp.get("id") or "")
        message_id = str(msg_obj.get("id") or resp.get("messageId") or "")
        thread_id = str(msg_obj.get("threadId") or resp.get("threadId") or req_thread_id)
        compose_ref = message_id or draft_id
        gmail_url = (
            f"https://mail.google.com/mail/u/0/#drafts?compose={compose_ref}"
            if compose_ref
            else "https://mail.google.com/mail/u/0/#drafts"
        )
        return {
            "dry_run": False,
            "action": action,
            "draft_id": draft_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "to": to_list,
            "cc": cc_list,
            "subject": subject_str,
            "gmail_url": gmail_url,
            "proposal_hash": proposal_hash,
        }

    calendar_id = str(normalized["calendar_id"])
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
