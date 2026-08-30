import re

def generate_concise_thread_title(prompt: str, max_words: int = 3) -> str:
    if not prompt:
        return "Task Execution"
    
    clean = re.sub(r"^(thread:|parallel:|\/goal|\/plan|\/deep-research)\s*", "", prompt, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\[Attached file\(s\)[^\]]+\]", "", clean).strip()
    clean = re.sub(r"[#*_`~]", "", clean).strip()

    low = clean.lower()
    if "stream" in low or "cut off" in low or "collision" in low or "interrupted" in low or ("output" in low and "thread" in low):
        return "Thread Stream Debug"
    elif "youtube music" in low or "playlist" in low or "liked songs" in low:
        return "Music Playlist Discovery"
    elif "keep" in low or "d&d" in low or "dungeons" in low:
        return "D&D & Keep Lore Index"
    elif "docker" in low or "storage" in low or "nas" in low or "host1" in low:
        return "Homelab Storage Audit"
    elif "takeout" in low or "chrome" in low or "search history" in low:
        return "Search History Deep Dive"
    elif "email" in low or "pubsub" in low or "pub-sub" in low or "sms" in low:
        return "Email & SMS Push"
    elif "reboot" in low or "restart" in low:
        return "Reboot Engine Setup"
    elif "triage" in low or "nightly" in low:
        return "Nightly Assistant Sweep"

    stopwords = {
        "ok", "so", "heres", "here", "a", "an", "the", "new", "issue", "problem",
        "question", "look", "looks", "like", "just", "well", "now", "hey", "can",
        "could", "would", "should", "please", "tell", "me", "my", "we", "our",
        "you", "your", "that", "this", "it", "its", "was", "were", "is", "are",
        "have", "has", "had", "do", "does", "did", "to", "for", "in", "on", "at",
        "from", "with", "about", "all", "of", "and", "or", "but", "if", "then",
        "when", "why", "how", "what", "which", "who", "run", "perform", "check",
        "analyze", "generate", "build", "investigate", "test", "minor", "comment"
    }

    words = [w for w in re.findall(r"[a-zA-Z0-9]+", clean) if len(w) > 1]
    meaningful = [w.capitalize() for w in words if w.lower() not in stopwords]

    if len(meaningful) >= 2:
        return " ".join(meaningful[:max_words])
    elif meaningful:
        return meaningful[0] + " Task"
    elif words:
        return " ".join([w.capitalize() for w in words[:max_words]])
    return "Task Execution"

tests = [
    "ok so heres a new issue - it looks like when your output from the threaded request concluded, you outputted to the main thread, and when you did that, it cut off your response to my new request i had started in the main thread",
    "minor comment on your crab cavern commentary, which was AMAZING. you have some raw latex formatting in your message",
    "i want to add some more complexity to the Zero reboot process",
    "thread: Analyze my 444 Liked Songs from YouTube Music and generate a 25-song deep-cut playlist",
    "Perform a deep analysis of my 307 Google Keep notes in Takeout",
    "What is the current CPU utilization on Host1?"
]

for t in tests:
    print("PROMPT:", t[:60] + "...")
    print("TITLE: ", "🧵 " + generate_concise_thread_title(t))
    print("-" * 40)
