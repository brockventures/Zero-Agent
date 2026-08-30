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
