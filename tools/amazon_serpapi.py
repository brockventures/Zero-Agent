#!/usr/bin/env python3
"""Amazon Search & Product Intelligence MCP Server & CLI Tool for Zero/Ivy.

Powered by SerpApi using the Amazon engine endpoints.
Loads SERPAPI_API_KEY from environment or /secrets/env.json.
"""

import json
import logging
import os
import sys
import urllib.parse
import urllib.request
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer

log = logging.getLogger("amazon_serpapi")

BASE_URL = "https://serpapi.com/search.json"
TIMEOUT = 30
SECRETS_PATH = os.environ.get("SECRETS_PATH", "/secrets/env.json")
CACHE_DIR = Path(os.environ.get("SERPAPI_CACHE_DIR", "/workspace/data/serpapi_cache"))
CACHE_TTL_SECONDS = 3600 * 4  # 4-hour cache for live shopping sessions

server = MCPServer("amazon-search")


def get_api_key() -> str:
    """Retrieve SerpApi API key from env or secrets file."""
    key = os.environ.get("SERPAPI_API_KEY", "")
    if not key and os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                key = data.get("SERPAPI_API_KEY", "")
        except Exception as e:
            log.warning(f"Failed to read {SECRETS_PATH}: {e}")
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
            json.dump({"cached_at": time.time(), "data": data}, f)
    except Exception as e:
        log.debug(f"Cache write error: {e}")


def _fetch_serpapi(params: Dict[str, Any], use_cache: bool = True) -> Dict[str, Any]:
    """Execute request to SerpApi endpoint with intelligent disk caching."""
    # Build cache key from query parameters (excluding api_key)
    sorted_params = sorted([(k, str(v)) for k, v in params.items() if k != "api_key"])
    cache_str = urllib.parse.urlencode(sorted_params)
    cache_key = hashlib.sha256(cache_str.encode("utf-8")).hexdigest()

    if use_cache:
        cached_data = _get_cache(cache_key)
        if cached_data is not None:
            return {"ok": True, "data": cached_data, "cached": True}

    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "SERPAPI_API_KEY not found in environment or /secrets/env.json"}

    params_with_key = dict(params)
    params_with_key["api_key"] = api_key
    url = f"{BASE_URL}?{urllib.parse.urlencode(params_with_key)}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Zero-Amazon-Tool/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                return {"ok": False, "error": data["error"]}
            if use_cache:
                _set_cache(cache_key, data)
            return {"ok": True, "data": data, "cached": False}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("error", f"HTTP {e.code}: {e.reason}")
        except Exception:
            err_msg = f"HTTP {e.code}: {e.reason}"
        return {"ok": False, "error": err_msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def search_amazon(
    query: str,
    amazon_domain: str = "amazon.com",
    page: int = 1,
    sort_by: str = "",
    limit: int = 10
) -> str:
    """
    Search Amazon for products.

    Args:
        query: Search keywords (e.g. "USB C hub 4k 60hz").
        amazon_domain: Regional Amazon domain (e.g. "amazon.com", "amazon.co.uk", "amazon.de", "amazon.ca").
        page: Result page number (default 1).
        sort_by: Optional sorting: 'price-asc-rank', 'price-desc-rank', 'review-rank', 'date-desc-rank'.
        limit: Maximum number of products to return (default 10).
    """
    q = (query or "").strip()
    if not q:
        return json.dumps({"ok": False, "error": "Query parameter is required"})

    params = {
        "engine": "amazon",
        "k": q,
        "amazon_domain": amazon_domain,
        "page": page,
    }
    if sort_by:
        params["s"] = sort_by

    res = _fetch_serpapi(params)
    if not res.get("ok"):
        return json.dumps(res)

    raw = res["data"]
    organic = raw.get("organic_results", [])
    products: List[Dict[str, Any]] = []

    for item in organic[:limit]:
        products.append({
            "asin": item.get("asin"),
            "title": item.get("title"),
            "price": item.get("price_string") or item.get("price"),
            "original_price": item.get("original_price"),
            "rating": item.get("rating"),
            "reviews_total": item.get("reviews_total"),
            "is_prime": item.get("is_prime", False),
            "link": item.get("link"),
            "thumbnail": item.get("thumbnail"),
        })

    return json.dumps({
        "ok": True,
        "query": q,
        "domain": amazon_domain,
        "page": page,
        "total_returned": len(products),
        "products": products,
    }, indent=2)


@server.tool()
def get_amazon_product(asin: str, amazon_domain: str = "amazon.com") -> str:
    """
    Fetch comprehensive product specifications, pricing, description, and variants for an ASIN.

    Args:
        asin: Amazon Product Standard Identification Number (e.g. "B09V3HN1KC").
        amazon_domain: Regional Amazon domain (default "amazon.com").
    """
    asin_str = (asin or "").strip().upper()
    if not asin_str:
        return json.dumps({"ok": False, "error": "ASIN is required"})

    params = {
        "engine": "amazon_product",
        "asin": asin_str,
        "amazon_domain": amazon_domain,
    }

    res = _fetch_serpapi(params)
    if not res.get("ok"):
        return json.dumps(res)

    raw = res["data"]
    prod = raw.get("product_results", {})

    result = {
        "ok": True,
        "asin": prod.get("asin", asin_str),
        "title": prod.get("title"),
        "price": prod.get("price_string") or prod.get("price"),
        "original_price": prod.get("original_price"),
        "brand": prod.get("brand"),
        "rating": prod.get("rating"),
        "reviews_count": prod.get("reviews_count"),
        "availability": prod.get("availability"),
        "is_prime": prod.get("is_prime", False),
        "feature_bullets": prod.get("feature_bullets", []),
        "description": prod.get("description"),
        "specifications": prod.get("specifications", []),
        "images": [img.get("link") for img in prod.get("images", []) if isinstance(img, dict) and img.get("link")][:5],
        "variants": [
            {
                "asin": v.get("asin"),
                "title": v.get("title"),
                "price": v.get("price"),
                "available": v.get("available", True),
            }
            for v in prod.get("variants", [])[:10]
        ],
        "link": prod.get("link"),
    }

    return json.dumps(result, indent=2)


@server.tool()
def get_amazon_reviews(
    asin: str,
    amazon_domain: str = "amazon.com",
    limit: int = 10
) -> str:
    """
    Fetch customer product reviews, review breakdown, and AI summary for an Amazon ASIN.

    Args:
        asin: Amazon Product ASIN.
        amazon_domain: Regional domain (default "amazon.com").
        limit: Number of top reviews to return (default 10).
    """
    asin_str = (asin or "").strip().upper()
    if not asin_str:
        return json.dumps({"ok": False, "error": "ASIN is required"})

    params = {
        "engine": "amazon_product",
        "asin": asin_str,
        "amazon_domain": amazon_domain,
    }

    res = _fetch_serpapi(params)
    if not res.get("ok"):
        return json.dumps(res)

    raw = res["data"]
    reviews_info = raw.get("reviews_information", {})
    prod = raw.get("product_results", {})

    top_reviews_raw = reviews_info.get("top_reviews", [])
    reviews = []
    for r in top_reviews_raw[:limit]:
        reviews.append({
            "title": r.get("title"),
            "rating": r.get("rating"),
            "author": r.get("author"),
            "date": r.get("date"),
            "verified_purchase": r.get("verified_purchase", False),
            "helpful_votes": r.get("helpful_votes"),
            "text": r.get("text"),
        })

    return json.dumps({
        "ok": True,
        "asin": asin_str,
        "product_title": prod.get("title"),
        "average_rating": prod.get("rating") or reviews_info.get("rating"),
        "total_reviews": prod.get("reviews_count") or reviews_info.get("total_reviews"),
        "summary": reviews_info.get("summary"),
        "ratings_breakdown": reviews_info.get("ratings_breakdown", {}),
        "top_reviews": reviews,
    }, indent=2)


@server.tool()
def get_amazon_product_and_reviews(
    asin: str,
    amazon_domain: str = "amazon.com",
    reviews_limit: int = 5
) -> str:
    """
    Fetch consolidated product details, specifications, price, seller info, and top customer reviews in a single tool call.

    Args:
        asin: Amazon Product ASIN (e.g. "B0B94MF4LP").
        amazon_domain: Regional domain (default "amazon.com").
        reviews_limit: Max top customer reviews to include (default 5).
    """
    asin_str = (asin or "").strip().upper()
    if not asin_str:
        return json.dumps({"ok": False, "error": "ASIN is required"})

    params = {
        "engine": "amazon_product",
        "asin": asin_str,
        "amazon_domain": amazon_domain,
    }

    res = _fetch_serpapi(params)
    if not res.get("ok"):
        return json.dumps(res)

    raw = res["data"]
    prod = raw.get("product_results", {})
    reviews_info = raw.get("reviews_information", {})

    top_reviews_raw = reviews_info.get("top_reviews", [])
    reviews = []
    for r in top_reviews_raw[:reviews_limit]:
        reviews.append({
            "title": r.get("title"),
            "rating": r.get("rating"),
            "author": r.get("author"),
            "date": r.get("date"),
            "verified_purchase": r.get("verified_purchase", False),
            "helpful_votes": r.get("helpful_votes"),
            "text": r.get("text"),
        })

    return json.dumps({
        "ok": True,
        "cached": res.get("cached", False),
        "asin": prod.get("asin", asin_str),
        "title": prod.get("title"),
        "brand": prod.get("brand"),
        "price": prod.get("price_string") or prod.get("price"),
        "original_price": prod.get("original_price"),
        "rating": prod.get("rating") or reviews_info.get("rating"),
        "reviews_count": prod.get("reviews_count") or reviews_info.get("total_reviews"),
        "availability": prod.get("availability"),
        "is_prime": prod.get("is_prime", False),
        "seller": prod.get("merchant_info") or prod.get("seller") or "Amazon / Brand Store",
        "feature_bullets": prod.get("feature_bullets", [])[:5],
        "specifications": prod.get("specifications", [])[:8],
        "review_summary": reviews_info.get("summary"),
        "ratings_breakdown": reviews_info.get("ratings_breakdown", {}),
        "top_reviews": reviews,
        "link": prod.get("link"),
    }, indent=2)


# ==========================================
# CLI Interface
# ==========================================
def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 amazon_serpapi.py search <query> [--domain=amazon.com] [--limit=10] [--sort=price-asc-rank]")
        print("  python3 amazon_serpapi.py product <asin> [--domain=amazon.com]")
        print("  python3 amazon_serpapi.py reviews <asin> [--sort=helpful|recent] [--page=1]")
        print("  python3 amazon_serpapi.py full <asin> [--domain=amazon.com] [--limit=5]")
        print("  python3 amazon_serpapi.py --sse [--port=8768]")
        print("  python3 amazon_serpapi.py --stdio")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "--sse" or "--sse" in sys.argv:
        port = 8768
        for arg in sys.argv:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
            elif arg == "--port" and sys.argv.index(arg) + 1 < len(sys.argv):
                port = int(sys.argv[sys.argv.index(arg) + 1])
        print(f"Starting Amazon Search MCP Server on SSE port {port}...")
        server.run(transport="sse", host="127.0.0.1", port=port)
        return

    if cmd == "--stdio":
        server.run(transport="stdio")
        return

    domain = "amazon.com"
    limit = 10
    sort_by = ""
    page = 1

    for arg in sys.argv[2:]:
        if arg.startswith("--domain="):
            domain = arg.split("=", 1)[1]
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
        elif arg.startswith("--sort="):
            sort_by = arg.split("=", 1)[1]
        elif arg.startswith("--page="):
            page = int(arg.split("=", 1)[1])

    if cmd == "search":
        if len(sys.argv) < 3:
            print("Error: search query required")
            sys.exit(1)
        query = sys.argv[2]
        print(search_amazon(query=query, amazon_domain=domain, page=page, sort_by=sort_by, limit=limit))
    elif cmd == "product":
        if len(sys.argv) < 3:
            print("Error: ASIN required")
            sys.exit(1)
        asin = sys.argv[2]
        print(get_amazon_product(asin=asin, amazon_domain=domain))
    elif cmd == "reviews":
        if len(sys.argv) < 3:
            print("Error: ASIN required")
            sys.exit(1)
        asin = sys.argv[2]
        print(get_amazon_reviews(asin=asin, amazon_domain=domain, limit=limit))
    elif cmd in ("full", "details"):
        if len(sys.argv) < 3:
            print("Error: ASIN required")
            sys.exit(1)
        asin = sys.argv[2]
        print(get_amazon_product_and_reviews(asin=asin, amazon_domain=domain, reviews_limit=limit))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
