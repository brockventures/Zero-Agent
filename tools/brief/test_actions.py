"""Unit tests for BRIEF gate config, write action validation, OAuth separation, and Calendar write execution."""

from __future__ import annotations

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
    GoogleCalendarWriteClient,
    build_oauth_consent_urls,
    canonical_proposal_hash,
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
    assert ALLOWED_GATE_ACTIONS == frozenset({"calendar.create", "calendar.update"})

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

    # 2. Missing write token + fallback enabled -> uses shared oauth + exchanges token
    captured_requests: list[dict[str, Any]] = []

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
                "scope": "https://www.googleapis.com/auth/calendar.events",
            }
        )

    monkeypatch.setattr("tools.brief.actions.urllib.request.urlopen", _fake_urlopen)

    cfg_fallback = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        gate_write_token_path=write_token,
        gate_allow_shared_oauth_fallback=True,
    )
    res_fb = resolve_gate_write_credentials(
        cfg_fallback,
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
            now_fn=lambda: 2000.0,
            fallback_oauth_path=shared_oauth,
        )
    assert res_dedicated["used_shared_fallback"] is False
    assert res_dedicated["credentials_path"] == str(write_token)
    assert "client_id=dedicated-write-cid" in captured_requests[-1]["body"]
    assert "overly permissive mode" in caplog.text

    # 4. Token HTTP error never leaks client_secret or refresh_token
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

    wr_parsed = urllib.parse.urlparse(urls["write_url"])
    wr_qs = urllib.parse.parse_qs(wr_parsed.query)
    assert wr_qs["client_id"] == ["12345.apps.googleusercontent.com"]
    assert wr_qs["access_type"] == ["offline"]
    assert wr_qs["prompt"] == ["consent"]
    assert wr_qs["scope"] == ["https://www.googleapis.com/auth/calendar.events"]

    serialized = json.dumps(urls)
    assert secret_val not in serialized
    assert "client_secret" not in serialized


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

    # Live execution via GoogleCalendarWriteClient + mocked urllib
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

    # 1 token exchange + 1 POST + 1 PATCH
    assert len(http_calls) == 3
    assert http_calls[1]["method"] == "POST"
    assert http_calls[2]["method"] == "PATCH"
    post_payload = json.loads(http_calls[1]["body"])
    assert post_payload["attendees"] == [{"email": "owner@example.com"}]

    # Secrets must never appear in receipts
    for receipt in (create_receipt, update_receipt):
        dumped = json.dumps(receipt)
        assert "ya29.live-write-token" not in dumped
        assert "super-secret-value-do-not-leak" not in dumped
        assert "refresh-secret-do-not-leak" not in dumped
