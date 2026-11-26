"""Unit tests for BRIEF gate config, write action validation, split OAuth, and Calendar/Gmail draft execution."""

from __future__ import annotations

import base64
import email
import email.policy
import io
import json
import logging
import sys
import tomllib
import urllib.error
import urllib.parse
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief.actions import (
    ALLOWED_GATE_ACTIONS,
    CALENDAR_WRITE_SCOPE,
    GMAIL_API_BASE,
    GMAIL_COMPOSE_SCOPE,
    READONLY_OAUTH_SCOPES,
    WRITE_OAUTH_SCOPES,
    GoogleCalendarWriteClient,
    GoogleGmailDraftWriteClient,
    build_oauth_consent_urls,
    canonical_proposal_hash,
    exchange_oauth_authorization_code,
    execute_action,
    resolve_gate_write_credentials,
    validate_action_args,
)
from tools.brief.config import DEFAULT_GATE_WRITE_TOKEN_PATH, BriefConfig


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _write_oauth_file(path: Path, *, mode: int = 0o600, client_id: str = "cid-123") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "client_secret": "super-secret-value-do-not-leak",
                "refresh_token": "refresh-secret-do-not-leak",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def test_gate_config_defaults_toml_example_and_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIEF_OWNER_EMAILS", raising=False)
    example_parsed = tomllib.loads((REPO_ROOT / "config" / "brief.toml.example").read_text(encoding="utf-8"))
    gate_sec = example_parsed["gate"]
    assert gate_sec["write_token_path"] == "/secrets/brief_write_token.json"
    assert gate_sec["allow_shared_oauth_fallback"] is False
    assert gate_sec["max_proposal_ttl_seconds"] == 604800
    assert gate_sec["default_calendar_id"] == "primary"

    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    cfg_default = BriefConfig(state_dir=state_dir, memory_dir=memory_dir)
    assert cfg_default.gate_write_token_path == DEFAULT_GATE_WRITE_TOKEN_PATH
    assert cfg_default.gate_allow_shared_oauth_fallback is False
    assert cfg_default.gate_max_proposal_ttl_seconds == 604800
    assert cfg_default.gate_default_calendar_id == "primary"
    assert cfg_default.gate_db_path == state_dir / "gate.db"

    # Unprefixed [gate] keys
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        '[gate]\n'
        'write_token_path = "/tmp/custom_write.json"\n'
        'allow_shared_oauth_fallback = true\n'
        'max_proposal_ttl_seconds = 3600\n'
        'default_calendar_id = "team@example.com"\n',
        encoding="utf-8",
    )
    cfg_loaded = BriefConfig.load(
        config_path=toml_path,
        state_dir=state_dir,
        memory_dir=memory_dir,
        secrets_path=tmp_path / "none.json",
    )
    assert cfg_loaded.gate_write_token_path == Path("/tmp/custom_write.json")
    assert cfg_loaded.gate_allow_shared_oauth_fallback is True
    assert cfg_loaded.gate_max_proposal_ttl_seconds == 3600
    assert cfg_loaded.gate_default_calendar_id == "team@example.com"
    assert cfg_loaded.gate_db_path == state_dir / "gate.db"

    # Prefixed gate_* alias fallback
    alias_toml = tmp_path / "alias.toml"
    alias_toml.write_text(
        '[gate]\n'
        'gate_write_token_path = "/tmp/alias_write.json"\n'
        'gate_allow_shared_oauth_fallback = true\n'
        'gate_max_proposal_ttl_seconds = 7200\n'
        'gate_default_calendar_id = "family@example.org"\n',
        encoding="utf-8",
    )
    cfg_alias = BriefConfig.load(
        config_path=alias_toml,
        state_dir=state_dir,
        memory_dir=memory_dir,
        secrets_path=tmp_path / "none.json",
    )
    assert cfg_alias.gate_write_token_path == Path("/tmp/alias_write.json")
    assert cfg_alias.gate_allow_shared_oauth_fallback is True
    assert cfg_alias.gate_max_proposal_ttl_seconds == 7200
    assert cfg_alias.gate_default_calendar_id == "family@example.org"


def test_allowlist_and_canonical_proposal_hash_stability() -> None:
    assert ALLOWED_GATE_ACTIONS == frozenset({"calendar.create", "calendar.update", "gmail.create_draft"})

    for blocked in ("gmail.send", "gmail.delete", "calendar.delete", "shell.exec", ""):
        with pytest.raises(ValueError):
            validate_action_args(blocked, {"summary": "Nope"})
        with pytest.raises(ValueError):
            execute_action(blocked, {"summary": "Nope"}, BriefConfig(dry_run=True))

    args_a = {
        "summary": "Soccer Practice",
        "start": "2026-11-24T16:00:00-08:00",
        "end": "2026-11-24T17:30:00-08:00",
        "attendees": ["coach@example.org", "owner@example.com"],
        "location": "North Field",
    }
    args_b = MappingProxyType(
        {
            "location": "North Field",
            "attendees": ["coach@example.org", "owner@example.com"],
            "end": "2026-11-24T17:30:00-08:00",
            "start": "2026-11-24T16:00:00-08:00",
            "summary": "Soccer Practice",
        }
    )
    hash_a = canonical_proposal_hash("calendar.create", args_a)
    hash_b = canonical_proposal_hash("calendar.create", args_b)
    assert len(hash_a) == 64
    assert hash_a == hash_b
    assert hash_a != canonical_proposal_hash(
        "calendar.create",
        {**args_a, "summary": "Tampered Practice"},
    )


def test_validate_action_args_create_and_update_schemas() -> None:
    # Naive timestamps localized to America/Los_Angeles (-08:00 in Nov)
    created = validate_action_args(
        "calendar.create",
        {
            "summary": "  Dentist Visit  ",
            "start": "2026-11-24T09:00:00",
            "end": "2026-11-24T10:00:00",
            "attendees": ["Owner <owner@example.com>", "coach@example.org", "OWNER@example.com"],
            "location": "  Main St Clinic ",
            "description": "Routine checkup",
        },
    )
    assert created == {
        "calendar_id": "primary",
        "summary": "Dentist Visit",
        "start": "2026-11-24T09:00:00-08:00",
        "end": "2026-11-24T10:00:00-08:00",
        "location": "Main St Clinic",
        "description": "Routine checkup",
        "attendees": ["owner@example.com", "coach@example.org"],
    }

    # Unknown/extra fields strictly rejected
    with pytest.raises(ValueError, match="Unsupported argument"):
        validate_action_args(
            "calendar.create",
            {
                "summary": "Meet",
                "start": "2026-11-24T09:00:00",
                "end": "2026-11-24T10:00:00",
                "sendUpdates": "all",
            },
        )

    # end <= start rejected
    with pytest.raises(ValueError, match="strictly after"):
        validate_action_args(
            "calendar.create",
            {
                "summary": "Meet",
                "start": "2026-11-24T10:00:00",
                "end": "2026-11-24T10:00:00",
            },
        )

    # Invalid attendees rejected (string instead of list, invalid email)
    with pytest.raises(TypeError, match="attendees"):
        validate_action_args(
            "calendar.create",
            {
                "summary": "Meet",
                "start": "2026-11-24T09:00:00",
                "end": "2026-11-24T10:00:00",
                "attendees": "owner@example.com",
            },
        )
    with pytest.raises(ValueError, match="not a valid email"):
        validate_action_args(
            "calendar.create",
            {
                "summary": "Meet",
                "start": "2026-11-24T09:00:00",
                "end": "2026-11-24T10:00:00",
                "attendees": ["not-an-email"],
            },
        )

    # Update requires event_id + at least one mutable field
    with pytest.raises(ValueError, match="event_id"):
        validate_action_args("calendar.update", {"summary": "New"})
    with pytest.raises(ValueError, match="at least one mutable field"):
        validate_action_args("calendar.update", {"event_id": "evt-1"})
    with pytest.raises(ValueError, match="Unsupported argument"):
        validate_action_args("calendar.update", {"event_id": "evt-1", "summary": "New", "extra": 1})
    with pytest.raises(ValueError, match="strictly after"):
        validate_action_args(
            "calendar.update",
            {
                "event_id": "evt-1",
                "start": "2026-11-24T12:00:00-08:00",
                "end": "2026-11-24T11:00:00-08:00",
            },
        )

    updated = validate_action_args(
        "calendar.update",
        {"event_id": " evt-99 ", "location": "Field B", "calendar_id": "work@example.com"},
    )
    assert updated == {
        "calendar_id": "work@example.com",
        "event_id": "evt-99",
        "location": "Field B",
    }


def test_validate_action_args_gmail_create_draft_and_crlf_injection() -> None:
    # Single recipient string normalized to lowercase list
    draft_single = validate_action_args(
        "gmail.create_draft",
        {
            "to": "Coach Bob <Coach@Example.org>",
            "subject": "  Saturday Practice Update  ",
            "body": "Hi Coach,\n\nWe will arrive 15 minutes early.\n",
        },
    )
    assert draft_single == {
        "to": ["coach@example.org"],
        "subject": "Saturday Practice Update",
        "body": "Hi Coach,\n\nWe will arrive 15 minutes early.",
    }

    # Multi-recipient deduplication + threading metadata
    draft_full = validate_action_args(
        "gmail.create_draft",
        {
            "to": ["Owner <owner@example.com>", "coach@example.org", "OWNER@example.com"],
            "cc": ["Assistant <asst@example.org>", "ASST@example.org"],
            "bcc": "archive@example.com",
            "subject": "Re: Game Schedule",
            "body": "Confirmed for 10:00 AM PT.",
            "thread_id": "18f123abc_XYZ-9",
            "in_reply_to": " <msg-123@mail.example.com> ",
            "references": " <msg-100@mail.example.com> <msg-123@mail.example.com> ",
        },
    )
    assert draft_full == {
        "to": ["owner@example.com", "coach@example.org"],
        "cc": ["asst@example.org"],
        "bcc": ["archive@example.com"],
        "subject": "Re: Game Schedule",
        "body": "Confirmed for 10:00 AM PT.",
        "thread_id": "18f123abc_XYZ-9",
        "in_reply_to": "<msg-123@mail.example.com>",
        "references": "<msg-100@mail.example.com> <msg-123@mail.example.com>",
    }

    # Missing required arguments
    for missing_field in ("to", "subject", "body"):
        base = {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}
        base.pop(missing_field)
        with pytest.raises(ValueError, match="Missing required argument"):
            validate_action_args("gmail.create_draft", base)

    # Unsupported extra arguments rejected
    with pytest.raises(ValueError, match="Unsupported argument"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello", "send_now": True},
        )

    # CRLF header injection strictly rejected across all header fields
    crlf_cases = [
        {"to": "a@example.com\r\nBcc: evil@example.org", "subject": "Hi", "body": "Hello"},
        {"to": ["a@example.com\nX-Evil: 1"], "subject": "Hi", "body": "Hello"},
        {"to": "a@example.com", "cc": "b@example.com\r\nBcc: evil@example.org", "subject": "Hi", "body": "Hello"},
        {"to": "a@example.com", "bcc": "b@example.com\nX-Injected: 1", "subject": "Hi", "body": "Hello"},
        {"to": "a@example.com", "subject": "Hello\r\nBcc: evil@example.org", "body": "Hello"},
        {"to": "a@example.com", "subject": "Hello\n", "body": "Hello"},
        {"to": "a@example.com", "subject": "Hi", "body": "Hello", "in_reply_to": "<id@example.com>\r\nBcc: x@y.com"},
        {"to": "a@example.com", "subject": "Hi", "body": "Hello", "references": "<id@example.com>\nX: y"},
        {"to": "a@example.com", "subject": "Hi", "body": "Hello", "thread_id": "abc\n"},
    ]
    for bad_payload in crlf_cases:
        with pytest.raises(ValueError, match="CR, LF, or NUL"):
            validate_action_args("gmail.create_draft", bad_payload)

    # Invalid emails, empty to, bad thread_id, oversized fields
    with pytest.raises(ValueError, match="at least one valid email"):
        validate_action_args("gmail.create_draft", {"to": [], "subject": "Hi", "body": "Hello"})
    with pytest.raises(ValueError, match="not a valid email"):
        validate_action_args("gmail.create_draft", {"to": "not-an-email", "subject": "Hi", "body": "Hello"})
    with pytest.raises(ValueError, match="thread_id"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello", "thread_id": "bad/thread id"},
        )
    with pytest.raises(ValueError, match="thread_id"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello", "thread_id": "a" * 129},
        )
    with pytest.raises(ValueError, match="subject must be at most 500"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "s" * 501, "body": "Hello"},
        )
    with pytest.raises(ValueError, match="body must be at most 50000"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "b" * 50001},
        )
    with pytest.raises(ValueError, match="in_reply_to must be at most 500"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello", "in_reply_to": "r" * 501},
        )
    with pytest.raises(ValueError, match="references must be at most 2000"):
        validate_action_args(
            "gmail.create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello", "references": "r" * 2001},
        )


def test_resolve_gate_write_credentials_strict_separation_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_token = tmp_path / "brief_write_token.json"
    shared_oauth = tmp_path / "google_oauth.json"
    _write_oauth_file(shared_oauth, mode=0o600, client_id="shared-cid")

    # 1. Missing write token + fallback disabled -> immediate refusal
    cfg_strict = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        gate_allow_shared_oauth_fallback=False,
    )
    with pytest.raises(RuntimeError, match="gate_allow_shared_oauth_fallback is disabled"):
        resolve_gate_write_credentials(cfg_strict, fallback_oauth_path=shared_oauth)

    # 2. Missing write token + fallback enabled -> uses shared oauth for calendar,
    #    but STRICTLY REFUSES fallback for gmail.* actions
    captured_requests: list[dict[str, Any]] = []
    mock_scope = f"{CALENDAR_WRITE_SCOPE} {GMAIL_COMPOSE_SCOPE}"

    def _fake_urlopen(req: Any, timeout: float = 15.0) -> _FakeHttpResponse:
        captured_requests.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "body": req.data.decode("utf-8") if req.data else "",
                "timeout": timeout,
            }
        )
        return _FakeHttpResponse(
            {
                "access_token": "ya29.write-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": mock_scope,
            }
        )

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _fake_urlopen)

    cfg_fallback = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        gate_allow_shared_oauth_fallback=True,
    )
    with pytest.raises(RuntimeError, match="shared fallback is strictly prohibited"):
        resolve_gate_write_credentials(
            cfg_fallback,
            action="gmail.create_draft",
            fallback_oauth_path=shared_oauth,
        )
    with pytest.raises(RuntimeError, match="strictly prohibited"):
        resolve_gate_write_credentials(
            cfg_fallback,
            action="gmail.send",
            fallback_oauth_path=shared_oauth,
        )

    res_fb = resolve_gate_write_credentials(
        cfg_fallback,
        action="calendar.create",
        now_fn=lambda: 1000.0,
        fallback_oauth_path=shared_oauth,
    )
    assert res_fb["used_shared_fallback"] is True
    assert res_fb["credentials_path"] == str(shared_oauth)
    assert res_fb["access_token"] == "ya29.write-access-token"
    assert res_fb["expires_at"] == 4600.0
    assert "client_id=shared-cid" in captured_requests[-1]["body"]

    # 3. Dedicated write token present with permissive mode logs warning and takes precedence
    _write_oauth_file(write_token, mode=0o644, client_id="dedicated-write-cid")
    with caplog.at_level(logging.WARNING):
        res_dedicated = resolve_gate_write_credentials(
            cfg_strict,
            action="gmail.create_draft",
            now_fn=lambda: 2000.0,
            fallback_oauth_path=shared_oauth,
        )
    assert res_dedicated["used_shared_fallback"] is False
    assert res_dedicated["credentials_path"] == str(write_token)
    assert "client_id=dedicated-write-cid" in captured_requests[-1]["body"]
    assert "overly permissive mode" in caplog.text

    # gmail.send remains strictly refused even when dedicated write token exists
    with pytest.raises(RuntimeError, match="strictly prohibited"):
        resolve_gate_write_credentials(
            cfg_strict,
            action="gmail.send",
            fallback_oauth_path=shared_oauth,
        )

    # 4. Scope verification: fail closed if token exchange returns non-empty scope missing action scope
    mock_scope = CALENDAR_WRITE_SCOPE  # missing gmail.compose
    with pytest.raises(RuntimeError, match=GMAIL_COMPOSE_SCOPE):
        resolve_gate_write_credentials(
            cfg_strict,
            action="gmail.create_draft",
            fallback_oauth_path=shared_oauth,
        )

    mock_scope = GMAIL_COMPOSE_SCOPE  # missing calendar.events
    with pytest.raises(RuntimeError, match=CALENDAR_WRITE_SCOPE):
        resolve_gate_write_credentials(
            cfg_strict,
            action="calendar.update",
            fallback_oauth_path=shared_oauth,
        )

    # 5. Token HTTP error never leaks client_secret or refresh_token
    def _failing_urlopen(req: Any, timeout: float = 15.0) -> Any:
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(b""))

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _failing_urlopen)
    with pytest.raises(RuntimeError) as exc_info:
        resolve_gate_write_credentials(cfg_strict, fallback_oauth_path=shared_oauth)
    msg = str(exc_info.value)
    assert "super-secret-value-do-not-leak" not in msg
    assert "refresh-secret-do-not-leak" not in msg


def test_build_oauth_consent_urls_never_leaks_client_secret(tmp_path: Path) -> None:
    # Nested Google Cloud "installed" structure
    oauth_path = tmp_path / "google_oauth.json"
    secret_val = "GOCSPX-TOP-SECRET-VALUE-999"
    oauth_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "12345.apps.googleusercontent.com",
                    "client_secret": secret_val,
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )

    urls = build_oauth_consent_urls(oauth_path)
    assert set(urls.keys()) == {"readonly_url", "write_url"}

    ro_parsed = urllib.parse.urlparse(urls["readonly_url"])
    ro_qs = urllib.parse.parse_qs(ro_parsed.query)
    assert ro_qs["client_id"] == ["12345.apps.googleusercontent.com"]
    assert ro_qs["access_type"] == ["offline"]
    assert ro_qs["prompt"] == ["consent"]
    assert "gmail.readonly" in ro_qs["scope"][0]
    assert "calendar.readonly" in ro_qs["scope"][0]
    assert "calendar.events" not in ro_qs["scope"][0]
    assert "gmail.compose" not in ro_qs["scope"][0]
    assert "gmail.send" not in ro_qs["scope"][0]

    wr_parsed = urllib.parse.urlparse(urls["write_url"])
    wr_qs = urllib.parse.parse_qs(wr_parsed.query)
    assert wr_qs["client_id"] == ["12345.apps.googleusercontent.com"]
    assert wr_qs["access_type"] == ["offline"]
    assert wr_qs["prompt"] == ["consent"]
    assert wr_qs["scope"] == [WRITE_OAUTH_SCOPES]
    assert CALENDAR_WRITE_SCOPE in wr_qs["scope"][0]
    assert GMAIL_COMPOSE_SCOPE in wr_qs["scope"][0]
    assert "gmail.send" not in wr_qs["scope"][0]

    serialized = json.dumps(urls)
    assert secret_val not in serialized
    assert "client_secret" not in serialized


def test_exchange_oauth_authorization_code_roles_scopes_and_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_path = tmp_path / "google_oauth.json"
    secret_val = "GOCSPX-EXCHANGE-SECRET-999"
    client_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid-split-1",
                    "client_secret": secret_val,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )

    token_requests: list[dict[str, Any]] = []
    mock_response_payload: dict[str, Any] = {}

    def _mock_urlopen(req: Any, timeout: float = 15.0) -> _FakeHttpResponse:
        body_qs = urllib.parse.parse_qs(req.data.decode("utf-8"))
        token_requests.append({"url": req.full_url, "form": body_qs, "timeout": timeout})
        return _FakeHttpResponse(mock_response_payload)

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _mock_urlopen)

    # 1. Read-only role extracts code from redirect URL and writes 0600 JSON
    ro_out = tmp_path / "secrets" / "brief_readonly_token.json"
    mock_response_payload = {
        "access_token": "ya29.ro-access",
        "refresh_token": "1//ro-refresh-secret",
        "expires_in": 3599,
        "token_type": "Bearer",
        "scope": READONLY_OAUTH_SCOPES,
    }
    ro_meta = exchange_oauth_authorization_code(
        "http://localhost/?code=4%2F0A-ro-code-123&scope=ignored",
        role="readonly",
        oauth_client_path=client_path,
        output_path=ro_out,
    )
    assert token_requests[-1]["form"]["code"] == ["4/0A-ro-code-123"]
    assert token_requests[-1]["form"]["grant_type"] == ["authorization_code"]
    assert ro_meta["role"] == "readonly"
    assert ro_meta["written"] is True
    assert ro_meta["output_path"] == str(ro_out)
    assert (ro_out.stat().st_mode & 0o777) == 0o600
    ro_dumped = json.dumps(ro_meta)
    assert secret_val not in ro_dumped
    assert "1//ro-refresh-secret" not in ro_dumped
    assert "ya29.ro-access" not in ro_dumped

    # 2. Write role with raw code writes flat 0600 JSON usable by resolve_gate_write_credentials
    wr_out = tmp_path / "secrets" / "brief_write_token.json"
    mock_response_payload = {
        "access_token": "ya29.wr-access",
        "refresh_token": "1//wr-refresh-secret",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": WRITE_OAUTH_SCOPES,
    }
    wr_meta = exchange_oauth_authorization_code(
        "4/0A-raw-write-code",
        role="write",
        oauth_client_path=client_path,
        output_path=wr_out,
    )
    assert token_requests[-1]["form"]["code"] == ["4/0A-raw-write-code"]
    assert wr_meta["role"] == "write"
    assert (wr_out.stat().st_mode & 0o777) == 0o600
    wr_written = json.loads(wr_out.read_text(encoding="utf-8"))
    assert wr_written["client_id"] == "cid-split-1"
    assert wr_written["refresh_token"] == "1//wr-refresh-secret"
    assert wr_written["role"] == "write"

    # Verify resolve_gate_write_credentials loads the newly written write token
    cfg_written = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=wr_out,
        gate_allow_shared_oauth_fallback=False,
    )
    resolved = resolve_gate_write_credentials(cfg_written, action="gmail.create_draft")
    assert resolved["access_token"] == "ya29.wr-access"

    # 3. Missing refresh_token rejected
    mock_response_payload = {"access_token": "ya29.no-rt", "scope": WRITE_OAUTH_SCOPES}
    with pytest.raises(RuntimeError, match="missing refresh_token"):
        exchange_oauth_authorization_code("4/code", role="write", oauth_client_path=client_path)

    # 4. Scope mismatches & forbidden scopes rejected
    mock_response_payload = {
        "access_token": "ya29.bad-scope",
        "refresh_token": "1//rt",
        "scope": READONLY_OAUTH_SCOPES,
    }
    with pytest.raises(RuntimeError, match="missing required scope"):
        exchange_oauth_authorization_code("4/code", role="write", oauth_client_path=client_path)

    mock_response_payload = {
        "access_token": "ya29.send-scope",
        "refresh_token": "1//rt",
        "scope": f"{WRITE_OAUTH_SCOPES} https://www.googleapis.com/auth/gmail.send",
    }
    with pytest.raises(RuntimeError, match="prohibited scope"):
        exchange_oauth_authorization_code("4/code", role="write", oauth_client_path=client_path)

    mock_response_payload = {
        "access_token": "ya29.ro-with-write",
        "refresh_token": "1//rt",
        "scope": f"{READONLY_OAUTH_SCOPES} {GMAIL_COMPOSE_SCOPE}",
    }
    with pytest.raises(RuntimeError, match="prohibited scope"):
        exchange_oauth_authorization_code("4/code", role="readonly", oauth_client_path=client_path)

    # 5. OAuth redirect error & bad role rejected
    with pytest.raises(RuntimeError, match="access_denied"):
        exchange_oauth_authorization_code(
            "http://localhost/?error=access_denied",
            role="write",
            oauth_client_path=client_path,
        )
    with pytest.raises(ValueError, match="Unsupported OAuth role"):
        exchange_oauth_authorization_code("4/code", role="admin", oauth_client_path=client_path)


def test_google_gmail_draft_write_client_mime_threading_and_401_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_token = _write_oauth_file(tmp_path / "brief_write_token.json", mode=0o600)
    cfg = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        dry_run=False,
    )

    # MIME & threading header encoding
    raw_b64 = GoogleGmailDraftWriteClient.build_raw_mime_message(
        to=["coach@example.org", "owner@example.com"],
        cc=["team@example.org"],
        bcc="bcc@example.com",
        subject="Practice — Saturday",
        body="See you at 09:00 PT.\nBring water.",
        in_reply_to="<orig-1@mail.example.com>",
        references="<root-0@mail.example.com> <orig-1@mail.example.com>",
    )
    decoded_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    parsed_msg = email.message_from_bytes(decoded_bytes, policy=email.policy.default)
    assert parsed_msg["To"] == "coach@example.org, owner@example.com"
    assert parsed_msg["Cc"] == "team@example.org"
    assert parsed_msg["Bcc"] == "bcc@example.com"
    assert parsed_msg["Subject"] == "Practice — Saturday"
    assert parsed_msg["In-Reply-To"] == "<orig-1@mail.example.com>"
    assert parsed_msg["References"] == "<root-0@mail.example.com> <orig-1@mail.example.com>"
    assert "Bring water." in parsed_msg.get_content()

    # 401 triggers single forced token refresh and retries draft POST
    token_seq = iter(["ya29.expired-token", "ya29.fresh-token"])
    draft_attempts = 0
    seen_calls: list[dict[str, Any]] = []

    def _mock_urlopen(req: Any, timeout: float = 15.0) -> _FakeHttpResponse:
        nonlocal draft_attempts
        url = req.full_url
        method = req.get_method()
        raw_body = req.data.decode("utf-8") if req.data else ""
        headers = {k.lower(): v for k, v in req.header_items()}
        seen_calls.append({"url": url, "method": method, "body": raw_body, "headers": headers})

        if url == "https://oauth2.googleapis.com/token":
            return _FakeHttpResponse(
                {
                    "access_token": next(token_seq),
                    "expires_in": 3600,
                    "scope": WRITE_OAUTH_SCOPES,
                }
            )
        if url == f"{GMAIL_API_BASE}/users/me/drafts" and method == "POST":
            draft_attempts += 1
            if draft_attempts == 1:
                assert headers["authorization"] == "Bearer ya29.expired-token"
                raise urllib.error.HTTPError(url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""))
            assert headers["authorization"] == "Bearer ya29.fresh-token"
            return _FakeHttpResponse(
                {
                    "id": "r-draft-100",
                    "message": {
                        "id": "18f-msg-200",
                        "threadId": "18f-thread-9",
                        "labelIds": ["DRAFT"],
                    },
                }
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _mock_urlopen)

    client = GoogleGmailDraftWriteClient(cfg, now_fn=lambda: 5000.0)
    resp = client.create_draft(
        {
            "message": {
                "raw": raw_b64,
                "threadId": "18f-thread-9",
            }
        }
    )
    assert resp["id"] == "r-draft-100"
    assert draft_attempts == 2
    assert len(seen_calls) == 4  # token1 -> 401 draft -> token2 -> 200 draft


def test_execute_action_dry_run_and_live_http_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_token = _write_oauth_file(tmp_path / "brief_write_token.json", mode=0o600)
    cfg_dry = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        dry_run=True,
    )

    def _forbid_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Dry-run must not make network calls")

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _forbid_network)

    dry_receipt = execute_action(
        "calendar.create",
        {
            "summary": "Coach Call",
            "start": "2026-11-24T14:00:00",
            "end": "2026-11-24T14:30:00",
            "attendees": ["coach@example.org"],
        },
        cfg_dry,
    )
    assert dry_receipt["dry_run"] is True
    assert dry_receipt["calendar_id"] == "primary"
    assert dry_receipt["event_id"].startswith("dryrun-")
    assert dry_receipt["summary"] == "Coach Call"
    assert dry_receipt["start_pt"] == "2026-11-24 14:00 PT"
    assert dry_receipt["end_pt"] == "2026-11-24 14:30 PT"
    assert dry_receipt["html_link"]

    # Dry-run gmail.create_draft returns deterministic receipt without network calls
    dry_draft = execute_action(
        "gmail.create_draft",
        {
            "to": ["Coach <coach@example.org>"],
            "cc": ["owner@example.com"],
            "subject": "Game Snacks",
            "body": "We can bring oranges.",
            "thread_id": "thr-42",
        },
        cfg_dry,
    )
    assert dry_draft["dry_run"] is True
    assert dry_draft["action"] == "gmail.create_draft"
    assert dry_draft["draft_id"].startswith("dryrun-")
    assert dry_draft["message_id"].startswith("dryrun-msg-")
    assert dry_draft["thread_id"] == "thr-42"
    assert dry_draft["to"] == ["coach@example.org"]
    assert dry_draft["cc"] == ["owner@example.com"]
    assert dry_draft["subject"] == "Game Snacks"
    assert dry_draft["gmail_url"].startswith("https://mail.google.com/mail/u/0/#drafts?compose=")
    assert len(dry_draft["proposal_hash"]) == 64

    # Live execution via GoogleCalendarWriteClient + GoogleGmailDraftWriteClient + mocked urllib
    http_calls: list[dict[str, Any]] = []

    def _mock_urlopen(req: Any, timeout: float = 15.0) -> _FakeHttpResponse:
        url = req.full_url
        method = req.get_method()
        raw_body = req.data.decode("utf-8") if req.data else ""
        http_calls.append({"url": url, "method": method, "body": raw_body, "headers": dict(req.headers)})

        if url == "https://oauth2.googleapis.com/token":
            return _FakeHttpResponse({"access_token": "ya29.live-write-token", "expires_in": 3600})
        if method == "POST" and "/calendars/primary%40group.calendar.google.com/events" in url:
            return _FakeHttpResponse(
                {
                    "id": "created-evt-1",
                    "htmlLink": "https://calendar.google.com/event?eid=created-evt-1",
                    "summary": "Team Lunch",
                    "start": {"dateTime": "2026-11-24T12:00:00-08:00"},
                    "end": {"dateTime": "2026-11-24T13:00:00-08:00"},
                }
            )
        if method == "PATCH" and "/calendars/primary/events/evt%2Fspecial%201" in url:
            return _FakeHttpResponse(
                {
                    "id": "evt/special 1",
                    "htmlLink": "https://calendar.google.com/event?eid=patched",
                    "summary": "Updated Title",
                    "start": {"dateTime": "2026-11-24T15:00:00-08:00"},
                    "end": {"dateTime": "2026-11-24T16:00:00-08:00"},
                }
            )
        if method == "POST" and url == f"{GMAIL_API_BASE}/users/me/drafts":
            return _FakeHttpResponse(
                {
                    "id": "r-live-draft-7",
                    "message": {
                        "id": "18f-live-msg-7",
                        "threadId": "thr-live-99",
                        "labelIds": ["DRAFT"],
                    },
                }
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _mock_urlopen)

    cfg_live = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        dry_run=False,
    )
    client = GoogleCalendarWriteClient(cfg_live, now_fn=lambda: 5000.0)

    create_receipt = execute_action(
        "calendar.create",
        {
            "calendar_id": "primary@group.calendar.google.com",
            "summary": "Team Lunch",
            "start": "2026-11-24T12:00:00",
            "end": "2026-11-24T13:00:00",
            "attendees": ["owner@example.com"],
        },
        cfg_live,
        api_client=client,
    )
    assert create_receipt["dry_run"] is False
    assert create_receipt["event_id"] == "created-evt-1"
    assert create_receipt["html_link"] == "https://calendar.google.com/event?eid=created-evt-1"
    assert create_receipt["summary"] == "Team Lunch"
    assert create_receipt["start_pt"] == "2026-11-24 12:00 PT"
    assert create_receipt["end_pt"] == "2026-11-24 13:00 PT"
    assert create_receipt["calendar_id"] == "primary@group.calendar.google.com"

    # Verify token was cached on second call and event_id is URL-encoded
    update_receipt = execute_action(
        "calendar.update",
        {
            "event_id": "evt/special 1",
            "summary": "Updated Title",
            "start": "2026-11-24T15:00:00",
            "end": "2026-11-24T16:00:00",
        },
        cfg_live,
        api_client=client,
    )
    assert update_receipt["dry_run"] is False
    assert update_receipt["event_id"] == "evt/special 1"
    assert update_receipt["summary"] == "Updated Title"
    assert update_receipt["start_pt"] == "2026-11-24 15:00 PT"
    assert update_receipt["end_pt"] == "2026-11-24 16:00 PT"

    # Live gmail.create_draft receipt
    draft_receipt = execute_action(
        "gmail.create_draft",
        {
            "to": "coach@example.org",
            "cc": ["parent@example.com"],
            "subject": "Roster Question",
            "body": "Is Saturday still on?",
            "thread_id": "thr-live-99",
            "in_reply_to": "<msg-0@mail.example.com>",
        },
        cfg_live,
    )
    assert draft_receipt["dry_run"] is False
    assert draft_receipt["action"] == "gmail.create_draft"
    assert draft_receipt["draft_id"] == "r-live-draft-7"
    assert draft_receipt["message_id"] == "18f-live-msg-7"
    assert draft_receipt["thread_id"] == "thr-live-99"
    assert draft_receipt["to"] == ["coach@example.org"]
    assert draft_receipt["cc"] == ["parent@example.com"]
    assert draft_receipt["subject"] == "Roster Question"
    assert draft_receipt["gmail_url"] == "https://mail.google.com/mail/u/0/#drafts?compose=18f-live-msg-7"

    draft_http_payload = json.loads(http_calls[-1]["body"])
    assert draft_http_payload["message"]["threadId"] == "thr-live-99"
    raw_mime = base64.urlsafe_b64decode(draft_http_payload["message"]["raw"].encode("ascii"))
    parsed_live_mime = email.message_from_bytes(raw_mime, policy=email.policy.default)
    assert parsed_live_mime["In-Reply-To"] == "<msg-0@mail.example.com>"
    assert "Is Saturday still on?" in parsed_live_mime.get_content()

    # 1 calendar token + 1 POST + 1 PATCH + 1 gmail token + 1 gmail draft POST = 5 calls
    assert len(http_calls) == 5
    assert http_calls[1]["method"] == "POST"
    assert http_calls[2]["method"] == "PATCH"
    assert http_calls[4]["method"] == "POST"
    post_payload = json.loads(http_calls[1]["body"])
    assert post_payload["attendees"] == [{"email": "owner@example.com"}]

    # Secrets must never appear in receipts
    for receipt in (create_receipt, update_receipt, draft_receipt):
        dumped = json.dumps(receipt)
        assert "ya29.live-write-token" not in dumped
        assert "super-secret-value-do-not-leak" not in dumped
        assert "refresh-secret-do-not-leak" not in dumped
