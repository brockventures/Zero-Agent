# ⚡ Zero — Autonomous Systems Engineering Co-Pilot & Discord Agent Stack

Zero is a production-grade, stateful, autonomous Discord agent architecture powered by **Google Antigravity** (`agy`). Built for high-leverage systems engineering, homelab operations, ambient team coordination, and intelligent tool workflows.

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
       • 2-Stage dynamic thread namer                                • In-flight peer turn steering
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

### 1. 🧵 2-Stage Dynamic Semantic Thread Naming
* **Stage 1 (Pre-Turn):** Instantly analyzes inbound user intent, strips conversational filler, and establishes a clean 3–5 word semantic thread title.
* **Stage 2 (Post-Turn Auto-Healing):** Dynamically inspects the agent's deliverables and refines the thread title from the generated Markdown headers.

### 2. 🌐 Self-Hosted Headless Browser Integration (Browserless)
* Integrated with headless Browserless Chromium for zero-API dynamic JavaScript DOM evaluation.
* Powers the **`shopping-advisor`** skill: extracts verified canonical Product Detail Pages (PDPs), real-time pricing, and variant availability without third-party scraping services.

### 3. 👥 Multi-Agent & Ambient Group Routing (Crab Cavern)
* **Banana Mutex Coordination:** Enforces atomic claim-before-post discipline in multi-agent group chats.
* **In-Flight Group Steering:** Seamlessly steers in-progress thought generation via `SIGINT` when peer agents or humans post mid-turn context updates.
* **Ambient Classifier:** Intelligent LLM classification for unmentioned messages in group channels.

### 4. 🗜️ 25-Turn Rolling Auto-Compactor & Carry-Forward Memory
* Automatically compresses conversation history every 25 turns, extracting durable facts and carry-forward context to ensure infinite conversation sustainability without context blowup.

---

## 🚀 Quickstart & Setup

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
