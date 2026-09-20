---
description: Strict guidelines for generating e-commerce, shopping, and retail links. Forbids hallucinated URL paths.
globs: "*"
---

# E-Commerce & Retail Link Generation Rules

1. **NEVER Guess or Synthesize Product Slug Paths:**
   - LLMs frequently invent product IDs or category path codes (e.g. `zappos.com/p/.../product/9813589`).
   - If an exact, confirmed product link is not verified via API, **ALWAYS use the retailer's canonical search URL format**:
     - **Zappos:** `https://www.zappos.com/search?term={url_encoded_query}`
     - **Amazon:** `https://www.amazon.com/s?k={url_encoded_query}`
     - **REI:** `https://www.rei.com/search?q={url_encoded_query}`

2. **Always Include Exact Fit / Variant Attributes in Query:**
   - Example: Include `men 10.5` and model generation in the URL term so the user lands on filtered, in-stock results.

3. **Mandatory Retail Sourcing Pipeline for Physical Goods & Replacements:**
   - Whenever the user asks to find, identify, buy, replace, or recommend physical products (from text queries, attached photos/screenshots, or conversational replacement requests like *"Those X we ordered ended up being not quite right. Do you have other suggestions?"*):
     - **ALWAYS activate the `shopping-advisor` skill.**
     - **NEVER provide text-only conceptual commentary or bare brand names without direct product links.**
     - **NEVER extract ASINs or `/dp/` links from web search snippets or LLM training data.** Search snippets frequently contain deprecated, mismatched, or regional ASINs.
     - **ALWAYS run `python3 /workspace/tools/amazon_serpapi.py search "<product query>" --limit 5`** to retrieve confirmed, active 1P/Prime ASINs.
     - **ALWAYS run `python3 /workspace/.agents/skills/shopping-advisor/scripts/verify_links.py "<url>"`** to confirm HTTP 200 resolution before presenting to the user.
