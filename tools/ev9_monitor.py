"""Used Kia EV9 listing monitor for Ivy-Gemini.

Searches MarketCheck's Cars API for 2024 Kia EV9 Land/GT/GT-Line listings under
budget/mileage near Redwood City, CA (zip 94061, 75mi radius), diffs against
previously-seen listings (added / gone / price-changed), ranks them into
in-budget picks plus a watch list, guarantees a blue option gets surfaced when
one exists, generates a buy-timing tip, and renders a Discord digest.

This is a SECOND, INDEPENDENT implementation of the monitor that also runs on
Ivy (Claude)'s Pixel VM (`~/projects/car_monitor/search.py`, systemd
`car-monitor.timer`). The two are deliberately non-interfering: separate state
directories, separate schedules, separate Discord posts. The point is
resilience — this copy keeps running when the Pixel VM is off. Logic is ported
from that script (rules, weights, thresholds and their rationale are
preserved); the transport is `requests` to match this codebase.

Cost note: this doubles the account's daily MarketCheck search usage (one
search call from each implementation). Per-VIN fair-price lookups are only
made when a digest is actually posted, not on every capture.

State lives in $CAR_MONITOR_DIR (default /app/car_monitor_data), bind-mounted
from ./car_monitor_data on Host2 so it survives image rebuilds.
Credential: $MARKETCHECK_API_KEY.
"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

log = logging.getLogger("ev9_monitor")

API_KEY = os.environ.get("MARKETCHECK_API_KEY", "")
if not API_KEY and os.path.exists("/secrets/env.json"):
    try:
        import json
        with open("/secrets/env.json") as f:
            API_KEY = json.load(f).get("MARKETCHECK_API_KEY", "")
    except Exception:
        pass

DATA_DIR = Path(os.environ.get("CAR_MONITOR_DIR", "/workspace/car_monitor_data"))
LISTINGS_FILE = DATA_DIR / "listings.json"
PRICE_HISTORY_FILE = DATA_DIR / "price_history.jsonl"
SOLD_FILE = DATA_DIR / "sold_estimate.jsonl"

BUDGET_MAX = 50000
MILES_MAX = 50000
TARGET_TRIMS = {"land", "gt", "gt-line"}

# Cast a slightly wider net than the display filters (BUDGET_MAX / MILES_MAX)
# so the watch-list band downstream still has data to work with. Redwood City,
# CA = zip 94061; 75mi radius covers the Bay Area comfortably under the free
# tier's 100mi cap.
SEARCH_ENDPOINT = "https://api.marketcheck.com/v2/search/car/active"
SEARCH_ZIP = "94061"
SEARCH_RADIUS = 75
QUERY_PRICE_MAX = 56000
QUERY_MILES_MAX = 60000
TIMEOUT = 30

# MarketCheck Price™ — a per-VIN valuation call, separate from the bulk search.
# Path/params verified against live docs 2026-07-18: needs vin, miles,
# dealer_type (franchise|independent — not available per-listing, and every
# dealer source in this market is a branded franchise site) and a location.
# Response on this tier is just {"marketcheck_price": int, "msrp": int}.
PRICE_ENDPOINT = "https://api.marketcheck.com/v2/predict/car/us/marketcheck_price"
PRICE_TIMEOUT = 15
FAIR_PRICE_AT_MARKET_BAND = 750  # within this $ delta, call it "at market"

# Ranking. Price and mileage are the primary signals (dollar-for-dollar and a
# fraction of a dollar per mile, so they dominate). Trim/color/history are soft
# tie-breakers as flat dollar-equivalent bonuses — big enough to reorder close
# matches, never big enough to override a real price or mileage gap.
RANK_MILES_WEIGHT = 0.15      # $ equivalent penalty per mile
RANK_BONUS_GT = 2500          # GT / GT-Line over Land
RANK_BONUS_BLUE = 2000        # any blue-ish exterior color
RANK_BONUS_CLEAN_TITLE = 600  # carfax_clean_title is True (not just non-False)
RANK_BONUS_1_OWNER = 400      # carfax_1_owner is True
TOP_N_DISPLAY = 3


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _listing_id(title, price, miles) -> str:
    """Stable fingerprint for dedup — fallback used only when a VIN is missing."""
    raw = f"{str(title).lower().strip()}|{price}|{miles}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _is_target_trim(title) -> bool:
    t = str(title or "").lower()
    return any(trim in t for trim in TARGET_TRIMS)


def _normalize_trim(raw) -> str:
    """Map a MarketCheck build.trim string onto the canonical Land/GT/GT-Line
    set that the price history and digest group by."""
    t = (raw or "").lower()
    if "gt-line" in t or "gt line" in t:
        return "GT-Line"
    if "gt" in t and "land" not in t:
        return "GT"
    if "land" in t:
        return "Land"
    return (raw or "Unknown").strip() or "Unknown"


def _parse_price(s):
    """Parse '$42,999' -> 42999."""
    if s is None or s == "":
        return None
    cleaned = str(s).replace("$", "").replace(",", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _parse_miles(s):
    """Parse '17,975 mi' or '17K' -> 17975 / 17000."""
    if s is None or s == "":
        return None
    raw = str(s).lower()
    if "k" in raw:
        try:
            return int(float(raw.replace("k", "").replace(",", "").strip()) * 1000)
        except ValueError:
            pass
    cleaned = raw.replace(",", "").replace("mi", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# MarketCheck API
# --------------------------------------------------------------------------

def marketcheck_search(api_key: str = "") -> list:
    """Query MarketCheck's active-inventory endpoint for used 2024 Kia EV9
    listings near Redwood City. Returns the raw listing records (dicts), or []
    on any failure — never raises, so one bad API day can't kill the job.

    Paginates defensively (50 rows per call) even though current inventory fits
    in a single page.
    """
    key = api_key or API_KEY
    if not key:
        log.error("MARKETCHECK_API_KEY is not set")
        return []

    params = {
        "api_key": key,
        "make": "Kia",
        "model": "EV9",
        "year": "2024",
        "car_type": "used",
        "zip": SEARCH_ZIP,
        "radius": SEARCH_RADIUS,
        "price_range": f"0-{QUERY_PRICE_MAX}",
        "miles_range": f"0-{QUERY_MILES_MAX}",
        "rows": 50,
        "start": 0,
    }

    all_records = []
    while True:
        try:
            r = requests.get(SEARCH_ENDPOINT, params=params, timeout=TIMEOUT)
            data = r.json()
        except Exception as e:  # noqa: BLE001
            # Deliberately log the exception type only — the request URL
            # carries the API key.
            log.error("MarketCheck search failed: %s", type(e).__name__)
            break

        batch = data.get("listings", []) or []
        all_records.extend(batch)
        num_found = data.get("num_found", 0)
        params["start"] += len(batch)
        if not batch or params["start"] >= num_found or len(all_records) >= 200:
            break

    return all_records


def fetch_fair_price(vin: str, miles, api_key: str = ""):
    """MarketCheck's predicted fair price for one VIN. Returns an int or None
    on any failure (missing VIN, API error, no prediction) — callers must treat
    None as "no tag", never crash a digest over one failed lookup.

    Only called for listings actually displayed (top-3 + blue pick): it's a
    per-VIN call, not a bulk search, and the account's tier is shared with the
    daily search call from both monitor implementations.
    """
    key = api_key or API_KEY
    if not key or not vin or miles is None:
        return None
    params = {
        "api_key": key,
        "vin": vin,
        "miles": miles,
        "dealer_type": "franchise",
        "zip": SEARCH_ZIP,
    }
    try:
        r = requests.get(PRICE_ENDPOINT, params=params, timeout=PRICE_TIMEOUT)
        return r.json().get("marketcheck_price")
    except Exception as e:  # noqa: BLE001
        log.warning("fair-price lookup failed for %s: %s", vin, type(e).__name__)
        return None


def fair_price_tag(price, fair_price) -> str:
    """Render a '$X under/over/at market' tag, or '' if no prediction."""
    if fair_price is None or price is None:
        return ""
    delta = fair_price - price  # positive = listed below prediction = good deal
    if abs(delta) <= FAIR_PRICE_AT_MARKET_BAND:
        return " [at market]"
    if delta > 0:
        return f" [${delta:,} under market]"
    return f" [${abs(delta):,} over market]"


def build_listings(raw_records: list) -> list:
    """Map MarketCheck records into the internal listing shape, filtered to the
    target trims (Land / GT / GT-Line — MarketCheck also returns Wind / Light).

    Dedup key is the VIN when present, falling back to a title+price+miles hash.
    """
    listings = []
    seen = set()

    for r in raw_records:
        build = r.get("build", {}) or {}
        dealer = r.get("dealer", {}) or {}

        raw_trim = build.get("trim") or ""
        if not _is_target_trim(raw_trim):
            continue
        trim = _normalize_trim(raw_trim)

        price = _parse_price(r.get("price"))
        if price is None:
            continue
        miles = _parse_miles(r.get("miles"))

        vin = (r.get("vin") or "").strip()
        title = r.get("heading") or f"2024 Kia EV9 {raw_trim}".strip()
        # Color is tracked and displayed but never used to exclude a listing —
        # blue-only hard filtering is intentionally OFF (Ryan's call 2026-07-07).
        color = r.get("exterior_color") or None

        city, state = dealer.get("city"), dealer.get("state")
        if city and state:
            location = f"{city}, {state}"
        elif state:
            location = state
        else:
            location = None

        lid = vin if vin else _listing_id(title, price, miles or 0)
        if lid in seen:
            continue
        seen.add(lid)

        listings.append({
            "id": lid,
            "title": title,
            "price": price,
            "miles": miles,
            "trim": trim,
            "color": color,
            "location": location,
            "url": r.get("vdp_url") or "",
            "source": r.get("source") or dealer.get("name") or "MarketCheck",
            "vin": vin or None,
            # Carfax signals — real fields, confirmed present in MarketCheck's
            # response. "clean_title" is False for many listings rather than
            # only True/None, so False/None is neutral-unconfirmed, not a
            # negative signal; only an explicit True earns a ranking bonus.
            "carfax_clean_title": r.get("carfax_clean_title"),
            "carfax_1_owner": r.get("carfax_1_owner"),
        })

    return listings


# --------------------------------------------------------------------------
# State + diffing
# --------------------------------------------------------------------------

def load_listings() -> dict:
    if LISTINGS_FILE.exists():
        try:
            return json.loads(LISTINGS_FILE.read_text())
        except Exception:  # noqa: BLE001
            log.exception("listings.json is unreadable — starting from empty state")
    return {}


def save_listings(listings_map: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LISTINGS_FILE.write_text(json.dumps(listings_map, indent=2))


def diff_listings(old_map: dict, new_listings: list, today_str: str):
    """Compare new listings against stored state. Returns (updated_map, diff)."""
    new_map = {l["id"]: l for l in new_listings}
    new_ids = set(new_map)
    old_ids = set(old_map)

    added, price_drops, price_ups, gone, unchanged = [], [], [], [], []

    for lid in new_ids:
        l = new_map[lid]
        if lid not in old_ids:
            l["first_seen"] = today_str
            l["last_seen"] = today_str
            l["status"] = "active"
            added.append(l)
            continue

        old = old_map[lid]
        l["first_seen"] = old.get("first_seen", today_str)
        l["last_seen"] = today_str
        l["status"] = "active"
        old_price, new_price = old.get("price"), l.get("price")
        if old_price and new_price and old_price != new_price:
            delta = new_price - old_price
            l["price_delta"] = delta
            l["price_prev"] = old_price
            (price_drops if delta < 0 else price_ups).append(l)
        else:
            unchanged.append(l)

    for lid in old_ids - new_ids:
        old = old_map[lid]
        if old.get("status") == "gone":
            continue
        first_seen = old.get("first_seen", today_str)
        try:
            days = (date.fromisoformat(today_str) - date.fromisoformat(first_seen)).days
        except Exception:  # noqa: BLE001
            days = 0
        gone.append({**old, "date_gone": today_str, "days_listed": days, "status": "gone"})

    updated_map = {l["id"]: l for l in added + price_drops + price_ups + unchanged}
    for g in gone:
        updated_map[g["id"]] = g

    return updated_map, {
        "added": added,
        "price_drops": price_drops,
        "price_ups": price_ups,
        "gone": gone,
        "unchanged": unchanged,
    }


def append_price_history(all_listings: list, today_str: str) -> None:
    active = [l for l in all_listings if l.get("status") != "gone"]
    prices = [l["price"] for l in active if l.get("price")]
    if not prices:
        return
    land = [l["price"] for l in active if l.get("price") and l.get("trim") == "Land"]
    gt = [l["price"] for l in active if l.get("price") and l.get("trim") in ("GT", "GT-Line")]
    entry = {
        "date": today_str,
        "count": len(active),
        "avg_price": round(sum(prices) / len(prices)),
        "min_price": min(prices),
        "max_price": max(prices),
        "by_trim": {
            "Land": {"count": len(land),
                     "avg": round(sum(land) / len(land)) if land else None,
                     "floor": min(land) if land else None},
            "GT": {"count": len(gt),
                   "avg": round(sum(gt) / len(gt)) if gt else None,
                   "floor": min(gt) if gt else None},
        },
        "listings": [{"id": l["id"], "price": l["price"], "trim": l.get("trim")}
                     for l in active],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PRICE_HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def append_sold_estimates(gone_listings: list) -> None:
    if not gone_listings:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOLD_FILE, "a") as f:
        for g in gone_listings:
            f.write(json.dumps(g) + "\n")


def load_price_history() -> list:
    if not PRICE_HISTORY_FILE.exists():
        return []
    entries = []
    for line in PRICE_HISTORY_FILE.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return entries


# --------------------------------------------------------------------------
# Filtering + ranking
# --------------------------------------------------------------------------

def filter_in_budget(listings: list) -> list:
    return [l for l in listings
            if l.get("price") and l["price"] <= BUDGET_MAX
            and (not l.get("miles") or l["miles"] <= MILES_MAX)]


def filter_watch_list(listings: list) -> list:
    """Close, but over budget or over the mileage cap."""
    result = []
    for l in listings:
        p, m = l.get("price"), l.get("miles")
        if p and BUDGET_MAX < p <= BUDGET_MAX + 5000:
            result.append(l)
        elif m and m > MILES_MAX and p and p <= BUDGET_MAX:
            result.append(l)
    return result


def rank_score(listing: dict) -> float:
    """Higher is better. Price/miles pull the score down (cheaper + lower-miles
    wins); trim/color/history push it up as tie-breaking bonuses."""
    price = listing.get("price") or QUERY_PRICE_MAX
    miles = listing.get("miles")
    if miles is None:
        miles = QUERY_MILES_MAX  # unknown mileage = worst case, not a free pass

    score = -price - (RANK_MILES_WEIGHT * miles)

    if listing.get("trim") in ("GT", "GT-Line"):
        score += RANK_BONUS_GT
    if is_blue(listing):
        score += RANK_BONUS_BLUE
    # NOTE: "clean title" is a registry-brand status (not salvage, flood,
    # lemon-buyback or rebuilt) — it is NOT "no accident history". A car can
    # have a clean title and a repaired accident. MarketCheck's tier exposes no
    # accident field at all, so this stays a weak soft signal.
    if listing.get("carfax_clean_title") is True:
        score += RANK_BONUS_CLEAN_TITLE
    if listing.get("carfax_1_owner") is True:
        score += RANK_BONUS_1_OWNER

    return score


def rank_best(listings: list, n: int = TOP_N_DISPLAY) -> list:
    return sorted(listings, key=rank_score, reverse=True)[:n]


def is_blue(listing: dict) -> bool:
    return "blue" in (listing.get("color") or "").lower()


def pick_blue_bonus(shown: list, pool: list):
    """If nothing in `shown` is blue but the pool has a blue option, return the
    best-scoring blue listing as an extra pick (Ryan's request 2026-07-16 —
    "make sure at least one blue option gets included every day"). Never
    displaces a genuinely better non-blue car. None if not needed/possible."""
    if any(is_blue(l) for l in shown):
        return None
    candidates = [l for l in pool if is_blue(l) and l not in shown]
    if not candidates:
        return None
    return max(candidates, key=rank_score)


# --------------------------------------------------------------------------
# Digest formatting
# --------------------------------------------------------------------------

def _format_listing_line(l: dict, show_color_loc: bool = True, fair_price=None) -> str:
    """One listing as a single Discord line — shared by the in-budget, watch
    list and blue-pick renderers so the format stays consistent."""
    price_str = f"${l['price']:,}"
    miles_str = f"{l['miles']:,} mi" if l.get("miles") else "mi unknown"
    trim_str = l.get("trim", "")
    color_str = f", {l['color']}" if show_color_loc and l.get("color") else ""
    loc_str = f" — {l['location']}" if show_color_loc and l.get("location") else ""
    tag = ""
    if l.get("price_delta") and l["price_delta"] < 0:
        tag = f" [DOWN ${abs(l['price_delta']):,}]"
    tag += fair_price_tag(l.get("price"), fair_price)
    url = l.get("url", "")
    link = f" — [View listing]({url})" if url else ""
    return f"{price_str} — {trim_str}, {miles_str}{color_str}{loc_str}{tag}{link}"


def format_digest(diff: dict, all_listings: list, today_str: str,
                  price_history: list, with_fair_price: bool = False) -> str:
    """Render a clean, high-signal Discord digest focusing on market momentum,
    top value/blue picks, and week-over-week deltas."""
    dt_str = datetime.strptime(today_str, "%Y-%m-%d").strftime("%a %b %d")
    lines = [f"🚘 **EV9 Market Brief** · {dt_str}"]

    active = [l for l in all_listings if l.get("price") and l.get("status") != "gone"]
    if not active:
        lines.append("⚠️ No active EV9 listings found in current search area.")
        return "\n".join(lines)

    land_prices = [l["price"] for l in active if l.get("trim") == "Land"]
    gt_prices = [l["price"] for l in active if l.get("trim") in ("GT", "GT-Line")]
    
    land_floor = f"${min(land_prices)/1000:.1f}k" if land_prices else "N/A"
    gt_floor = f"${min(gt_prices)/1000:.1f}k" if gt_prices else "N/A"

    # Compute 30d pace & average days on lot
    pace_str = ""
    if len(price_history) >= 2:
        prev = price_history[max(0, len(price_history) - 4)]  # ~1 month ago if weekly
        delta = round((sum(l["price"] for l in active) / len(active)) - prev.get("avg_price", 0))
        if delta < 0:
            pace_str = f"🔻 -${abs(delta):,}/mo"
        elif delta > 0:
            pace_str = f"🔺 +${delta:,}/mo"
        else:
            pace_str = "flat"

    days_listed = [l.get("days_listed", 0) for l in active if l.get("days_listed")]
    avg_days = round(sum(days_listed) / len(days_listed)) if days_listed else 0
    pace_display = f" | 30d Pace: **{pace_str}**" if pace_str else ""
    days_display = f" · Avg on Lot: **{avg_days}d**" if avg_days else ""

    lines.append(f"📊 **{len(active)} Active** | Floors: **{land_floor}** (Land) · **{gt_floor}** (GT){pace_display}{days_display}\n")

    # Top Value & Top Blue Picks
    in_budget = filter_in_budget(active)
    if in_budget:
        ranked = rank_best(in_budget, n=5)
        top_value = ranked[0]
        
        v_url = top_value.get("url", "")
        v_link = f" [[View]]({v_url})" if v_url else ""
        lines.append(f"🏆 **Top Value:** **${top_value['price']:,}** — {top_value.get('trim','')} ({top_value.get('miles',0):,} mi, {top_value.get('color','')}, {top_value.get('location','')}){v_link}")

        candidates_blue = [l for l in in_budget if is_blue(l)]
        if candidates_blue:
            top_blue = max(candidates_blue, key=rank_score)
            if top_blue.get("vin") != top_value.get("vin"):
                b_url = top_blue.get("url", "")
                b_link = f" [[View]]({b_url})" if b_url else ""
                lines.append(f"🔵 **Top Blue:** **${top_blue['price']:,}** — {top_blue.get('trim','')} ({top_blue.get('miles',0):,} mi, {top_blue.get('color','')}, {top_blue.get('location','')}){b_link}")
        lines.append("")
    else:
        lines.append("⚠️ **In-Budget Picks:** None found under budget/mileage caps.\n")

    # 7-Day Rolling Movement Windows
    today_dt = date.fromisoformat(today_str)
    seven_days_ago = (today_dt - timedelta(days=7)).isoformat()

    new_7d = [l for l in active if l.get("first_seen", "") >= seven_days_ago]
    drops_7d = [l for l in active if l.get("price_delta", 0) < 0]
    if not drops_7d and diff.get("price_drops"):
        drops_7d = diff["price_drops"]

    sold_7d = []
    if SOLD_FILE.exists():
        try:
            with open(SOLD_FILE) as f:
                for line in f:
                    if line.strip():
                        g = json.loads(line)
                        if g.get("date_gone", "") >= seven_days_ago:
                            sold_7d.append(g)
        except Exception:
            pass
    if not sold_7d and diff.get("gone"):
        sold_7d = diff["gone"]

    # Deltas & Movement Highlights
    has_movement = False
    if drops_7d:
        has_movement = True
        lines.append(f"📉 **Price Cuts This Week ({len(drops_7d)}):**")
        for d in drops_7d[:3]:
            u = d.get("url", "")
            l_str = f" [[View]]({u})" if u else ""
            lines.append(f"  • **${d['price']:,}** (🔻 -${abs(d.get('price_delta',0)):,}) — {d.get('trim','')}, {d.get('miles',0):,} mi ({d.get('location','')}){l_str}")
        lines.append("")

    if new_7d:
        has_movement = True
        lines.append(f"✨ **New Arrivals This Week ({len(new_7d)}):**")
        for a in new_7d[:3]:
            u = a.get("url", "")
            l_str = f" [[View]]({u})" if u else ""
            lines.append(f"  • **${a['price']:,}** — {a.get('trim','')}, {a.get('miles',0):,} mi, {a.get('color','')} ({a.get('location','')}){l_str}")
        lines.append("")

    if sold_7d:
        has_movement = True
        lines.append(f"🏷️ **Sold / Delisted This Week ({len(sold_7d)}):**")
        for g in sold_7d[:3]:
            lines.append(f"  • **${g.get('price',0):,}** — {g.get('trim','')}, listed {g.get('days_listed',0)}d")
        lines.append("")

    if not has_movement:
        lines.append("⚡ **Market Movement:** Steady — no price cuts or new arrivals in the past 7 days.")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def run_capture(save: bool = True, with_fair_price: bool = False) -> dict:
    """One monitor pass: search, diff against stored state, optionally persist,
    and render the digest. Returns {"ok", "digest", "counts", "tracked"}.

    `save=False` gives a read-only preview (diffs against state but writes
    nothing), which is what the on-demand tool uses so an ad-hoc question can't
    consume the daily diff.
    """
    if not API_KEY:
        return {"ok": False, "error": "MARKETCHECK_API_KEY is not set."}

    today_str = date.today().isoformat()
    raw_records = marketcheck_search()
    if not raw_records:
        return {"ok": False,
                "error": "MarketCheck returned no records (API error or empty inventory).",
                "raw_count": 0}

    listings = build_listings(raw_records)
    old_map = load_listings()
    updated_map, diff = diff_listings(old_map, listings, today_str)
    all_listings = list(updated_map.values())

    if save:
        save_listings(updated_map)
        append_price_history(all_listings, today_str)
        append_sold_estimates(diff["gone"])

    price_history = load_price_history()
    digest = format_digest(diff, all_listings, today_str, price_history,
                           with_fair_price=with_fair_price)

    return {
        "ok": True,
        "date": today_str,
        "digest": digest,
        "saved": save,
        "raw_count": len(raw_records),
        "tracked": len([l for l in all_listings if l.get("status") != "gone"]),
        "counts": {
            "added": len(diff["added"]),
            "price_drops": len(diff["price_drops"]),
            "price_ups": len(diff["price_ups"]),
            "gone": len(diff["gone"]),
        },
    }


def ev9_monitor(action: str = "report", days: int = 14) -> dict:
    """On-demand EV9 monitor tool. See the tool declaration in bot.py.

    "report"  — live MarketCheck search + digest, read-only (state untouched).
    "history" — recent price-history entries from stored state, no API call.
    """
    try:
        if action == "report":
            return run_capture(save=False, with_fair_price=True)

        if action == "history":
            entries = load_price_history()[-max(1, days):]
            if not entries:
                return {"ok": True, "history": [],
                        "note": "No price history captured yet."}
            return {"ok": True, "history": [
                {"date": e.get("date"), "count": e.get("count"),
                 "avg_price": e.get("avg_price"), "min_price": e.get("min_price"),
                 "by_trim": e.get("by_trim")} for e in entries]}

        return {"ok": False, "error": f"Unknown action: {action}"}
    except Exception as e:  # noqa: BLE001
        log.exception("ev9_monitor failed")
        return {"ok": False, "error": repr(e)}


def generate_trend_plot(output_path: str = "/tmp/ev9_trend.png", days: int = 30) -> str:
    """Render a price history trend chart using matplotlib and return the file path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    entries = load_price_history()
    if not entries:
        return ""

    recent = entries[-max(1, days):]
    if len(recent) < 2:
        return ""

    dates = []
    land_floors, land_avgs = [], []
    gt_floors, gt_avgs = [], []
    overall_avgs = []

    for e in recent:
        d_str = e.get("date")
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d")
        except Exception:
            continue
        dates.append(d)
        overall_avgs.append(e.get("avg_price"))
        bt = e.get("by_trim", {}) or {}
        l_info = bt.get("Land", {}) or {}
        g_info = bt.get("GT", {}) or {}
        land_floors.append(l_info.get("floor"))
        land_avgs.append(l_info.get("avg"))
        gt_floors.append(g_info.get("floor"))
        gt_avgs.append(g_info.get("avg"))

    if not dates:
        return ""

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor("#1e1e24")
    ax.set_facecolor("#1e1e24")

    # Plot lines
    if any(land_floors):
        ax.plot(dates, land_floors, label="Land Floor", color="#4dabf7", linestyle="--", marker="o", markersize=4)
    if any(land_avgs):
        ax.plot(dates, land_avgs, label="Land Avg", color="#1c7ed6", linewidth=2)
    if any(gt_floors):
        ax.plot(dates, gt_floors, label="GT Floor", color="#ff8787", linestyle="--", marker="s", markersize=4)
    if any(gt_avgs):
        ax.plot(dates, gt_avgs, label="GT Avg", color="#f03e3e", linewidth=2)

    ax.set_title("Kia EV9 Price Trends (Bay Area)", color="#ffffff", fontsize=13, fontweight="bold", pad=12)
    ax.set_ylabel("Price ($)", color="#ced4da", fontsize=10)
    ax.yaxis.set_major_formatter("${x:,.0f}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.tick_params(colors="#ced4da", labelsize=9)
    ax.grid(True, linestyle=":", alpha=0.3, color="#868e96")

    for spine in ax.spines.values():
        spine.set_color("#495057")

    legend = ax.legend(facecolor="#2b2b36", edgecolor="#495057", labelcolor="#ffffff", fontsize=9)
    fig.tight_layout()

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    return str(output_path)
