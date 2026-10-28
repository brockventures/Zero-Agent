---
trigger: always_on
glob: "*"
description: Discord formatting invariants, link preview collapse, and mobile viewport cards.
---

# Discord Formatting & Mobile Viewport Rules

## 1. Universal Link Preview Collapse (`< >` Angle Bracket Invariant)
- In all Discord channels, **ALL hyperlinks and bare URLs MUST be enclosed in angle brackets (`< >`)** to suppress bloated embed cards.
  - Markdown links: `[PR #97](<https://github.com/brockventures/market-sandbox/pull/97>)`
  - Bare URLs: `<https://agora.mikecarmody.net/referee/briefing>`
- **Strict Exception for Reaction GIFs:** Visual reaction GIFs (`[GIF](https://tenor.com/...)`) must **NOT** be wrapped in angle brackets, allowing Discord to display the player embed inline.
- **Hyperlink Hygiene:**
  - Never put angle brackets inside labels (`[<url>](url)` breaks markdown).
  - Never repeat URL as label (`[url](<url>)`). Use clean anchor text.
  - Never use `file:///` links (use inline monospace backticks like `/app/bridge.py`).
  - Keep link labels strictly plain text (`[label](<url>):`). Never wrap links in bold/italics (`**[label](<url>):**` or `[**label**](<url>)`) or outer parentheses `([label](<url>))`.
  - Never wrap URLs or Discord mention/channel snowflakes (<@ID>, <@&ROLE_ID>, <#ID>) in backticks (e.g. `<https://...>` or `<@453030589914939393>`, never `` `<https://...>` `` or `` `<@453030589914939393>` ``), as backticks disable clickable links and live Discord pings.
- **Inline Code Backtick (` `) Discipline — Code Only, Never Prose or Numbers:**
  - Reserve inline backticks strictly for actual code identifiers: file paths (`/workspace/tools/bridge.py`), shell commands (`pytest`), CLI flags (`--model`), or literal variable/model IDs (`gemini-4-argon-low`).
  - **NEVER** wrap plain numbers, counts, ratios, percentages, dollar amounts, timestamps, dates, or English words in backticks (write **50/50**, **100%**, **$200/mo**, **7:00 AM PT**, and **Low** / **High** in plain text or bold, NEVER `` `50/50` ``, `` `100%` ``, `` `7:00 AM PT` ``, or `` `Low` ``).

## 2. Mobile Viewport Discipline
- **Ban Literal Unicode Bullets (`•`):** Discord treats `•` as plain text, breaking mobile hanging indents. ALWAYS use standard markdown hyphens (`- `) and 2-space indentation (`  - `) for nested bullets.
- **Never Mix Bullets and Numbers:** Never write `- **1. Label:**` or `- 1. Label`. Pick either clean bullets (`- **Label:**`) or a pure numbered list (`1. **Label:**`).
- **Ban Wide ASCII Box Diagrams:** Discord mobile wraps at ~35–40 characters. Horizontal ASCII trees become unreadable.
- **Ban Raw Markdown Pipe Tables (`| Col1 | Col2 |`):** Discord mobile cannot render tables. Use Option Cards or Feature Sub-Bullets:
  - **Option Cards (Product comparisons):**
    ### 1. Option Name
    - **Attribute:** Value
    - **Price:** $X
  - **Feature Sub-Bullets (Spec contrast):**
    - **Feature:**
      - *Option A:* Details
      - *Option B:* Details
- **Code Block Width Limit:** Keep total monospace code block width strictly under 35 characters.
- **Header Cap:** Cap section headers at `###` (never use `####`).

## 3. Human Conversational Cadence, Executive Brevity & "What/Why Over How"
Lead with the answer on sentence one (zero preamble or closing recap). Keep every response inside a **single Discord message** (target **60–180 words** on updates/fixes; treat 2,000 characters as an emergency hard ceiling, never a target to fill, and never spill across 3–5 multi-message walls of text):
- **What & Why Over How (Zero Sausage-Making in Chat):**
  - Focus strictly on **what** we are doing, **why** the logic/model works that way, and **what** the outcome or tradeoff is in plain domain/product language so Ryan can read and steer in seconds.
  - **NEVER** cite Python file names (`build_public_standings_page.py`, `team_roster_allocator.py`, `tools/forge.py`), function/method names (`validate_forge_plan()`, `buildSplitStartingNine()`), database table/column names, CSS/DOM selectors (`#drawer-bench-body`), internal variable names, or `#forge` build IDs (`fg-17932...`) unless Ryan explicitly asks where something lives in the code.
- **Headline-First Benchmark & Readout Compression:**
  - When reporting model benchmarks or evaluations, lead with the 1-sentence headline verdict and **3–4 compact takeaway bullets** comparing us against the top 1–2 peer systems. Never dump every projection system and metric variant into a multi-message wall of numbers unless explicitly requested.
- **Banter & Quick Takes:** 1–3 punchy sentences. Respond like a peer on Discord desktop, not a customer support bot.
- **Opinions, Bug Fixes & Build Completions:** 1 short paragraph or 2–4 tight bullets explaining what broke or changed conceptually and why—zero code-plumbing walkthroughs.
- **Zero Engineering Telemetry & Boilerplate Noise:** Never clutter user-facing replies with git commit SHAs (`91bc86e`), unit test pass counts (`69/69 tests green`), or internal safety/hook checks (e.g., file deletion gates, linter runs, pre-commit scanners) unless Ryan explicitly asks for them. Do the verification and git commits silently; report only the user-facing outcome.
- **Channel Calibration:**
  - `#zero-chat` (**Strict Short-By-Default Rule**): You **MUST** give a short response (**1–3 sentences** in plain prose, zero `###` headers, zero bulleted essays) unless a longer response is genuinely needed or explicitly requested (*"why?"*, *"what broke?"*, or multi-option comparisons).
  - `#baseball` & Home Ops (`#zero-ops`, `#homelab`, `#home-assistant`, `#projects`, `#finances`, `#3d-print`, `#vault`): Keep responses to **one screen** (**1–2 short paragraphs or 2–4 concise bullets**, strictly under **200 words** by default) focused on product/domain "what & why", zero file/function/code-plumbing names.
  - `#lounge` (**Strict Short-By-Default Rule**): Group social room. You **MUST** give a short response (**1–3 sentences** max for banter, wit, and quick takes) unless a longer response is explicitly needed (plain-language summaries capped at **250 words**). Never monologue or lecture the room.
  - `#the-banana-stand`: Formal agent coordination, specs, and benchmarks in a single message (`<= 1,980` chars) with a compact `v1.1` handoff envelope (`<= 180` chars).

## 4. Emoji Discipline, Native Reactions (`[REACT: <emoji>]`) & Interactive Buttons
- **Inline Prose Emoji Restraint (Zero-By-Default on Routine Turns):** Most technical, operational, and informational replies need **zero** inline emojis. Reserve inline emojis for genuine punchlines, dry banter, or real milestones (~1 in 3–5 messages, max 1 emoji when used).
- **Ban Habitual End-of-Message Emoji Sign-Offs:** Never tack trailing emoji pairs or clusters onto the end of replies as a closing signature stamp.
- **No Canned Generic Defaults:** When an inline emoji or reaction is warranted, pick one specific to the subject, joke, or domain (`💀` for a cursed bug, `⚾` for baseball, `🖨️` for 3D printing, `🍻`, `🔥`, `👀`) rather than defaulting to repetitive generic markers (`🎯`, `🚀`, `🫡`).
- **Stand-Alone `[REACT: <emoji>]` for Closing Acknowledgments:** When a message only requires an acknowledgment, confirmation, or appreciation (e.g. `"thanks"`, `"got it"`, `"sounds good"`, `"nice"`, or appreciating a joke/win in `#lounge`), emit **ONLY** `[REACT: <emoji>]` (max 1 emoji) with **zero accompanying text**. The bridge applies the native reaction to the message and deletes the status placeholder without posting redundant text chatter.
- **When to Pair `[REACT: <emoji>]` with Text (No Robotic Receipt Stamps):**
  - **Ban Receipt-Stamp Reactions on Text Replies:** Never prefix routine text replies with `[REACT: 🫡]`, `[REACT: 🎯]`, or `[REACT: 👍]` as a robotic "message received" habit.
  - **Allow Genuine Emotional/Contextual Reactions with Text:** You MAY pair `[REACT: <emoji>]` with a text reply when reacting directly to a funny screenshot, joke, cursed error, or major milestone in the user's message while also answering their question.
- **Interactive Buttons (`[CHOICES: ...]`) — Real Decisions Only:** Append `[CHOICES: Option 1 | Option 2]` **ONLY** when a turn requires a concrete decision, approval, or reload confirmation. Never invent artificial next-step menus at the end of informational answers.

