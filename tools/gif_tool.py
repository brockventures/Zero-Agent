#!/usr/bin/env python3
"""
Reaction GIF Tool for Discord Banter (Dynamic-First with OCR Safety & Anti-Repetition)
Prioritizes contextual live search, verifies HTTP 200, runs OCR over animation frames
to reject toxic/out-of-pocket text, tracks recent history, and formats properly titled markdown links.
"""
import sys, os, re, json, random, urllib.request, urllib.parse, io
from pathlib import Path

HISTORY_FILE = Path("/workspace/data/gif_history.json")

FILLER_WORDS = {
    "gif", "gifs", "quality", "intergalactic", "thumbnail", "reaction",
    "reactions", "hd", "video", "clip", "clips", "trending"
}

BLOCKED_OCR_TERMS = {
    "nigger", "nigga", "faggot", "retard", "cunt", "bitch", "pussy",
    "cock", "dick", "tits", "boobs", "nude", "porn", "nsfw", "sex",
    "hitler", "nazi", "swastika", "kill yourself", "kys"
}

CONTRACTION_MAP = {
    "ive": "I've", "im": "I'm", "dont": "Don't", "cant": "Can't",
    "wont": "Won't", "youre": "You're", "thats": "That's", "theyre": "They're",
    "didnt": "Didn't", "isnt": "Isn't", "arent": "Aren't", "wasnt": "Wasn't",
    "id": "I'd", "youll": "You'll", "well": "We'll", "theres": "There's"
}

FRANCHISE_SIGNATURES = {
    "arrested_development": {
        "display_name": "Arrested Development",
        "keywords": [
            "arrested development", "lucille bluth", "michael bluth", "gob bluth",
            "buster bluth", "tobias funke", "george bluth", "george michael",
            "maeby bluth", "lindsay bluth", "banana stand", "dead dove",
            "huge mistake", "lucille 2", "motherboy", "oscar bluth", "ann veal",
            "good for her", "gene parmesan", "carl weathers stew", "blue myself",
            "anustart", "there's always money", "bluth", "lucille"
        ]
    },
    "silicon_valley": {
        "display_name": "Silicon Valley",
        "keywords": [
            "silicon valley", "gilfoyle", "dinesh", "erlich bachman",
            "jared dunn", "richard hendricks", "gavin belson", "jian yang",
            "russ hanneman", "pied piper", "always blue", "aviato",
            "middle out", "not hotdog"
        ]
    },
    "curb_your_enthusiasm": {
        "display_name": "Curb Your Enthusiasm",
        "keywords": [
            "curb your enthusiasm", "larry david", "leon black", "susie greene",
            "jeff greene", "cheryl david", "pretty pretty good", "social assassin",
            "spite store", "curb"
        ]
    },
    "i_think_you_should_leave": {
        "display_name": "I Think You Should Leave (ITYSL)",
        "keywords": [
            "i think you should leave", "itysl", "tim robinson", "dan flashes",
            "calico cut pants", "hot dog suit", "coffin flop", "sloppy steaks",
            "corncob tv", "karl havoc", "driving crooner", "tables",
            "you sure about that", "we're all trying to find the guy"
        ]
    },
    "succession": {
        "display_name": "Succession",
        "keywords": [
            "succession", "logan roy", "kendall roy", "roman roy", "shiv roy",
            "tom wambsgans", "cousin greg", "greg hirsch", "gerri kellman",
            "connor roy", "l to the og", "waystar royco", "boar on the floor"
        ]
    },
    "veep": {
        "display_name": "Veep",
        "keywords": [
            "veep", "selina meyer", "dan egan", "amy brookhimer", "jonah ryan",
            "gary walsh", "mike mclintock", "sue wilson", "richard splett"
        ]
    },
    "30_rock": {
        "display_name": "30 Rock",
        "keywords": [
            "30 rock", "jack donaghy", "liz lemon", "tracy jordan",
            "jenna maroney", "kenneth parcell", "lemon what a week",
            "good god lemon", "werewolf bar mitzvah", "rural juror",
            "night cheese"
        ]
    },
    "parks_and_rec": {
        "display_name": "Parks & Recreation",
        "keywords": [
            "parks and rec", "parks and recreation", "ron swanson", "leslie knope",
            "april ludgate", "andy dwyer", "tom haverford", "ben wyatt",
            "chris traeger", "treat yo self", "pawnee", "duke silver"
        ]
    },
    "community": {
        "display_name": "Community",
        "keywords": [
            "community", "abed nadir", "troy barnes", "jeff winger",
            "britta perry", "annie edison", "dean pelton", "senor chang",
            "troy and abed", "streets ahead", "greendale", "pop pop",
            "darkest timeline"
        ]
    },
    "the_office": {
        "display_name": "The Office",
        "keywords": [
            "the office", "michael scott", "dwight schrute", "jim halpert",
            "pam beesly", "stanley hudson", "dunder mifflin", "creed bratton",
            "kevin malone", "thats what she said", "that's what she said"
        ]
    },
    "it_crowd": {
        "display_name": "The IT Crowd",
        "keywords": [
            "it crowd", "the it crowd", "maurice moss", "moss", "roy trenneman",
            "jen barber", "richmond avenal", "have you tried turning it off"
        ]
    },
    "seinfeld": {
        "display_name": "Seinfeld",
        "keywords": [
            "seinfeld", "george costanza", "elaine benes", "cosmo kramer",
            "jerry seinfeld", "newman", "festivus", "no soup for you",
            "serenity now"
        ]
    },
    "always_sunny": {
        "display_name": "It's Always Sunny",
        "keywords": [
            "always sunny", "its always sunny", "it's always sunny",
            "charlie kelly", "dennis reynolds", "mac mcdonald", "dee reynolds",
            "frank reynolds", "dayman", "nightman", "pepe silvia", "paddys pub"
        ]
    }
}


def detect_franchise(text: str | None) -> str | None:
    """Detect known comedic franchise from query, URL slug, title, or OCR text."""
    if not text:
        return None
    norm = text.lower().replace("-", " ").replace("_", " ")
    for fid, meta in FRANCHISE_SIGNATURES.items():
        for kw in meta["keywords"]:
            if re.search(rf"\b{re.escape(kw)}\b", norm):
                return fid
    return None


def get_runtime_gif_rules() -> dict:
    """Load GIF diversity rules from runtime_rules.json with robust fallbacks."""
    rules_path = Path("/workspace/config/runtime_rules.json")
    default_rules = {
        "enabled": True,
        "default_cooldown_turns": 5,
        "franchise_cooldowns": {
            "arrested_development": 8
        },
        "quarantined_franchises": [],
        "rotation_pool": [
            "silicon_valley", "curb_your_enthusiasm", "i_think_you_should_leave",
            "succession", "30_rock", "parks_and_rec", "community", "veep", "it_crowd"
        ]
    }
    if rules_path.exists():
        try:
            with open(rules_path) as f:
                data = json.load(f)
            return data.get("gif_diversity", default_rules)
        except Exception:
            pass
    return default_rules


def clean_slug_title(slug: str) -> str:
    """Format URL slug into a clean, properly titled human-readable hyperlink label."""
    if not slug:
        return "Reaction GIF"
    clean = re.sub(r"(-gif)?-\d+$", "", slug)
    words = [w for w in re.split(r"[-_\s]+", clean) if w]
    deduped = []
    seen = set()
    for w in words:
        wl = w.lower()
        if wl in FILLER_WORDS:
            continue
        if wl not in seen:
            seen.add(wl)
            if wl in CONTRACTION_MAP:
                formatted_w = CONTRACTION_MAP[wl]
            else:
                formatted_w = w.capitalize()
            deduped.append(formatted_w)
    title = " ".join(deduped[:6])
    return title or "Reaction GIF"


def extract_gif_ocr(media_url: str, max_samples: int = 5) -> str:
    """Download preview GIF and run OCR across sampled animation frames to detect burned-in text."""
    if not media_url:
        return ""
    try:
        from PIL import Image
        import pytesseract

        req = urllib.request.Request(
            media_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            content = resp.read()

        im = Image.open(io.BytesIO(content))
        n_frames = getattr(im, "n_frames", 1)

        extracted = []
        step = max(1, n_frames // max_samples)
        for f in range(0, n_frames, step):
            im.seek(f)
            frame = im.convert("L")
            txt = pytesseract.image_to_string(frame, timeout=1.5).strip()
            clean_line = " ".join(txt.split())
            if clean_line and clean_line not in extracted and len(clean_line) > 1:
                extracted.append(clean_line)
            if len(extracted) >= 3:
                break

        return " | ".join(extracted)
    except Exception as e:
        return ""


def is_ocr_safe(ocr_text: str) -> bool:
    """Verify OCR text does not contain offensive, toxic, or out-of-pocket terms."""
    if not ocr_text:
        return True
    lower = ocr_text.lower()
    for term in BLOCKED_OCR_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lower):
            print(f"[GIF] OCR rejected candidate containing blocked term '{term}': {ocr_text}", file=sys.stderr)
            return False
    return True


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

def load_history() -> list[dict]:
    """Load history records. Seamlessly handles legacy flat string URLs and structured dicts."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                raw = json.load(f)
            records = []
            for item in raw:
                if isinstance(item, str):
                    slug = item.split("/")[-1]
                    records.append({
                        "url": item,
                        "query": "",
                        "title": clean_slug_title(slug),
                        "franchise": detect_franchise(slug),
                        "timestamp": 0
                    })
                elif isinstance(item, dict):
                    records.append(item)
            return records
        except Exception:
            pass
    return []


def get_history_urls(history: list[dict | str] | None = None) -> set[str]:
    """Extract set of URLs from history records (supports dicts and raw strings)."""
    if history is None:
        history = load_history()
    urls = set()
    for item in history:
        if isinstance(item, str):
            urls.add(item)
        elif isinstance(item, dict) and "url" in item:
            urls.add(item["url"])
    return urls


def record_history(
    url: str,
    query: str = "",
    title: str = "",
    franchise: str | None = None
):
    """Record a GIF delivery to persistent history with franchise tracking."""
    try:
        import time
        records = load_history()
        detected_f = franchise or detect_franchise(f"{query} {title} {url}")
        records.append({
            "url": url,
            "query": query,
            "title": title or clean_slug_title(url.split("/")[-1]),
            "franchise": detected_f,
            "timestamp": int(time.time())
        })
        records = records[-100:]  # Keep last 100 to prevent repeats
        with open(HISTORY_FILE, "w") as f:
            json.dump(records, f, indent=2)
    except Exception:
        pass


def check_cooldown(
    franchise: str | None,
    history: list[dict | str] | None = None
) -> tuple[bool, int, int]:
    """
    Check if a franchise is on cooldown.
    Returns: (is_on_cooldown, distance, threshold)
    - distance: 1 = used on the most recent GIF turn, 2 = 2 turns ago, etc.
    - threshold: required number of non-franchise GIFs before it can be used again.
    """
    if not franchise:
        return False, 0, 0

    rules = get_runtime_gif_rules()
    if not rules.get("enabled", True):
        return False, 0, 0

    quarantined = rules.get("quarantined_franchises", [])
    if franchise in quarantined:
        return True, 1, 999

    threshold = rules.get("franchise_cooldowns", {}).get(
        franchise, rules.get("default_cooldown_turns", 5)
    )

    if history is None:
        history = load_history()

    distance = 1
    found = False
    for item in reversed(history):
        f = None
        if isinstance(item, dict):
            f = item.get("franchise") or detect_franchise(f"{item.get('query', '')} {item.get('title', '')} {item.get('url', '')}")
        elif isinstance(item, str):
            f = detect_franchise(item.split("/")[-1])

        if f == franchise:
            found = True
            break
        distance += 1

    if found and distance <= threshold:
        return True, distance, threshold

    return False, distance if found else 999, threshold


def get_cooldown_summary(history: list[dict | str] | None = None) -> dict:
    """Generate a summary of cooled-down franchises and eligible rotation for prompts/tools."""
    rules = get_runtime_gif_rules()
    active_cooldowns = {}
    for fid, meta in FRANCHISE_SIGNATURES.items():
        is_cd, dist, thresh = check_cooldown(fid, history=history)
        if is_cd:
            active_cooldowns[fid] = {
                "display_name": meta["display_name"],
                "distance": dist,
                "threshold": thresh,
                "remaining": max(1, thresh - dist + 1)
            }

    pool = rules.get("rotation_pool", list(FRANCHISE_SIGNATURES.keys()))
    eligible = [
        FRANCHISE_SIGNATURES[f]["display_name"]
        for f in pool
        if f in FRANCHISE_SIGNATURES and f not in active_cooldowns
    ]

    return {
        "cooldowns": active_cooldowns,
        "eligible": eligible
    }


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
            figures = re.findall(
                r'<figure[^>]*>.*?<a[^>]+href=\"(/view/[^\"]+)\"[^>]*>.*?<img[^>]+src=\"(https://media[^\"]+\.gif)\"',
                html,
                re.DOTALL
            )
            seen = set()
            for m, media_url in figures:
                if m in seen:
                    continue
                seen.add(m)
                full_url = "https://tenor.com" + m
                raw_slug = m.split("/view/")[-1]
                title = clean_slug_title(raw_slug)
                results.append({
                    "title": title,
                    "url": full_url,
                    "media_url": media_url
                })
                if len(results) >= limit:
                    break

            if len(results) < limit:
                matches = re.findall(r'href=\"(/view/[^\"]+)\"', html)
                for m in matches:
                    if m in seen:
                        continue
                    seen.add(m)
                    full_url = "https://tenor.com" + m
                    raw_slug = m.split("/view/")[-1]
                    title = clean_slug_title(raw_slug)
                    results.append({
                        "title": title,
                        "url": full_url,
                        "media_url": None
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
            gifs = re.findall(r'https://media\d*\.giphy\.com/media/[a-zA-Z0-9]+/[^\"]+\.gif', html)
            seen = set()
            for idx, m in enumerate(matches):
                full_url = f"https://giphy.com/gifs/{m}"
                if full_url in seen:
                    continue
                seen.add(full_url)
                raw_slug = re.sub(r"-[a-zA-Z0-9]+$", "", m)
                title = clean_slug_title(raw_slug) if raw_slug else m
                media_url = gifs[idx] if idx < len(gifs) else None
                results.append({
                    "title": title,
                    "url": full_url,
                    "media_url": media_url
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[GIF] Giphy search error: {e}", file=sys.stderr)
    return results


def get_contextual_gif(query: str, run_ocr: bool = True, force: bool = False) -> dict:
    """
    Dynamic GIF Picker with OCR Safety & Franchise Cooldown Verification:
    1. Primary: Searches Tenor, filters against history & cooled-down franchises, verifies HTTP 200, checks OCR.
    2. Fallback: Searches Giphy, filters against history & cooled-down franchises, verifies HTTP 200, checks OCR.
    3. Formats properly titled markdown hyperlinks: [Title](URL).
    4. Skip: If both providers return no valid links, returns None.
    """
    # 0. If query explicitly targets a franchise on cooldown, reject early unless force=True
    q_franchise = detect_franchise(query)
    if q_franchise and not force:
        is_cd, dist, thresh = check_cooldown(q_franchise)
        if is_cd:
            meta = FRANCHISE_SIGNATURES.get(q_franchise, {})
            disp = meta.get("display_name", q_franchise)
            summary = get_cooldown_summary()
            eligible_str = ", ".join(summary["eligible"][:6]) or "general queries"
            msg = (
                f"Franchise '{disp}' is on cooldown ({dist}/{thresh} turns). "
                f"Eligible rotation: {eligible_str}."
            )
            print(f"[GIF] Cooldown blocked query '{query}': {msg}", file=sys.stderr)
            return {
                "title": None,
                "url": None,
                "markdown": None,
                "ocr_text": "",
                "source": "cooldown_blocked",
                "error": msg,
                "franchise": q_franchise
            }

    raw_history = load_history()
    history_urls = get_history_urls(raw_history)

    # Tier 1: Dynamic Tenor search
    tenor_candidates = search_tenor(query, limit=8)
    tenor_fresh = [c for c in tenor_candidates if c["url"] not in history_urls]
    tenor_pool = tenor_fresh if tenor_fresh else tenor_candidates

    valid_tenor = []
    for c in tenor_pool:
        c_slug = c["url"].split("/")[-1]
        c_franchise = detect_franchise(f"{query} {c.get('title', '')} {c_slug}")
        if c_franchise and not force:
            is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
            if is_cd:
                print(f"[GIF] Candidate skipped: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                continue

        if is_valid_gif_url(c["url"]):
            ocr_text = ""
            if run_ocr and c.get("media_url"):
                ocr_text = extract_gif_ocr(c["media_url"])
                if not is_ocr_safe(ocr_text):
                    continue
                if not c_franchise:
                    c_franchise = detect_franchise(ocr_text)
                    if c_franchise and not force:
                        is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
                        if is_cd:
                            print(f"[GIF] Candidate skipped by OCR: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                            continue

            c["ocr_text"] = ocr_text
            c["franchise"] = c_franchise
            valid_tenor.append(c)
        if len(valid_tenor) >= 3:
            break

    if valid_tenor:
        pick = random.choice(valid_tenor)
        title = pick["title"]
        pick_f = pick.get("franchise") or detect_franchise(f"{query} {title} {pick['url']}")
        record_history(pick["url"], query=query, title=title, franchise=pick_f)
        return {
            "title": title,
            "url": pick["url"],
            "markdown": f"[{title}]({pick['url']})",
            "ocr_text": pick.get("ocr_text", ""),
            "source": "dynamic_tenor",
            "franchise": pick_f
        }

    # Tier 2: Dynamic Giphy fallback
    giphy_candidates = search_giphy(query, limit=8)
    giphy_fresh = [c for c in giphy_candidates if c["url"] not in history_urls]
    giphy_pool = giphy_fresh if giphy_fresh else giphy_candidates

    valid_giphy = []
    for c in giphy_pool:
        c_slug = c["url"].split("/")[-1]
        c_franchise = detect_franchise(f"{query} {c.get('title', '')} {c_slug}")
        if c_franchise and not force:
            is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
            if is_cd:
                print(f"[GIF] Giphy candidate skipped: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                continue

        if is_valid_gif_url(c["url"]):
            ocr_text = ""
            if run_ocr and c.get("media_url"):
                ocr_text = extract_gif_ocr(c["media_url"])
                if not is_ocr_safe(ocr_text):
                    continue
                if not c_franchise:
                    c_franchise = detect_franchise(ocr_text)
                    if c_franchise and not force:
                        is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
                        if is_cd:
                            print(f"[GIF] Giphy candidate skipped by OCR: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                            continue

            c["ocr_text"] = ocr_text
            c["franchise"] = c_franchise
            valid_giphy.append(c)
        if len(valid_giphy) >= 3:
            break

    if valid_giphy:
        pick = random.choice(valid_giphy)
        title = pick["title"]
        pick_f = pick.get("franchise") or detect_franchise(f"{query} {title} {pick['url']}")
        record_history(pick["url"], query=query, title=title, franchise=pick_f)
        return {
            "title": title,
            "url": pick["url"],
            "markdown": f"[{title}]({pick['url']})",
            "ocr_text": pick.get("ocr_text", ""),
            "source": "dynamic_giphy",
            "franchise": pick_f
        }

    # Tier 3: Graceful skip (no hardcoded static URLs)
    return {
        "title": None,
        "url": None,
        "markdown": None,
        "ocr_text": "",
        "source": "skip",
        "franchise": None
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: gif_tool.py <query> [--force] [--cooldowns]")
        sys.exit(0)

    args = sys.argv[1:]
    if "--cooldowns" in args:
        summary = get_cooldown_summary()
        print(json.dumps(summary, indent=2))
        sys.exit(0)

    force = False
    if "--force" in args:
        force = True
        args.remove("--force")

    q = " ".join(args)
    res = get_contextual_gif(q, force=force)
    print(json.dumps(res, indent=2))
