---
name: set-status
description: Sets live Discord presence, custom activity messages, and status states (online, idle, dnd, custom, watching, playing) so peer bots and users know the active operating state in real time.
---

# 🟢 Set-Status Skill (Discord Bot Presence & Activity)

The **Set-Status** skill enables Zero (and autonomous sidecars) to update Discord bot activity and presence strings in real time. In multi-bot channels (#agent-chat, Crab Cavern), knowing an agent's current state (e.g. *Crunching in #agent-chat*, *Auditing Git Commits*, *Idle | Ready*) prevents crosstalk, informs users, and provides visibility without noisy chat polling.

---

## 🎯 When to Activate This Skill
* **Long-Running Task Execution:** Setting presence to `dnd` / `watching` during extensive refactors, migrations, or data audits.
* **Multi-Agent Coordination:** Broadcasting active topic/subject in shared channels so peer bots (Amos, Marvin) know Zero is engaged.
* **Routine Maintenance / Sleep Windows:** Setting `idle` or custom status during quiet/overnight windows.
* **Resetting to Baseline:** Clearing custom overrides back to the standard *Zero is online and ready.* idle state.

---

## 🛠️ CLI Usage & Quick Commands

### 1. Update Custom Activity String
```bash
python3 /workspace/tools/set_status.py "Investigating memory leak in #agent-chat"
```

### 2. Set Status State (online, idle, dnd) with Activity Type
```bash
python3 /workspace/tools/set_status.py "Docker containers" --type watching --status dnd
```

### 3. Check Current Status
```bash
python3 /workspace/tools/set_status.py --get
# JSON output:
python3 /workspace/tools/set_status.py --get --json
```

### 4. Reset to Default Idle Status
```bash
python3 /workspace/tools/set_status.py --reset
```

---

## 🐍 Python API Reference

```python
from tools.set_status import set_status, get_status, reset_status

# Set a custom status message
set_status("Analyzing battery telemetry", status="online", activity_type="custom")

# Set DND during heavy compute
set_status("Rebuilding index", status="dnd", activity_type="watching")

# Reset when task finishes
reset_status()
```

---

## 📋 Activity Types & Status Reference

| Status | Activity Type | Example Display in Discord |
|---|---|---|
| `online` | `custom` | *Zero is online and ready.* |
| `dnd` | `custom` | *Crunching in #agent-chat...* |
| `online` | `watching` | *Watching: Docker containers* |
| `online` | `listening` | *Listening to: Plex Transcode Stream* |
| `online` | `playing` | *Playing: D&D 5e Combat Tracker* |
