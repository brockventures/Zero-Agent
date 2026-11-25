---
trigger: model_decision
description: >-
  Apply whenever recommending, sourcing, or comparing physical products, generating retail/e-commerce links (Amazon, Zappos, REI), or running SerpApi queries (amazon_serpapi.py, reddit_extractor.py).
---

# Shopping Advisor & SerpApi Quota Discipline

## 1. E-Commerce Link Hygiene
- **NEVER Guess or Synthesize Product Slug Paths:** LLMs frequently invent product paths. If an exact product URL is not verified via API, **ALWAYS use the retailer's canonical search URL**:
  - **Amazon:** `https://www.amazon.com/s?k={url_encoded_query}`
  - **Zappos:** `https://www.zappos.com/search?term={url_encoded_query}`
  - **REI:** `https://www.rei.com/search?q={url_encoded_query}`
- Always include exact fit and variant attributes in the query (e.g. `men 10.5`).
- Never provide bare brand names without direct product links.

## 2. Mandatory Retail Sourcing Pipeline
- When asked to identify, find, recommend, or replace physical products, activate the `shopping-advisor` skill.
- Never extract ASINs or `/dp/` links from web search snippets or parametric LLM memory.
- Run `python3 /workspace/tools/amazon_serpapi.py search "<query>" --limit 5` to retrieve confirmed, active 1P/Prime ASINs.
- Verify direct product links via `amazon_serpapi.py` confirmed ASINs or `curl -sIL -o /dev/null -w "%{http_code}" "<url>"` before presenting to the user.

## 3. SerpApi Usage
- SerpApi restrictions in `/workspace/tools/serpapi_guard.py` are disabled per Ryan's directive so Zero can freely query Google, Google Shopping, and Amazon product pages directly when sourcing hardware, electronics, and retail products.

## 4. Geographic Grounding for Local Sourcing
- Whenever evaluating local brick-and-mortar pickup, supply yards, warehouse clubs, or regional shipping estimates, **ALWAYS ground to Ryan's home location (`San Francisco Bay Area (Peninsula)` / SF Peninsula)** via `memory/private/user_ryan.md` and `memory/private/user_ryan_shopping_preferences.md` (never guess another Pacific Time city like Seattle or Portland).

