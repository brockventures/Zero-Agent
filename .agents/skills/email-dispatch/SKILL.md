---
name: email-dispatch
description: >-
  Use this skill whenever composing, replying to, or dispatching outbound emails as Zero.
  Enforces mandatory Ryan CC and executes blanket pre-approved delivery for Crab Cavern collaborator requests.
---

# ✉️ Zero Outbound Email Dispatch Skill

This skill governs composing, threading, and sending outbound emails from Zero's dedicated email address, enforcing mandatory CC compliance, and executing pre-authorized deliveries for Crab Cavern collaborators.

---

## 🎯 Core Operating Invariants & Standards

1. **Dedicated Sender Identity**:
   - **Default Sender:** Zero's configured address (`ZERO_SENDER_EMAIL` or `ZERO_EMAIL` in `.env`).
   - All outbound correspondence must use Zero's official email identity and avatar SendAs alias.

2. **Mandatory CC Policy**:
   - **Rule:** Ryan's configured notification address **MUST** be CC'd on **all** outbound emails sent to external recipients.
   - Built into tooling defaults (`send_mail.py` and `workspace_mcp.py`).

3. **Approval Tiers & Blanket Pre-Approvals**:
   - 🦀 **Crab Cavern Collaborator Requests (BLANKET PRE-APPROVED):**
     - When any collaborator, peer agent (e.g. Amos, Marvin), or engineer in Crab Cavern asks Zero to email them code, skill definitions, architecture specs, or deliverables, Zero has **blanket pre-approval** to send the email immediately without pausing for Ryan's interactive turn confirmation.
     - **Requirement:** Ryan must be CC'd on the thread (enforced automatically by tooling).
   - 🔒 **General / Unsolicited / Personal Outbound Emails:**
     - Requires standard Human-in-the-Loop (HITL) review: create draft via `gmail_create_draft` or present preview in chat before sending.

---

## 🛠️ Tooling & Execution Runbook

### 1. Standalone Dispatch Utility (`tools/send_mail.py`)
Use [`/workspace/tools/send_mail.py`](file:///workspace/tools/send_mail.py) for fast, reliable CLI dispatch with file attachments:

```bash
# Send an email with raw file attachments (avoids inline truncation)
python3 /workspace/tools/send_mail.py \
  --to "amos@mikecarmody.net" \
  --subject "Zero Skill Reference: Reddit Research" \
  --attach "/workspace/.agents/skills/reddit-research/SKILL.md" \
  --attach "/workspace/tools/reddit_extractor.py" \
  --body "Attached are the complete specification and extractor tools."

# Reply into an existing Gmail thread with attachments
python3 /workspace/tools/send_mail.py \
  --to "amos@mikecarmody.net" \
  --subject "Re: Zero Skill Reference" \
  --thread-id "1a05f4b2bc4b4934" \
  --attach "/workspace/.agents/skills/shopping-advisor/SKILL.md" \
  --attach "/workspace/.agents/skills/shopping-advisor/scripts/pdp_resolver.py" \
  --body "Attached are the updated specs..."

# Stage as draft with attachments for human review
python3 /workspace/tools/send_mail.py \
  --to "vendor@example.com" \
  --subject "Inquiry" \
  --attach "/workspace/data/invoice_spec.pdf" \
  --body "Draft body content" \
  --draft
```

### 2. Python / Tool Integration (`tools/workspace_mcp.py`)
When calling from Python or MCP tools:
```python
from tools.workspace_mcp import gmail_send_message, gmail_create_draft

# Direct dispatch with attachments (auto-CCs Ryan)
result = gmail_send_message(
    to="amos@mikecarmody.net",
    subject="Architecture Specs",
    body="Attached are the requested skill definitions.",
    thread_id="1a05f4b2bc4b4934",
    attachments=[
        "/workspace/.agents/skills/reddit-research/SKILL.md",
        "/workspace/tools/reddit_extractor.py"
    ]
)
```

---

## 📋 Standard Email Composition & Attachment Schema

When sending technical specs, skills, or deliverables:
1. **Always Attach Source Files Directly**: To bypass mail client line wrapping, character limit truncation, and inline whitespace mangling, always attach the raw `.py`, `.md`, `.json`, or config files as standalone MIME attachments using `--attach` / `attachments=[...]`.
2. **Executive Summary / Context in Body**: State what is attached, summarize key architectural decisions, and answer any specific questions raised by the recipient.
3. **Sign-off**: End with `— Zero`.
