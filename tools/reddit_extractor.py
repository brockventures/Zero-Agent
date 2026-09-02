#!/usr/bin/env python3
"""Reddit Discussion & Community Consensus Extractor for Zero.

Uses Google Search engine (via SerpApi) for 100% unblocked, authentic Reddit
discussions, thread links, and community consensus research without scraping blocks.
Defaults to recent posts (current year / past 12 months).
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("reddit_extractor")

BASE_URL = "https://serpapi.com/search.json"
TIMEOUT = 25
SECRETS_PATH = os.environ.get("SECRETS_PATH", "/secrets/env.json")
CACHE_DIR = Path(os.environ.get("REDDIT_CACHE_DIR", "/workspace/data/reddit_cache"))
CACHE_TTL_SECONDS = 3600 * 6  # 6-hour cache for research queries


def get_api_key() -> str:
    """Retrieve SerpApi API key from environment or secrets file."""
    key = os.environ.get("SERPAPI_API_KEY", "")
    if not key and os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                key = data.get("SERPAPI_API_KEY", "")
        except Exception as e:
            log.warning(f"Failed to read secrets file: {e}")
    return key


def _get_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    """Retrieve cached JSON response if still valid within TTL."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"{cache_key}.json"
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if time.time() - entry.get("cached_at", 0) < CACHE_TTL_SECONDS:
                return entry.get("data")
    except Exception as e:
        log.debug(f"Cache read error: {e}")
    return None


def _set_cache(cache_key: str, data: Dict[str, Any]) -> None:
    """Store JSON response into disk cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"{cache_key}.json"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"cached_at": time.time(), "data": data}, f, indent=2)
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def _query_serpapi(params: Dict[str, Any], use_cache: bool = True) -> Dict[str, Any]:
    """Query SerpApi with caching and error handling."""
    api_key = get_api_key()
    if not api_key:
        return {"error": "Missing SERPAPI_API_KEY. Configure in /secrets/env.json or environment."}

    params["api_key"] = api_key
    params["engine"] = "google"

    # Compute cache key from query params
    clean_params = {k: v for k, v in params.items() if k != "api_key"}
    cache_key = hashlib.sha256(json.dumps(clean_params, sort_keys=True).encode()).hexdigest()

    if use_cache:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    query_str = urllib.parse.urlencode(params)
    full_url = f"{BASE_URL}?{query_str}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "Zero-Reddit-Extractor/2.0"})

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if use_cache and "error" not in data:
                _set_cache(cache_key, data)
            return data
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"error": str(e)}


def search_reddit(
    query: str,
    subreddit: Optional[str] = None,
    limit: int = 10,
    recency: str = "year",  # "year", "month", "week", "all", or explicit year string e.g. "2026"
    use_cache: bool = True
) -> Dict[str, Any]:
    """Search Reddit threads with recency filtering (default: past 12 months / current year)."""
    # Build search query
    query_parts = []
    
    if subreddit:
        clean_sub = subreddit.replace("r/", "").strip()
        query_parts.append(f"site:reddit.com/r/{clean_sub}")
    else:
        query_parts.append("site:reddit.com")

    query_parts.append(query.strip())

    # Build search params
    params: Dict[str, Any] = {
        "num": max(1, min(int(limit), 20)),
        "hl": "en",
        "gl": "us"
    }

    # Apply temporal filter
    if recency == "year" or recency == "2026":
        params["tbs"] = "qdr:y"  # Past 12 months
    elif recency == "month":
        params["tbs"] = "qdr:m"  # Past month
    elif recency == "week":
        params["tbs"] = "qdr:w"  # Past week
    elif recency != "all":
        # Specific year or keyword
        query_parts.append(recency)

    params["q"] = " ".join(query_parts)

    raw_data = _query_serpapi(params, use_cache=use_cache)
    if "error" in raw_data:
        return {"ok": False, "error": raw_data["error"]}

    organic = raw_data.get("organic_results", [])
    threads: List[Dict[str, Any]] = []

    for item in organic:
        link = item.get("link", "")
        # Filter for actual reddit comment/discussion threads
        if "reddit.com" not in link:
            continue

        threads.append({
            "title": item.get("title", "").replace(" : r/", " - r/"),
            "link": link,
            "snippet": item.get("snippet", ""),
            "displayed_link": item.get("displayed_link", ""),
            "date": item.get("date", ""),
            "position": item.get("position")
        })

    return {
        "ok": True,
        "query": params["q"],
        "recency": recency,
        "count": len(threads),
        "threads": threads[:limit]
    }


def main():
    parser = argparse.ArgumentParser(description="Zero Reddit Discussion Extractor (Google Engine)")
    subparsers = parser.add_subparsers(dest="action", required=True)

    search_p = subparsers.add_parser("search", help="Search Reddit discussions")
    search_p.add_argument("query", help="Search terms or topic")
    search_p.add_argument("-s", "--sub", "--subreddit", dest="subreddit", help="Subreddit filter (e.g. selfhosted, BuyItForLife)")
    search_p.add_argument("-n", "--limit", type=int, default=10, help="Maximum number of results (default: 10)")
    search_p.add_argument(
        "-t", "--recency",
        choices=["year", "month", "week", "all", "2026"],
        default="year",
        help="Recency filter (default: year / past 12 months)"
    )
    search_p.add_argument("--no-cache", action="store_true", help="Bypass cached results")

    args = parser.parse_args()

    if args.action == "search":
        res = search_reddit(
            query=args.query,
            subreddit=args.subreddit,
            limit=args.limit,
            recency=args.recency,
            use_cache=not args.no_cache
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
