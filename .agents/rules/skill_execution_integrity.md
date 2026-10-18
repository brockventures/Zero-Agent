---
trigger: always_on
glob: "*"
description: Strict protocol forbidding silent skill abandonment, swallowed tool failures, or ungrounded downgrades when skills or APIs encounter errors.
---

# Skill Execution Integrity & Failure Disclosure Protocol

1. **NO SILENT DOWNGRADES:**
   - When a user's prompt triggers an established Skill (e.g. `shopping-advisor`, `repo-evaluator`, `reddit-research`), you MUST execute the required research tools and pipeline steps defined in that skill.
   - If a tool, API, or external command returns an error (such as HTTP 503, 429, timeout, or empty output), you are **STRICTLY FORBIDDEN** from silently abandoning the skill and substituting ungrounded parametric LLM memory.

2. **MANDATORY DISCLOSURE & FALLBACK ORDER:**
   - **Step A (Automated Failover):** Immediately attempt an alternative available tool (e.g., if platform `search_web` fails with a 503 capacity error, pivot immediately to `/workspace/tools/reddit_extractor.py`, `/workspace/tools/amazon_serpapi.py`, or `read_url_content`).
   - **Step B (Explicit Disclosure):** If a tool cannot be recovered or an alternate does not exist, you MUST explicitly disclose the failure to the user:
     > *"⚠️ Note: [Tool/Pillar Name] was unavailable ([Error Code/Reason]). Proceeding with verified results from [Other Pillars]."*
   - Never pretend research was executed if a tool was skipped.

3. **DECOUPLE FORMATTING FROM RIGOR:**
   - User requests for brevity (e.g., "TL;DR", "keep it concise") or channel constraints (e.g., Discord mobile limits, no pipe tables) dictate **how results are structured and presented**.
   - Presentation constraints NEVER justify skipping backend research, skipping review defect audits, or cutting corners on skill verification. Do the rigorous work in the background; deliver the output concisely.

4. **STRICT BAN ON SILENT PIVOTS & MOCK SUBSTITUTIONS:**
   - When tasked with sourcing, downloading, inspecting, or processing an authentic third-party asset, model, file, or specification (e.g. MakerWorld, Printables, GitHub, Thingiverse), the agent is **STRICTLY FORBIDDEN** from silently abandoning the requested source and fabricating, mocking, or synthesizing an unapproved replacement (such as procedurally generating a box in CAD, synthesizing a mock API payload, or inventing a dummy file) without explicit user consent.
   - Roadblocks (anti-bot 403s, CAPTCHAs, Cloudflare Turnstile, authentication gates) MUST be tackled using designated failover tools (such as `pixel-browser` on physical mobile hardware).
   - If designated failovers are exhausted or blocked by credential/permission requirements, the agent MUST immediately stop, disclose the exact barrier, and request explicit authorization or guidance per `ask_for_credentials_first.md`. Never pretend a procedural substitute was what the user asked for.
