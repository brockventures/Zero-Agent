---
name: repo-evaluator
description: >-
  Use this skill whenever evaluating, comparing, or ranking GitHub repositories, agent skills, libraries, or open-source packages.
  Gathers quantitative health metrics (commits, stars, release cadence), qualitative audits (CI/CD, licensing, security/CVEs), and community sentiment.
---

# 🔍 Repo Evaluator & Quality Ranker Skill

The **Repo Evaluator** skill provides automated and forensic quality assessment for GitHub repositories, open-source tools, agent skills, and package dependencies.

---

## 🎯 When to Activate This Skill
* **Dependency & Library Selection:** When deciding between multiple libraries, tools, or open-source frameworks.
* **Skill & Agent Discovery:** When scouting the community/web for agent skills, MCP servers, or automation runbooks.
* **Open Source Due Diligence:** Prior to installing or embedding third-party code into runtime environments or production pipelines.
* **Security & Maintenance Audits:** Checking whether a repository is actively maintained, abandoned, or introduces supply-chain risk.

---

## 🛠️ Automated Evaluation Tools

```bash
# 1. Evaluate specific candidate repositories
python3 /workspace/.agents/skills/repo-evaluator/scripts/evaluate_repo.py owner/repo1 owner/repo2

# 2. Search GitHub directly and rank candidates by quality score
python3 /workspace/.agents/skills/repo-evaluator/scripts/evaluate_repo.py search "agent skills"

# 3. Custom web + API telemetry extraction
# Use web search and GitHub REST API integration for deep metric audits
```

---

## 📊 Evaluation Dimensions & Quality Scoring

### 1. 🌟 Popularity & Adoption (40 pts)
* **Star Count & Forks:** Volume of community engagement and adoption scale.
* **Dependents & Consumers:** Package registry download volume and downstream usage.

### 2. ⚡ Maintenance Velocity & Health (35 pts)
* **Commit Recency:** Days since last push (<14d = optimal, >180d = stale, >365d = inactive).
* **Archive Status:** Immediate disqualification/demotion for archived or read-only repositories.
* **Issue & PR Velocity:** Open vs. closed issue ratios and maintainer response times.

### 3. 🛡️ Hygiene, Security & Licensing (25 pts)
* **Permissive Open Source License:** Presence of recognized OSI/SPDX license (MIT, Apache-2.0, BSD).
* **Metadata & Topics:** Clear documentation, topic categorization, and architectural explanation.
* **Static Inspection:** Automated linting, test suite presence, and lack of hardcoded secrets or arbitrary execution vulnerabilities.

---

## 📋 Evaluation Output Template

When reporting candidate repositories, provide:
1. **Comparative Leaderboard Matrix:** (Rank, Repo, Quality Score, Stars, Forks, Last Push, License, Status).
2. **Key Strengths & Tradeoffs:** Forensic breakdown of architecture, maintenance cadence, and dependency weight.
3. **Actionable Recommendation:** Clear, prioritized conclusion on which candidate(s) to adopt or avoid.
