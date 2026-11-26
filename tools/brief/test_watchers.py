"""Unit tests for BRIEF read-only Gmail and Calendar watermark watchers."""

from __future__ import annotations

import base64
import io
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief.config import BriefConfig
from tools.brief.queue import BriefQueue
from tools.brief.watchers import (
    CALENDAR_API_BASE,
    GMAIL_API_BASE,
    GOOGLE_TOKEN_URI,
    CalendarWatcher,
    CursorStore,
    GmailWatcher,
    GoogleReadonlyApiClient,
    classify_gmail_filter,
    resolve_readonly_oauth_credentials,
    should_prefilter_gmail,
    touch_heartbeat,
)


class MockHttpError(Exception):
    """Minimal HTTP error carrying status code for mocked API clients."""

    def __init__(self, code: int, message: str = "http error") -> None:
        super().__init__(f"HTTP {code}: {message}")
        self.code = code
        self.status_code = code


class FakeApiClient:
    """Method-aware GET-only mock for Gmail and Calendar API endpoints."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        copied = dict(params or {})
        self.calls.append((url, copied))
        return self._handler(url, copied)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _make_config(tmp_path: Path) -> BriefConfig:
    return BriefConfig(
        allowlisted_emails=["owner@example.com"],
        dry_run=True,
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )


def test_cursor_store_atomic_persistence_and_heartbeat(tmp_path: Path) -> None:
    cursors_path = tmp_path / "nested" / "cursors.json"
    store = CursorStore(cursors_path)

    # Missing file fails gracefully to empty dict
    assert store.load() == {}

    store.save({"gmail_history_id": "100"})
    assert store.load() == {"gmail_history_id": "100"}

    updated = store.update({"calendar_sync_token": "sync-1"})
    assert updated == {"gmail_history_id": "100", "calendar_sync_token": "sync-1"}
    assert store.get("calendar_sync_token") == "sync-1"

    # Temp files are cleaned up after atomic os.replace
    leftover_tmps = list(cursors_path.parent.glob("*.tmp*"))
    assert leftover_tmps == []

    hb_path = touch_heartbeat(tmp_path / "heartbeats", "gmail_watcher", now=1795490000.25)
    assert hb_path == tmp_path / "heartbeats" / "gmail_watcher.hb"
    assert float(hb_path.read_text(encoding="utf-8").strip()) == pytest.approx(1795490000.25)


def test_resolve_readonly_oauth_credentials_precedence_and_get_only_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readonly_path = tmp_path / "brief_readonly_token.json"
    fallback_path = tmp_path / "google_oauth.json"
    fallback_path.write_text(
        json.dumps(
            {
                "client_id": "fallback-cid",
                "client_secret": "fallback-csec",
                "refresh_token": "fallback-rtok",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("tools.brief.watchers.DEFAULT_BRIEF_READONLY_TOKEN_PATH", readonly_path)
    monkeypatch.setattr("tools.brief.watchers.DEFAULT_GOOGLE_OAUTH_PATH", fallback_path)

    seen_requests: list[tuple[str, str, bytes | None]] = []

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> _FakeResp:
        seen_requests.append((req.get_method(), req.full_url, req.data))
        if req.full_url == GOOGLE_TOKEN_URI:
            body = req.data.decode("utf-8") if req.data else ""
            if "fallback-rtok" in body:
                return _FakeResp({"access_token": "tok-fallback", "expires_in": 3600})
            return _FakeResp({"access_token": "tok-readonly", "expires_in": 3600})
        return _FakeResp({"historyId": "999", "emailAddress": "brief-bot@example.com"})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # Falls back to google_oauth.json when brief_readonly_token.json does not exist
    creds_fb = resolve_readonly_oauth_credentials(now_fn=lambda: 1000.0)
    assert creds_fb["access_token"] == "tok-fallback"
    assert creds_fb["credentials_path"] == str(fallback_path)

    # Prefers brief_readonly_token.json once present
    readonly_path.write_text(
        json.dumps(
            {
                "client_id": "ro-cid",
                "client_secret": "ro-csec",
                "refresh_token": "ro-rtok",
            }
        ),
        encoding="utf-8",
    )
    creds_ro = resolve_readonly_oauth_credentials(now_fn=lambda: 1000.0)
    assert creds_ro["access_token"] == "tok-readonly"
    assert creds_ro["credentials_path"] == str(readonly_path)

    # GoogleReadonlyApiClient only executes GET requests against Gmail/Calendar
    client = GoogleReadonlyApiClient(now_fn=lambda: 1000.0)
    profile = client.get(f"{GMAIL_API_BASE}/users/me/profile")
    assert profile["historyId"] == "999"
    api_methods = [m for m, url, _ in seen_requests if url != GOOGLE_TOKEN_URI]
    assert api_methods == ["GET"]


def test_resolve_readonly_oauth_config_isolation_permissions_and_watcher_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    readonly_path = tmp_path / "brief_readonly_token.json"
    fallback_path = tmp_path / "google_oauth.json"
    explicit_path = tmp_path / "explicit_oauth.json"

    fallback_path.write_text(
        json.dumps(
            {
                "client_id": "fb-cid",
                "client_secret": "fb-csec",
                "refresh_token": "fb-rtok",
            }
        ),
        encoding="utf-8",
    )
    fallback_path.chmod(0o600)

    explicit_path.write_text(
        json.dumps(
            {
                "client_id": "exp-cid",
                "client_secret": "exp-csec",
                "refresh_token": "exp-rtok",
            }
        ),
        encoding="utf-8",
    )
    explicit_path.chmod(0o600)

    monkeypatch.setattr("tools.brief.watchers.DEFAULT_GOOGLE_OAUTH_PATH", fallback_path)

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> _FakeResp:
        if req.full_url == GOOGLE_TOKEN_URI:
            body = req.data.decode("utf-8") if req.data else ""
            if "ro-rtok" in body:
                return _FakeResp({"access_token": "tok-ro", "expires_in": 3600})
            if "exp-rtok" in body:
                return _FakeResp({"access_token": "tok-exp", "expires_in": 3600})
            return _FakeResp({"access_token": "tok-fb", "expires_in": 3600})
        if req.full_url.endswith("/users/me/profile"):
            return _FakeResp({"historyId": "321", "emailAddress": "brief@example.com"})
        if "/calendars/primary/events" in req.full_url:
            return _FakeResp({"nextSyncToken": "sync-321", "items": []})
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # 1. Missing readonly_token_path with allow_shared_readonly_fallback=False refuses shared fallback
    strict_cfg = BriefConfig(
        allowlisted_emails=["owner@example.com"],
        readonly_token_path=readonly_path,
        allow_shared_readonly_fallback=False,
        state_dir=tmp_path / "state_strict",
        memory_dir=tmp_path / "mem_strict",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )
    with pytest.raises(RuntimeError, match="allow_shared_readonly_fallback is disabled"):
        resolve_readonly_oauth_credentials(config=strict_cfg, now_fn=lambda: 1000.0)

    # 2. Missing readonly_token_path with allow_shared_readonly_fallback=True falls back cleanly
    permissive_cfg = BriefConfig(
        allowlisted_emails=["owner@example.com"],
        readonly_token_path=readonly_path,
        allow_shared_readonly_fallback=True,
        state_dir=tmp_path / "state_fb",
        memory_dir=tmp_path / "mem_fb",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )
    fb_creds = resolve_readonly_oauth_credentials(config=permissive_cfg, now_fn=lambda: 1000.0)
    assert fb_creds["access_token"] == "tok-fb"
    assert fb_creds["credentials_path"] == str(fallback_path)
    assert fb_creds["used_shared_fallback"] is True

    # 3. Overly permissive mode (> 0600) on readonly_token_path logs a warning; 0600 does not
    readonly_path.write_text(
        json.dumps(
            {
                "client_id": "ro-cid",
                "client_secret": "ro-csec",
                "refresh_token": "ro-rtok",
            }
        ),
        encoding="utf-8",
    )
    readonly_path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="tools.brief.watchers"):
        ro_creds_warn = resolve_readonly_oauth_credentials(
            config=strict_cfg,
            now_fn=lambda: 1000.0,
        )
    assert ro_creds_warn["access_token"] == "tok-ro"
    assert ro_creds_warn["credentials_path"] == str(readonly_path)
    assert ro_creds_warn["used_shared_fallback"] is False
    assert any("expected 0600" in rec.message for rec in caplog.records)

    caplog.clear()
    readonly_path.chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="tools.brief.watchers"):
        ro_creds_clean = resolve_readonly_oauth_credentials(
            config=strict_cfg,
            now_fn=lambda: 1000.0,
        )
    assert ro_creds_clean["access_token"] == "tok-ro"
    assert not any("expected 0600" in rec.message for rec in caplog.records)

    # 4. GmailWatcher & CalendarWatcher respect config and explicit oauth_path overrides
    queue = BriefQueue(strict_cfg.db_path, allowlisted_senders=strict_cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(strict_cfg.cursors_path)

    gmail_watcher = GmailWatcher(queue, cursors, strict_cfg, now_fn=lambda: 1000.0)
    assert gmail_watcher.api_client.config is strict_cfg
    assert gmail_watcher.poll_once()["history_id"] == "321"

    cal_watcher = CalendarWatcher(
        queue,
        cursors,
        strict_cfg,
        oauth_path=explicit_path,
        now_fn=lambda: 1000.0,
    )
    assert cal_watcher.api_client.credentials_path == explicit_path
    assert cal_watcher.api_client._get_token() == "tok-exp"
    assert cal_watcher.poll_once()["sync_token"] == "sync-321"


def test_should_prefilter_gmail_precedence_and_header_formats() -> None:
    own = ["brief-inbox@example.com"]
    allowlist = ["owner@example.com"]

    # System/folder labels always drop first
    assert should_prefilter_gmail(["INBOX", "SENT"], {}, "school-office@example.org", own) == (
        True,
        "label:SENT",
    )
    assert should_prefilter_gmail(["DRAFT"], {}, "owner@example.com", own) == (True, "label:DRAFT")
    assert should_prefilter_gmail(["TRASH"], {}, "owner@example.com", own) == (True, "label:TRASH")
    assert should_prefilter_gmail(["SPAM"], {}, "owner@example.com", own) == (True, "label:SPAM")

    # Own sender address always drops (even with display name & mixed case)
    assert should_prefilter_gmail(
        ["INBOX"],
        {},
        "BRIEF Bot <Brief-Inbox@Example.COM>",
        own,
        allowlisted_emails=allowlist,
    ) == (True, "own_sender")

    # Category labels drop
    for cat in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"):
        assert should_prefilter_gmail(["INBOX", cat], {}, "school-office@example.org", own) == (
            True,
            f"label:{cat}",
        )

    # Primary List-Unsubscribe mail from non-allowlisted senders survives as snippet_only (not dropped)
    gmail_headers = [
        {"name": "From", "value": "Deals <deals@example.com>"},
        {"name": "List-Unsubscribe", "value": "<mailto:unsub@example.com>"},
    ]
    assert classify_gmail_filter(
        ["INBOX"],
        gmail_headers,
        "Deals <deals@example.com>",
        own,
        allowlisted_emails=allowlist,
    ) == ("snippet_only", "list_unsubscribe")
    assert should_prefilter_gmail(
        ["INBOX"],
        gmail_headers,
        "Deals <deals@example.com>",
        own,
        allowlisted_emails=allowlist,
    ) == (False, "list_unsubscribe")

    # Allowlisted sender bypasses List-Unsubscribe (dict header format, case-insensitive)
    assert should_prefilter_gmail(
        ["INBOX"],
        {"list-unsubscribe": "<https://example.com/unsub>"},
        "Owner <OWNER@EXAMPLE.COM>",
        own,
        allowlisted_emails=allowlist,
    ) == (False, "")

    # Regular external message survives
    assert should_prefilter_gmail(
        ["INBOX", "IMPORTANT"],
        [{"name": "Subject", "value": "Early Dismissal"}],
        "School Office <school-office@example.org>",
        own,
        allowlisted_emails=allowlist,
    ) == (False, "")


def test_classify_gmail_filter_two_tier_domain_and_vip_rules() -> None:
    own = ["brief-inbox@example.com"]
    allowlist = ["owner@example.com"]
    vip = ["coach@team.example.org"]
    domain_allowlist = ["konstella.com", "@SchoolPortal.EDU"]
    blocked = ["marketing.example", ".spammer.net"]

    # Blocked domain (exact + subdomain) drops even without List-Unsubscribe or if VIP
    assert classify_gmail_filter(
        ["INBOX"],
        {},
        "Offers <promo@marketing.example>",
        own,
        blocked_domains=blocked,
    ) == ("drop", "blocked_domain:marketing.example")
    assert classify_gmail_filter(
        ["INBOX"],
        {},
        "Sub <bounce@mail.marketing.example>",
        own,
        blocked_domains=blocked,
    ) == ("drop", "blocked_domain:marketing.example")
    assert classify_gmail_filter(
        ["INBOX"],
        {},
        "Spoof <vip@spammer.net>",
        own,
        vip_senders=["vip@spammer.net"],
        blocked_domains=blocked,
    ) == ("drop", "blocked_domain:spammer.net")

    # Lookalike domain must NOT match dot-boundary rule for blocked or allowlisted domains
    assert classify_gmail_filter(
        ["INBOX"],
        {"List-Unsubscribe": "<mailto:u@evilmarketing.example>"},
        "News <news@evilmarketing.example>",
        own,
        blocked_domains=blocked,
    ) == ("snippet_only", "list_unsubscribe")
    assert classify_gmail_filter(
        ["INBOX"],
        {"List-Unsubscribe": "<mailto:u@evilkonstella.com>"},
        "Phish <alerts@evilkonstella.com>",
        own,
        automated_domain_allowlist=domain_allowlist,
    ) == ("snippet_only", "list_unsubscribe")

    # Allowlisted portal domain (exact and subdomain) passes full even with List-Unsubscribe
    assert classify_gmail_filter(
        ["INBOX"],
        {"List-Unsubscribe": "<https://konstella.com/unsub>"},
        "Konstella <no-reply@konstella.com>",
        own,
        automated_domain_allowlist=domain_allowlist,
    ) == ("full", "")
    assert classify_gmail_filter(
        ["INBOX"],
        {"List-Unsubscribe": "<https://konstella.com/unsub>"},
        "Konstella <alerts@notify.konstella.com>",
        own,
        automated_domain_allowlist=domain_allowlist,
    ) == ("full", "")

    # VIP sender passes full even with List-Unsubscribe
    assert classify_gmail_filter(
        ["INBOX"],
        {"List-Unsubscribe": "<mailto:unsub@team.example.org>"},
        "Coach <COACH@team.example.org>",
        own,
        allowlisted_emails=allowlist,
        vip_senders=vip,
    ) == ("full", "")


def test_gmail_watcher_two_tier_prefilter_envelopes(tmp_path: Path) -> None:
    cfg = BriefConfig(
        allowlisted_emails=["owner@example.com"],
        vip_senders=["teacher@school.example.org"],
        automated_domain_allowlist=["konstella.com"],
        blocked_domains=["marketing.example"],
        dry_run=True,
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)
    cursors.save({"gmail_history_id": "100"})

    long_snippet = "A" * 350
    messages: dict[str, dict[str, Any]] = {
        "m-promo": {
            "id": "m-promo",
            "threadId": "t-promo",
            "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
            "internalDate": "1795490100000",
            "payload": {
                "headers": [{"name": "From", "value": "Sales <sales@shop.example>"}],
                "mimeType": "text/plain",
                "body": {"data": _b64("Big sale")},
            },
        },
        "m-blocked": {
            "id": "m-blocked",
            "threadId": "t-blocked",
            "labelIds": ["INBOX"],
            "internalDate": "1795490101000",
            "payload": {
                "headers": [{"name": "From", "value": "Blaster <hi@mail.marketing.example>"}],
                "mimeType": "text/plain",
                "body": {"data": _b64("Blocked body")},
            },
        },
        "m-konstella-sub": {
            "id": "m-konstella-sub",
            "threadId": "t-konstella",
            "labelIds": ["INBOX"],
            "internalDate": "1795490102000",
            "snippet": "Short snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Konstella <notify@mail.konstella.com>"},
                    {"name": "Subject", "value": "Volunteer Sign-Up"},
                    {"name": "List-Unsubscribe", "value": "<https://konstella.com/u>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("FULL_KONSTELLA_BODY: Classroom snack shift tomorrow.")},
            },
        },
        "m-vip": {
            "id": "m-vip",
            "threadId": "t-vip",
            "labelIds": ["INBOX"],
            "internalDate": "1795490103000",
            "snippet": "VIP snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Ms. Lee <teacher@school.example.org>"},
                    {"name": "Subject", "value": "Field Trip Permission"},
                    {"name": "List-Unsubscribe", "value": "<mailto:u@school.example.org>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("FULL_VIP_BODY: Sign permission slip by Friday.")},
            },
        },
        "m-unknown-unsub": {
            "id": "m-unknown-unsub",
            "threadId": "t-unknown",
            "labelIds": ["INBOX"],
            "internalDate": "1795490104000",
            "snippet": long_snippet,
            "payload": {
                "headers": [
                    {"name": "From", "value": "Digest <news@unknown-newsletter.example>"},
                    {"name": "Subject", "value": "Weekly Digest"},
                    {"name": "List-Unsubscribe", "value": "<mailto:unsub@unknown-newsletter.example>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("FULL_NEWSLETTER_BODY_MUST_NOT_APPEAR")},
            },
        },
    }

    def _handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/users/me/history"):
            return {
                "historyId": "110",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m-promo"}},
                            {"message": {"id": "m-blocked"}},
                            {"message": {"id": "m-konstella-sub"}},
                            {"message": {"id": "m-vip"}},
                            {"message": {"id": "m-unknown-unsub"}},
                        ]
                    }
                ],
            }
        if "/users/me/messages/" in url:
            return messages[url.rsplit("/", 1)[-1]]
        raise AssertionError(f"Unexpected URL: {url}")

    watcher = GmailWatcher(
        queue,
        cursors,
        cfg,
        api_client=FakeApiClient(_handler),
        now_fn=lambda: 1795490200.0,
        sleep_fn=lambda _: None,
    )
    res = watcher.poll_once()
    assert res["status"] == "ok"
    assert res["filtered"] == 2
    assert res["enqueued"] == 3

    events = {}
    while (ev := queue.claim_next_event(now=1795490300.0)) is not None:
        events[ev["external_id"]] = ev

    assert set(events.keys()) == {"m-konstella-sub", "m-vip", "m-unknown-unsub"}
    assert "FULL_KONSTELLA_BODY: Classroom snack shift tomorrow." in events["m-konstella-sub"]["body"]
    assert "Filter: primary_list_unsubscribe_snippet_only" not in events["m-konstella-sub"]["body"]

    assert "FULL_VIP_BODY: Sign permission slip by Friday." in events["m-vip"]["body"]
    assert "Filter: primary_list_unsubscribe_snippet_only" not in events["m-vip"]["body"]

    snippet_body = events["m-unknown-unsub"]["body"]
    assert "Subject: Weekly Digest" in snippet_body
    assert "From: Digest <news@unknown-newsletter.example>" in snippet_body
    assert (
        "Filter: primary_list_unsubscribe_snippet_only "
        "(default bias: IGNORE unless genuinely actionable)"
    ) in snippet_body
    assert "FULL_NEWSLETTER_BODY_MUST_NOT_APPEAR" not in snippet_body
    snippet_tail = snippet_body.rsplit("\n\n", 1)[-1]
    assert len(snippet_tail) == 300
    assert snippet_tail.endswith("...")


def test_gmail_watcher_startup_zero_backfill_incremental_prefilter_and_404_skip(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)
    alerts: list[str] = []

    messages: dict[str, dict[str, Any]] = {
        "m-survive": {
            "id": "m-survive",
            "threadId": "thread-school-1",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1795490100000",
            "snippet": "Early dismissal",
            "payload": {
                "headers": [
                    {"name": "From", "value": "School Office <school-office@example.org>"},
                    {"name": "Subject", "value": "Early Dismissal Today"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("Pickup moved to 1:30 PM PT.")},
            },
        },
        "m-promo": {
            "id": "m-promo",
            "threadId": "thread-promo-1",
            "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
            "internalDate": "1795490101000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Promo <promo@example.com>"},
                    {"name": "Subject", "value": "Sale"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("50% off")},
            },
        },
        "m-owner": {
            "id": "m-owner",
            "threadId": "thread-owner-1",
            "labelIds": ["INBOX"],
            "internalDate": "1795490102000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Owner <owner@example.com>"},
                    {"name": "Subject", "value": "Check in"},
                    {"name": "List-Unsubscribe", "value": "<mailto:u@example.com>"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("Please track this.")},
            },
        },
    }

    phase = {"step": "startup"}

    def _handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/users/me/profile"):
            return {"historyId": "500", "emailAddress": "brief-inbox@example.com"}
        if url.endswith("/users/me/history"):
            assert params["startHistoryId"] == "500"
            assert params["historyTypes"] == "messageAdded"
            if params.get("pageToken") is None:
                return {
                    "historyId": "505",
                    "nextPageToken": "page-2",
                    "history": [
                        {"messagesAdded": [{"message": {"id": "m-survive"}}]},
                        {"messagesAdded": [{"message": {"id": "m-deleted"}}]},
                    ],
                }
            return {
                "historyId": "510",
                "history": [
                    # Duplicate m-survive across pages should be deduplicated
                    {"messagesAdded": [{"message": {"id": "m-survive"}}]},
                    {"messagesAdded": [{"message": {"id": "m-promo"}}]},
                    {"messagesAdded": [{"message": {"id": "m-owner"}}]},
                ],
            }
        if "/users/me/messages/" in url:
            msg_id = url.rsplit("/", 1)[-1]
            if msg_id == "m-deleted":
                raise MockHttpError(404, "message deleted mid-poll")
            return messages[msg_id]
        raise AssertionError(f"Unexpected URL: {url}")

    client = FakeApiClient(_handler)
    watcher = GmailWatcher(
        queue,
        cursors,
        cfg,
        api_client=client,
        notify_fn=alerts.append,
        now_fn=lambda: 1795490200.0,
        sleep_fn=lambda _: None,
    )

    # Startup watermark: saves historyId without emitting historical messages
    startup_res = watcher.poll_once()
    assert startup_res["status"] == "watermarked"
    assert startup_res["history_id"] == "500"
    assert cursors.load()["gmail_history_id"] == "500"
    assert cursors.load()["gmail_email_address"] == "brief-inbox@example.com"
    assert queue.get_queue_stats()["total"] == 0
    assert (cfg.heartbeats_dir / "gmail_watcher.hb").is_file()

    # Incremental poll: paginates, deduplicates, skips 404, prefilters promo, enqueues survivors
    phase["step"] = "incremental"
    inc_res = watcher.poll_once()
    assert inc_res["status"] == "ok"
    assert inc_res["enqueued"] == 2
    assert inc_res["filtered"] == 1
    assert inc_res["missing_404"] == 1
    assert cursors.load()["gmail_history_id"] == "510"
    assert alerts == []

    ev1 = queue.claim_next_event(now=1795490300.0)
    ev2 = queue.claim_next_event(now=1795490300.0)
    ev3 = queue.claim_next_event(now=1795490300.0)
    assert ev1 is not None
    assert ev1["source"] == "gmail"
    assert ev1["key"] == "thread-school-1"
    assert ev1["target_tag"] == "intake/gmail/thread-school-1"
    assert ev1["external_id"] == "m-survive"
    assert ev1["trusted"] is False
    assert "Pickup moved to 1:30 PM PT." in ev1["body"]

    assert ev2 is not None
    assert ev2["external_id"] == "m-owner"
    assert ev2["trusted"] is True
    assert ev3 is None


def test_gmail_watcher_cursor_not_advanced_on_queue_failure_and_404_410_recovery(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)
    cursors.save({"gmail_history_id": "200"})
    alerts: list[str] = []

    class FailingQueue(BriefQueue):
        def enqueue_event(self, *args: Any, **kwargs: Any) -> int | None:
            raise RuntimeError("disk full simulated")

    def _handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/users/me/history"):
            if params["startHistoryId"] == "200":
                return {
                    "historyId": "250",
                    "history": [{"messagesAdded": [{"message": {"id": "m-1"}}]}],
                }
            raise MockHttpError(404, "historyId too old")
        if url.endswith("/users/me/messages/m-1"):
            return {
                "id": "m-1",
                "threadId": "t-1",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [{"name": "From", "value": "someone@example.com"}],
                },
            }
        if url.endswith("/users/me/profile"):
            return {"historyId": "900", "emailAddress": "brief@example.com"}
        raise AssertionError(f"Unexpected URL: {url}")

    bad_watcher = GmailWatcher(
        FailingQueue(cfg.db_path),
        cursors,
        cfg,
        api_client=FakeApiClient(_handler),
        notify_fn=alerts.append,
        now_fn=lambda: 1795490200.0,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(RuntimeError, match="disk full"):
        bad_watcher.poll_once()
    # Cursor MUST NOT advance if queue commit failed
    assert cursors.load()["gmail_history_id"] == "200"

    # Expired cursor (404/410) re-watermarks and sends PT gap alert
    cursors.save({"gmail_history_id": "100"})
    recovery_watcher = GmailWatcher(
        queue,
        cursors,
        cfg,
        api_client=FakeApiClient(_handler),
        notify_fn=alerts.append,
        now_fn=lambda: 1795490200.0,
        sleep_fn=lambda _: None,
    )
    res = recovery_watcher.poll_once()
    assert res["status"] == "rewatermarked"
    assert res["history_id"] == "900"
    assert cursors.load()["gmail_history_id"] == "900"
    assert len(alerts) == 1
    assert "PT" in alerts[0]
    assert "900" in alerts[0]


def test_gmail_watcher_backoff_retries_on_429_and_5xx(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)
    sleeps: list[float] = []
    attempts = {"count": 0}

    def _handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise MockHttpError(429, "rate limited")
        if attempts["count"] == 2:
            raise MockHttpError(503, "backend unavailable")
        return {"historyId": "777"}

    watcher = GmailWatcher(
        queue,
        cursors,
        cfg,
        api_client=FakeApiClient(_handler),
        now_fn=lambda: 1795490200.0,
        sleep_fn=sleeps.append,
    )
    res = watcher.poll_once()
    assert res["status"] == "watermarked"
    assert res["history_id"] == "777"
    assert attempts["count"] == 3
    assert len(sleeps) == 2
    assert all(delay > 0 for delay in sleeps)


def test_calendar_watcher_startup_incremental_cancelled_filtering_and_410_recovery(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)
    alerts: list[str] = []

    now_ts = 1795490000.0  # 2026-11-23 ~19:13 PT
    mode = {"phase": "startup"}

    def _handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        assert url == f"{CALENDAR_API_BASE}/calendars/primary/events"
        if mode["phase"] == "startup":
            if params.get("pageToken") is None:
                return {
                    "nextPageToken": "p2",
                    "items": [{"id": "hist-1", "status": "confirmed", "summary": "Old"}],
                }
            return {
                "nextSyncToken": "sync-100",
                "items": [{"id": "hist-2", "status": "confirmed", "summary": "Old 2"}],
            }

        if mode["phase"] == "incremental":
            assert params.get("syncToken") == "sync-100"
            return {
                "nextSyncToken": "sync-200",
                "items": [
                    {
                        "id": "ev-past-cancelled-dt",
                        "status": "cancelled",
                        "updated": "2026-11-23T10:00:00Z",
                        "start": {"dateTime": "2026-11-20T10:00:00-08:00"},
                        "end": {"dateTime": "2026-11-20T11:00:00-08:00"},
                    },
                    {
                        "id": "ev-past-cancelled-allday",
                        "status": "cancelled",
                        "updated": "2026-11-23T10:00:00Z",
                        "start": {"date": "2026-11-21"},
                        "end": {"date": "2026-11-22"},
                    },
                    {
                        "id": "ev-future-cancelled",
                        "status": "cancelled",
                        "summary": "Cancelled Future Sync",
                        "updated": "2026-11-23T18:00:00Z",
                        "organizer": {"email": "owner@example.com"},
                        "start": {"dateTime": "2026-11-26T10:00:00-08:00"},
                        "end": {"dateTime": "2026-11-26T11:00:00-08:00"},
                    },
                    {
                        "id": "ev-updated-etag-fallback",
                        "status": "confirmed",
                        "etag": '"etag-99"',
                        "summary": "School Pickup Shift",
                        "creator": {"email": "school-office@example.org"},
                        "start": {"date": "2026-11-25"},
                        "end": {"date": "2026-11-26"},
                    },
                ],
            }

        if mode["phase"] == "expired":
            if params.get("syncToken") == "sync-200":
                raise MockHttpError(410, "Sync token is no longer valid")
            return {"nextSyncToken": "sync-300", "items": []}

        raise AssertionError(f"Unknown phase: {mode['phase']}")

    watcher = CalendarWatcher(
        queue,
        cursors,
        cfg,
        api_client=FakeApiClient(_handler),
        notify_fn=alerts.append,
        now_fn=lambda: now_ts,
        sleep_fn=lambda _: None,
    )

    # 1. Startup watermark: paginates without emitting historical events
    res_start = watcher.poll_once()
    assert res_start["status"] == "watermarked"
    assert res_start["sync_token"] == "sync-100"
    assert cursors.load()["calendar_sync_token"] == "sync-100"
    assert queue.get_queue_stats()["total"] == 0
    assert (cfg.heartbeats_dir / "calendar_watcher.hb").is_file()

    # 2. Incremental poll: drops cancelled past timed/all-day events, keeps future cancelled + confirmed
    mode["phase"] = "incremental"
    res_inc = watcher.poll_once()
    assert res_inc["status"] == "ok"
    assert res_inc["enqueued"] == 2
    assert res_inc["filtered"] == 2
    assert cursors.load()["calendar_sync_token"] == "sync-200"

    c1 = queue.claim_next_event(now=now_ts + 10)
    c2 = queue.claim_next_event(now=now_ts + 10)
    c3 = queue.claim_next_event(now=now_ts + 10)
    assert c1 is not None and c2 is not None and c3 is None

    by_key = {c1["key"]: c1, c2["key"]: c2}
    future_ev = by_key["ev-future-cancelled"]
    assert future_ev["external_id"] == "ev-future-cancelled:2026-11-23T18:00:00Z"
    assert future_ev["target_tag"] == "intake/calendar/ev-future-cancelled"
    assert future_ev["trusted"] is True
    assert "PT" in future_ev["body"]

    etag_ev = by_key["ev-updated-etag-fallback"]
    assert etag_ev["external_id"] == 'ev-updated-etag-fallback:"etag-99"'
    assert etag_ev["trusted"] is False
    assert "all-day" in etag_ev["body"]

    # 3. HTTP 410 syncToken expiry: re-watermarks cleanly and sends PT alert
    mode["phase"] = "expired"
    res_exp = watcher.poll_once()
    assert res_exp["status"] == "rewatermarked"
    assert res_exp["sync_token"] == "sync-300"
    assert cursors.load()["calendar_sync_token"] == "sync-300"
    assert len(alerts) == 1
    assert "410" in alerts[0]
    assert "PT" in alerts[0]
