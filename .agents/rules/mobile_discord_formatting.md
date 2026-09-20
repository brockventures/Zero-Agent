---
description: Strict rules for Discord mobile formatting. Banning wide ASCII box diagrams, multi-column trees, and literal Unicode bullet points. Enforcing native markdown lists and compact cards.
globs: "*"
---

# Discord Mobile Formatting Rules

1. **BAN Literal Unicode Bullets (`•`): Use Native Markdown Lists (`-`):**
   - NEVER use the literal Unicode bullet character (`•`) to start list items.
   - Discord's markdown parser treats `•` as plain paragraph text, NOT a list. On mobile screens:
     - It breaks hanging indentation (wrapped text falls back under the bullet instead of indenting cleanly).
     - It creates awkward empty line gaps before indented sub-bullets.
     - It triggers mismatched mobile bullet styles (`•` then `o`).
   - **ALWAYS** use standard markdown hyphens (`- `) for top-level list items and 2-space indentation (`  - `) for nested sub-bullets. Discord natively renders these with mobile hanging indents and proper bullet styling.

2. **BAN Wide ASCII Box-Drawing Diagrams:**
   - NEVER use wide multi-column ASCII diagrams, flowchart trees (`┌──┬──┐`, `│`, `└─►`), or side-by-side text boxes.
   - On Discord mobile, code blocks wrap aggressively at ~35–40 characters. Horizontal ASCII trees wrap every single line and turn into an unreadable scrambled mess.

3. **Preferred Comparison Formats (NEVER use raw markdown pipe tables `| Col1 | Col2 |`):**
   - Discord mobile cannot render markdown pipe tables. While `bridge_formatting.py` automatically normalizes tables to compact mobile cards, direct responses should naturally favor clean markdown cards:
   - **Pattern A: Option Cards (Best for comparing products/models):**
     Group all attributes directly under each option header:
     ### 1. Chest Freezer (Manual Defrost)
     - **Defrost:** Manual (steady -10°F to 0°F)
     - **Breast Milk:** 🏆 Gold standard (12+ months)
     - **Power Outage:** 48+ hours sealed
     - **Price:** $200 – $400

     ### 2. Upright Freezer (Frost-Free)
     - **Defrost:** Auto / frost-free (daily heat cycles)
     - **Breast Milk:** ⚠️ Acceptable (3–6 months)
     - **Power Outage:** 12–24 hours
     - **Price:** $500 – $900

   - **Pattern B: Feature Sub-Bullets (Best for direct side-by-side spec contrasts):**
     - **Defrost Type:**
       - *Chest:* Manual (steady -10°F to 0°F)
       - *Upright:* Auto / frost-free (daily heat cycles)
     - **Breast Milk Storage:**
       - *Chest:* 🏆 Gold standard (zero heat cycles, 12+ months)
       - *Upright:* ⚠️ Acceptable, but inferior (3–6 months)

4. **Code Blocks Width Limit:**
   - If using monospace code blocks for tables or data, keep the total width **strictly under 35 characters**.
