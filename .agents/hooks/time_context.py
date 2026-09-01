#!/usr/bin/env python3
"""
PreInvocation Hook: Injects current Pacific Time context into the model's turn.
Prevents UTC-to-PT calculation errors.
"""
import sys
import json
import datetime
from zoneinfo import ZoneInfo

def main():
    try:
        # Read any incoming payload from stdin
        _ = sys.stdin.read()
    except Exception:
        pass

    try:
        now_pt = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        formatted_time = now_pt.strftime("%A, %B %d, %Y, %I:%M:%S %p %Z")
        iso_time = now_pt.isoformat()

        msg = (
            f"[System Context] Current Local Time: {formatted_time} ({iso_time})\n"
            f"Timezone: America/Los_Angeles (Pacific Time). Present all times, dates, and schedules in Pacific Time."
        )

        output = {
            "injectSteps": [
                {
                    "ephemeralMessage": msg
                }
            ]
        }
        print(json.dumps(output))
    except Exception as e:
        print(json.dumps({"injectSteps": []}))

if __name__ == "__main__":
    main()
