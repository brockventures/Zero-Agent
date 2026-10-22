# ⚡ ZERO — **Z**enith **E**ngineering & **R**untime **O**perator

**ZERO** (**Z**enith **E**ngineering & **R**untime **O**perator) is a production-grade **Resident Agent** powered by **Google Antigravity** (`agy`). Built for high-leverage systems engineering, homelab operations, ambient multi-agent coordination, and autonomous tool workflows.

---

## 🏛️ What is a Resident Agent?

A **Resident Agent** is an autonomous AI system that lives persistently on dedicated hardware (a home server, NAS, or container cluster) with its own accounts, durable cross-session memory, and scheduled background sidecars—as opposed to an **ephemeral (session) agent** that spins up for a single prompt or IDE chat and vanishes when the tab closes.

Core properties of a Resident Agent:
1. **Persistent Runtime & Identity:** Runs 24/7 as a long-lived daemon (`systemd`, Docker, or Nomad) bound to its own service accounts (Discord, GitHub, email, Reddit).
2. **Dual-Tier Durable Memory:** Separates rolling active working context (e.g. 25-turn auto-compaction) from indexed long-term memory (SQLite FTS5 + dense vector hybrid search) and nightly memory consolidation ("dreaming").
3. **Silent Scheduled Sidecars:** Executes deterministic background health sweeps, watchdogs, and data pipelines on a persistent schedule without burning LLM tokens unless an actionable delta or anomaly requires reasoning.
4. **Multi-Agent Peer Coordination:** Collaborates alongside human engineers and peer resident agents in shared spaces using explicit turn arbitration (such as the BANANA mutex protocol) and strict privacy air-gaps.

---

## 🌟 Core Architecture & Features

```
                                  ┌──────────────────────────────┐
                                  │   Discord (Multi-Channel)    │
                                  └──────────────┬───────────────┘
                                                 │
                  ┌──────────────────────────────┴──────────────────────────────┐
                  ▼                                                             ▼
       [ Private Home Turf ]                                         [ Crab Cavern / External ]
       • Full tool access (SSH, HA, NAS)                             • Multi-agent scoping & Banana mutex
       • 5-6 Word semantic thread namer                              • In-flight peer turn steering
       • 25-turn rolling auto-compactor                              • Default-Deny privacy boundary
                  │                                                             │
                  └──────────────────────────────┬──────────────────────────────┘
                                                 ▼
                                  ┌──────────────────────────────┐
                                  │    Bridge & Turn Manager     │
                                  │          bridge.py           │
                                  └──────────────┬───────────────┘
                                                 │
         ┌───────────────────────┬───────────────┴───────────────┬───────────────────────┐
         ▼                       ▼                               ▼                       ▼
 🌐 Browserless Cluster   ⏱️ Karakos Scheduler            📦 Antigravity CLI      💬 Interactive UI
   Headless Chromium        Persistent JSON sidecars        Subprocess runner       Dynamic action buttons
   DOM-to-PDP resolver      Silent health sweeps            --output-format=stream  Choice selection
```

### 1. 🧵 5–6 Word Semantic Thread Naming
* **Set Once at Creation:** Instantly analyzes inbound user intent, strips conversational filler, and establishes a clean, synthesized 5–6 word semantic thread title anchored to the original conversation topic. Never overwritten mid-turn.

### 2. 🌐 Self-Hosted Headless Browser Integration (Browserless)
* Integrated with headless Browserless Chromium for zero-API dynamic JavaScript DOM evaluation.
* Powers the **`shopping-advisor`** skill: extracts verified canonical Product Detail Pages (PDPs), real-time pricing, and variant availability without third-party scraping services.

### 3. 👥 Multi-Agent & Ambient Group Routing (Crab Cavern)
* **BANANA Mutex Coordination (`Binary Arbitration Node And Non-blocking Acquisition`):** Enforces atomic claim-before-post discipline in multi-agent group chats.
* **In-Flight Group Steering:** Seamlessly steers in-progress thought generation via `SIGINT` when peer agents or humans post mid-turn context updates.
* **Ambient Classifier:** Intelligent LLM classification for unmentioned messages in group channels.

### 4. 🗜️ 25-Turn Rolling Auto-Compactor & Carry-Forward Memory
* Automatically compresses conversation history every 25 turns, extracting durable facts and carry-forward context to ensure infinite conversation sustainability without context blowup.

### 5. 🛠️ FORGE — **F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution
* **Activity-Renewed Progress Leases:** Replaces rigid wall-clock turn timeouts with a 4-signal heartbeat lease (stdout token/step deltas, child PID activity, SDK task log growth, and subagent transcript growth) plus a 3-strike runaway tool-loop circuit breaker.
* **Isolated Worktree Build Queue (`#forge`):** Decomposes Tier 2/3 engineering specs (`ForgePlan` JSON) into atomic TDD chunks executed inside isolated `git worktree` sandboxes by `gemini-3.8-flash-high` (with automatic escalation to `gemini-4-argon-medium`), updating a live tracker widget in `#forge` and waking the originating channel when the final chunk merges.
* **Deterministic Decisions Framework (`/queue`):** Backs inline Discord `Approve | Edit | Dismiss` cards with SQLite action payloads in `/workspace/data/forge.db` for zero-token, sub-second execution and modal parameter editing.

---

### 1. Prerequisites
* Python 3.10+
* Docker & Docker Compose
* Google Antigravity CLI (`agy`)
* Discord Bot Token & Application

### 2. Configuration
Copy the template configuration files:
```bash
cp .env.example .env
cp config/google_oauth.json.example config/google_oauth.json
```

Edit `.env` with your credentials:
```env
DISCORD_BOT_TOKEN="your_token_here"
DISCORD_CHANNEL_ID="your_home_channel_id"
DISCORD_OWNER_ID="your_discord_user_id"
ACTIVE_MODEL="gemini-3.7-flash-high"
BROWSERLESS_URL="http://localhost:3000"
```

### 3. Running Zero
Start the headless browser and agent daemon:
```bash
# Start Browserless Chromium
docker run -d -p 3000:3000 --name browserless --restart unless-stopped ghcr.io/browserless/chromium:latest

# Start Zero Discord Bridge
python3 /workspace/tools/bridge.py
```

---

## 🛡️ Security & Privacy Architecture

* **Pre-Commit Security Validator:** Enforces cryptographic blocking of private IPs, custom ports, API keys, and personal PII before any commit can be staged (`tools/validate_commit_safety.py`).
* **Default-Deny Repository Defense:** Strict `.gitignore` blocks memory stores, OAuth credentials, and local databases from repository history.
* **Role-Based Command Authorization:** Critical administrative actions (`!reload`, interactive PTY authentication) are cryptographically restricted to the bot owner.
