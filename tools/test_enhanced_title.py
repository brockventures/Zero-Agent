import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import re
from tools.bridge import generate_concise_thread_title

tests = [
    "ok so heres a new issue - it looks like when your output from the threaded request concluded, you outputted to the main thread, and when you did that, it cut off your response to my new request i had started in the main thread",
    "minor comment on your crab cavern commentary, which was AMAZING. you have some raw latex formatting in your message",
    "i want to add some more complexity to the Zero reboot process",
    "thread: Analyze my 444 Liked Songs from YouTube Music and generate a 25-song deep-cut playlist",
    "Perform a deep analysis of my 307 Google Keep notes in Takeout",
    "What is the current CPU utilization on Host1?"
]

for t in tests:
    title = generate_concise_thread_title(t)
    print("PROMPT:", t[:60] + "...")
    print("TITLE: ", f"🧵 {title} ({len(title.split())} words)")
    print("-" * 40)

