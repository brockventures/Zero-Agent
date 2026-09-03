#!/usr/bin/env python3
"""
Reaction GIF Tool for Discord Banter (Dynamic-First with Anti-Repetition)
Prioritizes contextual live search, randomizes across top matches,
and tracks recent history to prevent repetition.
"""
import sys, os, re, json, random, urllib.request, urllib.parse
from pathlib import Path

HISTORY_FILE = Path("/workspace/data/gif_history.json")


def is_valid_gif_url(url: str, timeout: float = 2.5) -> bool:
    """Fast HTTP HEAD probe to verify a Tenor GIF URL returns HTTP 200 OK before delivering."""
    if not url or not url.startswith("http"):
        return False
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False

def load_history() -> list[str]:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def record_history(url: str):
    try:
        hist = load_history()
        hist.append(url)
        hist = hist[-100:]  # Keep last 100 to prevent repeats
        with open(HISTORY_FILE, "w") as f:
            json.dump(hist, f, indent=2)
    except Exception:
        pass

def search_tenor(query: str, limit: int = 6) -> list[dict]:
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", "", query).strip()
    slug = urllib.parse.quote(clean_q.replace(" ", "-"))
    url = f"https://tenor.com/search/{slug}-gifs"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    results = []
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            matches = re.findall(r'href=\"(/view/[^\"]+)\"', html)
            seen = set()
            for m in matches:
                if m in seen:
                    continue
                seen.add(m)
                full_url = "https://tenor.com" + m
                raw_slug = m.split("/view/")[-1]
                title = re.sub(r'(-gif)?-\d+$', '', raw_slug).replace('-', ' ')
                results.append({
                    "title": title,
                    "url": full_url
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[GIF] Search error: {e}", file=sys.stderr)
    return results

def search_giphy(query: str, limit: int = 6) -> list[dict]:
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", "", query).strip()
    slug = urllib.parse.quote(clean_q.replace(" ", "-"))
    url = f"https://giphy.com/search/{slug}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    results = []
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            matches = re.findall(r"https://giphy\.com/gifs/([a-zA-Z0-9_-]+)", html)
            seen = set()
            for m in matches:
                full_url = f"https://giphy.com/gifs/{m}"
                if full_url in seen:
                    continue
                seen.add(full_url)
                slug_part = re.sub(r"-[a-zA-Z0-9]+$", "", m)
                title = slug_part.replace("-", " ") if slug_part else m
                results.append({
                    "title": title,
                    "url": full_url
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[GIF] Giphy search error: {e}", file=sys.stderr)
    return results

def get_contextual_gif(query: str) -> dict:
    """
    Dynamic GIF Picker:
    1. Primary: Searches Tenor, filters against history, verifies HTTP 200.
    2. Fallback: Searches Giphy, filters against history, verifies HTTP 200.
    3. Skip: If both providers return no valid links, returns None (no hardcoded URLs).
    """
    history = set(load_history())

    # Tier 1: Dynamic Tenor search
    tenor_candidates = search_tenor(query, limit=8)
    tenor_fresh = [c for c in tenor_candidates if c["url"] not in history]
    tenor_pool = tenor_fresh if tenor_fresh else tenor_candidates

    valid_tenor = []
    for c in tenor_pool:
        if is_valid_gif_url(c["url"]):
            valid_tenor.append(c)
        if len(valid_tenor) >= 3:
            break

    if valid_tenor:
        pick = random.choice(valid_tenor)
        record_history(pick["url"])
        return {
            "title": pick["title"],
            "url": pick["url"],
            "source": "dynamic_tenor"
        }

    # Tier 2: Dynamic Giphy fallback
    giphy_candidates = search_giphy(query, limit=8)
    giphy_fresh = [c for c in giphy_candidates if c["url"] not in history]
    giphy_pool = giphy_fresh if giphy_fresh else giphy_candidates

    valid_giphy = []
    for c in giphy_pool:
        if is_valid_gif_url(c["url"]):
            valid_giphy.append(c)
        if len(valid_giphy) >= 3:
            break

    if valid_giphy:
        pick = random.choice(valid_giphy)
        record_history(pick["url"])
        return {
            "title": pick["title"],
            "url": pick["url"],
            "source": "dynamic_giphy"
        }

    # Tier 3: Graceful skip (no hardcoded static URLs)
    return {
        "title": None,
        "url": None,
        "source": "skip"
    }

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: gif_tool.py <query>")
        sys.exit(0)
    
    q = " ".join(sys.argv[1:])
    res = get_contextual_gif(q)
    print(json.dumps(res, indent=2))
