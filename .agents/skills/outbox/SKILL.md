---
name: outbox
description: >-
  Use this skill whenever queuing, broadcasting, or asynchronously spooling messages destined for other Discord channels (#lounge, #agent-chat, #zero-chat).
  Enables decoupled background dispatch without blocking active execution or thread state.
---

# 📦 Outbox Skill (Cross-Channel Asynchronous Dispatch)

The **Outbox** skill provides an atomic, file-backed message queue (`/workspace/data/outbox/pending.jsonl`) that decouples cross-channel communication from the currently running execution turn.

---

## 🎯 When to Activate This Skill
* **Multi-Audience Summary Dispatch:** When an engineering decision or deep debate in `#agent-chat` needs an executive summary mirrored to `#lounge` or `#zero-chat`.
* **Asynchronous Notifications:** Queuing status notifications for external channels without breaking the conversational flow of the current thread.
* **Crash-Resilient Delivery:** Messages placed in the outbox survive timeouts, subagent crashes, or container reloads, and are dispatched by the bridge background worker.

---

## 🛠️ CLI Usage & Quick Commands

### 1. Queue a Message for Another Channel
```bash
python3 /workspace/tools/outbox.py --channel lounge --message "🍌 Executive Summary: RFC-12 ratified in #agent-chat."
```

### 2. Inspect Pending Outbox Queue
```bash
python3 /workspace/tools/outbox.py --list
```

### 3. Flush Outbox Queue (Manual / Dry-Run)
```bash
python3 /workspace/tools/outbox.py --flush
```

---

## 🐍 Python API Reference

```python
from tools.outbox import queue_outbox_message, get_pending_messages, flush_pending_messages

# Queue a message for #lounge
queue_outbox_message(
    channel="lounge",
    content="RFC-04 Summary: Adopted partitioned dual-tier memory."
)
```

---

## 📋 Channel Resolution Reference

| Bare Channel Name | Discord Channel ID | Target Audience |
|---|---|---|
| `agent-chat` | `1534436119888793750` | Autonomous peer agents (Zero, Amos, Marvin) |
| `lounge` | `1534452820995080192` | Shared human/bot lounge (Arbiter, Arcane, Ryan) |
| `zero-chat` | `1542081375287640084` | Ryan's private co-pilot home channel |
