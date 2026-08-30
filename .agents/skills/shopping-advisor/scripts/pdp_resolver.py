#!/usr/bin/env python3
"""Resolves exact, direct Product Detail Page (PDP) URLs from retailer search results
using our self-hosted Browserless Chromium container on Host 2 (127.0.0.1:3000).
"""

import sys
import os
import json
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup

# Try importing local browser_tool
sys.path.insert(0, "/workspace")
try:
    from tools.browser_tool import get_content
    HAS_BROWSERLESS = True
except Exception:
    HAS_BROWSERLESS = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_html(url: str) -> str:
    """Fetch HTML via Browserless container if available, otherwise direct HTTP."""
    if HAS_BROWSERLESS:
        try:
            return get_content(url, wait_for_timeout=2000)
        except Exception as e:
            print(f"[PDP Resolver] Browserless fetch failed, falling back to direct HTTP: {e}", file=sys.stderr)
    
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def resolve_zappos_pdp(query: str) -> list[dict]:
    search_url = f"https://www.zappos.com/search?term={urllib.parse.quote_plus(query)}"
    results = []
    try:
        html = fetch_html(search_url)
        soup = BeautifulSoup(html, "html.parser")
        seen_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/p/") and "product" in href:
                full_url = f"https://www.zappos.com{href}"
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    title = a.get_text(" ", strip=True) or a.get("aria-label", "")
                    if len(title) > 3:
                        results.append({
                            "url": full_url,
                            "title": title,
                            "retailer": "Zappos"
                        })
    except Exception as e:
        print(f"[PDP Resolver] Error resolving Zappos PDP: {e}", file=sys.stderr)
    return results

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pdp_resolver.py <query>")
        sys.exit(1)
    
    q = " ".join(sys.argv[1:])
    pdp_results = resolve_zappos_pdp(q)
    print(json.dumps(pdp_results[:5], indent=2))
