import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import re
from tools.bridge import generate_concise_thread_title

tests = [
    "We are falling into threaded convos TOO often for my liking. How should we adjust the triggers?",
    "thread: Analyze my 444 Liked Songs from YouTube Music and generate a 25-song deep-cut playlist recommendation based on my Reel Big Fish, OK Go, and Fall Out Boy affinities.",
    "Perform a deep analysis of my 307 Google Keep notes in Takeout (D&D campaign notes, NPC rosters, homebrews, and project backlogs). Build a structured master index of all D&D lore and campaigns into /workspace/memory/dnd_campaign_lore.md.",
    "thread: Run a full storage, volume utilization, and container health audit across both Host1 (.82) and Host2 (.84).",
    "Investigate the Flagstar loan ALTA settlement statement and calculate remaining principal amortization"
]

for t in tests:
    title = generate_concise_thread_title(t)
    print("PROMPT:", t[:60] + "...")
    print("TITLE: ", f"🧵 {title} ({len(title.split())} words)")
    print("-" * 40)

