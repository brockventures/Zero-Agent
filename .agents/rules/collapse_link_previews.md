---
trigger: always_on
glob: "*"
description: Universal rule to collapse Discord link previews and embeds by default. Wraps markdown links and bare URLs in angle brackets.
---

# Collapse Link Previews by Default (Universal Invariant)

1. **UNIVERSAL LINK PREVIEW COLLAPSE (`< >` ANGLE BRACKET INVARIANT):**
   - In all Discord channels (`#zero-chat`, `#zero-ops`, `#lounge`, `#the-banana-stand`, `#server-updates`, etc.), **ALL hyperlinks and bare URLs MUST be enclosed in angle brackets (`< >`) by default** to suppress Discord's bloated link preview cards and embed widgets.
   - **Markdown Links:** ALWAYS wrap the target URL in angle brackets:
     - Correct: `[PR #97](<https://github.com/brockventures/market-sandbox/pull/97>)`
     - Incorrect: `[PR #97](https://github.com/brockventures/market-sandbox/pull/97)`
   - **Bare URLs:** If outputting a bare URL without anchor text, ALWAYS enclose it in angle brackets:
     - Correct: `<https://agora.mikecarmody.net/referee/briefing>`
     - Incorrect: `https://agora.mikecarmody.net/referee/briefing`

2. **STRICT EXCEPTION FOR VISUAL REACTION MEDIA (GIFs):**
   - Visual reaction GIFs (`[GIF](https://tenor.com/...)` or `giphy.com`) must **NOT** be wrapped in angle brackets, because angle brackets suppress the video/GIF player embed that Discord needs to display the reaction inline.
   - All other links (GitHub PRs, issues, commits, documentation, external sites, tools) MUST have angle brackets.

3. **HYPERLINK SYNTAX HYGIENE:**
   - **No Bold/Italic Outer Wrapping:** Never wrap markdown links in bold or italics (`**[label](url)**` breaks Discord markdown). Place styling inside the label: `[**label**](<url>)`.
   - **No Bracketed file:/// links:** Never output `file:///` links to Discord (Discord cannot open local files and renders bracketed clutter). Use inline monospace backticks instead (e.g. `/app/bridge.py` or `tools/agora_steering.py`).
   - **No Redundant Self-Anchoring:** Never write `[https://...](https://...)` or `([https://...](https://...))`. Use clean anchor text `[label](<url>)` or bare `<url>`.
