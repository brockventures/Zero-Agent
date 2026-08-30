import re

def generate_concise_thread_title(prompt: str, max_words: int = 4) -> str:
    if not prompt:
        return "Task Execution"
    
    clean = re.sub(r"^(thread:|parallel:|\/goal|\/plan|\/deep-research)\s*", "", prompt, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\[Attached file\(s\)[^\]]+\]", "", clean).strip()
    clean = re.sub(r"[#*_`~]", "", clean).strip()

    low = clean.lower()
    if "youtube music" in low or "playlist" in low or "liked songs" in low:
        return "Music Playlist Discovery"
    elif "keep" in low or "d&d" in low or "dungeons" in low:
        return "D&D & Keep Lore Index"
    elif "docker" in low or "storage" in low or "nas" in low or "host1" in low:
        return "Homelab Storage Audit"
    elif "takeout" in low or "chrome" in low or "search history" in low:
        return "Search History Deep Dive"
    elif "triage" in low or "nightly" in low:
        return "Nightly Assistant Sweep"

    stopwords = {"please", "can", "you", "i", "me", "my", "we", "our", "a", "an", "the", "and", "or", "to", "for", "in", "on", "at", "from", "with", "about", "all", "of", "is", "are", "do", "run", "perform", "analyze", "generate", "build", "check", "investigate"}
    
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", clean) if len(w) > 1]
    meaningful = [w.capitalize() for w in words if w.lower() not in stopwords]
    
    if len(meaningful) >= 2:
        return " ".join(meaningful[:max_words])
    elif words:
        return " ".join([w.capitalize() for w in words[:max_words]])
    return "Task Execution"

tests = [
    "thread: Analyze my 444 Liked Songs from YouTube Music and generate a 25-song deep-cut playlist recommendation based on my Reel Big Fish, OK Go, and Fall Out Boy affinities.",
    "Perform a deep analysis of my 307 Google Keep notes in Takeout (D&D campaign notes, NPC rosters, homebrews, and project backlogs). Build a structured master index of all D&D lore and campaigns into /workspace/memory/dnd_campaign_lore.md.",
    "thread: Run a full storage, volume utilization, and container health audit across both Host1 (.82) and Host2 (.84).",
    "Investigate the Flagstar loan ALTA settlement statement and calculate remaining principal amortization"
]

for t in tests:
    print("PROMPT:", t[:60] + "...")
    print("TITLE: ", "🧵 " + generate_concise_thread_title(t))
    print("-" * 40)
