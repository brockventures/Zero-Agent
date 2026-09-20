---
name: whole-foods-cart
description: >-
  Orchestrates automated Amazon Whole Foods delivery cart staging using official AFX URL encoding.
  Ingests recipes (Mealie/text), ad-hoc chat additions, and shared family grocery lists (Google Keep/Tasks/Sheets),
  applies 365-brand optimization, filters baseline pantry items, aggregates quantities across meals, and
  compiles clean 1-click Discord hyperlinks.
---

# 🛒 Whole Foods AFX Cart Staging & Grocery Automation Skill

This skill compiles grocery manifests into official **Amazon Fresh Experience (AFX)** GZIP-compressed, base64url-encoded landing URLs that stage items directly into Whole Foods carts with 1-click.

---

## 🎯 Architectural Philosophy & Invariants

1. **Zero Walled Gardens:** Bypasses fragile browser automation and deprecated Alexa list APIs by compiling official Amazon AFX payloads (`https://www.amazon.com/afx/ingredients/landingencoded?almBrandId=VUZHIFdob2xlIEZvb2Rz&encodedIngredients=...`).
2. **365 Brand Default for Packaged Goods:** Default to Whole Foods private label (`365`) for pantry staples, canned goods, dairy, and condiments to maximize stock match rates and avoid premium brand markups.
3. **Produce Quality Partitioning (Thin-Skin Organic Rule):** Mandate `"organic"` for thin/edible skins or large surface area items (berries, strawberries, greens, spinach, kale, grapes, apples, peaches, cherries, bell peppers). Allow conventional or organic for thick-skinned produce (avocados, bananas, onions, melons, sweet potatoes).
4. **Meat & Dairy Standards:** Default to 100% Grass-Fed for beef and red meat; standard 365 eggs; 100% organic whole dairy milk.
5. **Pantry Exclusion & Mandatory "Assumed in Stock" Visibility:** Baseline pantry spices and oils (salt, black pepper, cooking oil, basic flour) are excluded from the cart staging, but MUST ALWAYS be explicitly surfaced in a dedicated **"Assumed in Stock"** section. Never silently drop an ingredient without giving Ryan the visibility to catch if they've run out.
6. **Discord Hyperlink Discipline:** Always output final links as `🛒 [**Label**](<url>)` with the emoji outside brackets and angle brackets `< >` around the URL to suppress embed bloat and prevent underscore-induced italic errors.
7. **High-Signal Exception Formatting:** Do not dump exhaustive 30–45 line item manifests into Discord. The 1-click Whole Foods Cart link already holds all staged items. Discord messages must strictly be high-signal and executive: (1) Cart summary with dinner coverage, (2) 1-Click Cart Link, (3) Quantity & Packaging Checks (multi-count items where Amazon defaults to 1, fractional retail package surpluses), (4) Separate Trip / Costco Due items, and (5) Compact Assumed-in-Stock pantry list.

---

## 🔄 The 3 Ingestion Flows

### Flow 1: Recipe Ingestion (Mealie / Text / URL)
- Parse 1 to 5 recipes from Mealie (`http://nas2.local:9090`), pasted recipe text, or recipe URLs.
- Extract ingredients, amounts, and units.
- Strip items present in `data/pantry_staples.json` and catalog them into the **"Assumed in Stock"** block.
- Aggregate duplicate items across recipes (e.g., Recipe A 1 can black beans + Recipe B 2 cans -> 3 cans).
- Convert culinary measurements (cups, tbsp, oz) into purchasing units (`COUNT` for cans/jars/produce, `POUND` for meat/bulk).

### Flow 2: Ad-Hoc Chat Additions
- Parse natural language user additions: *"Add oat milk, bananas, and paper towels to the cart"*.
- Route items to either Whole Foods or Costco based on item profile.
- Merge new items with the active staging manifest.

### Flow 3: Shared Family Grocery List (Google Tasks)
- Live synchronization via official Google Tasks REST API v1 across Ryan, Emily, and kids devices:
  - **`🛒 Whole Foods`**: Primary delivery staging checklist.
  - **`🛒 Costco`**: Parallel path for in-person bulk shopping trips (paper goods, bulk snacks, frozen fries).
- Ingest uncompleted items with `status: needsAction` directly into the weekly manifest.

---

## 📦 Intelligent Multi-Cadence Replenishment Engine

Durable state is tracked in `/workspace/data/grocery_staples.json`:
- **Tier 1 — Weekly Cadence (7–10 Days):** Organic whole milk, standard 365 eggs, sourdough bread, bananas, organic apples, organic baby spinach, organic berries.
- **Tier 2 — Bi-Weekly Cadence (14–21 Days):** Honey Nut Cheerios, roasted seaweed snacks, Shin Ramyun, salted butter, Greek yogurt, Waterloo sparkling water, Dr. Praeger's Littles.
- **Tier 3 — Bi-Monthly Cadence (30–60 Days):** Dry pasta, protein bars, fruit snacks, chicken broth, extra virgin olive oil, bulk freezer items.
- **Delivery Rhythm & Timing:**
  - **Thursday 5:00 PM PT:** Zero posts the weekly staging manifest (staged cart + assumed in stock + due staples).
  - **Thursday 8:00–9:00 PM PT:** Ryan completes 1-click cart review and checks out on Amazon Whole Foods, locking in the Friday afternoon delivery slot before Friday morning capacity fills.
  - **Friday Afternoon:** Scheduled delivery arrival.

---

## 🛠️ Tooling Reference

- **Compiler Script:** `python3 /workspace/tools/whole_foods_afx.py`
  - `--recipe <name>`: Generate from predefined recipe.
  - `--items "<json>"`: Generate from raw item array.
  - `--json-out`: Output JSON manifest and link.
- **Manager Script:** `python3 /workspace/tools/grocery_manager.py`
  - Manages staples, merges recipe lists, and triggers AFX compilation.
- **Google Tasks Connector:** `python3 /workspace/tools/google_tasks_grocery.py`
  - `--list`: Ingest pending items from `🛒 Whole Foods`.
  - `--list-title "🛒 Costco" --list`: Ingest items from Costco list.
  - `--add "<item>"`: Add new item to specified list.
  - `--complete "<task_id>"`: Mark item completed after cart compilation.
