#!/usr/bin/env python3
"""
Reaction GIF Tool for Discord Banter (Dynamic-First with Anti-Repetition)
Prioritizes contextual live search, randomizes across top matches,
and tracks recent history to prevent repetition.
"""
import sys, os, re, json, random, urllib.request, urllib.parse
from pathlib import Path

HISTORY_FILE = Path("/workspace/data/gif_history.json")

CURATED_FALLBACKS = {
    "eye_roll": "https://tenor.com/view/april-parks-and-rec-sass-no-ew-gif-9121879",
    "shrug": "https://tenor.com/view/jordan-shrug-shrugging-michael-jordan-mj-gif-4923836",
    "stare": "https://tenor.com/view/jim-halpert-camera-stare-jim-look-jim-stare-the-office-gif-14191171665883802076",
    "disbelief": "https://tenor.com/view/in-disbelief-gif-13763291379473080094",
    "nod": "https://tenor.com/view/robert-redford-robert-redford-agreed-nod-gif-6882050326903489161",
    "facepalm": "https://tenor.com/view/star-trek-gif-12095420689275725462",
    "popcorn": "https://tenor.com/view/popcorn-micaheljackson-jackson-thriller-gif-7723623",
    "cheers": "https://tenor.com/view/great-gatsby-cheers-leonardo-dicaprio-toast-gif-7901048",
    "mic_drop": "https://tenor.com/view/drop-the-mic-obama-mic-drop-gif-13109295",
    "smug": "https://tenor.com/view/the-unbearable-weight-of-massive-gif-27604173"
}

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

def get_contextual_gif(query: str) -> dict:
    """
    Primary Dynamic Picker:
    Searches Tenor for the specific conversational context, filters out
    recently used GIFs to eliminate repeats, and selects from top candidates.
    """
    history = set(load_history())
    candidates = search_tenor(query, limit=8)

    # Filter out anything used recently
    fresh_candidates = [c for c in candidates if c["url"] not in history]
    pool = fresh_candidates if fresh_candidates else candidates

    if pool:
        # Pick randomly from the top 3 fresh candidates for organic variety
        pick = random.choice(pool[:3])
        record_history(pick["url"])
        return {
            "title": pick["title"],
            "url": pick["url"],
            "source": "dynamic_search"
        }

    # Emergency fallback only if search fails completely
    fallback_key = "shrug" if "shrug" in query.lower() else "eye_roll"
    fb_url = CURATED_FALLBACKS.get(fallback_key, CURATED_FALLBACKS["eye_roll"])
    record_history(fb_url)
    return {
        "title": fallback_key.replace("_", " ").title(),
        "url": fb_url,
        "source": "fallback"
    }

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: gif_tool.py <query>")
        sys.exit(0)
    
    q = " ".join(sys.argv[1:])
    res = get_contextual_gif(q)
    print(json.dumps(res, indent=2))
