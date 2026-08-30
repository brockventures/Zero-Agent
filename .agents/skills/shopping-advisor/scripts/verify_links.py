#!/usr/bin/env python3
"""Pre-flight verify retail URLs to ensure they return HTTP 200 before presenting to user."""

import sys
import urllib.request
import urllib.parse

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def verify_url(url: str, timeout: int = 8) -> bool:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 301, 302, 307, 308)
    except Exception as e:
        return False

def make_canonical_search_url(retailer: str, query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    retailer = retailer.lower().strip()
    if "zappos" in retailer:
        return f"https://www.zappos.com/search?term={encoded}"
    elif "amazon" in retailer:
        return f"https://www.amazon.com/s?k={encoded}"
    elif "rei" in retailer:
        return f"https://www.rei.com/search?q={encoded}"
    elif "brooks" in retailer:
        return f"https://www.brooksrunning.com/en_us/search?q={encoded}"
    elif "target" in retailer:
        return f"https://www.target.com/s?searchTerm={encoded}"
    return f"https://www.google.com/search?q={encoded}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: verify_links.py <url1> [url2 ...]")
        sys.exit(1)
    
    all_valid = True
    for u in sys.argv[1:]:
        ok = verify_url(u)
        status_tag = "✅ VALID" if ok else "❌ BROKEN"
        print(f"[{status_tag}] {u}")
        if not ok:
            all_valid = False
    
    sys.exit(0 if all_valid else 1)
