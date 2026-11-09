#!/usr/bin/env python3
"""Unit tests for marketing sweep, unsubscribe handler, and sidecar post context in tools/sidecars.py."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKTREE_ROOT) in sys.path:
    sys.path.remove(str(WORKTREE_ROOT))
sys.path.insert(0, str(WORKTREE_ROOT))

import tools.sidecars as sidecars  # noqa: E402


SAMPLE_SENDERS = [
    {"name": "Delta Dental", "email": "email@member-deltadental.org", "count": 1},
    {"name": "GrowFit Camp", "email": "redwoodcity@growfitpe.com", "count": 1},
    {"name": "Loop@stanfordalumni.org", "email": "loop@stanfordalumni.org", "count": 1},
    {"name": "Nyjeri Whipps, Stanford Engineering Fund", "email": "engineeringfund@stanford.edu", "count": 1},
    {"name": "Raley's Something Extra", "email": "se-email@raleysstores.com", "count": 1},
    {"name": "RENPHO US", "email": "hello@mail.renpho.com", "count": 1},
    {"name": "RENPHO US", "email": "information@mail.renpho.com", "count": 1},
    {"name": "Senator Alex Padilla", "email": "donotreply@padilla.senate.gov", "count": 1},
    {"name": "Whole Foods Market", "email": "wholefoodsmarket@mail.wholefoodsmarket.com", "count": 1},
    {"name": "Wing & Barrel Ranch", "email": "membership@wingandbarrelranch.com", "count": 1},
]


def _setup_isolated_paths(monkeypatch, tmp_path: Path):
    senders_file = tmp_path / "marketing_senders.json"
    unsub_file = tmp_path / "marketing_unsubscribed.json"
    last_post_file = tmp_path / "last_sidecar_post.json"
    state_file = tmp_path / "marketing_sweep_last_run"

    monkeypatch.setattr(sidecars, "MARKETING_SENDERS_FILE", str(senders_file))
    monkeypatch.setattr(sidecars, "MARKETING_UNSUBSCRIBED_FILE", str(unsub_file))
    monkeypatch.setattr(sidecars, "LAST_SIDECAR_POST_FILE", str(last_post_file))
    monkeypatch.setattr(sidecars, "MARKETING_STATE_FILE", str(state_file))
    return senders_file, unsub_file, last_post_file, state_file


def test_unsubscribe_marketing_senders_by_indices_and_emails(monkeypatch, tmp_path):
    senders_file, unsub_file, _, _ = _setup_isolated_paths(monkeypatch, tmp_path)
    senders_file.write_text(json.dumps(SAMPLE_SENDERS, indent=2), encoding="utf-8")

    def fake_unsub_and_trash(addr: str, name: str = "") -> dict:
        return {
            "name": name or addr,
            "email": addr,
            "trashed_count": 3,
            "message_ids": ["m1", "m2", "m3"],
            "unsubscribe_url": f"https://example.com/unsub?e={addr}",
            "unsubscribe_http_ok": True,
            "filter_created": True,
        }

    monkeypatch.setattr(sidecars, "_unsubscribe_and_trash_sender", fake_unsub_and_trash)

    res = sidecars.unsubscribe_marketing_senders([5, 6, 8])
    assert res["ok"] is True
    expected = {
        "se-email@raleysstores.com",
        "hello@mail.renpho.com",
        "information@mail.renpho.com",
        "donotreply@padilla.senate.gov",
    }
    assert set(res["emails"]) == expected
    assert res["trashed_total"] == 12

    persisted = json.loads(unsub_file.read_text(encoding="utf-8"))
    assert expected.issubset(set(persisted["emails"]))
    assert len(persisted["senders"]) == 4


def test_unsubscribe_and_trash_sender_gmail_api_flow(monkeypatch):
    import workspace_mcp

    monkeypatch.setattr(workspace_mcp, "_auth_headers", lambda: {"Authorization": "Bearer test-token"})
    calls = []

    class DummyResp:
        def __init__(self, payload: dict, status: int = 200):
            self._bytes = json.dumps(payload).encode("utf-8")
            self.status = status

        def read(self):
            return self._bytes

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = getattr(req, "method", "GET") or "GET"
        calls.append((method, url))
        if "/messages?" in url:
            return DummyResp({"messages": [{"id": "msg_101"}, {"id": "msg_102"}]})
        if "/messages/msg_101?format=metadata" in url:
            return DummyResp({
                "payload": {
                    "headers": [
                        {"name": "List-Unsubscribe", "value": "<https://unsub.example.org/oneclick>"},
                        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
                    ]
                }
            })
        if url.startswith("https://unsub.example.org/oneclick"):
            return DummyResp({"ok": True}, status=200)
        if "/settings/filters" in url:
            return DummyResp({"id": "flt_1"}, status=200)
        if url.endswith("/trash"):
            return DummyResp({"id": "trashed"}, status=200)
        return DummyResp({})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = sidecars._unsubscribe_and_trash_sender("se-email@raleysstores.com", "Raley's Something Extra")

    assert res["email"] == "se-email@raleysstores.com"
    assert res["trashed_count"] == 2
    assert res["unsubscribe_url"] == "https://unsub.example.org/oneclick"
    assert res["unsubscribe_http_ok"] is True
    assert res["filter_created"] is True
    assert any(m == "POST" and u == "https://unsub.example.org/oneclick" for m, u in calls)
    assert sum(1 for m, u in calls if m == "POST" and u.endswith("/trash")) == 2


def test_run_marketing_sweep_excludes_unsubscribed_and_saves_post(monkeypatch, tmp_path):
    senders_file, unsub_file, last_post_file, _ = _setup_isolated_paths(monkeypatch, tmp_path)

    unsub_file.write_text(
        json.dumps({
            "emails": [
                "se-email@raleysstores.com",
                "hello@mail.renpho.com",
                "information@mail.renpho.com",
                "donotreply@padilla.senate.gov",
            ],
            "senders": [],
        }),
        encoding="utf-8",
    )

    fake_messages = [
        {"id": "1", "from": '"Delta Dental" <email@member-deltadental.org>'},
        {"id": "2", "from": '"Raley\'s Something Extra" <se-email@raleysstores.com>'},
        {"id": "3", "from": '"RENPHO US" <hello@mail.renpho.com>'},
        {"id": "4", "from": '"Senator Alex Padilla" <donotreply@padilla.senate.gov>'},
        {"id": "5", "from": '"Whole Foods Market" <wholefoodsmarket@mail.wholefoodsmarket.com>'},
    ]
    monkeypatch.setattr(
        sidecars,
        "gmail_search",
        lambda q, max_results=50: json.dumps({"ok": True, "messages": fake_messages}),
    )

    should_post, msg = sidecars.run_marketing_sweep(force=True)
    assert should_post is True
    assert "Delta Dental" in msg
    assert "Whole Foods Market" in msg
    assert "Raley's Something Extra" not in msg
    assert "RENPHO US" not in msg
    assert "Senator Alex Padilla" not in msg

    saved_senders = json.loads(senders_file.read_text(encoding="utf-8"))
    saved_emails = {s["email"] for s in saved_senders}
    assert saved_emails == {
        "email@member-deltadental.org",
        "wholefoodsmarket@mail.wholefoodsmarket.com",
    }

    assert last_post_file.exists()
    last_post = json.loads(last_post_file.read_text(encoding="utf-8"))
    assert last_post["job_id"] == "marketing"
    assert "Biweekly Marketing Report" in last_post["message"]
    assert len(last_post["senders"]) == 2


def test_get_recent_marketing_report_context(monkeypatch, tmp_path):
    senders_file, _, last_post_file, _ = _setup_isolated_paths(monkeypatch, tmp_path)

    # Empty when files do not exist
    assert sidecars.get_recent_marketing_report_context() == ""

    senders_file.write_text(json.dumps(SAMPLE_SENDERS[:3], indent=2), encoding="utf-8")
    sidecars.save_last_sidecar_post(
        job_id="marketing",
        title="Biweekly Marketing Report — Nov 08",
        message="📬 **Biweekly Marketing Report** — Nov 08\n1. Delta Dental — 1 email",
        senders=SAMPLE_SENDERS[:3],
    )

    ctx = sidecars.get_recent_marketing_report_context()
    assert "Biweekly Marketing Report" in ctx
    assert "1. Delta Dental <email@member-deltadental.org>" in ctx
    assert "2. GrowFit Camp <redwoodcity@growfitpe.com>" in ctx
    assert "3. Loop@stanfordalumni.org <loop@stanfordalumni.org>" in ctx
