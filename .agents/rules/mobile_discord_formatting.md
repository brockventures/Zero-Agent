---
description: Strict rules for Discord mobile formatting. Banning wide ASCII box diagrams, multi-column trees, and wide code blocks.
globs: "*"
---

# Discord Mobile Formatting Rules

1. **BAN Wide ASCII Box-Drawing Diagrams:**
   - NEVER use wide multi-column ASCII diagrams, flowchart trees (`┌──┬──┐`, `│`, `└─►`), or side-by-side text boxes.
   - On Discord mobile, code blocks wrap aggressively at ~35–40 characters. Horizontal ASCII trees wrap every single line and turn into an unreadable scrambled mess.

2. **Preferred Comparison Formats (NEVER use markdown pipe tables `| Col1 | Col2 |`):**
   - Discord mobile cannot render markdown pipe tables. Tables get flattened or mangled into unreadable strings with nested parentheses like `• Feature (Val1): Val2`.
   - **Pattern A: Option Cards (Best for comparing products/models):**
     Group all attributes directly under each option header:
     ### 1. Chest Freezer (Manual Defrost)
     • **Defrost:** Manual (steady -10°F to 0°F)
     • **Breast Milk:** 🏆 Gold standard (12+ months)
     • **Power Outage:** 48+ hours sealed
     • **Price:** $200 – $400

     ### 2. Upright Freezer (Frost-Free)
     • **Defrost:** Auto / frost-free (daily heat cycles)
     • **Breast Milk:** ⚠️ Acceptable (3–6 months)
     • **Power Outage:** 12–24 hours
     • **Price:** $500 – $900

   - **Pattern B: Feature Sub-Bullets (Best for direct side-by-side spec contrasts):**
     • **Defrost Type:**
       - *Chest:* Manual (steady -10°F to 0°F)
       - *Upright:* Auto / frost-free (daily heat cycles)
     • **Breast Milk Storage:**
       - *Chest:* 🏆 Gold standard (zero heat cycles, 12+ months)
       - *Upright:* ⚠️ Acceptable, but inferior (3–6 months)

3. **Code Blocks Width Limit:**
   - If using monospace code blocks for tables or data, keep the total width **strictly under 35 characters**.
