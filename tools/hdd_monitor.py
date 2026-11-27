#!/usr/bin/env python3
"""Weekly Sunday Enterprise CMR HDD Market Monitor for Zero (#homelab).

Tracks 16TB-24TB CMR enterprise/NAS hard drives (Seagate IronWolf Pro,
Seagate Exos, WD Red Pro) across Amazon (via tools/amazon_serpapi.py) and
secondary retailers (ServerPartDeals, B&H Photo, Newegg) with per-source
exception isolation, SQLite historical price tracking, $/TB calculation,
and mobile-safe Discord digest formatting.

Host 2 (DS1525+) context:
- SHR array with 2x 16TB Seagate IronWolf Pro CMR drives and 3 open bays.
- Sweet spot: 16TB-20TB CMR enterprise drives in pairs.
- Thresholds: Good <= $18/TB, Target Buy <= $15/TB.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("hdd_monitor")

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

PT = ZoneInfo("America/Los_Angeles")

DB_PATH = Path(os.environ.get("HDD_PRICES_DB", "/workspace/data/hdd_prices.db"))
HOMELAB_CHANNEL_ID = 1544955535722545253

TARGET_CAPACITIES = (16, 18, 20, 22, 24)
SWEET_SPOT_CAPACITIES = (16, 18, 20)
TARGET_FAMILIES = ("IronWolf Pro", "Exos", "WD Red Pro")

GOOD_PRICE_PER_TB = 18.0
TARGET_BUY_PER_TB = 15.0

MIN_VALID_PRICE = 120.0
MAX_VALID_PRICE = 1500.0
MAX_DIGEST_CHARS = 1900

REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

AMAZON_QUERIES = (
    "Seagate IronWolf Pro 16TB 20TB",
    "Seagate Exos 16TB 20TB 24TB",
    "WD Red Pro 16TB 20TB 24TB",
)

_CAPACITY_RE = re.compile(r"\b(16|18|20|22|24)\s*TB\b", re.IGNORECASE)
_MODEL_RE = re.compile(r"\b(ST\d{4,5}[A-Z0-9]{2,6}|WD\d{3,4}[A-Z0-9]{3,6})\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

_EXCLUDE_KEYWORDS = (
    "enclosure",
    "caddy",
    "docking",
    "usb dock",
    "adapter",
    "cable",
    "2-pack",
    "2 pack",
    "4-pack",
    "4 pack",
    "multi-pack",
    "multipack",
    "bundle",
    "lot of",
    "nvme",
    "ssd",
    "diskstation",
    "heatsink",
)

_RECERT_KEYWORDS = (
    "recertified",
    "refurbished",
    "renewed",
    "reconditioned",
    "pre-owned",
    "used",
)


# --------------------------------------------------------------------------
# Parsing & classification helpers
# --------------------------------------------------------------------------


def parse_price(raw: Any) -> float | None:
    """Parse a raw price string or number into a float."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return round(val, 2) if val > 0 else None
    text = str(raw).strip()
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def parse_capacity_tb(title: str) -> int | None:
    """Extract target drive capacity in TB (16, 18, 20, 22, 24) from title."""
    if not title:
        return None
    m = _CAPACITY_RE.search(title)
    if not m:
        return None
    cap = int(m.group(1))
    return cap if cap in TARGET_CAPACITIES else None


def classify_family(title: str) -> str | None:
    """Classify a listing title into one of the target CMR enterprise families."""
    t = (title or "").lower()
    if not t:
        return None
    if "ironwolf pro" in t:
        return "IronWolf Pro"
    if "exos" in t:
        return "Exos"
    if "wd red pro" in t or ("red pro" in t and ("wd" in t or "western digital" in t)):
        return "WD Red Pro"
    return None


def extract_model_number(title: str) -> str:
    """Extract a Seagate or WD model number if present in the title."""
    if not title:
        return ""
    m = _MODEL_RE.search(title)
    return m.group(1).upper() if m else ""


def detect_condition(title: str) -> str:
    """Detect whether a drive listing is New or Recertified/Refurbished."""
    t = (title or "").lower()
    if any(kw in t for kw in _RECERT_KEYWORDS):
        return "Recertified"
    return "New"


def is_excluded_title(title: str) -> bool:
    """Return True if the title looks like an accessory, enclosure, SSD, or multi-pack."""
    t = (title or "").lower()
    return any(kw in t for kw in _EXCLUDE_KEYWORDS)


def compute_price_per_tb(price: float, capacity_tb: int) -> float:
    """Compute $/TB rounded to 2 decimal places."""
    if price is None or price <= 0:
        raise ValueError(f"Invalid price: {price}")
    if capacity_tb is None or capacity_tb <= 0:
        raise ValueError(f"Invalid capacity_tb: {capacity_tb}")
    return round(float(price) / int(capacity_tb), 2)


def classify_deal_tier(price_per_tb: float) -> str:
    """Classify $/TB into 'target_buy' (<= $15), 'good' (<= $18), or 'watch'."""
    if price_per_tb <= TARGET_BUY_PER_TB:
        return "target_buy"
    if price_per_tb <= GOOD_PRICE_PER_TB:
        return "good"
    return "watch"


def normalize_listing(raw: dict[str, Any], source: str) -> dict[str, Any] | None:
    """Normalize a raw retailer record into a canonical HDD listing dict."""
    title = str(raw.get("title") or "").strip()
    if not title or is_excluded_title(title):
        return None

    family = classify_family(title)
    if family not in TARGET_FAMILIES:
        return None

    capacity_tb = parse_capacity_tb(title)
    if capacity_tb not in TARGET_CAPACITIES:
        return None

    price = parse_price(raw.get("price"))
    if price is None or price < MIN_VALID_PRICE or price > MAX_VALID_PRICE:
        return None

    price_per_tb = compute_price_per_tb(price, capacity_tb)
    model = str(raw.get("model") or extract_model_number(title)).strip().upper()
    condition = str(raw.get("condition") or detect_condition(title)).strip()
    url = str(raw.get("url") or raw.get("link") or "").strip()
    tier = classify_deal_tier(price_per_tb)

    uid_seed = f"{source}|{family}|{capacity_tb}|{condition}|{model or title.lower()}|{url}"
    uid = hashlib.md5(uid_seed.encode("utf-8")).hexdigest()[:12]

    return {
        "id": uid,
        "source": source,
        "family": family,
        "capacity_tb": capacity_tb,
        "model": model,
        "condition": condition,
        "title": title,
        "price": price,
        "price_per_tb": price_per_tb,
        "tier": tier,
        "url": url,
    }


# --------------------------------------------------------------------------
# Retailer fetchers (per-source isolated)
# --------------------------------------------------------------------------


def _get_search_amazon() -> Callable[..., str]:
    """Import search_amazon lazily so tests and standalone runs both resolve cleanly."""
    try:
        from tools.amazon_serpapi import search_amazon
    except ImportError:
        from amazon_serpapi import search_amazon
    return search_amazon


def _clean_amazon_url(asin: str | None, raw_link: str | None) -> str:
    """Prefer short canonical /dp/ASIN URLs to keep Discord messages compact."""
    asin_clean = (asin or "").strip().upper()
    if asin_clean:
        return f"https://www.amazon.com/dp/{asin_clean}"
    link = (raw_link or "").strip()
    if link and not link.startswith("http"):
        return f"https://www.amazon.com{link if link.startswith('/') else '/' + link}"
    return link


def fetch_amazon_listings() -> list[dict[str, Any]]:
    """Query Amazon via tools/amazon_serpapi.py (search_amazon) for target CMR drives."""
    search_fn = _get_search_amazon()
    listings: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    errors: list[str] = []
    succeeded = 0

    for query in AMAZON_QUERIES:
        raw_json = search_fn(query=query, amazon_domain="amazon.com", limit=10)
        payload = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        if not isinstance(payload, dict) or not payload.get("ok"):
            err = payload.get("error", "Unknown SerpApi error") if isinstance(payload, dict) else "Invalid response"
            errors.append(str(err))
            continue

        succeeded += 1
        for item in payload.get("products", []) or []:
            if not isinstance(item, dict):
                continue
            url = _clean_amazon_url(item.get("asin"), item.get("link"))
            norm = normalize_listing(
                {
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "url": url,
                },
                source="Amazon",
            )
            if norm and (not norm["url"] or norm["url"] not in seen_urls):
                if norm["url"]:
                    seen_urls.add(norm["url"])
                listings.append(norm)

    if succeeded == 0 and errors:
        raise RuntimeError(f"Amazon SerpApi failed for all queries: {errors[0]}")
    return listings


def fetch_serverpartdeals_listings() -> list[dict[str, Any]]:
    """Query ServerPartDeals Shopify catalog JSON for 16TB-24TB CMR drives."""
    url = "https://serverpartdeals.com/products.json?limit=250"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    listings: list[dict[str, Any]] = []
    for prod in data.get("products", []) or []:
        if not isinstance(prod, dict):
            continue
        title = str(prod.get("title") or "").strip()
        handle = str(prod.get("handle") or "").strip()
        prod_url = f"https://serverpartdeals.com/products/{handle}" if handle else ""
        variants = prod.get("variants") or []
        available_prices = [
            parse_price(v.get("price"))
            for v in variants
            if isinstance(v, dict) and v.get("available", True)
        ]
        valid_prices = [p for p in available_prices if p is not None]
        if not valid_prices:
            continue
        best_price = min(valid_prices)
        norm = normalize_listing(
            {"title": title, "price": best_price, "url": prod_url},
            source="ServerPartDeals",
        )
        if norm:
            listings.append(norm)
    return listings


def _extract_json_ld_products(html_text: str, base_url: str, source: str) -> list[dict[str, Any]]:
    """Parse schema.org Product JSON-LD blocks from retailer search HTML."""
    listings: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("@type")
        if ntype == "ItemList":
            for el in node.get("itemListElement", []) or []:
                if isinstance(el, dict):
                    _walk(el.get("item") or el)
            return
        if ntype == "Product" or (isinstance(ntype, list) and "Product" in ntype):
            title = node.get("name") or ""
            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            prices = [parse_price(o.get("price")) for o in offer_list if isinstance(o, dict)]
            valid_prices = [p for p in prices if p is not None]
            if valid_prices:
                link = str(node.get("url") or (offer_list[0].get("url") if offer_list else "") or "").strip()
                if link and link.startswith("/"):
                    link = f"{base_url.rstrip('/')}{link}"
                norm = normalize_listing(
                    {"title": title, "price": min(valid_prices), "url": link},
                    source=source,
                )
                if norm:
                    listings.append(norm)
            return
        if "@graph" in node:
            _walk(node["@graph"])

    for block in _JSON_LD_RE.findall(html_text or ""):
        try:
            parsed = json.loads(block.strip())
            _walk(parsed)
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping malformed JSON-LD block from %s: %s", source, exc)
            continue
    return listings


def fetch_bh_listings() -> list[dict[str, Any]]:
    """Query B&H Photo search for 16TB-24TB IronWolf Pro / Exos / WD Red Pro."""
    url = "https://www.bhphotovideo.com/c/search?Ntt=Seagate+IronWolf+Pro+Exos+WD+Red+Pro+Hard+Drive"
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return _extract_json_ld_products(resp.text, "https://www.bhphotovideo.com", "B&H")


def fetch_newegg_listings() -> list[dict[str, Any]]:
    """Query Newegg search for 16TB-24TB IronWolf Pro / Exos / WD Red Pro."""
    url = "https://www.newegg.com/p/pl?d=Seagate+IronWolf+Pro+Exos+WD+Red+Pro+16TB+20TB"
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return _extract_json_ld_products(resp.text, "https://www.newegg.com", "Newegg")


DEFAULT_SOURCES: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...] = (
    ("Amazon", fetch_amazon_listings),
    ("ServerPartDeals", fetch_serverpartdeals_listings),
    ("B&H", fetch_bh_listings),
    ("Newegg", fetch_newegg_listings),
)


def collect_all_listings(
    sources: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Collect normalized HDD listings across all sources with per-source exception isolation."""
    active_sources = sources if sources is not None else DEFAULT_SOURCES
    all_listings: list[dict[str, Any]] = []
    source_status: dict[str, dict[str, Any]] = {}

    for name, fetcher in active_sources:
        try:
            items = fetcher() or []
            deduped: dict[str, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                lid = item.get("id") or f"{name}|{item.get('title')}|{item.get('price')}"
                existing = deduped.get(lid)
                if existing is None or item.get("price", float("inf")) < existing.get("price", float("inf")):
                    deduped[lid] = item
            clean_items = list(deduped.values())
            all_listings.extend(clean_items)
            source_status[name] = {"ok": True, "count": len(clean_items), "error": None}
        except Exception as exc:  # noqa: BLE001
            err_msg = f"{type(exc).__name__}: {exc}"
            log.warning("HDD source %s failed gracefully: %s", name, err_msg)
            source_status[name] = {"ok": False, "count": 0, "error": err_msg}

    all_listings.sort(key=lambda x: (x["price_per_tb"], x["price"], x["capacity_tb"]))
    return all_listings, source_status


# --------------------------------------------------------------------------
# SQLite price history persistence
# --------------------------------------------------------------------------


def init_db(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open and initialize the SQLite price history database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_date TEXT NOT NULL,
            observed_ts INTEGER NOT NULL,
            source TEXT NOT NULL,
            family TEXT NOT NULL,
            capacity_tb INTEGER NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            condition TEXT NOT NULL DEFAULT 'New',
            title TEXT NOT NULL,
            price REAL NOT NULL,
            price_per_tb REAL NOT NULL,
            url TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_date ON price_observations(observed_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_family_cap ON price_observations(family, capacity_tb)"
    )
    conn.commit()
    return conn


def record_observations(
    listings: list[dict[str, Any]],
    observed_date: str,
    db_path: Path | str = DB_PATH,
    observed_ts: int | None = None,
) -> int:
    """Persist observations for observed_date idempotently (replaces same-date rows)."""
    if not listings:
        init_db(db_path).close()
        return 0

    ts = int(observed_ts if observed_ts is not None else datetime.now(PT).timestamp())
    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                "DELETE FROM price_observations WHERE observed_date = ?",
                (observed_date,),
            )
            rows = [
                (
                    observed_date,
                    ts,
                    item["source"],
                    item["family"],
                    int(item["capacity_tb"]),
                    item.get("model", ""),
                    item.get("condition", "New"),
                    item["title"],
                    float(item["price"]),
                    float(item["price_per_tb"]),
                    item.get("url", ""),
                )
                for item in listings
            ]
            conn.executemany(
                """
                INSERT INTO price_observations (
                    observed_date, observed_ts, source, family, capacity_tb,
                    model, condition, title, price, price_per_tb, url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)
    finally:
        conn.close()


def get_historical_lows(
    db_path: Path | str = DB_PATH,
    exclude_date: str | None = None,
    condition: str | None = None,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Return lowest observed price & $/TB per (family, capacity_tb)."""
    path = Path(db_path)
    if not path.exists():
        return {}
    conn = init_db(path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if exclude_date:
            clauses.append("observed_date != ?")
            params.append(exclude_date)
        if condition:
            clauses.append("condition = ?")
            params.append(condition)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT family, capacity_tb, price, price_per_tb, source, observed_date, model, condition
            FROM price_observations
            {where}
            ORDER BY price ASC, observed_date DESC
        """
        lows: dict[tuple[str, int], dict[str, Any]] = {}
        for row in conn.execute(sql, params):
            key = (row["family"], int(row["capacity_tb"]))
            if key not in lows:
                lows[key] = {
                    "min_price": float(row["price"]),
                    "min_price_per_tb": float(row["price_per_tb"]),
                    "source": row["source"],
                    "observed_date": row["observed_date"],
                    "model": row["model"],
                    "condition": row["condition"],
                }
        return lows
    finally:
        conn.close()


def get_previous_best_prices(
    db_path: Path | str = DB_PATH,
    before_date: str | None = None,
) -> dict[tuple[str, int], float]:
    """Return the most recent prior observation's minimum price per (family, capacity_tb)."""
    path = Path(db_path)
    if not path.exists():
        return {}
    conn = init_db(path)
    try:
        if before_date:
            row = conn.execute(
                "SELECT MAX(observed_date) AS prev_date FROM price_observations WHERE observed_date < ?",
                (before_date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(observed_date) AS prev_date FROM price_observations"
            ).fetchone()
        prev_date = row["prev_date"] if row else None
        if not prev_date:
            return {}

        out: dict[tuple[str, int], float] = {}
        for r in conn.execute(
            """
            SELECT family, capacity_tb, MIN(price) AS min_price
            FROM price_observations
            WHERE observed_date = ?
            GROUP BY family, capacity_tb
            """,
            (prev_date,),
        ):
            out[(r["family"], int(r["capacity_tb"]))] = float(r["min_price"])
        return out
    finally:
        conn.close()


def get_price_history(
    family: str | None = None,
    capacity_tb: int | None = None,
    limit: int = 30,
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    """Query daily minimum price history from SQLite."""
    path = Path(db_path)
    if not path.exists():
        return []
    conn = init_db(path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if family:
            clauses.append("family = ?")
            params.append(family)
        if capacity_tb is not None:
            clauses.append("capacity_tb = ?")
            params.append(int(capacity_tb))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT observed_date, family, capacity_tb,
                   MIN(price) AS min_price,
                   MIN(price_per_tb) AS min_price_per_tb,
                   COUNT(*) AS listing_count
            FROM price_observations
            {where}
            GROUP BY observed_date, family, capacity_tb
            ORDER BY observed_date DESC, capacity_tb ASC, family ASC
            LIMIT ?
        """
        params.append(max(1, int(limit)))
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Digest formatting (Discord-mobile safe, <= 1900 chars, no pipe tables)
# --------------------------------------------------------------------------


def _format_deal_line(
    item: dict[str, Any],
    historical_lows: dict[tuple[str, int], dict[str, Any]],
    prev_best: dict[tuple[str, int], float],
) -> str:
    """Format a single drive deal into a compact mobile-safe bullet line."""
    key = (item["family"], item["capacity_tb"])
    price = item["price"]
    pptb = item["price_per_tb"]
    tier = item.get("tier") or classify_deal_tier(pptb)

    badge = "🔥" if tier == "target_buy" else ("✅" if tier == "good" else "•")
    cond_tag = " (Recert)" if item.get("condition") == "Recertified" else ""

    hist = historical_lows.get(key)
    atl_tag = ""
    if hist is None or price < hist["min_price"]:
        atl_tag = " · 🆕 ATL"
    elif abs(price - hist["min_price"]) < 0.01:
        atl_tag = " · AT ATL"
    else:
        atl_tag = f" · ATL ${hist['min_price']:.0f}"

    prev_price = prev_best.get(key)
    delta_tag = ""
    if prev_price is not None:
        delta = price - prev_price
        if delta <= -1.0:
            delta_tag = f" (🔻-${abs(delta):.0f})"
        elif delta >= 1.0:
            delta_tag = f" (🔺+${delta:.0f})"

    src = item["source"]
    url = item.get("url", "")
    src_link = f"[{src}](<{url}>)" if url else src
    return (
        f"{badge} **{item['capacity_tb']}TB {item['family']}**{cond_tag} — "
        f"**${price:,.2f}** (${pptb:.2f}/TB){delta_tag}{atl_tag} · {src_link}"
    )


def _select_best_by_slot(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the cheapest listing per (capacity_tb, family), prioritizing sweet spot 16TB-20TB."""
    best: dict[tuple[int, str], dict[str, Any]] = {}
    for item in listings:
        key = (int(item["capacity_tb"]), item["family"])
        current = best.get(key)
        if current is None or (item["price_per_tb"], item["price"]) < (
            current["price_per_tb"],
            current["price"],
        ):
            best[key] = item

    def _sort_key(x: dict[str, Any]) -> tuple[int, float, int, str]:
        in_sweet_spot = 0 if x["capacity_tb"] in SWEET_SPOT_CAPACITIES else 1
        return (in_sweet_spot, x["price_per_tb"], x["capacity_tb"], x["family"])

    return sorted(best.values(), key=_sort_key)


def _format_source_footer(source_status: dict[str, dict[str, Any]]) -> str:
    """Format compact retailer availability summary without pipe characters."""
    ok_parts = []
    err_parts = []
    for name, st in source_status.items():
        if st.get("ok"):
            ok_parts.append(f"{name} ({st.get('count', 0)})")
        else:
            err_parts.append(name)
    parts = []
    if ok_parts:
        parts.append(f"Sources: {', '.join(ok_parts)}")
    if err_parts:
        parts.append(f"Unavailable: {', '.join(err_parts)}")
    return " · ".join(parts) if parts else "Sources: none"


def format_digest(
    listings: list[dict[str, Any]],
    source_status: dict[str, dict[str, Any]],
    historical_lows: dict[tuple[str, int], dict[str, Any]] | None = None,
    prev_best: dict[tuple[str, int], float] | None = None,
    now_pt: datetime | None = None,
    max_chars: int = MAX_DIGEST_CHARS,
) -> str:
    """Render a Discord-mobile safe digest (no pipe tables, <url> links, PT timestamp, <= 1900 chars)."""
    ts_pt = (now_pt or datetime.now(PT)).strftime("%a %b %d, %Y · %I:%M %p PT")
    hist_lows = historical_lows or {}
    prev_map = prev_best or {}

    header = (
        f"💾 **Weekly Enterprise HDD Monitor** · {ts_pt}\n"
        f"DS1525+ SHR (2x16TB IronWolf Pro, 3 open bays) · "
        f"Good <= ${GOOD_PRICE_PER_TB:.0f}/TB · Target <= ${TARGET_BUY_PER_TB:.0f}/TB"
    )
    footer = _format_source_footer(source_status)

    if not listings:
        msg = f"{header}\n\n⚠️ No matching 16TB-24TB CMR drives found this run.\n\n{footer}"
        return msg[:max_chars]

    target_buys = [x for x in listings if x["price_per_tb"] <= TARGET_BUY_PER_TB]
    good_deals = [
        x for x in listings if TARGET_BUY_PER_TB < x["price_per_tb"] <= GOOD_PRICE_PER_TB
    ]
    best_slots = _select_best_by_slot(listings)

    sections: list[str] = [header]

    if target_buys:
        lines = [f"🔥 **TARGET BUY (<= ${TARGET_BUY_PER_TB:.0f}/TB)**"]
        for item in target_buys[:4]:
            lines.append(_format_deal_line(item, hist_lows, prev_map))
        sections.append("\n".join(lines))

    if good_deals:
        lines = [f"✅ **GOOD VALUE (<= ${GOOD_PRICE_PER_TB:.0f}/TB)**"]
        for item in good_deals[:4]:
            lines.append(_format_deal_line(item, hist_lows, prev_map))
        sections.append("\n".join(lines))

    if not target_buys and not good_deals:
        sections.append(
            f"⏳ **No deals <= ${GOOD_PRICE_PER_TB:.0f}/TB yet** — tracking cheapest options below:"
        )

    market_lines = ["📊 **Best Observed by Model (16TB-20TB Sweet Spot First)**"]
    shown_ids = {x["id"] for x in (target_buys[:4] + good_deals[:4])}
    remaining_slots = [x for x in best_slots if x["id"] not in shown_ids]
    if not remaining_slots:
        remaining_slots = best_slots
    for item in remaining_slots[:8]:
        market_lines.append(_format_deal_line(item, hist_lows, prev_map))
    sections.append("\n".join(market_lines))
    sections.append(footer)

    digest = "\n\n".join(sections)
    if len(digest) <= max_chars:
        return digest

    # Trim market_lines from bottom up until the entire digest fits inside max_chars
    while len(digest) > max_chars and len(market_lines) > 2:
        market_lines.pop()
        sections[-2] = "\n".join(market_lines)
        digest = "\n\n".join(sections)

    if len(digest) > max_chars:
        avail = max_chars - len(footer) - 8
        body = "\n\n".join(sections[:-1])
        if avail > 40:
            body = body[: avail - 3].rstrip() + "..."
            digest = f"{body}\n\n{footer}"
        else:
            digest = digest[:max_chars]

    return digest


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def run_capture(
    save: bool = True,
    db_path: Path | str = DB_PATH,
    now_pt: datetime | None = None,
    sources: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...] | None = None,
) -> dict[str, Any]:
    """Run one HDD price capture pass, persist to SQLite if save=True, and render digest."""
    current_pt = now_pt or datetime.now(PT)
    today_str = current_pt.strftime("%Y-%m-%d")

    listings, source_status = collect_all_listings(sources=sources)
    any_source_ok = any(st.get("ok") for st in source_status.values())

    if not any_source_ok and not listings:
        err_details = "; ".join(
            f"{name}: {st.get('error')}"
            for name, st in source_status.items()
            if st.get("error")
        )
        return {
            "ok": False,
            "date": today_str,
            "timestamp_pt": current_pt.strftime("%Y-%m-%d %I:%M %p PT"),
            "error": f"All HDD sources failed ({err_details or 'unknown error'})",
            "source_status": source_status,
            "listings": [],
            "saved": False,
        }

    # Compare against prior historical lows before writing today's observations
    historical_lows = get_historical_lows(db_path=db_path, exclude_date=today_str)
    prev_best = get_previous_best_prices(db_path=db_path, before_date=today_str)

    saved_count = 0
    if save and listings:
        saved_count = record_observations(
            listings,
            observed_date=today_str,
            db_path=db_path,
            observed_ts=int(current_pt.timestamp()),
        )

    digest = format_digest(
        listings=listings,
        source_status=source_status,
        historical_lows=historical_lows,
        prev_best=prev_best,
        now_pt=current_pt,
    )

    target_buys = [x for x in listings if x["price_per_tb"] <= TARGET_BUY_PER_TB]
    good_deals = [
        x for x in listings if TARGET_BUY_PER_TB < x["price_per_tb"] <= GOOD_PRICE_PER_TB
    ]

    return {
        "ok": True,
        "date": today_str,
        "timestamp_pt": current_pt.strftime("%Y-%m-%d %I:%M %p PT"),
        "digest": digest,
        "saved": bool(save and listings),
        "saved_count": saved_count,
        "count": len(listings),
        "target_buy_count": len(target_buys),
        "good_deal_count": len(good_deals),
        "listings": listings,
        "source_status": source_status,
    }


def hdd_monitor(
    action: str = "report",
    days: int = 30,
    db_path: Path | str = DB_PATH,
) -> dict[str, Any]:
    """On-demand HDD monitor interface ('report', 'capture', or 'history')."""
    try:
        if action == "report":
            return run_capture(save=False, db_path=db_path)
        if action == "capture":
            return run_capture(save=True, db_path=db_path)
        if action == "history":
            rows = get_price_history(limit=max(1, days) * 10, db_path=db_path)
            lows = {
                f"{fam} {cap}TB": info
                for (fam, cap), info in sorted(get_historical_lows(db_path=db_path).items())
            }
            return {"ok": True, "history": rows, "historical_lows": lows}
        return {"ok": False, "error": f"Unknown action: {action}"}
    except Exception as exc:
        log.exception("hdd_monitor failed")
        return {"ok": False, "error": repr(exc)}


def main() -> int:
    """CLI entry point for standalone testing and scheduled runs."""
    dry_run = "--dry-run" in sys.argv or "--report" in sys.argv
    post = "--post" in sys.argv
    res = run_capture(save=not dry_run)
    if not res.get("ok"):
        print(f"⚠️ **HDD Monitor Error**: {res.get('error')}", file=sys.stderr)
        return 1
    digest = res.get("digest", "")
    print(digest)
    if post and digest:
        try:
            from tools.outbox import queue_outbox_message

            queue_outbox_message(
                str(HOMELAB_CHANNEL_ID),
                digest,
                source_turn="hdd_monitor",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ Failed to queue HDD digest to #homelab: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
