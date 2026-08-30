---
name: shopping-advisor
description: Specialized skill for product research, price tracking, sizing/variant discovery, and resolving direct Product Detail Page (PDP) URLs using our self-hosted Browserless Chromium cluster without third-party APIs.
---

# 🛍️ Shopping Advisor & Product Intelligence Skill

This skill governs how to search for products, resolve direct canonical Product Detail Pages (PDPs), and provide 100% verified, live links using our self-hosted **Browserless Chromium container on Host 2 (`127.0.0.1:3000`)**.

## 🚀 The Automated Browserless Search-to-PDP Resolver

When linking to a specific product or variant:
1. **NEVER** guess or hallucinate product IDs, category codes, or URL slugs.
2. **NEVER** use external paid scraping services (like SerpAPI) when our local Browserless cluster is available.
3. Use the automated Browserless resolver scripts:

```bash
# 1. Resolve exact, in-stock canonical PDP URL from live retailer DOM
python3 /workspace/.agents/skills/shopping-advisor/scripts/pdp_resolver.py "<brand> <model> <gender> <size> <color>"

# 2. Pre-flight verify HTTP 200 status
python3 /workspace/.agents/skills/shopping-advisor/scripts/verify_links.py "<resolved_url>"
```

## ⚙️ Architecture & Component Layout

* **Container Endpoint:** `http://127.0.0.1:3000` (Browserless Chromium on Host 2)
* **Python Tool:** [`/workspace/tools/browser_tool.py`](file:///workspace/tools/browser_tool.py)
* **Resolver Script:** [`/workspace/.agents/skills/shopping-advisor/scripts/pdp_resolver.py`](file:///workspace/.agents/skills/shopping-advisor/scripts/pdp_resolver.py)
* **Pre-Flight Checker:** [`/workspace/.agents/skills/shopping-advisor/scripts/verify_links.py`](file:///workspace/.agents/skills/shopping-advisor/scripts/verify_links.py)

## 🛡️ Fallback Behavior

* If a product is genuinely discontinued or out of stock across all variants, explain that the inventory has phased out and provide the verified canonical search URL (`https://www.zappos.com/search?term=...`).
