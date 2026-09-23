"""Universal Multi-Category Cross-Bracket Arbitrage & Normalization Engine.

Scans and analyzes prediction market brackets across Macroeconomics, Weather,
Entertainment (Box Office), Financials, and Elections/Current Events on Kalshi.

Key capabilities:
1. MECE & Partition Safety Gate (prevents the "Field/Other" trap).
2. Horizon Gate (enforces max_days <= 14 to protect annualized ROC).
3. Pure Under-Round Basket Arbitrage detection (net of taker fee friction).
4. Monotonicity Inversion Arbitrage detection on cumulative threshold strikes.
5. Cross-Bracket Probability Normalization & Relative Value Over-Round Fading.
"""

from datetime import datetime, timezone
import json
import re
import urllib.request

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_MAX_HORIZON_DAYS = 14
DEFAULT_MIN_UNDERROUND_THRESHOLD = 0.95  # Total basket buy cost must be <= $0.95 (5%+ profit net of fees)
DEFAULT_OVERROUND_THRESHOLD = 1.15       # Flag for relative-value fade if sum of asks > 115%
TAKER_FEE_MULTIPLIER = 0.07              # Kalshi taker fee: 0.07 * p * (1 - p)


def calculate_taker_fee(price: float) -> float:
    """Calculate Kalshi taker fee in dollars for a given contract price."""
    p = max(0.01, min(0.99, float(price)))
    return round(TAKER_FEE_MULTIPLIER * p * (1.0 - p), 4)


def parse_iso_datetime(iso_str: str) -> datetime:
    """Safely parse ISO datetime string with UTC fallback."""
    if not iso_str:
        return datetime.now(timezone.utc)
    cleaned = iso_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        return datetime.now(timezone.utc)


def check_horizon(close_time_iso: str, max_days: int = DEFAULT_MAX_HORIZON_DAYS) -> tuple[bool, float]:
    """Check if market closes within the maximum horizon days.
    
    Returns (is_valid, days_to_close).
    """
    if not close_time_iso:
        return False, 999.0
    close_dt = parse_iso_datetime(close_time_iso)
    now_dt = datetime.now(timezone.utc)
    delta = close_dt - now_dt
    days_to_close = delta.total_seconds() / 86400.0
    if days_to_close < 0.0:
        return False, days_to_close
    return days_to_close <= max_days, round(days_to_close, 2)


def is_mece_or_closed_partition(event_title: str, markets: list[dict]) -> tuple[bool, str, str]:
    """Verify if market list forms a closed, mutually exclusive and collectively exhaustive (MECE) partition.
    
    Returns (is_mece, partition_type, reason).
    Partition types: 'numeric_range', 'cumulative_threshold', 'closed_binary', 'named_with_field', 'unclosed_candidate_list'
    """
    if not markets or len(markets) < 2:
        return False, "invalid", "Fewer than 2 markets"

    titles = [m.get("title", "") or m.get("ticker", "") for m in markets]
    subtitles = [m.get("yes_sub_title", "") or "" for m in markets]
    all_text = " ".join(titles + subtitles).lower()

    # 1. Check for Cumulative Thresholds ("above X", "greater than X", "X or more", "X+")
    is_cumulative = False
    cum_matches = 0
    for t in titles:
        if re.search(r"(above|greater than|over|\+|or more|at least)", t.lower()):
            cum_matches += 1
    if cum_matches >= len(markets) - 1:
        return True, "cumulative_threshold", "Cumulative threshold strikes"

    # 2. Check for Numeric Ranges (e.g. "65 to 66", "<65", ">70", "$30M to $40M")
    range_pattern = r"(\d+[A-Za-z°\$%]*\s*(?:to|-|and)\s*[\$]?\d+|<\s*[\$]?\d+|>\s*[\$]?\d+|less than|greater than|between\s*[\$]?\d+)"
    range_matches = 0
    for t in titles:
        if re.search(range_pattern, t.lower()):
            range_matches += 1
    if range_matches >= len(markets) - 1:
        return True, "numeric_range", "Continuous numeric range brackets"

    # 3. Check for Binary Head-to-Head (exactly 2 opposing outcomes)
    if len(markets) == 2:
        return True, "closed_binary", "Two-party closed matchup"

    # 4. Check for Named Candidates / Categorical list
    # Must have an explicit "Field", "Other", "Someone else", or "Any other" to be collectively exhaustive
    has_field_market = False
    for t in titles:
        if re.search(r"\b(other|field|someone else|any other candidate|all other)\b", t.lower()):
            has_field_market = True
            break
            
    if has_field_market:
        return True, "named_with_field", "Named candidate roster with explicit Field/Other catch-all"

    # If named list without "Field", partition is incomplete (The Field Trap)
    return False, "unclosed_candidate_list", "Incomplete candidate roster missing Field/Other catch-all contract"


def extract_numeric_strike(title: str, ticker: str) -> float | None:
    """Extract numeric threshold value from title or ticker for sorting cumulative strikes."""
    match = re.search(r"(?:above|over|at least|\+|>)\s*([\d\.]+)", title.lower())
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    # Fallback to digits in ticker
    match_ticker = re.search(r"-([T]?\d+[\.]?\d*)$", ticker)
    if match_ticker:
        raw = match_ticker.group(1).replace("T", "")
        try:
            return float(raw)
        except ValueError:
            pass
    return None


def detect_cross_bracket_arbitrage(
    markets: list[dict],
    event_title: str = "",
    min_underround: float = DEFAULT_MIN_UNDERROUND_THRESHOLD,
    min_overround: float = DEFAULT_OVERROUND_THRESHOLD
) -> dict:
    """Analyze a multi-bracket event for structural cross-bracket arbitrage and mispricing.
    
    Returns structured analysis containing:
    - is_mece: bool
    - partition_type: str
    - sum_ask: float
    - sum_bid: float
    - net_basket_buy_cost: float (sum_ask + taker fees)
    - has_underround_arb: bool (pure Dutch book buy)
    - has_monotonicity_arb: bool
    - has_overround_fade: bool (relative-value short opportunity)
    - monotonicity_details: list
    - best_fade_bracket: dict
    """
    is_mece, part_type, reason = is_mece_or_closed_partition(event_title, markets)
    
    analysis = {
        "event_title": event_title,
        "market_count": len(markets),
        "is_mece": is_mece,
        "partition_type": part_type,
        "partition_reason": reason,
        "sum_ask": 0.0,
        "sum_bid": 0.0,
        "total_taker_fees": 0.0,
        "net_basket_buy_cost": 0.0,
        "has_underround_arb": False,
        "arb_profit_per_share": 0.0,
        "has_monotonicity_arb": False,
        "monotonicity_details": [],
        "has_overround_fade": False,
        "best_fade_bracket": None,
        "normalized_distribution": []
    }

    if not markets:
        return analysis

    sum_ask = 0.0
    sum_bid = 0.0
    total_fees = 0.0
    valid_markets = []

    for m in markets:
        ask = float(m.get("yes_ask_dollars") or 0.0)
        bid = float(m.get("yes_bid_dollars") or 0.0)
        ticker = m.get("ticker", "")
        title = m.get("title", "") or ticker
        
        sum_ask += ask
        sum_bid += bid
        if ask > 0.0:
            total_fees += calculate_taker_fee(ask)
            
        valid_markets.append({
            "ticker": ticker,
            "title": title,
            "yes_ask": ask,
            "yes_bid": bid,
            "no_ask": round(1.0 - bid, 4) if bid > 0.0 else 1.0,
            "volume_24h": m.get("volume_24h_fp") or m.get("volume_24h") or 0,
            "open_interest": m.get("open_interest_fp") or m.get("open_interest") or 0,
            "market_obj": m
        })

    analysis["sum_ask"] = round(sum_ask, 4)
    analysis["sum_bid"] = round(sum_bid, 4)
    analysis["total_taker_fees"] = round(total_fees, 4)
    net_buy_cost = round(sum_ask + total_fees, 4)
    analysis["net_basket_buy_cost"] = net_buy_cost

    # 1. Deterministic Under-Round Basket Arb (Only valid on MECE/closed partitions)
    if is_mece and sum_ask > 0.0 and net_buy_cost < 1.00 and sum_ask <= min_underround:
        analysis["has_underround_arb"] = True
        analysis["arb_profit_per_share"] = round(1.00 - net_buy_cost, 4)

    # 2. Monotonicity Inversion Check (Cumulative threshold strikes)
    if part_type == "cumulative_threshold":
        # Extract strikes and sort ascending
        strike_list = []
        for vm in valid_markets:
            s_val = extract_numeric_strike(vm["title"], vm["ticker"])
            if s_val is not None:
                strike_list.append((s_val, vm))
        
        strike_list.sort(key=lambda x: x[0])
        # P(>K1) >= P(>K2) when K1 < K2. Inversion occurs if Ask(K2) > Bid(K1)
        for i in range(len(strike_list) - 1):
            k1, m1 = strike_list[i]
            k2, m2 = strike_list[i + 1]
            
            # An actionable arb exists if Ask(higher strike K2) < Bid(lower strike K1)
            # Or if Ask(K2) > Ask(K1) by a margin indicating book inversion
            if m2["yes_bid"] > m1["yes_ask"] and m1["yes_ask"] > 0.0:
                analysis["has_monotonicity_arb"] = True
                analysis["monotonicity_details"].append({
                    "type": "bid_ask_crossing",
                    "lower_strike": k1,
                    "lower_ticker": m1["ticker"],
                    "lower_ask": m1["yes_ask"],
                    "higher_strike": k2,
                    "higher_ticker": m2["ticker"],
                    "higher_bid": m2["yes_bid"],
                    "immediate_credit": round(m2["yes_bid"] - m1["yes_ask"], 4)
                })
            elif m2["yes_ask"] > m1["yes_ask"] and m1["yes_ask"] > 0.0:
                analysis["has_monotonicity_arb"] = True
                analysis["monotonicity_details"].append({
                    "type": "ask_inversion",
                    "lower_strike": k1,
                    "lower_ticker": m1["ticker"],
                    "lower_ask": m1["yes_ask"],
                    "higher_strike": k2,
                    "higher_ticker": m2["ticker"],
                    "higher_ask": m2["yes_ask"],
                    "inversion_spread": round(m2["yes_ask"] - m1["yes_ask"], 4)
                })

    # 3. Cross-Bracket Probability Normalization & Relative Value Over-Round Fading
    if is_mece and sum_ask >= min_overround and sum_ask > 0.0:
        analysis["has_overround_fade"] = True
        
        # Compute normalized probabilities
        dist = []
        max_inflation_diff = -1.0
        best_fade = None
        
        uniform_prior = 1.0 / len(valid_markets)
        for vm in valid_markets:
            p_norm = round(vm["yes_ask"] / sum_ask, 4)
            # Inflation difference: how much retail inflated this strike above normalized weight
            inflation = round(vm["yes_ask"] - p_norm, 4)
            entry = {
                "ticker": vm["ticker"],
                "title": vm["title"],
                "yes_ask": vm["yes_ask"],
                "normalized_prob": p_norm,
                "inflation": inflation,
                "no_ask": vm["no_ask"]
            }
            dist.append(entry)
            
            # Select best strike to fade (highest positive inflation and liquid bid)
            if inflation > max_inflation_diff and vm["yes_ask"] >= 0.15:
                max_inflation_diff = inflation
                best_fade = entry

        analysis["normalized_distribution"] = dist
        analysis["best_fade_bracket"] = best_fade

    return analysis


def sweep_kalshi_events(
    api_base: str = KALSHI_API,
    max_days: int = DEFAULT_MAX_HORIZON_DAYS,
    limit: int = 100
) -> list[dict]:
    """Query live Kalshi events across all categories and identify cross-bracket opportunities."""
    url = f"{api_base}/events?status=open&limit={limit}&with_nested_markets=true"
    req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0", "Accept": "application/json"})
    
    opportunities = []
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[CrossBracket] Error querying Kalshi events: {e}")
        return opportunities

    events = data.get("events", [])
    now_utc = datetime.now(timezone.utc)

    for ev in events:
        event_ticker = ev.get("event_ticker", "")
        if event_ticker.startswith("KXMVE"):
            continue  # Skip synthetic multivariate combo baskets
            
        markets = ev.get("markets", [])
        if len(markets) < 2:
            continue

        # Check horizon filter using first market expiration
        first_exp = markets[0].get("latest_expiration_time") or markets[0].get("expiration_time")
        is_valid_horizon, days_to_close = check_horizon(first_exp, max_days=max_days)
        if not is_valid_horizon:
            continue

        event_title = ev.get("title", "")
        category = ev.get("category", "General")

        analysis = detect_cross_bracket_arbitrage(
            markets=markets,
            event_title=event_title
        )

        # Retain if any actionable signal was flagged
        if analysis["has_underround_arb"] or analysis["has_monotonicity_arb"] or analysis["has_overround_fade"]:
            opportunities.append({
                "event_ticker": event_ticker,
                "event_title": event_title,
                "category": category,
                "days_to_close": days_to_close,
                "analysis": analysis
            })

    return opportunities


if __name__ == "__main__":
    print(f"[{datetime.now(timezone.utc).isoformat()}] Running live Kalshi Universal Cross-Bracket Scanner...")
    opps = sweep_kalshi_events(max_days=14, limit=100)
    print(f"Scanned open events. Found {len(opps)} actionable cross-bracket opportunities within 14-day horizon.")
    for o in opps[:5]:
        a = o["analysis"]
        print(f"\n>>> [{o['category']}] {o['event_title']} ({o['event_ticker']}) - Closes in {o['days_to_close']}d")
        print(f"    Partition: {a['partition_type']} (MECE: {a['is_mece']})")
        print(f"    Sum Ask: ${a['sum_ask']:.2f} | Sum Bid: ${a['sum_bid']:.2f} | Taker Fees: ${a['total_taker_fees']:.2f}")
        if a["has_underround_arb"]:
            print(f"    🚨 UNDER-ROUND BASKET ARB! Net profit per share: ${a['arb_profit_per_share']:.4f}")
        if a["has_monotonicity_arb"]:
            print(f"    ⚠️ MONOTONICITY INVERSION DETECTED: {len(a['monotonicity_details'])} anomalies")
        if a["has_overround_fade"] and a["best_fade_bracket"]:
            b = a["best_fade_bracket"]
            print(f"    📉 OVER-ROUND FADE: Strike '{b['title']}' inflated at ${b['yes_ask']:.2f} (norm: ${b['normalized_prob']:.2f}) -> Buy NO at ${b['no_ask']:.2f}")
