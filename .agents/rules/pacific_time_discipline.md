---
description: Mandatory Pacific Timezone (PT) conversion discipline. Strictly prohibits quoting raw UTC timestamps or assuming UTC is local time.
globs: "*"
always_on: true
---

# Pacific Time (PT) Invariant & Time Conversion Discipline

1. **Ryan Brock and All Operations are Anchored to Pacific Time (PT):**
   - Ryan, the team, and all Crab Cavern and homelab operations operate strictly in Pacific Time (`America/Los_Angeles`, PT / PDT / PST).

2. **System Clocks and Explicit UTC Metadata:**
   - Raw VM clocks, syslog lines, and `<ADDITIONAL_METADATA>` are recorded in UTC.
   - **PROVENANCE-GATED CONVERSION:** ONLY convert timestamps that are explicitly identified as UTC (e.g. marked with `UTC`, `Z`, `+00:00`) or sourced directly from raw UTC system metadata.
   - Example: `04:00 UTC` = `9:00 PM PT` the previous evening during Daylight Saving Time (PDT, UTC-7).
   - NEVER refer to `04:00 UTC` as "4 AM" or "early morning" — it is 9 PM the previous evening.

3. **Double-Conversion & Unmarked Timestamp Safeguards:**
   - **Never Double-Convert:** If a timestamp already carries a Pacific Time label (`PT`, `PDT`, `PST`, `-07:00`, `-08:00`) — such as messages in `channel_history.py` which are pre-converted in code — do NOT convert it again.
   - **Unmarked Conversational Times:** Colloquial or unmarked times mentioned by Ryan or peer agents in chat (e.g. "let's sync at 4:00", "task finished at 9:15") are ALREADY local Pacific Time. NEVER assume conversational times are UTC or subtract hours from them.
   - When in doubt with ambiguous third-party logs without timezone tags, verify provenance before converting.

4. **Output Formatting:**
   - All human-facing dates, times, schedules, and digests MUST be presented in Pacific Time (labeled as `PT`).
   - Never output raw un-converted UTC timestamps to Discord.
