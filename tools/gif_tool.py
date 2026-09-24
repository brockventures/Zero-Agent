#!/usr/bin/env python3
"""
Reaction GIF Tool for Discord Banter (Dynamic-First with OCR Safety & Anti-Repetition)
Prioritizes contextual live search, verifies HTTP 200, runs OCR over animation frames
to reject toxic/out-of-pocket text, tracks recent history, and formats properly titled markdown links.
"""
import sys, os, re, json, random, urllib.request, urllib.parse, io, sqlite3
from pathlib import Path

HISTORY_FILE = Path("/workspace/data/gif_history.json")
FAILURE_LOG_FILE = Path("/workspace/data/gif_failures.jsonl")
CANONICAL_GIFS_FILE = Path("/workspace/data/canonical_gifs.json")

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
    },
    "brooklyn_nine_nine": {
        "display_name": "Brooklyn Nine-Nine",
        "keywords": [
            "brooklyn nine-nine", "brooklyn 99", "b99", "captain holt",
            "raymond holt", "jake peralta", "amy santiago", "rosa diaz",
            "terry jeffords", "charles boyle", "vindication", "nine nine"
        ]
    },
    "reaction_classics": {
        "display_name": "Reaction Classics",
        "keywords": [
            "antonio banderas", "assassins", "doc rivers", "disbelief",
            "ted striker", "airplane movie", "airplane!"
        ]
    },
    "nathan_for_you": {
        "display_name": "Nathan for You",
        "keywords": [
            "nathan for you", "nathan fielder", "nfy", "the rehearsal"
        ]
    },
    "jurassic_park": {
        "display_name": "Jurassic Park",
        "keywords": [
            "jurassic park", "dennis nedry", "nedry", "ray arnold",
            "magic word", "hold onto your butts"
        ]
    },
    "office_space": {
        "display_name": "Office Space",
        "keywords": [
            "office space", "bill lumbergh", "lumbergh", "that would be great",
            "milton", "red stapler", "peter gibbons"
        ]
    },
    "the_simpsons": {
        "display_name": "The Simpsons",
        "keywords": [
            "the simpsons", "simpsons", "homer simpson", "bart simpson",
            "lisa simpson", "marge simpson", "mr burns", "ned flanders"
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
        "default_cooldown_turns": 2,
        "franchise_cooldowns": {
            "arrested_development": 4
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


def extract_gif_ocr(media_url: str, max_samples: int = 2) -> str:
    """Download preview GIF and run OCR across sampled animation frames to detect burned-in text."""
    if not media_url:
        return ""
    try:
        from PIL import Image
        import pytesseract
        import concurrent.futures

        req = urllib.request.Request(
            media_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            content = resp.read()

        im = Image.open(io.BytesIO(content))
        n_frames = getattr(im, "n_frames", 1)

        if n_frames <= 1:
            sample_frames = [0]
        elif max_samples <= 1:
            sample_frames = [n_frames // 2]
        elif max_samples == 2:
            sample_frames = [int(n_frames * 0.35), int(n_frames * 0.75)]
        else:
            step = max(1, n_frames // max_samples)
            sample_frames = list(range(0, n_frames, step))[:max_samples]

        frames_to_ocr = []
        for f in sample_frames:
            try:
                im.seek(f)
                frames_to_ocr.append(im.convert("L"))
            except Exception:
                pass

        if not frames_to_ocr:
            return ""

        def _ocr_frame(f_img):
            try:
                return pytesseract.image_to_string(f_img, timeout=1.5).strip()
            except Exception:
                return ""

        if len(frames_to_ocr) == 1:
            texts = [_ocr_frame(frames_to_ocr[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(frames_to_ocr))) as pool:
                texts = list(pool.map(_ocr_frame, frames_to_ocr))

        extracted = []
        for txt in texts:
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
    """Fast HTTP HEAD probe to verify a Tenor GIF URL returns HTTP 200 OK before delivering.
    Rejects hallucinated Tenor URLs where arbitrary numerical IDs redirect to a completely different slug."""
    if not url or not url.startswith("http"):
        return False
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            raw_geturl = resp.geturl() if hasattr(resp, "geturl") else url
            final_url = raw_geturl if isinstance(raw_geturl, str) else url
            if "tenor.com/view/" in url and final_url != url:
                orig_slug = url.split("/view/")[-1].lower()
                final_slug = final_url.split("/view/")[-1].lower()
                orig_words = {w for w in re.findall(r"[a-z0-9]+", orig_slug) if w not in FILLER_WORDS and not w.isdigit()}
                final_words = {w for w in re.findall(r"[a-z0-9]+", final_slug) if w not in FILLER_WORDS and not w.isdigit()}
                overlap = orig_words & final_words
                if not overlap and (orig_words or final_words):
                    print(f"[GIF] Hallucinated Tenor URL rejected: {url} redirected to {final_url} (0 word overlap)", file=sys.stderr)
                    return False
            return True
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


def get_history_urls(history: list[dict | str] | None = None, limit: int = 15) -> set[str]:
    """Extract set of URLs from recent history records (supports dicts and raw strings)."""
    if history is None:
        history = load_history()
    recent = history[-limit:] if limit else history
    urls = set()
    for item in recent:
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
        detected_f = franchise or detect_franchise(f"{title} {url}")
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


def log_gif_failure(
    query: str,
    reason: str,
    details: str = "",
    franchise: str | None = None,
    candidate_url: str | None = None,
):
    """Record a structured failure entry in gif_failures.jsonl for forensic observability."""
    try:
        import datetime
        now_pt = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=-7))
        ).strftime("%Y-%m-%d %H:%M:%S PT")
        entry = {
            "timestamp": now_pt,
            "query": query,
            "reason": reason,
            "details": details,
            "franchise": franchise,
            "candidate_url": candidate_url,
        }
        FAILURE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if FAILURE_LOG_FILE.exists():
            try:
                with open(FAILURE_LOG_FILE, "r") as f:
                    lines = f.readlines()
            except Exception:
                pass
        lines.append(json.dumps(entry) + "\n")
        if len(lines) > 200:
            lines = lines[-200:]
        with open(FAILURE_LOG_FILE, "w") as f:
            f.writelines(lines)
    except Exception as e:
        print(f"[GIF] Error writing failure log: {e}", file=sys.stderr)


def get_recent_failures(limit: int = 15) -> list[dict]:
    """Retrieve recent failure entries from gif_failures.jsonl for auditing."""
    if not FAILURE_LOG_FILE.exists():
        return []
    try:
        with open(FAILURE_LOG_FILE, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
        entries = []
        for line in reversed(lines[-limit:]):
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries
    except Exception:
        return []



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
    if not rules.get("enabled", False):
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


STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "with",
    "is", "it", "on", "at", "by", "this", "that", "gif", "gifs",
    "about", "we", "you", "they", "i", "my", "your", "our", "are",
    "was", "were", "be", "been", "being", "have", "has", "had",
    "can", "could", "will", "would", "should", "just", "like",
    "into", "out", "all", "any", "some", "what", "who", "when",
    "where", "why", "how", "get", "got", "from"
}


def score_candidate(query: str, candidate: dict) -> float:
    """
    Score relevance of a candidate GIF against the search query.
    Enforces franchise integrity, token overlap, and key concept matching.
    Returns:
        float score (higher is better; negative values indicate disqualified candidates).
    """
    if not query or not candidate:
        return 0.0

    q_norm = query.lower().strip()
    c_url = candidate.get("url", "")
    slug = candidate.get("slug") or c_url.split("/")[-1]
    slug_norm = slug.lower().replace("-", " ").replace("_", " ")
    title_norm = candidate.get("title", "").lower()
    c_text = f"{title_norm} {slug_norm}"

    q_franchise = detect_franchise(query)
    c_franchise = detect_franchise(c_text)

    score = 0.0

    # 1. Franchise Integrity
    if q_franchise:
        if c_franchise == q_franchise:
            score += 40.0
        else:
            meta = FRANCHISE_SIGNATURES.get(q_franchise, {})
            kws = meta.get("keywords", [])
            if any(kw in c_text for kw in kws):
                score += 30.0
            else:
                # Disqualify: Candidate lacks requested franchise
                return -100.0

    # 2. Token Overlap
    q_words = [w for w in re.findall(r"[a-z0-9]+", q_norm) if w not in STOP_WORDS]
    if q_words:
        matched_words = 0
        for w in q_words:
            if re.search(rf"\b{re.escape(w)}\b", c_text):
                matched_words += 1
            elif w in c_text:
                matched_words += 0.5
        overlap_ratio = matched_words / len(q_words)
        score += overlap_ratio * 30.0

    # 3. Exact phrase match
    clean_q_phrase = " ".join(q_words)
    if clean_q_phrase and clean_q_phrase in c_text:
        score += 20.0

    if len(title_norm.split()) >= 2:
        score += 5.0

    return score


def search_tenor(query: str, limit: int = 12) -> list[dict]:
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", "", query).strip()
    if not clean_q:
        return []

    def _fetch_slug(slug_str: str) -> list[dict]:
        url = f"https://tenor.com/search/{slug_str}-gifs"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        res = []
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
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
                    res.append({
                        "title": title,
                        "url": full_url,
                        "media_url": media_url,
                        "slug": raw_slug
                    })
                    if len(res) >= limit:
                        break

                if len(res) < limit:
                    matches = re.findall(r'href=\"(/view/[^\"]+)\"', html)
                    for m in matches:
                        if m in seen:
                            continue
                        seen.add(m)
                        full_url = "https://tenor.com" + m
                        raw_slug = m.split("/view/")[-1]
                        title = clean_slug_title(raw_slug)
                        res.append({
                            "title": title,
                            "url": full_url,
                            "media_url": None,
                            "slug": raw_slug
                        })
                        if len(res) >= limit:
                            break
        except Exception:
            pass
        return res

    words = clean_q.split()
    primary_slug = urllib.parse.quote("-".join(words))
    results = _fetch_slug(primary_slug)

    # Fallback to 2-word slug if 3+ word slug returned nothing / 404ed
    if not results and len(words) > 2:
        secondary_slug = urllib.parse.quote("-".join(words[:2]))
        results = _fetch_slug(secondary_slug)

    return results



def search_giphy(query: str, limit: int = 12) -> list[dict]:
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
                    "media_url": media_url,
                    "slug": raw_slug
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[GIF] Giphy search error: {e}", file=sys.stderr)
    return results


def load_canonical_registry() -> list[dict]:
    """Load canonical GIF registry from disk."""
    if CANONICAL_GIFS_FILE.exists():
        try:
            with open(CANONICAL_GIFS_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[GIF] Warning loading canonical registry: {e}", file=sys.stderr)
    return []


_CANONICAL_DB_CACHE = None

def _get_canonical_fts_db(registry: list[dict]) -> sqlite3.Connection:
    global _CANONICAL_DB_CACHE
    if _CANONICAL_DB_CACHE is not None:
        return _CANONICAL_DB_CACHE

    con = sqlite3.connect(":memory:")
    con.execute("""
    CREATE VIRTUAL TABLE gifs_fts USING fts5(
        id UNINDEXED,
        franchise,
        character,
        vibes,
        situation,
        tags,
        title,
        tokenize='porter ascii'
    );
    """)
    for g in registry:
        con.execute("""
        INSERT INTO gifs_fts(id, franchise, character, vibes, situation, tags, title)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            g.get("id", ""),
            g.get("franchise", ""),
            g.get("character", ""),
            " ".join(g.get("vibes", [])).replace("_", " "),
            g.get("situation", ""),
            " ".join(g.get("tags", [])).replace("_", " "),
            g.get("title", "")
        ))
    _CANONICAL_DB_CACHE = con
    return con


def score_canonical_candidate(query: str, entry: dict, fts_score: float | None = None) -> float:
    """
    Score relevance of a canonical GIF entry against situational search queries.
    Evaluates situation descriptions, vibes, tags, character name, title, and franchise affinity.
    Higher score is better; 0.0 indicates no substantive relevance.
    """
    if not query or not entry:
        return 0.0

    q_norm = query.lower().strip()
    entry_id = str(entry.get("id", "")).lower()

    # Direct ID Match bonus (instant winner)
    if entry_id and (entry_id == q_norm or entry_id == q_norm.replace("-", "_") or entry_id.replace("_", " ") == q_norm):
        return 1000.0

    q_words = [w for w in re.findall(r"[a-z0-9]+", q_norm) if len(w) > 1 and w not in STOP_WORDS]
    if not q_words:
        return 0.0

    entry_title = str(entry.get("title", "")).lower()
    entry_char = str(entry.get("character", "")).lower()
    entry_franchise = str(entry.get("franchise", "")).lower()
    entry_vibes = [str(v).lower() for v in entry.get("vibes", [])]
    entry_tags = [str(t).lower() for t in entry.get("tags", [])]
    entry_situation = str(entry.get("situation", "")).lower()

    score = 0.0
    if fts_score is not None:
        # BM25 returns negative values (e.g. -25.0 is better than -5.0).
        # Convert to positive base boost
        score += max(0.0, -fts_score * 10.0)

    # 1. Exact Vibe & Tag Matches
    matched_words = 0
    matched_vibe = False
    clean_q_underscore = "_".join(q_words)
    for v in entry_vibes:
        if clean_q_underscore == v or any(w == v for w in q_words):
            score += 35.0
            matched_vibe = True
            matched_words += 1
        elif any(w in v.replace("_", " ").split() for w in q_words):
            score += 15.0

    for t in entry_tags:
        if clean_q_underscore == t or any(w == t for w in q_words):
            score += 20.0
            matched_words += 1
        elif any(w in t.replace("_", " ").split() for w in q_words):
            score += 15.0

    # 2. Token matches in situation, character, title
    for w in q_words:
        w_hit = False
        if re.search(rf"\b{re.escape(w)}\b", entry_situation):
            score += 25.0
            w_hit = True
        if re.search(rf"\b{re.escape(w)}\b", entry_title):
            score += 15.0
            w_hit = True
        if re.search(rf"\b{re.escape(w)}\b", entry_char):
            score += 20.0
            w_hit = True
        if w_hit:
            matched_words += 1

    # 3. Multi-word phrase matches in situation
    for i in range(len(q_words) - 1):
        pair = f"{q_words[i]} {q_words[i+1]}"
        if pair in entry_situation:
            score += 40.0
            matched_words += 2

    # 4. Franchise Affinity
    q_franchise = detect_franchise(query)
    if q_franchise and entry_franchise == q_franchise:
        score += 35.0

    # Multi-word specificity guard: if query has 2+ substantive words,
    # require at least 2 token matches, an exact vibe match, explicit franchise affinity,
    # or a strong multi-term FTS5 match (bm25 <= -4.0)
    has_strong_fts = (fts_score is not None and fts_score <= -4.0)
    if len(q_words) >= 2 and matched_words < 2 and not matched_vibe and not (q_franchise and entry_franchise == q_franchise) and not has_strong_fts:
        return 0.0

    return score


def _get_gemini_api_key() -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        return api_key
    secrets_path = Path("/secrets/env.json")
    if secrets_path.exists():
        try:
            with open(secrets_path) as f:
                data = json.load(f)
                return data.get("GEMINI_API_KEY")
        except Exception:
            pass
    return None


def _call_gemini_api_direct(prompt: str, timeout: float = 3.0) -> str | None:
    """Execute direct HTTPS call to Gemini Flash Lite REST API for sub-second semantic classification."""
    api_key = _get_gemini_api_key()
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 30}
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception as e:
        print(f"[GIF] Direct Gemini API call failed: {e}", file=sys.stderr)
    return None


def llm_select_canonical_gif(
    query: str,
    registry: list[dict],
    history_urls: set[str],
    history: list[dict | str] | None = None,
    force: bool = False,
    timeout: int = 15
) -> dict | None:
    """
    Use Gemini Flash (direct REST API fast-path, falling back to agy CLI) to select
    the single best canonical reaction GIF based on conversational situation, subtext, and irony.
    """
    if not query or not registry:
        return None

    exclude_ids = set()
    for entry in registry:
        if entry.get("url") in history_urls and not force:
            exclude_ids.add(entry.get("id"))
        f_slug = entry.get("franchise")
        if f_slug and not force:
            is_cd, dist, thresh = check_cooldown(f_slug, history=history)
            if is_cd:
                exclude_ids.add(entry.get("id"))

    lines = []
    for g in registry:
        vibes_str = ", ".join(g.get("vibes", []))
        lines.append(f"- {g['id']}: {g.get('situation', '')} [vibes: {vibes_str}]")

    catalog_str = "\n".join(lines)
    exclude_block = ""
    if exclude_ids:
        exclude_block = f"\nRECENTLY USED GIFS (DO NOT SELECT):\n" + "\n".join(f"- {x}" for x in sorted(exclude_ids)) + "\n"

    prompt = f"""You are the reaction GIF selector for Zero, an autonomous systems engineering agent.
Analyze the user's conversational situation or upcoming message context, and select the single most fitting, comedic, or ironic reaction GIF from the canonical catalog below.

SITUATION:
"{query}"
{exclude_block}
CANONICAL GIF CATALOG:
{catalog_str}

CRITICAL INSTRUCTIONS:
1. Select the SINGLE BEST GIF ID from the catalog that fits the tone, humor, or situation.
2. DO NOT pick any GIF from the RECENTLY USED list.
3. Return ONLY the chosen GIF ID on a line by itself. Do not include markdown, explanations, or quotes.
4. DIVERSIFY SELECTION: Prefer underutilized or fresh gems from the catalog over repeated staples when multiple GIFs fit the tone.
"""
    raw_out = None
    try:
        raw_out = _call_gemini_api_direct(prompt, timeout=3.0)
    except Exception as e:
        print(f"[GIF] Direct API error: {e}", file=sys.stderr)

    if not raw_out:
        import subprocess
        try:
            res = subprocess.run(
                [
                    "agy",
                    "--model=gemini-3.8-flash-low",
                    "--disable-slash-commands",
                    f"-p={prompt}"
                ],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            raw_out = res.stdout.strip()
        except Exception as e:
            print(f"[GIF] LLM selection error or timeout: {e}", file=sys.stderr)
            return None

    if not raw_out:
        return None

    try:
        chosen_entry = None
        for g in registry:
            gid = g.get("id", "")
            if gid and re.search(rf"\b{re.escape(gid)}\b", raw_out):
                chosen_entry = g
                break

        if not chosen_entry:
            return None

        if chosen_entry.get("url") in history_urls and not force:
            return None

        f_slug = chosen_entry.get("franchise")
        if f_slug and not force:
            is_cd, dist, thresh = check_cooldown(f_slug, history=history)
            if is_cd:
                return None

        if not is_valid_gif_url(chosen_entry["url"]):
            return None

        title = chosen_entry.get("title") or clean_slug_title(chosen_entry["url"].split("/view/")[-1])
        record_history(chosen_entry["url"], query=query, title=title, franchise=f_slug)

        return {
            "title": title,
            "url": chosen_entry["url"],
            "markdown": f"[GIF]({chosen_entry['url']})",
            "ocr_text": "",
            "source": "canonical_registry",
            "franchise": f_slug,
            "canonical_id": chosen_entry.get("id"),
            "situation": chosen_entry.get("situation"),
            "vibes": chosen_entry.get("vibes")
        }
    except Exception as e:
        print(f"[GIF] Selection post-processing error: {e}", file=sys.stderr)
        return None


def find_canonical_gif(query: str, history: list[dict | str] | None = None, force: bool = False, use_llm: bool = True) -> dict | None:
    """
    Tier-0: Match situational query against curated canonical Hall of Fame GIFs.
    First evaluates via Gemini Flash LLM semantic selection; falls back to
    in-memory SQLite FTS5 BM25 Porter stemmer.
    Enforces anti-repetition and HTTP validity.
    Completely bypasses dynamic OCR safety verification; all canonical GIFs
    are pre-vetted, manually reviewed, and guaranteed clean.
    """
    registry = load_canonical_registry()
    if not registry:
        return None

    history_urls = get_history_urls(history)

    # 1. Check exact ID match first
    q_norm = query.lower().strip().replace("-", "_")
    for entry in registry:
        if entry.get("id", "").lower() == q_norm:
            f_slug = entry.get("franchise")
            if f_slug and not force:
                is_cd, dist, thresh = check_cooldown(f_slug, history=history)
                if is_cd:
                    continue
            if entry.get("url") in history_urls and not force:
                continue
            if is_valid_gif_url(entry["url"]):
                title = entry.get("title") or clean_slug_title(entry["url"].split("/view/")[-1])
                record_history(entry["url"], query=query, title=title, franchise=f_slug)
                return {
                    "title": title,
                    "url": entry["url"],
                    "markdown": f"[GIF]({entry['url']})",
                    "ocr_text": "",
                    "source": "canonical_registry",
                    "franchise": f_slug,
                    "canonical_id": entry.get("id"),
                    "situation": entry.get("situation"),
                    "vibes": entry.get("vibes")
                }

    # 2. LLM Semantic Selection (Primary)
    if use_llm:
        llm_pick = llm_select_canonical_gif(query, registry, history_urls, history=history, force=force)
        if llm_pick:
            return llm_pick

    # 2. FTS5 BM25 search
    fts_ranks = {}
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1 and w not in STOP_WORDS]
    if words:
        try:
            con = _get_canonical_fts_db(registry)
            fts_q = " OR ".join(words)
            cur = con.execute("""
                SELECT id, bm25(gifs_fts, 1.0, 1.5, 8.0, 12.0, 3.0, 2.0) as rank
                FROM gifs_fts
                WHERE gifs_fts MATCH ?
            """, (fts_q,))
            fts_ranks = {row[0]: row[1] for row in cur.fetchall()}
        except Exception as e:
            # Fall back to pure token matching
            pass

    # 3. Score all candidates with historical frequency decay penalty
    if history is None:
        history = load_history()
    recent_30 = history[-30:] if history else []
    recent_freq = {}
    for item in recent_30:
        if isinstance(item, dict):
            cid = item.get("canonical_id") or item.get("title")
            u = item.get("url")
            if cid:
                recent_freq[cid] = recent_freq.get(cid, 0) + 1
            if u:
                recent_freq[u] = recent_freq.get(u, 0) + 1

    candidates = []
    for entry in registry:
        url = entry.get("url")
        if not url:
            continue

        s = score_canonical_candidate(query, entry, fts_score=fts_ranks.get(entry.get("id")))
        if s >= 35.0:
            # Frequency decay penalty: penalize GIFs that have been used multiple times in the last 30 deliveries
            cid = entry.get("id")
            freq = max(recent_freq.get(cid, 0), recent_freq.get(url, 0))
            if freq > 0 and not force:
                s = max(0.0, s - (freq * 35.0))
            if s >= 35.0 or force:
                candidates.append((s, entry))

    if not candidates:
        return None

    # Sort highest score first
    candidates.sort(key=lambda x: x[0], reverse=True)

    # 4. Filter by cooldown and history to get eligible pool
    eligible_candidates = []
    for score, pick in candidates:
        url = pick.get("url")
        f_slug = pick.get("franchise")

        if url in history_urls and not force:
            continue

        if f_slug and not force:
            is_cd, dist, thresh = check_cooldown(f_slug, history=history)
            if is_cd:
                continue

        if not is_valid_gif_url(url):
            continue

        eligible_candidates.append((score, pick))

    if not eligible_candidates:
        return None

    # 5. Top-K Jitter Selection: Sample stochastically from candidates within 15% of top score
    top_score = eligible_candidates[0][0]
    top_tier = [c for c in eligible_candidates if c[0] >= top_score * 0.85]
    if len(top_tier) > 1 and not force:
        weights = [max(1.0, c[0]) for c in top_tier]
        chosen_score, pick = random.choices(top_tier, weights=weights, k=1)[0]
    else:
        chosen_score, pick = eligible_candidates[0]

    url = pick.get("url")
    f_slug = pick.get("franchise")
    title = pick.get("title") or clean_slug_title(url.split("/view/")[-1])
    record_history(url, query=query, title=title, franchise=f_slug)

    return {
        "title": title,
        "url": url,
        "markdown": f"[GIF]({url})",
        "ocr_text": "",
        "source": "canonical_registry",
        "franchise": f_slug,
        "canonical_id": pick.get("id"),
        "score": round(chosen_score, 1),
        "situation": pick.get("situation"),
        "vibes": pick.get("vibes")
    }


def get_contextual_gif(query: str, run_ocr: bool = True, force: bool = False, allow_dynamic: bool = False, use_llm: bool = True) -> dict:
    """
    Reaction GIF Picker with Tier-0 Canonical Fast-Path & Dynamic Fallback:
    0. Tier 0 (Canonical Registry): Matches against curated Hall of Fame GIFs.
       Pre-vetted safe; completely bypasses OCR frame downloads and text extraction.
    1. Tier 1 (Tenor Dynamic): Searches Tenor, checks history, validates HTTP 200, runs OCR safety check.
    2. Tier 2 (Giphy Dynamic): Searches Giphy, checks history, validates HTTP 200, runs OCR safety check.
    3. Tier 3 (Graceful Skip): Returns clean skip if no suitable candidates match.
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
            log_gif_failure(
                query=query,
                reason="cooldown_blocked",
                details=msg,
                franchise=q_franchise,
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

    import concurrent.futures

    def _validate_and_pick(candidate_pool: list[dict], source_name: str) -> dict | None:
        scored_candidates = []
        for c in candidate_pool:
            s = score_candidate(query, c)
            if s >= 0:
                scored_candidates.append((s, c))

        if not scored_candidates:
            return None

        # Sort by relevance score descending. Preserve search ranking for ties.
        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        for score, c in scored_candidates:
            c_slug = c.get("slug") or c["url"].split("/")[-1]
            c_title = c.get("title", "")
            c_franchise = detect_franchise(f"{c_title} {c_slug}")
            if c_franchise and not force:
                is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
                if is_cd:
                    log_gif_failure(
                        query=query,
                        reason="candidate_cooldown",
                        details=f"Franchise '{c_franchise}' on cooldown ({dist}/{thresh})",
                        franchise=c_franchise,
                        candidate_url=c["url"],
                    )
                    print(f"[GIF] Candidate skipped: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                    continue

            # Parallel validation: HTTP HEAD probe and OCR extraction run concurrently
            url_valid = False
            ocr_text = ""
            media_url = c.get("media_url")

            if run_ocr and media_url:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                    f_url = ex.submit(is_valid_gif_url, c["url"])
                    f_ocr = ex.submit(extract_gif_ocr, media_url)
                    url_valid = f_url.result()
                    ocr_text = f_ocr.result()
            else:
                url_valid = is_valid_gif_url(c["url"])

            if not url_valid:
                log_gif_failure(
                    query=query,
                    reason="url_probe_failed",
                    details="HTTP HEAD status returned non-200 or timed out",
                    candidate_url=c["url"],
                )
                continue

            if run_ocr and ocr_text:
                if not is_ocr_safe(ocr_text):
                    log_gif_failure(
                        query=query,
                        reason="ocr_safety_rejected",
                        details=f"Blocked term detected in OCR text: '{ocr_text}'",
                        candidate_url=c["url"],
                    )
                    continue
                if not c_franchise:
                    c_franchise = detect_franchise(ocr_text)
                    if c_franchise and not force:
                        is_cd, dist, thresh = check_cooldown(c_franchise, history=raw_history)
                        if is_cd:
                            log_gif_failure(
                                query=query,
                                reason="ocr_cooldown_detected",
                                details=f"Franchise '{c_franchise}' detected in OCR on cooldown ({dist}/{thresh})",
                                franchise=c_franchise,
                                candidate_url=c["url"],
                            )
                            print(f"[GIF] Candidate skipped by OCR: franchise '{c_franchise}' on cooldown ({dist}/{thresh}).", file=sys.stderr)
                            continue

            # Best matching, fully valid, safe candidate is selected
            title = c["title"]
            pick_f = c_franchise or detect_franchise(f"{title} {c_slug} {ocr_text}")
            record_history(c["url"], query=query, title=title, franchise=pick_f)
            return {
                "title": title,
                "url": c["url"],
                "markdown": f"[GIF]({c['url']})",
                "ocr_text": ocr_text,
                "source": source_name,
                "franchise": pick_f
            }
        return None

    # Tier 0: Canonical Registry Lookup
    canonical_pick = find_canonical_gif(query, history=raw_history, force=force, use_llm=use_llm)
    if canonical_pick:
        return canonical_pick

    rules = get_runtime_gif_rules()
    canonical_only = rules.get("canonical_only", False) and not allow_dynamic

    if canonical_only:
        log_gif_failure(
            query=query,
            reason="no_canonical_match",
            details="Query did not match any eligible canonical Hall of Fame GIFs and canonical_only is enforced",
            franchise=q_franchise,
        )
        return {
            "title": None,
            "url": None,
            "markdown": None,
            "ocr_text": "",
            "source": "skip",
            "franchise": None
        }

    # Tier 1: Dynamic Tenor search
    tenor_candidates = search_tenor(query, limit=12)
    tenor_fresh = [c for c in tenor_candidates if c["url"] not in history_urls]
    tenor_pool = tenor_fresh if tenor_fresh else tenor_candidates
    tenor_pick = _validate_and_pick(tenor_pool, "dynamic_tenor")
    if tenor_pick:
        return tenor_pick

    # Tier 2: Dynamic Giphy fallback
    giphy_candidates = search_giphy(query, limit=12)
    giphy_fresh = [c for c in giphy_candidates if c["url"] not in history_urls]
    giphy_pool = giphy_fresh if giphy_fresh else giphy_candidates
    giphy_pick = _validate_and_pick(giphy_pool, "dynamic_giphy")
    if giphy_pick:
        return giphy_pick

    # Tier 3: Graceful skip (no hardcoded static URLs)
    log_gif_failure(
        query=query,
        reason="skip_no_candidates",
        details="All candidate search and fallback providers failed to produce a valid GIF",
    )
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
        print("Usage: gif_tool.py <query> [--force] [--cooldowns] [--failures] [--canonical] [--dynamic] [--no-llm]")
        sys.exit(0)

    args = sys.argv[1:]
    if "--cooldowns" in args:
        summary = get_cooldown_summary()
        print(json.dumps(summary, indent=2))
        sys.exit(0)

    if "--failures" in args:
        failures = get_recent_failures(limit=20)
        print(json.dumps(failures, indent=2))
        sys.exit(0)

    if "--canonical" in args:
        reg = load_canonical_registry()
        print(json.dumps(reg, indent=2))
        sys.exit(0)

    force = False
    if "--force" in args:
        force = True
        args.remove("--force")

    allow_dynamic = False
    if "--dynamic" in args:
        allow_dynamic = True
        args.remove("--dynamic")

    use_llm = True
    if "--no-llm" in args:
        use_llm = False
        args.remove("--no-llm")
    elif "--llm" in args:
        use_llm = True
        args.remove("--llm")

    q = " ".join(args)
    res = get_contextual_gif(q, force=force, allow_dynamic=allow_dynamic, use_llm=use_llm)
    print(json.dumps(res, indent=2))

