---
name: shopping-advisor
description: >-
  Use this skill whenever the user asks for product recommendations, buying advice,
  reviews, comparisons, alternatives, or replacements, or to find/source a product to buy on Amazon or retail
  (e.g., "what is the best X", "find me steak knives", "recommend an espresso machine", "what should I buy for Y",
  "those shoes we ordered ended up being not quite right, do you have other suggestions", "this didn't work out, what should I get instead",
  "alternatives to X", "find this on amazon", "what is this item, can you find it on amazon", or image-based product identification and sourcing).
  Executes live Amazon SerpApi pricing/ASIN retrieval, defect review audits, Reddit consensus (r/BuyItForLife),
  and Wirecutter/RTINGS testing.
---

# 🛍️ Product Research & Shopping Advisor Skill

This skill orchestrates end-to-end product research across three simultaneous intelligence pillars: **Reddit Enthusiast Consensus**, **Editorial Lab Testing (Wirecutter / RTINGS)**, and **Live Amazon Retail Intelligence**.

---

## 🎯 Core Research & Recommendation Philosophy

**Three-Pillar Parallel Research Protocol:**
• 💬 **Pillar 1: Reddit Consensus** (`reddit-research` skill) ──► Real-world longevity, repairability, and known defect patterns.
• 🗞️ **Pillar 2: Editorial Lab Testing** (Wirecutter & RTINGS) ──► Standardized test rankings (*Top Pick*, *Budget Pick*).
• 📦 **Pillar 3: Retail & Fulfillment Intelligence** (Amazon SerpApi, Warehouse Clubs & Big Box) ──► Live pricing, seller vetting, defect inspection, and freight vs. local pickup trade-offs.

---

### 1. The Multi-Pillar Research Protocol

When handling evaluative product requests (*"best X"*, *"what should I buy for Y"*), execute three tracks concurrently:

* **Pillar 1: Community Consensus (Directly invokes [`reddit-research`](file:///workspace/.agents/skills/reddit-research/SKILL.md))**:
  - Run [`/workspace/tools/reddit_extractor.py`](file:///workspace/tools/reddit_extractor.py) or use `gemini-3.5-flash-low` with search grounding.
  - Target subreddits like `r/BuyItForLife`, `r/espresso`, `r/HomeImprovement`, or domain-specific hubs.
  - Extracts true long-term durability, motor/gear failures, customer support ease, and repairability.

* **Pillar 2: Editorial & Lab Testing (Wirecutter & RTINGS)**:
  - Formulate targeted search queries against Wirecutter and RTINGS:
    - `site:nytimes.com/wirecutter best <product category>`
    - `site:rtings.com <product category> review`
  - Ingest lab testing rankings: *Top Pick*, *Budget Pick*, *Upgrade Pick*.
  - Compares lab results against real-world Reddit wear-and-tear reports.

* **Pillar 3: Retail, Big-Box & Fulfillment Intelligence ([`amazon_serpapi.py`](file:///workspace/tools/amazon_serpapi.py) & Canonical Search Generators)**:
  - Queries Amazon SerpApi for candidate models, reviews, and defects.
  - Evaluates warehouse clubs (Costco) and big-box retailers (Home Depot, Best Buy, Lowe's) when freight damage, bulky transit, or member warranties make warehouse pickup superior to parcel delivery.
  - Enforces **Seller & Fulfillment Hierarchy**:
    - **Tier 1 (Required Default):** Ships from & sold by `Amazon.com` (1P), Verified Brand Official Storefront, or Authorized Big-Box / Warehouse Club (Costco, Home Depot, Best Buy).
    - **Tier 2 (Fallback):** High-volume Prime-eligible (FBA) 3rd party in **New** condition.
    - **Tier 3 (Avoid):** Non-Prime or unverified 3rd party sellers.

---

### 2. Multi-Source Conflict Resolution

When external sources disagree, explicitly highlight the tension:
* **Wirecutter vs. Reddit Divergence:** (e.g. Wirecutter picks an appliance for modern convenience/UI, but Reddit reports high electronic board failure after 2 years). Surface both: explain why Wirecutter loves the feature set and why Reddit warns about longevity.
* **Enthusiast Consensus vs. Amazon Bestsellers:** Highlight why the enthusiast pick (e.g. all-metal commercial gears) outperforms the Amazon sales-volume leader (e.g. high-margin sponsored plastic build).

---

### 3. The Anti-Conceptual Trap & Product Replacement Invariant

* **Zero Purely Conceptual Responses:** When the user asks for suggestions, recommendations, or alternatives for physical products—even conversationally without mentioning a store or asking for links explicitly (e.g., *"Those shoes we ordered ended up being not quite right. Do you have other suggestions?"*, *"What should I look into instead?"*, *"Recommend a replacement"*):
  - **NEVER stop at plain-text analysis, biomechanical essays, or bare brand names.**
  - Explaining the ergonomic, biomechanical, or engineering failure mode of a prior item is great context, but it **MUST be immediately accompanied by verified direct product page links** (`/dp/<ASIN>`) for candidate replacements.
* **Product Replacements & Post-Return Workflow:**
  1. **Diagnose the failure mode:** Pinpoint the mechanical or structural flaw of the returned product (e.g. rigid forefoot rocker, excessive stack height, unstable bevel).
  2. **Select targeted counter-models:** Identify 3-4 specific models that resolve that exact defect (e.g., flexible forefoot, moderate stack, neutral styling).
  3. **Execute live retail retrieval:** Run `python3 /workspace/tools/amazon_serpapi.py search "<query>" --limit 5` to pull active 1P/Prime ASINs, live pricing, and reviews.
  4. **Verify links:** Pre-flight check URLs via `python3 /workspace/.agents/skills/shopping-advisor/scripts/verify_links.py`.
  5. **Deliver standard recommendation cards:** Present structured cards containing direct product detail links (`https://www.amazon.com/dp/<ASIN>`), live pricing, and defect/consensus audits.

---

## 🛠️ Tooling & Execution Runbook

### Step 1: Run Reddit & Editorial Research Concurrently
```bash
# 1. Reddit Community Consensus (via reddit_extractor.py)
python3 /workspace/tools/reddit_extractor.py search "best drip coffee maker" --sub BuyItForLife -t year

# 2. Editorial Wirecutter & RTINGS Search (via Google search engine)
# Query: site:nytimes.com/wirecutter "best drip coffee maker" 2026
```

### Step 2: Query Live Amazon Pricing & Seller Intelligence
```bash
# Search candidate products on Amazon
python3 /workspace/tools/amazon_serpapi.py search "Moccamaster KBGV Select" --limit 5

# Fetch verified customer reviews for critical defect auditing
python3 /workspace/tools/amazon_serpapi.py reviews "<ASIN>" --limit 10
```

### Step 3: Verify Links & Output Canonical Cards
* Verify that all retail links are live or adhere to verified canonical search formats via [`scripts/verify_links.py`](file:///workspace/.agents/skills/shopping-advisor/scripts/verify_links.py).
* Strictly follow [`.agents/rules/ecommerce_links.md`](file:///workspace/.agents/rules/ecommerce_links.md) and [`.agents/rules/mobile_discord_formatting.md`](file:///workspace/.agents/rules/mobile_discord_formatting.md).

---

## 📋 Standard Multi-Source Recommendation Card

```markdown
🏆 **1. [Product Name / Model]** — *Recommended: [e.g. Best Overall / Enthusiast Pick]*
- 💵 **Price:** `$XX.XX` (New, Prime / Retail)
- 🏷️ **Seller:** Ships from / Sold by `Amazon.com` (or `[Brand] Official Storefront` / `Costco` / `Home Depot`)
- ⭐ **Multi-Source Consensus:**
  - 🗞️ **Wirecutter:** *Top Pick* (Praised for temperature stability and brew speed)
  - 💬 **Reddit (r/BuyItForLife):** *Consensus Favorite* (5+ year longevity, replaceable parts)
  - 📦 **Retail / Amazon Rating:** 4.7 ★ (8,500+ reviews)
- 🔍 **Critical Takeaway & Defect Audit:** [Summary of verified user failure modes, transit hazards, or build notes]
- 🔗 **Link:** [Amazon Product Detail Page](https://www.amazon.com/dp/ASIN) | [Home Depot Search](https://www.homedepot.com/s/QUERY) | [Costco Search](https://www.costco.com/s?dept=All&keyword=QUERY)

🥈 **2. [Product Name / Model]** — *Alternative: [e.g. Best Value / Runner-Up]*
- 💵 **Price:** `$XX.XX` (New, Prime / Retail)
- 🏷️ **Seller:** Ships from / Sold by `Amazon.com` (or authorized retailer)
- ⭐ **Multi-Source Consensus:**
  - 🗞️ **Wirecutter:** *Budget Pick*
  - 💬 **Reddit:** *Solid Entry-Level Recommendation*
  - 📦 **Retail / Amazon Rating:** 4.5 ★ (22,000+ reviews)
- 🔍 **Critical Takeaway & Defect Audit:** [Summary of pros, cons, and why it differs from Pick #1]
- 🔗 **Link:** [Amazon Product Detail Page](https://www.amazon.com/dp/ASIN) (or [Retailer Search Link](canonical_url))
```
