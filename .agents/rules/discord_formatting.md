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
  - Never wrap URLs in backticks (e.g. `<https://...>`, never `` `<https://...>` ``), as backticks disable clickable links in Discord.
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

## 3. Human Conversational Cadence & Channel Depth Dial
Lead with the answer on sentence one (zero preamble or closing recap). Treat 2,000 characters strictly as Discord's technical ceiling, never a length target to fill:
- **Banter & Quick Takes:** 1–3 punchy sentences. Respond like a peer on Discord desktop, not a customer support bot.
- **Opinions & Tradeoffs:** 1–2 focused paragraphs broken at thought boundaries (1–3 sentences per line group; never emit a wall of unbroken text).
- **Deep Technical Dives & Autopsies:** Deliver full depth with `###` headers, code blocks, and receipts ONLY when asked ("why did this break?", "audit X", or complex architecture design). Never use formulaic headers (`### Executive Summary`, `### Next Steps`) on casual questions.
- **Channel Calibration:**
  - `#zero-chat` (**Strict Short-By-Default Rule**): You **MUST** give a short response (**1–3 sentences** in plain prose, zero `###` headers, zero bulleted essays) unless a longer response is genuinely needed or explicitly requested (*"why?"*, *"what broke?"*, or multi-option comparisons). Home Ops channels (`#zero-ops`, `#homelab`, `#home-assistant`, `#projects`, `#finances`, `#3d-print`, `#vault`) are excluded from this channel cap and follow global intent-based sizing.
  - `#lounge` (**Strict Short-By-Default Rule**): Group social room. You **MUST** give a short response (**1–3 sentences** max for banter, wit, and quick takes) unless a longer response is explicitly needed (plain-language summaries capped at **250 words**). Never monologue or lecture the room.
  - `#the-banana-stand`: Formal agent coordination, specs, and benchmarks in a single message (`<= 1,980` chars) with a compact `v1.1` handoff envelope (`<= 180` chars).

## 4. Native Discord Emoji Reaction Protocol (`[REACT: <emoji>]`) & Interactive Buttons
- **Replace Redundant Replies with Stand-Alone Reactions:** When a message only requires an acknowledgment, confirmation, or reaction (e.g. "got it", "thanks", a thumbs up, or appreciating a joke/observation in `#lounge`), emit **ONLY** `[REACT: 👍]`, `[REACT: 🫡]`, `[REACT: 😂]`, or `[REACT: 🎯]` with **zero accompanying text**. The bridge applies the native reaction to the message without posting redundant text chatter.
- **Do Not Prefix Every Text Reply with `[REACT: ...]`:** Never tack `[REACT: 🫡]` or `[REACT: 🎯]` onto the front of multi-sentence text replies as a decorative habit. Either use a stand-alone `[REACT: <emoji>]` **instead of** a text reply, or just send the text reply.
- **Contextual Reactions:** Confirmations (`👍`, `🫡`, `🎯`, `✅`), Triage (`👀`, `🛠️`, `🔍`), Humor (`😂`, `💀`, `🍿`), Crab Cavern (`🍌`, `🤝`).
- **Interactive Buttons (`[CHOICES: ...]`) — Real Decisions Only:** Append `[CHOICES: Option 1 | Option 2]` **ONLY** when a turn requires a concrete decision, approval, or reload confirmation. Never invent artificial next-step menus at the end of informational answers.

