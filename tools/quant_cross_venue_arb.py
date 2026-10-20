#!/usr/bin/env python3
"""Track 1: Cross-Venue Prediction Market Arbitrage Engine (Kalshi vs. Polymarket).

Scans active Kalshi and Polymarket order books across:
1. Live head-to-head sports games (NFL, MLB, NBA, NHL) with league-scoped, date-verified canonical team matching.
2. Same-date daily crypto strike ladders (BTC, ETH) across Kalshi (KXBTCD/KXETHD) and Polymarket.
3. Macro and political events (Federal Reserve, CPI inflation, elections).

Identifies synthetic risk-free spreads via:
- Taker-Taker Arbitrage: Kalshi_Ask + Polymarket_Ask + $0.007 Fee <= $0.995
- Maker-Taker Arbitrage: Kalshi_Maker_Bid(+1c, $0 fee) + Polymarket_Ask <= $0.985 (when Kalshi spread >= 3c)

Executes simulated paper trades into /workspace/data/quant_paper.db under an isolated $1,000.00 paper portfolio.
"""

import json
import os
import re
import sqlite3
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/quant_paper.db")
MAX_HORIZON_DAYS = 14.0

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/events"
POLYMARKET_CLOB = "https://clob.polymarket.com"

# Target cross-venue event matching patterns for macro/politics
EVENT_MATCH_PATTERNS = [
    {
        "category": "fed_funds",
        "keywords": ["fed", "rate", "interest rate", "basis points", "bps", "fomc"],
        "min_edge": 0.035
    },
    {
        "category": "cpi_inflation",
        "keywords": ["cpi", "inflation", "headline cpi", "core cpi"],
        "min_edge": 0.035
    },
    {
        "category": "us_politics",
        "keywords": ["presidential election", "popular vote", "senate", "house", "electoral college"],
        "min_edge": 0.040
    }
]

MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"
}

MONTH_NAME_TO_NUM = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12"
}

LEAGUE_TEAMS = {
    "NFL": {
        "crd": "ARI", "cardinals": "ARI", "arizona": "ARI", "ari": "ARI",
        "falcons": "ATL", "atlanta": "ATL", "atl": "ATL",
        "ravens": "BAL", "baltimore": "BAL", "bal": "BAL",
        "bills": "BUF", "buffalo": "BUF", "buf": "BUF",
        "panthers": "CAR", "carolina": "CAR", "car": "CAR",
        "bears": "CHI", "chicago": "CHI", "chi": "CHI",
        "bengals": "CIN", "cincinnati": "CIN", "cin": "CIN",
        "browns": "CLE", "cleveland": "CLE", "cle": "CLE",
        "cowboys": "DAL", "dallas": "DAL", "dal": "DAL",
        "broncos": "DEN", "denver": "DEN", "den": "DEN",
        "lions": "DET", "detroit": "DET", "det": "DET",
        "packers": "GB", "green bay": "GB", "gb": "GB",
        "texans": "HOU", "houston": "HOU", "hou": "HOU",
        "colts": "IND", "indianapolis": "IND", "ind": "IND",
        "jaguars": "JAC", "jacksonville": "JAC", "jac": "JAC", "jax": "JAC",
        "chiefs": "KC", "kansas city": "KC", "kc": "KC",
        "raiders": "LV", "las vegas": "LV", "lv": "LV",
        "chargers": "LAC", "los angeles chargers": "LAC", "la chargers": "LAC", "lac": "LAC",
        "rams": "LAR", "los angeles rams": "LAR", "la rams": "LAR", "lar": "LAR",
        "dolphins": "MIA", "miami": "MIA", "mia": "MIA",
        "vikings": "MIN", "minnesota": "MIN", "min": "MIN",
        "patriots": "NE", "new england": "NE", "ne": "NE",
        "saints": "NO", "new orleans": "NO", "no": "NO",
        "giants": "NYG", "new york giants": "NYG", "ny giants": "NYG", "nyg": "NYG",
        "jets": "NYJ", "new york jets": "NYJ", "ny jets": "NYJ", "nyj": "NYJ",
        "eagles": "PHI", "philadelphia": "PHI", "phi": "PHI",
        "steelers": "PIT", "pittsburgh": "PIT", "pit": "PIT",
        "49ers": "SF", "san francisco": "SF", "sf": "SF",
        "seahawks": "SEA", "seattle": "SEA", "sea": "SEA",
        "buccaneers": "TB", "tampa bay": "TB", "tb": "TB",
        "titans": "TEN", "tennessee": "TEN", "ten": "TEN",
        "commanders": "WAS", "washington": "WAS", "was": "WAS",
    },
    "MLB": {
        "dodgers": "LAD", "los angeles d": "LAD", "lad": "LAD",
        "brewers": "MIL", "milwaukee": "MIL", "mil": "MIL",
        "guardians": "CLE", "cleveland": "CLE", "cle": "CLE",
        "rays": "TB", "tampa bay": "TB", "tb": "TB",
        "yankees": "NYY", "new york y": "NYY", "nyy": "NYY",
        "mets": "NYM", "new york m": "NYM", "nym": "NYM",
        "phillies": "PHI", "philadelphia": "PHI", "phi": "PHI",
        "padres": "SD", "san diego": "SD", "sd": "SD",
        "tigers": "DET", "detroit": "DET", "det": "DET",
        "royals": "KC", "kansas city": "KC", "kc": "KC",
    },
    "NBA": {
        "magic": "ORL", "orlando": "ORL", "orl": "ORL",
        "cavaliers": "CLE", "cleveland": "CLE", "cle": "CLE",
        "bucks": "MIL", "milwaukee": "MIL", "mil": "MIL",
        "hornets": "CHA", "charlotte": "CHA", "cha": "CHA",
        "bulls": "CHI", "chicago": "CHI", "chi": "CHI",
        "nuggets": "DEN", "denver": "DEN", "den": "DEN",
        "mavericks": "DAL", "dallas": "DAL", "dal": "DAL",
        "rockets": "HOU", "houston": "HOU", "hou": "HOU",
        "thunder": "OKC", "oklahoma city": "OKC", "okc": "OKC",
        "hawks": "ATL", "atlanta": "ATL", "atl": "ATL",
        "nets": "BKN", "brooklyn": "BKN", "bkn": "BKN",
        "wizards": "WAS", "washington": "WAS", "was": "WAS",
        "timberwolves": "MIN", "minnesota": "MIN", "min": "MIN",
        "knicks": "NYK", "new york": "NYK", "nyk": "NYK",
        "spurs": "SAS", "san antonio": "SAS", "sas": "SAS",
        "jazz": "UTA", "utah": "UTA", "uta": "UTA",
        "warriors": "GSW", "golden state": "GSW", "gsw": "GSW",
        "lakers": "LAL", "los angeles l": "LAL", "lal": "LAL",
        "kings": "SAC", "sacramento": "SAC", "sac": "SAC",
        "trail blazers": "POR", "portland": "POR", "por": "POR",
        "heat": "MIA", "miami": "MIA", "mia": "MIA",
        "celtics": "BOS", "boston": "BOS", "bos": "BOS",
        "suns": "PHX", "phoenix": "PHX", "phx": "PHX",
        "pelicans": "NOP", "new orleans": "NOP", "nop": "NOP",
        "pistons": "DET", "detroit": "DET", "det": "DET",
        "clippers": "LAC", "los angeles c": "LAC", "lac": "LAC",
        "raptors": "TOR", "toronto": "TOR", "tor": "TOR",
        "76ers": "PHI", "philadelphia": "PHI", "phi": "PHI",
        "pacers": "IND", "indiana": "IND", "ind": "IND",
        "grizzlies": "MEM", "memphis": "MEM", "mem": "MEM",
    },
    "NHL": {
        "ducks": "ANA", "anaheim": "ANA", "ana": "ANA",
        "bruins": "BOS", "boston": "BOS", "bos": "BOS",
        "sabres": "BUF", "buffalo": "BUF", "buf": "BUF",
        "flames": "CGY", "calgary": "CGY", "cgy": "CGY", "cal": "CGY",
        "hurricanes": "CAR", "carolina": "CAR", "car": "CAR",
        "blackhawks": "CHI", "chicago": "CHI", "chi": "CHI",
        "avalanche": "COL", "colorado": "COL", "col": "COL",
        "blue jackets": "CBJ", "columbus": "CBJ", "cbj": "CBJ",
        "stars": "DAL", "dallas": "DAL", "dal": "DAL",
        "red wings": "DET", "detroit": "DET", "det": "DET",
        "oilers": "EDM", "edmonton": "EDM", "edm": "EDM",
        "panthers": "FLA", "florida": "FLA", "fla": "FLA",
        "kings": "LAK", "los angeles": "LAK", "la": "LAK", "lak": "LAK",
        "wild": "MIN", "minnesota": "MIN", "min": "MIN",
        "canadiens": "MTL", "montreal": "MTL", "mtl": "MTL", "mon": "MTL",
        "predators": "NSH", "nashville": "NSH", "nsh": "NSH",
        "devils": "NJD", "new jersey": "NJD", "nj": "NJD", "njd": "NJD",
        "islanders": "NYI", "new york i": "NYI", "nyi": "NYI",
        "rangers": "NYR", "new york r": "NYR", "nyr": "NYR",
        "senators": "OTT", "ottawa": "OTT", "ott": "OTT",
        "flyers": "PHI", "philadelphia": "PHI", "phi": "PHI",
        "penguins": "PIT", "pittsburgh": "PIT", "pit": "PIT",
        "sharks": "SJS", "san jose": "SJS", "sj": "SJS", "sjs": "SJS",
        "kraken": "SEA", "seattle": "SEA", "sea": "SEA",
        "blues": "STL", "st louis": "STL", "st. louis": "STL", "stl": "STL",
        "lightning": "TB", "tampa bay": "TB", "tb": "TB", "tbl": "TB",
        "maple leafs": "TOR", "toronto": "TOR", "tor": "TOR",
        "utah": "UTA", "utah hockey club": "UTA", "uta": "UTA",
        "canucks": "VAN", "vancouver": "VAN", "van": "VAN",
        "golden knights": "VGK", "vegas": "VGK", "vgk": "VGK", "las": "VGK",
        "capitals": "WSH", "washington": "WSH", "wsh": "WSH", "was": "WSH",
        "jets": "WPG", "winnipeg": "WPG", "wpg": "WPG",
    }
}

# Fallback combined dictionary for backward compatibility
CANONICAL_TEAMS = {}
for _lg in ("NFL", "MLB", "NBA"):
    CANONICAL_TEAMS.update(LEAGUE_TEAMS[_lg])

def init_db(conn):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cross_venue_portfolio (
                id INTEGER PRIMARY KEY,
                starting_balance REAL,
                cash REAL,
                realized_pnl REAL,
                last_updated TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cross_venue_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                event_name TEXT,
                category TEXT,
                kalshi_ticker TEXT,
                kalshi_side TEXT,
                kalshi_price REAL,
                polymarket_id TEXT,
                polymarket_side TEXT,
                polymarket_price REAL,
                contracts INTEGER,
                sum_cost REAL,
                locked_profit REAL,
                net_edge REAL,
                status TEXT,
                settled_at TEXT,
                realized_pnl REAL
            )
        """)
        cur = conn.execute("SELECT id, starting_balance, cash FROM cross_venue_portfolio WHERE id = 1")
        row = cur.fetchone()
        if not row:
            now_str = datetime.now(PT).isoformat()
            conn.execute(
                "INSERT INTO cross_venue_portfolio (id, starting_balance, cash, realized_pnl, last_updated) VALUES (1, 1000.0, 1000.0, 0.0, ?)",
                (now_str,)
            )

def fetch_json(url, timeout=7):
    headers = {"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def parse_kalshi_date(event_ticker):
    """Extract YYYY-MM-DD from Kalshi event ticker (e.g. KXNFLGAME-26OCT11PHIJAC -> 2026-10-11)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if m:
        yy, mon, dd = m.groups()
        if mon in MONTH_MAP:
            return f"20{yy}-{MONTH_MAP[mon]}-{dd}"
    return None

def parse_poly_title_date(title):
    """Extract YYYY-MM-DD from Polymarket title like 'Bitcoin above ___ on October 14?'."""
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
        title.lower()
    )
    if m:
        mon_name, day_str = m.groups()
        mon_num = MONTH_NAME_TO_NUM[mon_name]
        year = datetime.now(PT).year
        return f"{year}-{mon_num}-{int(day_str):02d}"
    return None

def extract_teams(text, league=None):
    """Extract canonical team codes from text using league-scoped dictionary when provided."""
    text = text.lower()
    mapping = LEAGUE_TEAMS.get(league, CANONICAL_TEAMS) if league else CANONICAL_TEAMS
    found = set()
    for name, code in mapping.items():
        if " " in name and name in text:
            found.add(code)
    for tok in re.findall(r"[a-z0-9]+", text):
        if tok in mapping:
            found.add(mapping[tok])
    return found

def normalize_text(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()

def get_kalshi_active_events():
    """Fetch active Kalshi events across sports series, daily crypto series, and macro events."""
    all_events = []
    series_list = ["KXNFLGAME", "KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXBTCD", "KXETHD", "KXFED", "KXCPI"]
    for s in series_list:
        try:
            url = f"{KALSHI_API}/events?series_ticker={s}&with_nested_markets=true&limit=50&status=open"
            data = fetch_json(url)
            all_events.extend(data.get("events", []))
        except Exception:
            pass
    try:
        url = f"{KALSHI_API}/events?limit=40&status=open"
        data = fetch_json(url)
        all_events.extend(data.get("events", []))
    except Exception:
        pass
    
    # Deduplicate by event_ticker
    seen = set()
    unique = []
    for e in all_events:
        t = e.get("event_ticker")
        if t and t not in seen:
            seen.add(t)
            unique.append(e)
    return unique

def get_polymarket_active_events():
    """Fetch active Polymarket events across top volume, 4 major sports tags, crypto, and macro."""
    all_events = []
    try:
        url = f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&order=volume24hr&ascending=false"
        all_events.extend(fetch_json(url))
    except Exception:
        pass
        
    for tag in ["nfl", "mlb", "nba", "nhl", "crypto", "fed", "cpi", "macro", "politics"]:
        try:
            url = f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug={tag}&order=volume24hr&ascending=false"
            all_events.extend(fetch_json(url))
        except Exception:
            pass
            
    seen = set()
    unique = []
    for e in all_events:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            unique.append(e)
    return unique

def match_sports_events(kalshi_events, poly_events):
    """Match head-to-head sports games between Kalshi and Polymarket using date and league-scoped team aliases."""
    poly_by_id = {pe["id"]: pe for pe in poly_events if pe.get("id")}
    matched = []
    
    for ke in kalshi_events:
        k_ticker = ke.get("event_ticker", "")
        if not any(k in k_ticker for k in ["NFLGAME", "MLBGAME", "NBAGAME", "NHLGAME"]):
            continue
            
        league = (
            "NFL" if "NFL" in k_ticker else
            "MLB" if "MLB" in k_ticker else
            "NBA" if "NBA" in k_ticker else "NHL"
        )
        k_date = parse_kalshi_date(k_ticker)
        if k_date:
            try:
                g_dt = datetime.strptime(k_date, "%Y-%m-%d").date()
                days_away = (g_dt - datetime.now(PT).date()).days
                if days_away < 0 or days_away > MAX_HORIZON_DAYS:
                    continue
            except Exception:
                pass
        k_teams = extract_teams(ke.get("title", "") + " " + k_ticker, league=league)
        if len(k_teams) != 2:
            continue
            
        k_markets = ke.get("markets", [])
        if len(k_markets) < 2:
            continue
            
        for pe_id, pe in poly_by_id.items():
            p_slug = pe.get("slug", "")
            p_title = pe.get("title", "")
            if k_date and k_date not in p_slug and k_date not in pe.get("endDate", ""):
                continue
                
            for pm in pe.get("markets", []):
                pq = pm.get("question", "")
                outcomes = json.loads(pm.get("outcomes", "[]")) if isinstance(pm.get("outcomes"), str) else pm.get("outcomes", [])
                
                pq_l = pq.lower()
                pt_l = p_title.lower()
                if (
                    len(outcomes) == 2
                    and ("vs" in pq_l or "vs" in pt_l or pq == p_title)
                    and not any(sub in pq_l for sub in ["inning", "quarter", "period", "margin", "total", "spread", "first"])
                    and "props" not in pt_l
                ):
                    p_teams = extract_teams(p_title + " " + pq + " " + " ".join(outcomes), league=league)
                    if p_teams == k_teams:
                        matched.append({
                            "category": f"sports_{league.lower()}",
                            "league": league,
                            "date": k_date,
                            "teams": list(k_teams),
                            "kalshi_event": ke,
                            "poly_event": pe,
                            "poly_market": pm,
                            "poly_outcomes": outcomes
                        })
                        break
    return matched

def match_crypto_strike_events(kalshi_events, poly_events):
    """Match same-date daily BTC/ETH strike ladders between Kalshi (KXBTCD/KXETHD) and Polymarket."""
    matches = []
    for ke in kalshi_events:
        kt = ke.get("event_ticker", "")
        if not (kt.startswith("KXBTCD-") or kt.startswith("KXETHD-")):
            continue
        # Only match the primary daily 5pm EDT (17) settlement series to align with daily spot close
        if not kt.endswith("17"):
            continue
        k_date = parse_kalshi_date(kt)
        if not k_date:
            continue
        coin = "BTC" if "BTC" in kt else "ETH"
        coin_kw = "bitcoin" if coin == "BTC" else "ethereum"
        
        for pe in poly_events:
            pt = pe.get("title", "")
            pt_l = pt.lower()
            if coin_kw in pt_l and "above" in pt_l:
                p_date = parse_poly_title_date(pt)
                if p_date == k_date:
                    matches.append({
                        "category": f"crypto_{coin.lower()}_daily",
                        "coin": coin,
                        "date": k_date,
                        "kalshi_event": ke,
                        "poly_event": pe
                    })
    return matches

def evaluate_crypto_strike_matchup(match):
    """Evaluate same-date, same-strike binary arbitrage between Kalshi KXBTCD/KXETHD and Polymarket."""
    opps = []
    ke = match["kalshi_event"]
    pe = match["poly_event"]
    coin = match["coin"]
    date_str = match["date"]
    kalshi_fee = 0.007

    k_by_strike = {}
    for km in ke.get("markets", []):
        fl = km.get("floor_strike")
        if fl is not None:
            strike = int(round(float(fl) + 0.01))
            k_by_strike[strike] = km

    for pm in pe.get("markets", []):
        q = pm.get("question", "")
        m = re.search(r"above\s+\$([\d,]+)", q, re.IGNORECASE)
        if not m:
            continue
        strike = int(m.group(1).replace(",", ""))
        if strike not in k_by_strike:
            continue

        km = k_by_strike[strike]
        k_yes_ask = float(km.get("yes_ask_dollars") or 1.0)
        k_no_ask = float(km.get("no_ask_dollars") or 1.0)
        p_bid = float(pm.get("bestBid") or 0.0)
        p_yes_ask = float(pm.get("bestAsk") or 1.0)
        p_no_ask = round(1.0 - p_bid, 4) if p_bid > 0.0 else 1.0

        # Require active non-trivial quotes on both legs
        if 0.02 <= k_yes_ask <= 0.98 and 0.02 <= p_no_ask <= 0.98:
            net_cost_a = round(k_yes_ask + p_no_ask + kalshi_fee, 4)
            net_edge_a = round(1.00 - net_cost_a, 4)
            if 0.010 <= net_edge_a <= 0.15:
                opps.append({
                    "event_name": f"[{coin}] {date_str} >${strike:,}",
                    "category": match["category"],
                    "leg1_venue": "Kalshi",
                    "leg1_ticker": km.get("ticker"),
                    "leg1_side": "YES",
                    "leg1_price": k_yes_ask,
                    "leg2_venue": "Polymarket",
                    "leg2_ticker": str(pm.get("id")),
                    "leg2_side": "NO",
                    "leg2_price": p_no_ask,
                    "gross_cost": round(k_yes_ask + p_no_ask, 4),
                    "total_cost": net_cost_a,
                    "net_edge": net_edge_a
                })

        if 0.02 <= p_yes_ask <= 0.98 and 0.02 <= k_no_ask <= 0.98:
            net_cost_b = round(p_yes_ask + k_no_ask + kalshi_fee, 4)
            net_edge_b = round(1.00 - net_cost_b, 4)
            if 0.010 <= net_edge_b <= 0.15:
                opps.append({
                    "event_name": f"[{coin}] {date_str} >${strike:,}",
                    "category": match["category"],
                    "leg1_venue": "Polymarket",
                    "leg1_ticker": str(pm.get("id")),
                    "leg1_side": "YES",
                    "leg1_price": p_yes_ask,
                    "leg2_venue": "Kalshi",
                    "leg2_ticker": km.get("ticker"),
                    "leg2_side": "NO",
                    "leg2_price": k_no_ask,
                    "gross_cost": round(p_yes_ask + k_no_ask, 4),
                    "total_cost": net_cost_b,
                    "net_edge": net_edge_b
                })
    return opps

def match_cross_venue_events(kalshi_events, poly_events):
    """Match macro and political cross-venue events using keyword and token overlap."""
    pairs = []
    for ke in kalshi_events:
        k_ticker = ke.get("event_ticker", "")
        if any(k in k_ticker for k in ["NFLGAME", "MLBGAME", "NBAGAME", "NHLGAME", "KXBTCD", "KXETHD"]):
            continue
            
        k_title = ke.get("title", "")
        k_norm = normalize_text(k_title)
        
        for pe in poly_events:
            p_end = pe.get("endDate")
            if p_end:
                try:
                    p_dt = datetime.fromisoformat(p_end.replace("Z", "+00:00"))
                    pe_days = (p_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
                    if pe_days < 0 or pe_days > MAX_HORIZON_DAYS:
                        continue
                except Exception:
                    pass
            p_title = pe.get("title", "")
            curr_y = datetime.now(PT).year
            if any(str(y) in p_title for y in range(curr_y + 1, curr_y + 10)):
                continue
            p_norm = normalize_text(p_title)
            
            matched_cat = None
            for pcfg in EVENT_MATCH_PATTERNS:
                kw_matches = sum(1 for kw in pcfg["keywords"] if kw in k_norm and kw in p_norm)
                if kw_matches >= 2 or (kw_matches >= 1 and any(kw == k_norm for kw in pcfg["keywords"])):
                    matched_cat = pcfg["category"]
                    break
                    
            k_tokens = set(k_norm.split())
            p_tokens = set(p_norm.split())
            intersection = k_tokens.intersection(p_tokens)
            similarity = len(intersection) / max(len(k_tokens), len(p_tokens), 1)
            
            if matched_cat or similarity >= 0.45:
                pairs.append({
                    "category": matched_cat or "general_macro",
                    "kalshi_event": ke,
                    "polymarket_event": pe,
                    "similarity": round(similarity, 3)
                })
    return pairs

def evaluate_sports_matchup(match):
    """Evaluate synthetic arbitrage (Taker-Taker and Kalshi Maker-Taker) between matching sports games."""
    opps = []
    ke = match["kalshi_event"]
    pm = match["poly_market"]
    teams = match["teams"]
    league = match.get("league")
    if len(teams) != 2:
        return opps
        
    t0, t1 = teams[0], teams[1]
    
    # Map Kalshi markets to team codes using ticker suffix + yes_sub_title first to prevent prefix collisions
    km_by_team = {}
    for km in ke.get("markets", []):
        suffix = km.get("ticker", "").split("-")[-1]
        sub = km.get("yes_sub_title", "")
        m_teams = extract_teams(f"{suffix} {sub}", league=league)
        if not m_teams:
            m_teams = extract_teams(km.get("ticker", "") + " " + km.get("title", ""), league=league)
        for t in m_teams:
            if t in teams:
                km_by_team[t] = km
                
    pm_by_team = {}
    outcomes = match["poly_outcomes"]
    outcome_prices = json.loads(pm.get("outcomePrices", "[]")) if isinstance(pm.get("outcomePrices"), str) else pm.get("outcomePrices", [])
    clob_tokens = json.loads(pm.get("clobTokenIds", "[]")) if isinstance(pm.get("clobTokenIds"), str) else pm.get("clobTokenIds", [])
    
    best_bid = float(pm.get("bestBid") or 0.0)
    best_ask = float(pm.get("bestAsk") or 0.0)
    
    for idx, o in enumerate(outcomes):
        oteams = extract_teams(o, league=league)
        for t in oteams:
            if t in teams:
                if idx == 0:
                    ask_p = best_ask if best_ask > 0 else (float(outcome_prices[0]) if outcome_prices else 0.5)
                else:
                    ask_p = round(1.0 - best_bid, 4) if best_bid > 0 else (float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.5)
                pm_by_team[t] = {
                    "outcome": o,
                    "ask": ask_p,
                    "token_id": clob_tokens[idx] if idx < len(clob_tokens) else None
                }
                
    km0, km1 = km_by_team.get(t0), km_by_team.get(t1)
    p0, p1 = pm_by_team.get(t0), pm_by_team.get(t1)
    if not (km0 and km1 and p0 and p1):
        return opps
        
    k0_ask = float(km0.get("yes_ask_dollars") or 1.0)
    k0_bid = float(km0.get("yes_bid_dollars") or 0.0)
    k1_ask = float(km1.get("yes_ask_dollars") or 1.0)
    k1_bid = float(km1.get("yes_bid_dollars") or 0.0)
    p0_ask = float(p0["ask"])
    p1_ask = float(p1["ask"])

    k_liq = min(
        float(km0.get("liquidity_dollars") or km0.get("liquidity") or 10000.0),
        float(km1.get("liquidity_dollars") or km1.get("liquidity") or 10000.0),
    )
    p_liq = float(pm.get("liquidityNum") or pm.get("liquidity") or 10000.0)
    pair_fillable_usd = round(min(300.0, max(45.0, min(k_liq, p_liq) * 0.03)), 2)
    
    kalshi_fee = 0.007
    poly_fee = 0.000
    
    # Direction A1 (Taker-Taker): Buy Kalshi Team 0 YES + Buy Polymarket Team 1 YES
    cost_a = k0_ask + p1_ask
    net_cost_a = round(cost_a + kalshi_fee + poly_fee, 4)
    net_edge_a = round(1.00 - net_cost_a, 4)
    if net_edge_a >= 0.005:
        opps.append({
            "event_name": f"[{match['league']}] {match['date']} {ke.get('title')}",
            "category": match["category"],
            "leg1_venue": "Kalshi",
            "leg1_ticker": km0.get("ticker"),
            "leg1_side": f"{t0} YES",
            "leg1_price": k0_ask,
            "leg2_venue": "Polymarket",
            "leg2_ticker": str(pm.get("id")),
            "leg2_side": f"{t1} YES",
            "leg2_price": p1_ask,
            "gross_cost": round(cost_a, 4),
            "total_cost": net_cost_a,
            "net_edge": net_edge_a,
            "max_fillable_usd": pair_fillable_usd,
        })
    elif (k0_ask - k0_bid) >= 0.03 and k0_bid >= 0.15:
        # Direction A2 (Maker-Taker): Post Kalshi Maker Bid+1c ($0 maker fee) + Buy Polymarket Team 1 YES
        k0_maker = round(k0_bid + 0.01, 2)
        net_cost_am = round(k0_maker + p1_ask, 4)
        net_edge_am = round(1.00 - net_cost_am, 4)
        if net_edge_am >= 0.015:
            opps.append({
                "event_name": f"[{match['league']}] {match['date']} {ke.get('title')} (Maker)",
                "category": match["category"],
                "leg1_venue": "Kalshi",
                "leg1_ticker": km0.get("ticker"),
                "leg1_side": f"{t0} YES (Maker)",
                "leg1_price": k0_maker,
                "leg2_venue": "Polymarket",
                "leg2_ticker": str(pm.get("id")),
                "leg2_side": f"{t1} YES",
                "leg2_price": p1_ask,
                "gross_cost": net_cost_am,
                "total_cost": net_cost_am,
                "net_edge": net_edge_am,
                "max_fillable_usd": pair_fillable_usd,
            })
        
    # Direction B1 (Taker-Taker): Buy Polymarket Team 0 YES + Buy Kalshi Team 1 YES
    cost_b = p0_ask + k1_ask
    net_cost_b = round(cost_b + kalshi_fee + poly_fee, 4)
    net_edge_b = round(1.00 - net_cost_b, 4)
    if net_edge_b >= 0.005:
        opps.append({
            "event_name": f"[{match['league']}] {match['date']} {ke.get('title')}",
            "category": match["category"],
            "leg1_venue": "Polymarket",
            "leg1_ticker": str(pm.get("id")),
            "leg1_side": f"{t0} YES",
            "leg1_price": p0_ask,
            "leg2_venue": "Kalshi",
            "leg2_ticker": km1.get("ticker"),
            "leg2_side": f"{t1} YES",
            "leg2_price": k1_ask,
            "gross_cost": round(cost_b, 4),
            "total_cost": net_cost_b,
            "net_edge": net_edge_b,
            "max_fillable_usd": pair_fillable_usd,
        })
    elif (k1_ask - k1_bid) >= 0.03 and k1_bid >= 0.15:
        # Direction B2 (Maker-Taker): Buy Polymarket Team 0 YES + Post Kalshi Maker Bid+1c ($0 maker fee)
        k1_maker = round(k1_bid + 0.01, 2)
        net_cost_bm = round(p0_ask + k1_maker, 4)
        net_edge_bm = round(1.00 - net_cost_bm, 4)
        if net_edge_bm >= 0.015:
            opps.append({
                "event_name": f"[{match['league']}] {match['date']} {ke.get('title')} (Maker)",
                "category": match["category"],
                "leg1_venue": "Polymarket",
                "leg1_ticker": str(pm.get("id")),
                "leg1_side": f"{t0} YES",
                "leg1_price": p0_ask,
                "leg2_venue": "Kalshi",
                "leg2_ticker": km1.get("ticker"),
                "leg2_side": f"{t1} YES (Maker)",
                "leg2_price": k1_maker,
                "gross_cost": net_cost_bm,
                "total_cost": net_cost_bm,
                "net_edge": net_edge_bm,
                "max_fillable_usd": pair_fillable_usd,
            })
        
    return opps

def evaluate_cross_venue_pair(k_event, p_event):
    """Evaluate synthetic arbitrage between matching Kalshi and Polymarket binary markets."""
    opps = []
    k_markets = k_event.get("markets", [])
    p_markets = p_event.get("markets", [])
    if len(k_markets) != 1 or len(p_markets) != 1:
        return opps
    
    for km in k_markets:
        k_ticker = km.get("ticker", "")
        k_yes_ask = float(km.get("yes_ask_dollars") or 0.0)
        k_yes_bid = float(km.get("yes_bid_dollars") or 0.0)
        k_no_ask = round(1.0 - k_yes_bid, 2) if k_yes_bid > 0.0 else 1.0
        
        for pm in p_markets:
            p_id = str(pm.get("id", ""))
            raw_op = pm.get("outcomePrices", "[]")
            outcome_prices = json.loads(raw_op) if isinstance(raw_op, str) else (raw_op or [])
            try:
                p_yes_ask = float(pm.get("bestAsk") or (outcome_prices[0] if len(outcome_prices) > 0 else 0.0))
                p_bid = float(pm.get("bestBid") or 0.0)
                p_no_ask = round(1.0 - p_bid, 4) if p_bid > 0.0 else (float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.0)
            except Exception:
                continue
                
            if 0.05 <= k_yes_ask <= 0.95 and 0.05 <= p_no_ask <= 0.95:
                total_cost_a = k_yes_ask + p_no_ask + 0.015
                if total_cost_a < 0.97:
                    net_edge_a = round(1.00 - total_cost_a, 4)
                    opps.append({
                        "event_name": k_event.get("title", ""),
                        "leg1_venue": "Kalshi",
                        "leg1_ticker": k_ticker,
                        "leg1_side": "YES",
                        "leg1_price": k_yes_ask,
                        "leg2_venue": "Polymarket",
                        "leg2_ticker": p_id,
                        "leg2_side": "NO",
                        "leg2_price": p_no_ask,
                        "total_cost": round(total_cost_a, 4),
                        "net_edge": net_edge_a
                    })
                    
            if 0.05 <= p_yes_ask <= 0.95 and 0.05 <= k_no_ask <= 0.95:
                total_cost_b = p_yes_ask + k_no_ask + 0.015
                if total_cost_b < 0.97:
                    net_edge_b = round(1.00 - total_cost_b, 4)
                    opps.append({
                        "event_name": k_event.get("title", ""),
                        "leg1_venue": "Polymarket",
                        "leg1_ticker": p_id,
                        "leg1_side": "YES",
                        "leg1_price": p_yes_ask,
                        "leg2_venue": "Kalshi",
                        "leg2_ticker": k_ticker,
                        "leg2_side": "NO",
                        "leg2_price": k_no_ask,
                        "total_cost": round(total_cost_b, 4),
                        "net_edge": net_edge_b
                    })
    return opps

def settle_resolved_cross_venue_trades(conn, verbose: bool = False):
    """Settle OPEN cross-venue arbitrage pairs whose game date has passed or Kalshi market settled."""
    init_db(conn)
    open_trades = conn.execute("SELECT * FROM cross_venue_trades WHERE status = 'OPEN'").fetchall()
    if not open_trades:
        return []

    today_pt = datetime.now(PT).date()
    now_str = datetime.now(PT).isoformat()
    settled = []

    for tr in open_trades:
        k_ticker = tr["kalshi_ticker"] or ""
        ev_name = tr["event_name"] or ""
        is_resolved = False

        m_dt = re.search(r"(\d{4}-\d{2}-\d{2})", ev_name)
        g_date_str = m_dt.group(1) if m_dt else parse_kalshi_date(k_ticker)
        if g_date_str:
            try:
                g_date = datetime.strptime(g_date_str, "%Y-%m-%d").date()
                if g_date < today_pt:
                    is_resolved = True
            except Exception:
                pass

        if not is_resolved and k_ticker:
            try:
                m_data = fetch_json(f"{KALSHI_API}/markets/{k_ticker}", timeout=4).get("market", {})
                if m_data.get("status") in ("finalized", "settled", "determined", "closed"):
                    is_resolved = True
            except Exception:
                pass

        if is_resolved:
            payout = round(float(tr["sum_cost"]) + float(tr["locked_profit"]), 2)
            pnl = round(float(tr["locked_profit"]), 2)
            with conn:
                conn.execute(
                    "UPDATE cross_venue_trades SET status = 'SETTLED', settled_at = ?, realized_pnl = ? WHERE id = ?",
                    (now_str, pnl, tr["id"])
                )
                conn.execute(
                    "UPDATE cross_venue_portfolio SET cash = ROUND(cash + ?, 2), realized_pnl = ROUND(realized_pnl + ?, 2), last_updated = ? WHERE id = 1",
                    (payout, pnl, now_str)
                )
            settled.append({"id": tr["id"], "event_name": ev_name, "payout": payout, "pnl": pnl})

    if settled and verbose:
        tot_pnl = sum(s["pnl"] for s in settled)
        print(f"  💰 Settled {len(settled)} resolved cross-venue arb pair(s) (Realized P&L: +${tot_pnl:.2f})")
    return settled

def run_cross_venue_scan(conn=None, execute=True, verbose=False):
    now_pt = datetime.now(PT)
    if verbose:
        print(f"[{now_pt.strftime('%Y-%m-%d %I:%M:%S %p PT')}] Scanning Kalshi vs. Polymarket Cross-Venue Arbitrage...")
    
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    init_db(conn)
    settle_resolved_cross_venue_trades(conn, verbose=verbose)
    
    k_events = get_kalshi_active_events()
    p_events = get_polymarket_active_events()
    if verbose:
        print(f"  Ingested {len(k_events)} Kalshi events, {len(p_events)} Polymarket events.")
    
    sports_matches = match_sports_events(k_events, p_events)
    crypto_matches = match_crypto_strike_events(k_events, p_events)
    macro_matches = match_cross_venue_events(k_events, p_events)
    if verbose:
        print(f"  Identified {len(sports_matches)} sports matchups, {len(crypto_matches)} crypto ladders & {len(macro_matches)} macro candidates.")
    
    opportunities = []
    for sm in sports_matches:
        opportunities.extend(evaluate_sports_matchup(sm))
    for cm in crypto_matches:
        opportunities.extend(evaluate_crypto_strike_matchup(cm))
    for mm in macro_matches:
        for o in evaluate_cross_venue_pair(mm["kalshi_event"], mm["polymarket_event"]):
            o["category"] = mm["category"]
            opportunities.append(o)
            
    opportunities.sort(key=lambda x: x["net_edge"], reverse=True)
    if verbose:
        print(f"  Found {len(opportunities)} locked synthetic arbitrage opportunities.")
    
    row = conn.execute("SELECT cash, starting_balance FROM cross_venue_portfolio WHERE id = 1").fetchone()
    current_cash = float(row["cash"]) if row else 1000.0
    
    existing_trades = conn.execute("SELECT kalshi_ticker, polymarket_id, status FROM cross_venue_trades WHERE status = 'OPEN'").fetchall()
    open_pairs = {(r["kalshi_ticker"], r["polymarket_id"]) for r in existing_trades}

    pm_decision = {
        "approved_indices": list(range(len(opportunities))),
        "sizing_multipliers": {},
        "rationale_by_index": {},
        "pm_thesis": "",
        "model_used": "gemini-4-argon-high",
    }
    unheld_opps = [
        o for o in opportunities
        if (
            (o["leg1_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg2_ticker"]),
            (o["leg2_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg1_ticker"]),
        ) not in open_pairs
    ]
    if execute and unheld_opps and current_cash >= 25.0:
        try:
            from tools.llm_sidecar import evaluate_quant_candidates_llm
            pm_decision = evaluate_quant_candidates_llm(
                "quant_cross_venue_arb",
                "Track 1: Kalshi vs Polymarket Cross-Venue Arbitrage",
                opportunities,
                {"cash": current_cash, "open_pairs_count": len(open_pairs), "open_pairs": [list(p) for p in list(open_pairs)[:10]]},
            )
            if verbose and pm_decision.get("pm_thesis"):
                print(f"  🧠 [Quant PM · {pm_decision.get('model_used')}]: {pm_decision['pm_thesis']}")
        except Exception as e:
            if verbose:
                print(f"  [Quant PM Note]: {e}")

    approved_set = set(pm_decision.get("approved_indices", range(len(opportunities))))
    mult_map = pm_decision.get("sizing_multipliers", {})
    rat_map = pm_decision.get("rationale_by_index", {})

    executed_trades = []
    for idx, o in enumerate(opportunities):
        leg_k = o["leg1_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg2_ticker"]
        leg_p = o["leg2_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg1_ticker"]

        pair_key = (leg_k, leg_p)
        conv_mult = float(mult_map.get(str(idx), 1.0))
        pm_note = rat_map.get(str(idx), "")
        if verbose:
            print(f"  🚨 ARB: {o['event_name']} | {o['leg1_venue']} {o['leg1_side']} (${o['leg1_price']:.2f}) + {o['leg2_venue']} {o['leg2_side']} (${o['leg2_price']:.2f}) -> Net Cost: ${o['total_cost']:.3f} (Net Edge: +{o['net_edge']*100:.1f}%)")
            if pm_note:
                print(f"     [PM Thesis]: {pm_note}")

        if idx not in approved_set:
            if verbose:
                print("     [PM VETO]: Skipped due to cross-venue contract resolution or liquidity risk.")
            continue

        if execute and pair_key not in open_pairs and current_cash >= 25.0:
            # Dynamic Profit-Maximizing Arbitrage Sizing:
            # Scale allocation with net_edge (6% of book at +1% edge up to 30% of book at +6.5%+ edge),
            # capital turnover velocity (boost same/next-day settlements), LLM PM conviction, and venue depth.
            open_stats = conn.execute(
                "SELECT COALESCE(SUM(sum_cost), 0.0) as c, COALESCE(SUM(locked_profit), 0.0) as l "
                "FROM cross_venue_trades WHERE status = 'OPEN'"
            ).fetchone()
            live_book_eq = current_cash + float(open_stats["c"]) + float(open_stats["l"])
            edge_val = float(o.get("net_edge") or 0.01)
            edge_pct_alloc = min(0.30, max(0.06, edge_val * 4.5))

            ev_str = o.get("event_name", "")
            m_dt = re.search(r"(\d{4}-\d{2}-\d{2})", ev_str)
            days_to_settle = 2.0
            if m_dt:
                try:
                    g_date = datetime.strptime(m_dt.group(1), "%Y-%m-%d").date()
                    days_to_settle = max(0.5, float((g_date - datetime.now(PT).date()).days))
                except Exception:
                    pass
            velocity_mult = 1.25 if days_to_settle <= 1.5 else (0.70 if days_to_settle >= 5.0 else 1.0)
            venue_depth_cap = float(o.get("max_fillable_usd") or 300.0)

            target_ticket = live_book_eq * edge_pct_alloc * velocity_mult * max(0.5, min(1.5, conv_mult))
            max_ticket = min(venue_depth_cap, round(live_book_eq * 0.30, 2), current_cash, max(35.0, target_ticket))
            contracts = int(max_ticket / o["total_cost"])
            if contracts < 5:
                continue

            sum_cost = round(contracts * o["total_cost"], 2)
            locked_pnl = round(contracts * o["net_edge"], 2)
            now_str = datetime.now(PT).isoformat()

            with conn:
                conn.execute("""
                    INSERT INTO cross_venue_trades (
                        timestamp, event_name, category,
                        kalshi_ticker, kalshi_side, kalshi_price,
                        polymarket_id, polymarket_side, polymarket_price,
                        contracts, sum_cost, locked_profit, net_edge,
                        status, settled_at, realized_pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL)
                """, (
                    now_str, o["event_name"], o["category"],
                    o["leg1_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg2_ticker"],
                    o["leg1_side"] if o["leg1_venue"] == "Kalshi" else o["leg2_side"],
                    o["leg1_price"] if o["leg1_venue"] == "Kalshi" else o["leg2_price"],
                    o["leg2_ticker"] if o["leg1_venue"] == "Kalshi" else o["leg1_ticker"],
                    o["leg2_side"] if o["leg1_venue"] == "Kalshi" else o["leg1_side"],
                    o["leg2_price"] if o["leg1_venue"] == "Kalshi" else o["leg1_price"],
                    contracts, sum_cost, locked_pnl, o["net_edge"]
                ))
                current_cash = round(current_cash - sum_cost, 2)
                conn.execute(
                    "UPDATE cross_venue_portfolio SET cash = ?, last_updated = ? WHERE id = 1",
                    (current_cash, now_str)
                )
                open_pairs.add(pair_key)
                executed_trades.append({
                    "event_name": o["event_name"],
                    "contracts": contracts,
                    "sum_cost": sum_cost,
                    "locked_pnl": locked_pnl,
                    "net_edge": o["net_edge"]
                })

    open_rows = conn.execute("SELECT sum_cost, locked_profit FROM cross_venue_trades WHERE status = 'OPEN'").fetchall()
    open_cost = sum(r["sum_cost"] for r in open_rows)
    open_locked = sum(r["locked_profit"] for r in open_rows)
    exp_eq = current_cash + open_cost + open_locked
    fill_note = f" · New fills: {len(executed_trades)}" if executed_trades else ""
    print(f"- **Track 1 (Cross-Venue Arb):** `${exp_eq:.2f}` expected equity (`${current_cash:.2f}` cash · `{len(open_rows)}` open pairs · `+${open_locked:.2f}` locked{fill_note})")
    return opportunities

if __name__ == "__main__":
    import sys
    run_cross_venue_scan(execute=True, verbose=("--verbose" in sys.argv or "-v" in sys.argv))
