---
name: security-auditor
description: Specialized security architecture audit skill. Performs deep automated scanning for hardcoded secrets, tokens, private IPs, and personal PII; validates git staging & .gitignore segregation defense; audits Indirect Prompt Injection (IPI) boundaries and container isolation.
---

# 🛡️ Security Auditor & Exfiltration Defense Skill

The **Security Auditor** skill provides automated and forensic security reviews for codebases, git repositories, multi-agent channels, and inbound external integrations.

---

## 🎯 When to Activate This Skill
* **Pre-Publish / Pre-Git Audits:** Before publishing code, sharing repositories, or pushing commits to GitHub/Crab Cavern.
* **Secret & PII Detection:** Scanning source trees for accidental leaks of OAuth tokens, SSH keys, private IPs, passwords, addresses, phone numbers, or personal emails.
* **Multi-Tenant / Multi-Agent Boundaries:** Evaluating whether untrusted external messages (from Discord, Slack, or webhooks) can trigger unauthorized tool execution, file access, or credential exfiltration.
* **Pre-Commit Enforcement:** Generating and verifying automated pre-commit hook validators.

---

## 🛠️ Automated Audit Tools

```bash
# 1. Run full secret and PII scan across workspace
python3 /workspace/.agents/skills/security-auditor/scripts/scan_secrets_and_pii.py /workspace/tools /workspace/config

# 2. Audit staged git changes against security rules
python3 /workspace/.agents/skills/security-auditor/scripts/audit_git_staging.py

# 3. Test multi-tenant container isolation boundaries
python3 /workspace/.agents/skills/security-auditor/scripts/verify_isolation.py
```

---

## 🔍 Core Security Guardrails & Checklists

### 1. 🔑 Secrets & Credentials Guardrail
* **Rule:** NEVER commit live secrets to git or store plaintext tokens in public folders.
* **Storage Standard:** All live tokens belong in `/secrets/` or mounted runtime volumes, never in `/workspace/config/`.
* **Templates:** Always provide sanitized `*.example` files (`.env.example`, `google_oauth.json.example`) with dummy values.

### 2. 🏠 Network Topology & Homelab Parameterization
* **Rule:** NEVER hardcode private subnet IPs (`192.168.x.x`, `10.x.x.x`), custom SSH daemon ports, private hostnames, or NAS paths (`/docker/...`) into shared tools.
* **Standard:** Use `os.environ.get()` with safe fallback defaults (`127.0.0.1`, `localhost`, port `22`, user `admin`).

### 3. 🧠 Personal Memory & PII Segregation
* **Rule:** All durable memories, family details, financial spreadsheets, and address books must remain local under `/workspace/memory/` and `/workspace/data/`.
* **Repository Rule:** Enforce strict **Default-Deny** in `.gitignore` for `memory/`, `data/`, `car_monitor_data/`, and `.env`.

### 4. 🤖 Multi-Agent / External Execution Isolation
* **Rule:** Untrusted external prompts (e.g. Crab Cavern, public Discord channels) must NEVER have access to host SSH keys, mounted `/secrets/`, or host-mutating MCP tools.
* **Administrative Commands:** `!reload`, `!reset`, and PTY auth token forwarding must strictly authenticate the bot owner's Discord User ID.
