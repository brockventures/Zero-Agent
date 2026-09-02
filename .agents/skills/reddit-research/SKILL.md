---
name: reddit-research
description: >-
  Use this skill whenever researching community consensus, real-world user experiences, subreddit debates, hardware/software failure modes, or technical recommendations from Reddit.
  Extracts authentic insights with verified thread links, prioritizing recent discussions (current year / past 12 months).
---

# 🔍 Reddit Community Consensus & Research Skill

This skill governs conducting authentic, unblocked research into Reddit discussions, extracting genuine community consensus, identifying real-world failure modes and edge cases, and citing 100% verified thread links.

---

## 🎯 Core Research Philosophy & Defaults

1. **Recency by Default (Temporal Filter)**:
   - **Default Rule:** Always prioritize **recent discussions** (current year `2026` or past 12 months `tbs=qdr:y`) unless historical perspective or older hardware/software versions are explicitly requested.
   - Technology ecosystems, homelab software, firmware, and pricing change rapidly; 2026 community sentiment takes precedence over legacy 2021–2023 discussions.

2. **Zero Link Hallucinations (Strict Link Integrity)**:
   - **Never** generate token-predicted URLs or fabricate slug paths like `reddit.com/r/sub/comments/fake_title`.
   - All Reddit citations **must** come directly from:
     - The Google Search Grounding metadata (`groundingMetadata.groundingChunks[].web.uri`) in interactive agent turns.
     - The organic search output from `/workspace/tools/reddit_extractor.py` (`link` field).

3. **Consensus vs. Fringe Nuance**:
   - Differentiate clearly between the **dominant community consensus** (top-upvoted opinions and widespread user agreement) and **vocal minority or edge-case objections**.
   - Always extract:
     - **The Consensus Verdict** (e.g. Synology for turnkey appliance reliability vs. TrueNAS SCALE for custom hardware and ZFS integrity).
     - **Key Trade-offs / Failure Modes** (e.g. hardware transcoding locks, proprietary disk cages, licensing shifts).
     - **Subreddit Biases** (e.g. `r/homelab` favors enterprise rack gear, while `r/selfhosted` favors low-power mini-PCs and Docker compose).

---

## 🛠️ Tooling & Execution Runbook

### 1. Interactive Agent Grounding (Zero / Crab Cavern)
When executing research turns in chat or delegating to subagents:
* **Model Selection:** Use the lightweight tier **`gemini-3.7-flash-low`** (or subagent `Model="flash_lite"`) by default. Reddit discussion reading and consensus summarization are high-volume, low-complexity retrieval tasks where Flash-Low provides optimal speed and minimal cost. Reserve `gemini-3.7-flash-high` only for deep multi-subsystem engineering audits.
* Formulate clear search queries with the current year or temporal constraints:
  - `site:reddit.com/r/selfhosted synology vs truenas 2026`
  - `site:reddit.com/r/BuyItForLife best espresso grinder 2026`
* Ingest the retrieved multi-paragraph passage chunks and map citations strictly to the returned metadata URLs.

### 2. Standalone Python CLI (`reddit_extractor.py`)
Use [`/workspace/tools/reddit_extractor.py`](file:///workspace/tools/reddit_extractor.py) for programmatic searches and verification:

```bash
# Search within a specific subreddit (defaults to past 12 months / 2026)
python3 /workspace/tools/reddit_extractor.py search "synology vs truenas" --sub selfhosted -n 5

# Search across all of Reddit for recent product consensus
python3 /workspace/tools/reddit_extractor.py search "best quiet dishwasher" --sub BuyItForLife -t year

# Target specific timeframes (options: year, month, week, all, 2026)
python3 /workspace/tools/reddit_extractor.py search "home assistant yellow vs green" --sub homeassistant -t month
```

---

## 📋 Standard Consensus Output Schema

When presenting Reddit research to Ryan or other agents, structure responses clearly:

```markdown
### 🏛️ Community Consensus: [Topic / Product / Software Debate]

**Subreddit(s) Analyzed:** `r/[subreddit]` (Discussions from [Year / Recent Timeframe])

#### 1. The Majority Verdict
* **Primary Recommendation / Consensus:** [Clear statement of the prevailing consensus pick or decision].
* **Core Rationale:** [Key reasons why the community recommends this option over alternatives].

#### 2. Key Contenders & Community Divide
* **Option A ([e.g. TrueNAS]):** Best for [use case]. Praised for [strengths]; criticized for [weaknesses].
* **Option B ([e.g. Synology]):** Best for [use case]. Praised for [strengths]; criticized for [weaknesses].

#### 3. Real-World Failure Modes & Quirks
* [Specific recurring complaints, firmware issues, or caveats raised by verified users].

#### 4. 🔗 Verified Relevant Threads
* 🌐 [Thread Title 1](https://www.reddit.com/r/...) — *Key takeaway from this thread*
* 🌐 [Thread Title 2](https://www.reddit.com/r/...) — *Key takeaway from this thread*
```
