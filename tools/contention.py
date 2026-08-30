#!/usr/bin/env python3
"""
contention.py - Multi-agent contention test harness for Crab Cavern.
Coordinates live contention verification across peer agents.
"""

import sys, time, json
import urllib.request
sys.path.insert(0, "/workspace/tools")
import banana

def test_live_lease():
    print(">>> 1. Inspecting current floor...")
    status = banana.get_status()
    print(f"Current holder: {status.get('holder')}")
    if status.get("holder") is not None:
        print(f"Floor is not clear! Held by {status.get('holder')}")
        return False

    print(">>> 2. Claiming floor for contention test (holding for 15 seconds)...")
    res = banana.claim("live-contention-test-zero-holding")
    print(f"Claim result: ok={res.get('ok')}, holder={res.get('state', {}).get('holder')}")

    print(">>> 3. Verifying status endpoint reports 'zero'...")
    s = banana.get_status()
    assert s.get("holder") == "zero", f"Expected 'zero', got {s.get('holder')}"
    print("Verified: status endpoint reports zero as active holder.")

    return True

def release_lease():
    print(">>> Releasing floor...")
    res = banana.release()
    print(f"Release result: ok={res.get('ok')}, released={res.get('released')}")
    s = banana.get_status()
    print(f"Status after release: {s.get('holder')}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "release":
        release_lease()
    elif len(sys.argv) > 1 and sys.argv[1] == "hold":
        test_live_lease()
    else:
        test_live_lease()
        time.sleep(2)
        release_lease()
