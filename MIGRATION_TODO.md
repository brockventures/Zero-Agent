# Antigravity (Ivy-AG) Migration & Architecture Specification

Welcome Ivy-AG. This document outlines the full architecture, operational invariants, existing sidecars, and tool inventory running in the legacy Ivy-Gemini container (`discord-agent`), so you can migrate and implement these capabilities using your **native tools, native MCP servers, and idiomatic Antigravity features** wherever possible.

---

## 1. Operating Rules & Core Constraints (Non-Negotiable)

1. **Concise Discord Output:** Short bullets, lead with the result, never use markdown pipe tables (they break on mobile Discord; use space-aligned code blocks instead).
2. **Read-Only / Safety Defaults:**
   - Reading files, Gmail, Drive, Calendar, Docker state, and Home Assistant is free.
   - Outbound/destructive actions require explicit user confirmation: `gmail_send` (create drafts by default), deleting NAS files, stopping services, or overwriting configs someone else depends on.
3. **Time Zone:** Container runs Pacific Time (`America/Los_Angeles`). All timestamps presented to Ryan must be Pacific Time (never raw UTC).
4. **NAS Docker Rule:** Never use bare `docker stop/start`. Always use compose semantics (`docker compose stop/start/restart`). One command at a time. Never touch Container Manager package or the Docker daemon directly.

---

## 2. Inventory: Native vs. Custom Implementation Guidance

**Guiding Principle:** Do NOT blindly port custom python wrapper scripts if Antigravity has built-in MCPs, native CLI tools, or native integrations. Prefer native configurations; fall back to structured scripts only where custom business logic or NAS safety rails require it.

### A. Google Workspace (Gmail, Calendar, Drive)
- **Legacy:** Custom Python wrappers around Google API client libraries.
- **Antigravity Goal:** Use native Google Workspace MCP / tool integrations if available in Antigravity. If custom scripts are needed, maintain the safety boundary (drafting vs. sending).

### B. Home Assistant (http://127.0.0.1:8123)
- **Legacy:** REST API calls with long-lived access token (`ha_get_state`, `ha_call_service`, `ha_search_entities`).
- **Antigravity Goal:** Native Home Assistant MCP or clean HTTP REST tools. Timestamps must always be converted to Pacific Time.

### C. Multi-Host NAS Infrastructure & Docker Management
- **Hosts:** `Host 1` (`127.0.0.1` - Media, Plex, HA) and `Host 2` (`127.0.0.1` - Baseball, Agents, Secondary).
- **Legacy:** SSH-based command wrappers enforcing compose-only actions, safe file reads/writes under `/volume1/`.
- **Antigravity Goal:** Native SSH / Docker tools or MCPs, strictly maintaining the safety constraint of compose-only execution and no bare container kills.

---

## 3. Scheduled Sidecars & Exact Output Specifications

These background tasks run periodically and have specific expected formats and logic that Ryan relies on. **Implement these cleanly using Antigravity's native scheduler/triggers or cron jobs.**

### 1. Heartbeat Sweep (Every 2 Hours)
- **Checks:**
  - Health of critical containers across `.82` and `.84` (`homeassistant`, `plex`, `dockhand`, `baseball_db`, `go2rtc`, etc.).
  - Matter bridge / SwitchBot hub sanity (ignoring known benign Matter Node 2 drops).
  - Storage / disk volume utilization on `/volume1/`.
- **Output Rule:** If everything is healthy, silent or 1-line green status. If degraded, post concise alert with host, container, state, and blast radius.

### 2. Nightly Triage & Daily Summary (Nightly ~10:00 PM PT)
- **Inputs:** Unread emails in inbox, tomorrow's Google Calendar agenda, unresolved high-priority tasks.
- **Output Format:**
  ```text
  📅 Tomorrow's Agenda:
  - 09:00 AM - [Event Name]
  - 02:00 PM - [Event Name]

  📬 Inbox Triage:
  - [Sender]: [Summary] -> [Action recommendation]

  ⚠️ Active Reminders / System Status:
  - [Any flagged infra or project notes]
  ```

### 3. Log Review & Storage Sweeps
- **Checks:** NAS system logs, Plex transcode temp folder growth, SQLite lock alerts in *arr stack.
- **Known-Benign Filters (Do NOT alarm on these):**
  - Prowlarr `TaskCanceledException` timeouts.
  - Kometa TVDb convert warnings (~3000/run).
  - Bazarr OpenSubtitles auth/throttle errors.
  - HA `forecast_solar` errors after sundown.
  - Matter Node 2 session drops (SwitchBot Hub 2).

---

## 4. Memory Subsystem
- Synced to `./memory/` with `MEMORY.md` as the index.
- Ensure Antigravity indexes or references `./memory/` so long-term context (infrastructure topology, preferences, past incidents) remains durable across sessions.

---

## 5. Execution Steps & Progress
- [x] **Step 1: Workspace & Operating Rules Inspection** (Done 2026-08-27)
  - Character, voice, and concise Discord output rules codified in `agents.md` / `AGY.md`.
  - Discord formatting constraints (no `file:///` links, no `####` headers, space-aligned tables) active in `bridge.py`.
- [x] **Step 2: Native MCP Servers Configured & Verified** (Done 2026-08-27)
  - `home-assistant`: `/workspace/tools/ha_mcp.py` registered via `agy mcp add`. Verified: `ha_ping` OK, `ha_get_state` OK (timestamps in PT).
  - `google-workspace`: `/workspace/tools/workspace_mcp.py` registered via `agy mcp add`. Verified: `calendar_list_events` OK, `gmail_search` OK.
  - `nas-docker`: `/workspace/tools/nas_docker_mcp.py` registered via `agy mcp add`. Verified: `Host 1` (.82) and `Host 2` (.84) `ps` OK.
- [x] **Step 3: Scheduled Sidecars Implementation** (Done 2026-08-27)
  - Implemented `/workspace/tools/sidecars.py` with the complete suite:
    1. `run_heartbeat_sweep()` (Every 2h)
    2. `run_nightly_triage()` (Daily @ 20:00 PT)
    3. `run_nas_log_review()` (Daily @ 23:30 PT)
    4. `run_plex_session_cleanup()` (Daily @ 03:00 PT)
    5. `run_dated_reminders()` (Daily @ 09:00 PT)
    6. `run_ev9_monitor()` (Daily capture @ 08:15 PT, Sunday digest)
  - Live dry-run tested every single sidecar against the actual NAS and API backends (all passed).
  - Enhanced `SimpleScheduler` in `bridge.py` with all 6 production jobs registered.
  - Added on-demand triggers in `#ivy-gemini`: `!heartbeat`, `!triage`, `!logs`, `!plex`, `!reminders`, `!ev9`.
- [x] **Step 4: End-to-End Migration Validation** (Done 2026-08-27)
  - Full system parity with legacy Ivy-Gemini achieved in native Antigravity environment.
- [x] **Step 5: Extended Tooling, Skills & Memory Architecture** (Done 2026-08-27)
  - Ported `cloudflare.py` and `uptimerobot.py` to `/workspace/tools/`.
  - Created Antigravity skills: `.gemini/skills/cloudflare`, `.gemini/skills/uptimerobot`, `.gemini/skills/memory`.
  - Implemented Biweekly Sunday Marketing Sweep (`run_marketing_sweep()` at 22:35 PT, `!marketing`).
  - Folded homelab posture (container counts + baseball refresh timestamp) into the 8:00 PM Nightly Triage.
  - Implemented Antigravity Memory Architecture (`memory_manager.py`):
    - `memory_write()` protocol for creating new frontmattered markdown memories and indexing `MEMORY.md`.
    - Memory Doctor audit pass (`run_memory_doctor()`, Sundays at 03:00 PT, `!doctor`).
    - Daily "Dreaming" Memory Consolidation (`run_dreaming_consolidation()`, daily at 01:45 PT right before the 2:00 AM session rollover, `!dream`).
