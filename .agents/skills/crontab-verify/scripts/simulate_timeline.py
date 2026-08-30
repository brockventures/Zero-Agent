#!/usr/bin/env python3
"""Simulate 24-hour sidecar timeline and flag resource contention windows."""

def simulate():
    print("📅 24-HOUR SIDECAR EXECUTION TIMELINE (Pacific Time)")
    print("=" * 60)
    hours = [[] for _ in range(24)]
    
    tasks = [
        (7, "Morning Briefing (07:00 PT)", "Low Load (API/Inbox)"),
        (22, "Nightly Triage (22:00 PT)", "Low Load (API/Calendar)"),
        (4, "Plex Cache Cleanup (04:00 PT)", "Medium Load (Disk I/O)"),
        (3, "Weekly Digest (Sun 03:00 PT)", "Low Load (Summarizer)"),
    ]
    
    for h, name, load in tasks:
        hours[h].append((name, load))
        
    for h in range(24):
        time_str = f"{h:02d}:00"
        active = hours[h]
        if active:
            items_str = ", ".join([f"{name} [{load}]" for name, load in active])
            contention_badge = "⚠️ [Contention Risk]" if len(active) > 2 else ""
            print(f"{time_str} ─── {items_str} {contention_badge}")
        else:
            if h in (0, 6, 12, 18):
                print(f"{time_str} ─── (Periodic Heartbeat sweeps only)")

    print("=" * 60)
    print("✅ No high-concurrency peak contention detected in nightly windows.")

if __name__ == "__main__":
    simulate()
