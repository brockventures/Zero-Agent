---
name: web-qa
description: >-
  Use this skill whenever verifying, testing, or visually inspecting local dev servers, staged web apps (e.g. Market Sandbox, Vercel deployments), Home Assistant dashboards, Mealie, or web interfaces.
  Executes on-demand headless browser automation via Playwright to audit DOM elements, check console errors, capture screenshots, and evaluate assertions.
---

# 🧪 Autonomous Web QA & Browser Evaluation Skill

This skill governs conducting on-demand, automated browser testing, DOM inspection, and visual verification using the **Web QA Tool** (`/workspace/tools/web_qa.py`).

---

## 🎯 When to Use This Skill

Activate this skill when:
1. **Verifying web application changes** (e.g. `market-sandbox` updates, Next.js UI fixes, Mealie modifications) before declaring completion.
2. **Self-QAing UI deployments** on local dev ports (`localhost:3000`, `localhost:5173`) or Vercel preview environments.
3. **Checking for silent client-side JavaScript errors, broken assets (404s), or failed API calls**.
4. **Capturing visual proof** (screenshots) to inspect with `view_file` or deliver to user.

---

## 🛠️ Tooling & Execution Runbook

### 1. Basic Page Audit & Screenshot
```bash
python3 /workspace/tools/web_qa.py "https://market-sandbox.vercel.app" --screenshot-name "market_home"
```
- Performs full-page load.
- Audits console errors, network failures, headings, and interactive elements.
- Saves screenshot to `/workspace/data/qa_screenshots/market_home.png`.

### 2. Targeted Assertions & Selector Verification
```bash
python3 /workspace/tools/web_qa.py "http://localhost:3000" \
  --wait-selector "#main-content" \
  --expect-selector "button.trade-btn" \
  --expect-text "Portfolio Value" \
  --screenshot-name "trade_screen"
```

### 3. Programmatic Python Execution
```python
from tools.web_qa import run_web_qa

result = run_web_qa(
    url="http://nas2.local:8000",
    wait_selector=".recipe-card",
    expect_texts=["Recipes"],
    screenshot_name="mealie_dashboard"
)

if not result["success"]:
    print(f"QA Failed: {result['page_errors']}")
```

---

## 🔒 Operational Invariants

1. **On-Demand Execution:**
   - Playwright is launched ephemerally per QA run and closed immediately upon completion.
   - Never leaves persistent browser daemons idling or hogging RAM on Host 2.
2. **Visual Verification:**
   - Screenshots are saved to `/workspace/data/qa_screenshots/`.
   - Inspect captured PNGs directly using `view_file` to evaluate layout, alignment, and visual appeal before reporting back.
3. **Strict Zero Silent Errors:**
   - If any uncaught JS exception (`pageerror`) or `console.error` occurs during page execution, the QA run reports failure so bugs are caught immediately.
