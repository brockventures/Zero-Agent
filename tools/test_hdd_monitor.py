"""Unit tests for tools/hdd_monitor.py and its sidecars/audit registration.

All network and SerpApi calls are strictly mocked; zero live HTTP requests.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKTREE_ROOT) in sys.path:
    sys.path.remove(str(WORKTREE_ROOT))
sys.path.insert(0, str(WORKTREE_ROOT))

import tools.hdd_monitor as hm
import tools.sidecar_audit as sa
from tools import sidecars

PT = ZoneInfo("America/Los_Angeles")


# --------------------------------------------------------------------------
# $/TB math, thresholds, and title parsing
# --------------------------------------------------------------------------


def test_compute_price_per_tb_and_tiers():
    assert hm.compute_price_per_tb(300.0, 20) == 15.0
    assert hm.compute_price_per_tb(289.99, 16) == 18.12
    assert hm.compute_price_per_tb(324.0, 18) == 18.0
    assert hm.compute_price_per_tb(240.0, 16) == 15.0

    assert hm.classify_deal_tier(14.5) == "target_buy"
    assert hm.classify_deal_tier(15.0) == "target_buy"
    assert hm.classify_deal_tier(15.01) == "good"
    assert hm.classify_deal_tier(18.0) == "good"
    assert hm.classify_deal_tier(18.01) == "watch"

    with pytest.raises(ValueError):
        hm.compute_price_per_tb(0, 20)
    with pytest.raises(ValueError):
        hm.compute_price_per_tb(300.0, 0)


def test_normalize_listing_filters_non_target_drives_and_accessories():
    good_iw = hm.normalize_listing(
        {
            "title": "Seagate IronWolf Pro 20TB Enterprise NAS Internal HDD ST20000NT001",
            "price": "$349.99",
            "url": "https://www.amazon.com/dp/B0B94MF4LP",
        },
        source="Amazon",
    )
    assert good_iw is not None
    assert good_iw["family"] == "IronWolf Pro"
    assert good_iw["capacity_tb"] == 20
    assert good_iw["model"] == "ST20000NT001"
    assert good_iw["price"] == 349.99
    assert good_iw["price_per_tb"] == 17.5
    assert good_iw["tier"] == "good"
    assert good_iw["condition"] == "New"

    recert_exos = hm.normalize_listing(
        {
            "title": "Seagate Exos X20 20TB ST20000NM007D Manufacturer Recertified SATA",
            "price": 259.99,
            "url": "https://serverpartdeals.com/products/exos-x20",
        },
        source="ServerPartDeals",
    )
    assert recert_exos is not None
    assert recert_exos["family"] == "Exos"
    assert recert_exos["condition"] == "Recertified"
    assert recert_exos["price_per_tb"] == 13.0
    assert recert_exos["tier"] == "target_buy"

    wd_red_pro = hm.normalize_listing(
        {
            "title": "Western Digital 18TB WD Red Pro NAS Internal Hard Drive WD181KFGX",
            "price": "$359.99",
            "url": "https://www.bhphotovideo.com/c/product/123",
        },
        source="B&H",
    )
    assert wd_red_pro is not None
    assert wd_red_pro["family"] == "WD Red Pro"
    assert wd_red_pro["capacity_tb"] == 18
    assert wd_red_pro["model"] == "WD181KFGX"

    # Reject non-Pro, out-of-range capacities, accessories, and multi-packs
    assert hm.normalize_listing({"title": "Seagate IronWolf 16TB NAS HDD", "price": "$280"}, "Amazon") is None
    assert hm.normalize_listing({"title": "WD Red Plus 16TB NAS Drive", "price": "$290"}, "Amazon") is None
    assert hm.normalize_listing({"title": "Seagate IronWolf Pro 8TB NAS HDD", "price": "$210"}, "Amazon") is None
    assert hm.normalize_listing({"title": "Seagate IronWolf Pro 20TB 2-Pack Bundle", "price": "$700"}, "Amazon") is None
    assert hm.normalize_listing({"title": "20TB IronWolf Pro USB Enclosure Case", "price": "$150"}, "Amazon") is None


# --------------------------------------------------------------------------
# Mocked source fetchers & partial-source failure resilience
# --------------------------------------------------------------------------


def test_fetch_amazon_listings_mocked_serpapi():
    def fake_search_amazon(query: str, amazon_domain: str = "amazon.com", limit: int = 10) -> str:
        if "IronWolf Pro" in query:
            return json.dumps({
                "ok": True,
                "products": [
                    {
                        "asin": "B0B94MF4LP",
                        "title": "Seagate IronWolf Pro 20TB Enterprise NAS HDD ST20000NT001",
                        "price": "$359.99",
                        "link": "https://www.amazon.com/dp/B0B94MF4LP?ref=tracking",
                    },
                    {
                        "asin": "B091234567",
                        "title": "USB Hard Drive Enclosure",
                        "price": "$29.99",
                        "link": "https://www.amazon.com/dp/B091234567",
                    },
                ],
            })
        if "Exos" in query:
            return json.dumps({
                "ok": True,
                "products": [
                    {
                        "asin": "B09T123456",
                        "title": "Seagate Exos X18 16TB Enterprise HDD ST16000NM000J",
                        "price": "$239.99",
                        "link": "https://www.amazon.com/dp/B09T123456",
                    }
                ],
            })
        return json.dumps({"ok": False, "error": "CircuitBreaker: daily quota warning"})

    with patch.object(hm, "_get_search_amazon", return_value=fake_search_amazon):
        items = hm.fetch_amazon_listings()

    assert len(items) == 2
    urls = {item["url"] for item in items}
    assert "https://www.amazon.com/dp/B0B94MF4LP" in urls
    assert "https://www.amazon.com/dp/B09T123456" in urls


def test_partial_source_failure_resilience(tmp_path):
    """If ServerPartDeals/B&H/Newegg fail or block, Amazon results still succeed."""
    db_path = tmp_path / "hdd_prices.db"

    def fake_search_amazon(query: str, amazon_domain: str = "amazon.com", limit: int = 10) -> str:
        if "IronWolf Pro" in query:
            return json.dumps({
                "ok": True,
                "products": [
                    {
                        "asin": "B0B94MF4LP",
                        "title": "Seagate IronWolf Pro 16TB NAS HDD ST16000NT001",
                        "price": "$279.99",
                        "link": "https://www.amazon.com/dp/B0B94MF4LP",
                    }
                ],
            })
        return json.dumps({"ok": True, "products": []})

    def fake_requests_get(url: str, *args, **kwargs):
        if "serverpartdeals.com" in url:
            raise requests.exceptions.ConnectTimeout("timed out")
        if "bhphotovideo.com" in url:
            resp = MagicMock()
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("403 Forbidden")
            return resp
        if "newegg.com" in url:
            raise requests.exceptions.ConnectionError("blocked")
        raise AssertionError(f"Unexpected URL requested: {url}")

    now_pt = datetime(2026, 11, 29, 8, 30, tzinfo=PT)  # Sunday
    with (
        patch.object(hm, "_get_search_amazon", return_value=fake_search_amazon),
        patch.object(hm.requests, "get", side_effect=fake_requests_get),
    ):
        res = hm.run_capture(save=True, db_path=db_path, now_pt=now_pt)

    assert res["ok"] is True
    assert res["count"] == 1
    assert res["saved"] is True
    assert res["source_status"]["Amazon"]["ok"] is True
    assert res["source_status"]["ServerPartDeals"]["ok"] is False
    assert res["source_status"]["B&H"]["ok"] is False
    assert res["source_status"]["Newegg"]["ok"] is False
    assert "Unavailable: ServerPartDeals, B&H, Newegg" in res["digest"]
    assert "16TB IronWolf Pro" in res["digest"]


def test_amazon_failure_still_succeeds_with_secondary_source(tmp_path):
    """If SerpApi fails, valid secondary JSON sources keep the run healthy."""
    db_path = tmp_path / "hdd_prices.db"

    def broken_amazon(*args, **kwargs):
        return json.dumps({"ok": False, "error": "SerpApi HTTP 429"})

    spd_payload = {
        "products": [
            {
                "title": "Seagate Exos X20 20TB ST20000NM007D Manufacturer Recertified",
                "handle": "seagate-exos-x20-20tb",
                "variants": [{"price": "269.99", "available": True}],
            }
        ]
    }
    bh_html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "ItemList", "itemListElement": [
      {"@type": "ListItem", "item": {
        "@type": "Product",
        "name": "WD Red Pro 20TB NAS Internal Hard Drive WD202KFGX",
        "url": "/c/product/1800001",
        "offers": {"@type": "Offer", "price": "359.00"}
      }}
    ]}
    </script>
    </head></html>
    """

    def fake_requests_get(url: str, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "serverpartdeals.com" in url:
            resp.json.return_value = spd_payload
            return resp
        if "bhphotovideo.com" in url:
            resp.text = bh_html
            return resp
        raise requests.exceptions.HTTPError("Newegg 403")

    now_pt = datetime(2026, 11, 29, 8, 30, tzinfo=PT)
    with (
        patch.object(hm, "_get_search_amazon", return_value=broken_amazon),
        patch.object(hm.requests, "get", side_effect=fake_requests_get),
    ):
        res = hm.run_capture(save=True, db_path=db_path, now_pt=now_pt)

    assert res["ok"] is True
    assert res["count"] == 2
    assert res["source_status"]["Amazon"]["ok"] is False
    assert res["source_status"]["ServerPartDeals"]["ok"] is True
    assert res["source_status"]["B&H"]["ok"] is True
    assert res["source_status"]["Newegg"]["ok"] is False


def test_all_sources_fail_returns_error(tmp_path):
    db_path = tmp_path / "hdd_prices.db"

    def fail_source():
        raise RuntimeError("network blocked")

    sources = (("Amazon", fail_source), ("ServerPartDeals", fail_source))
    res = hm.run_capture(save=True, db_path=db_path, sources=sources)
    assert res["ok"] is False
    assert "All HDD sources failed" in res["error"]
    assert res["saved"] is False


# --------------------------------------------------------------------------
# SQLite history & historical lows
# --------------------------------------------------------------------------


def test_sqlite_price_history_and_historical_lows(tmp_path):
    db_path = tmp_path / "hdd_prices.db"

    week1 = [
        hm.normalize_listing(
            {
                "title": "Seagate IronWolf Pro 16TB ST16000NT001",
                "price": 304.0,
                "url": "https://www.amazon.com/dp/B0TEST16",
            },
            "Amazon",
        ),
        hm.normalize_listing(
            {
                "title": "Seagate Exos X20 20TB ST20000NM007D",
                "price": 340.0,
                "url": "https://www.amazon.com/dp/B0TEST20",
            },
            "Amazon",
        ),
    ]
    week2 = [
        hm.normalize_listing(
            {
                "title": "Seagate IronWolf Pro 16TB ST16000NT001",
                "price": 272.0,
                "url": "https://www.amazon.com/dp/B0TEST16",
            },
            "Amazon",
        ),
        hm.normalize_listing(
            {
                "title": "Seagate Exos X20 20TB ST20000NM007D",
                "price": 360.0,
                "url": "https://www.amazon.com/dp/B0TEST20",
            },
            "Amazon",
        ),
    ]

    assert hm.record_observations(week1, "2026-11-15", db_path=db_path, observed_ts=1794760000) == 2
    # Idempotent re-insert for same date replaces instead of duplicating
    assert hm.record_observations(week1, "2026-11-15", db_path=db_path, observed_ts=1794760000) == 2

    prior_lows = hm.get_historical_lows(db_path=db_path, exclude_date="2026-11-22")
    assert prior_lows[("IronWolf Pro", 16)]["min_price"] == 304.0
    assert prior_lows[("Exos", 20)]["min_price"] == 340.0

    # Week 2 run sees new ATL on 16TB IronWolf Pro ($272 < $304) and higher price on 20TB Exos ($360 > $340)
    sources_w2 = (("Amazon", lambda: week2),)
    res = hm.run_capture(
        save=True,
        db_path=db_path,
        now_pt=datetime(2026, 11, 22, 8, 30, tzinfo=PT),
        sources=sources_w2,
    )
    assert res["ok"] is True
    assert "🆕 ATL" in res["digest"]
    assert "ATL $340" in res["digest"]
    assert "🔻-$32" in res["digest"]

    all_lows = hm.get_historical_lows(db_path=db_path)
    assert all_lows[("IronWolf Pro", 16)]["min_price"] == 272.0
    assert all_lows[("Exos", 20)]["min_price"] == 340.0

    hist = hm.get_price_history(family="IronWolf Pro", capacity_tb=16, db_path=db_path)
    assert len(hist) == 2
    assert hist[0]["observed_date"] == "2026-11-22"
    assert hist[0]["min_price"] == 272.0


# --------------------------------------------------------------------------
# Discord mobile-safe formatting
# --------------------------------------------------------------------------


def test_discord_digest_mobile_safe_and_under_1900_chars():
    listings = []
    idx = 0
    for cap in (16, 18, 20, 22, 24):
        for fam in ("IronWolf Pro", "Exos", "WD Red Pro"):
            idx += 1
            price = round(cap * (14.25 if idx % 3 == 0 else 17.5), 2)
            listings.append(
                hm.normalize_listing(
                    {
                        "title": f"{fam} {cap}TB Enterprise CMR Internal Hard Drive ST{cap}000NM00{idx}",
                        "price": price,
                        "url": f"https://www.amazon.com/dp/B0TEST{idx:04d}",
                    },
                    source="Amazon",
                )
            )
    listings = [x for x in listings if x is not None]
    listings.sort(key=lambda x: x["price_per_tb"])

    digest = hm.format_digest(
        listings=listings,
        source_status={
            "Amazon": {"ok": True, "count": len(listings), "error": None},
            "ServerPartDeals": {"ok": False, "count": 0, "error": "timeout"},
        },
        historical_lows={("IronWolf Pro", 16): {"min_price": 270.0}},
        prev_best={("IronWolf Pro", 16): 289.0},
        now_pt=datetime(2026, 11, 29, 8, 30, tzinfo=PT),
    )

    assert len(digest) <= 1900
    assert "|" not in digest  # No pipe tables
    assert "PT" in digest
    assert "(<https://www.amazon.com/dp/B0TEST" in digest
    assert "TARGET BUY" in digest
    assert "GOOD VALUE" in digest


# --------------------------------------------------------------------------
# Sidecar & auditor registration
# --------------------------------------------------------------------------


def test_sidecars_and_audit_registration(monkeypatch):
    fake_capture = {
        "ok": True,
        "date": "2026-11-29",
        "digest": "💾 **Weekly Enterprise HDD Monitor** · Sun Nov 29, 2026 · 08:30 AM PT",
        "saved": True,
        "count": 3,
    }
    monkeypatch.setattr(hm, "run_capture", lambda save=True: fake_capture)

    queued = []
    with patch("tools.outbox.queue_outbox_message", side_effect=lambda ch, msg, **kw: queued.append((ch, msg, kw))):
        ok, rep, extra = sidecars.run_hdd_monitor(force_digest=True, post_discord=True, save=True)

    assert ok is True
    assert "Weekly Enterprise HDD Monitor" in rep
    assert extra == fake_capture
    assert len(queued) == 1
    assert queued[0][0] == str(hm.HOMELAB_CHANNEL_ID)

    # Verify sidecar_audit hooks against worktree files
    monkeypatch.setattr(sa, "SIDECARS_FILE", WORKTREE_ROOT / "tools" / "sidecars.py")
    assert sa.KNOWN_ALIASES.get("hdd") == "hdd_monitor"
    actions = sa.get_sidecars_py_actions()
    assert "hdd" in actions
    assert "hdd_monitor" in actions
    registered_ids = sa.get_registered_sidecar_job_ids()
    assert "hdd" in registered_ids
