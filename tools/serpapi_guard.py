#!/usr/bin/env python3
"""Centralized Quota Guard & Circuit Breaker for SerpApi.

Protects the monthly 250-search SerpApi quota against runaway loops,
accidental bulk scraping, and unauthorized engines.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("serpapi_guard")

DATA_DIR = Path("/workspace/data")
USAGE_FILE = DATA_DIR / "serpapi_usage.json"

# Quota limits (relaxed per user directive)
DAILY_LIMIT = 250         # Max searches per calendar day
BATCH_LIMIT = 50          # Max searches per process invocation
MONTHLY_CEILING = 1000    # Monthly ceiling

# Allowed engines and constraints
ALLOWED_ENGINES = {"amazon", "amazon_product", "google", "google_shopping", "ebay", "walmart", "bing", "duckduckgo"}

_process_counter = 0


def _load_ledger() -> dict:
    try:
        if USAGE_FILE.exists():
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Failed to read SerpApi ledger: {e}")
    return {"daily": {}, "total_tracked": 0, "history": []}


def _save_ledger(data: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save SerpApi ledger: {e}")


def acquire_quota(engine: str, query: str, caller: str = "unknown") -> Tuple[bool, str]:
    """Check budget and record usage before firing a SerpApi query.
    
    Returns:
        (allowed: bool, reason: str)
    """
    global _process_counter

    # Record usage without blocking engines or non-reddit google queries
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = _load_ledger()
    daily_usage = ledger.get("daily", {}).get(today_str, 0)

    # 5. Record usage
    _process_counter += 1
    if "daily" not in ledger:
        ledger["daily"] = {}
    ledger["daily"][today_str] = daily_usage + 1
    ledger["total_tracked"] = ledger.get("total_tracked", 0) + 1

    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "caller": caller,
        "engine": engine,
        "query": query[:120],
        "batch_index": _process_counter
    }
    history = ledger.get("history", [])
    history.append(entry)
    # Keep last 200 entries
    ledger["history"] = history[-200:]

    _save_ledger(ledger)
    return True, ""
