---
name: context7-docs
description: >-
  Use this skill whenever looking up official, up-to-date documentation, API signatures, migration guides, or code examples for third-party libraries, frameworks, or SDKs (e.g. FastAPI, Pydantic, Tailwind, Playwright, Next.js, UniFi, Home Assistant, etc.).
  Eliminates LLM training-data hallucinations by retrieving live, versioned documentation chunks directly from Context7.
---

# 📚 Context7 Up-To-Date Documentation Skill

This skill governs retrieving canonical, version-accurate documentation, API signatures, and official code examples using the **Context7 Documentation Intelligence Tool** (`/workspace/tools/context7.py`).

---

## 🎯 When to Use This Skill

Activate this skill when:
1. **Writing or refactoring code** using modern libraries whose APIs have evolved (e.g. Pydantic v2 `@model_validator`, FastAPI lifespan handlers, Playwright locators, Next.js App Router, Tailwind v4).
2. **Encountering unexpected `TypeError`, `AttributeError`, or `DeprecationWarning`** from third-party libraries.
3. **Checking exact parameter names, default values, or return types** for library methods.
4. **Verifying official best practices and idioms** before writing non-trivial integrations.

---

## 🛠️ Tooling & Execution Runbook

### 1. One-Shot Quick Lookup
For immediate doc lookups matching a library and specific topic:
```bash
python3 /workspace/tools/context7.py query "<library_name>" "<topic or signature>"
```
*Examples:*
```bash
# Lookup Pydantic v2 model validator syntax
python3 /workspace/tools/context7.py query "pydantic" "model_validator mode after"

# Lookup FastAPI lifespan context managers
python3 /workspace/tools/context7.py query "fastapi" "lifespan context manager startup shutdown"

# Lookup Playwright locator filters
python3 /workspace/tools/context7.py query "playwright" "locator filter has_text"
```

### 2. Two-Step Precision Search
If the one-shot lookup needs finer granularity:
```bash
# Step 1: Search for library canonical IDs
python3 /workspace/tools/context7.py search "fastapi" --query "router"

# Step 2: Query specific section using the exact library ID
python3 /workspace/tools/context7.py docs "/websites/fastapi_tiangolo" "APIRouter lifespan"
```

---

## 🔒 Operational Invariants

1. **Local Caching:**
   - Results are automatically cached in `/workspace/data/context7_cache/` (TTL: 3 days). Repeat lookups within the same session or day are instantaneous (0ms latency, zero rate-limit impact).
2. **API Keys:**
   - Optional API key is auto-loaded from `/secrets/context7.json` or `CONTEXT7_API_KEY` env var. If not set, public free-tier access is utilized.
3. **Receipts & Grounding:**
   - Always reference the official source doc link provided in the Context7 output when explaining API patterns.
