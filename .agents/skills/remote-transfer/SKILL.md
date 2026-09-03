---
name: remote-transfer
description: >-
  Use this skill whenever transferring multi-megabyte or gigabyte payloads, ROM libraries, datasets, or backups over SSH to remote hosts, handhelds (Steam Deck), or network storage.
  Enforces bandwidth throttling (--bwlimit) to prevent flash controller I/O queue exhaustion ('D' state), wraps remote sessions with systemd-inhibit to prevent idle suspend comas, and runs decoupled transfers to prevent bridge turn stalls.
---

# 🚀 Remote Transfer & Flash Memory Governor

The **Remote Transfer** skill prevents remote machine lockups, bridge turn stalls, and target device sleep comas during large data migrations over SSH.

---

## 🎯 When to Activate This Skill
* **Large File & Batch Transfers ($\ge 100\text{ MB}$):** Staging ROM libraries, game images, container layers, disk images, or datasets.
* **Handheld / Battery Target Devices:** Deploying files to Steam Deck, ROG Ally, laptops, or portable Linux nodes.
* **Removable & Flash Media:** Writing to MicroSD cards, eMMC, or external flash storage.
* **Multi-Turn Chat Contexts:** Whenever transfers take longer than 10 seconds, to avoid freezing Discord bridge queue workers.

---

## 🧠 Forensic Background & Scars

On **September 2, 2026**, an unthrottled 1.3 GB transfer to a Steam Deck triggered a triple failure mode:
1. **Flash Controller Starvation (`D` state):** Unthrottled GbE/Wi-Fi saturated the MicroSD bus. Linux dirty page writebacks locked all subsequent SSH sessions in uninterruptible disk sleep.
2. **Idle Suspend Coma:** The target device's 15-minute idle timer triggered deep suspend mid-write. `systemd-logind` froze during writeback, requiring a 10-second hard power press to recover.
3. **Bridge Turn Stall & Steering Abort:** The interactive turn hung waiting for socket timeout. Follow-up Discord messages triggered `SIGINT` mid-turn steering, aborting background tasks and compounding perceived system latency.

---

## 🛡️ The 4 Mandatory Pillars

### 1. Bandwidth Throttling (`--bwlimit`)
Never saturate remote flash storage at raw network line speed. MicroSD cards have limited write caches; dirty page flushing will wedge concurrent processes.
* **Default Governor:** `--bwlimit=30000` (30 MB/s).
* **Maximum Safe Ceiling on MicroSD:** `35000` (35 MB/s).

### 2. Idle Sleep Inhibition (`systemd-inhibit`)
Remote SSH sessions do not register as interactive seat inputs with power daemons (KDE PowerDevil, GNOME Mutter, systemd-logind). The host will enter sleep mid-transfer unless explicitly inhibited.
* Unprivileged users cannot inhibit system `sleep`, but **can** inhibit `idle`:
  ```bash
  rsync --rsync-path="systemd-inhibit --what=idle rsync" ...
  ```

### 3. Non-Blocking Decoupled Execution
Never execute raw multi-gigabyte transfers synchronously inside an active conversational turn.
* Sequential workers (`home_turn_queue`, `ext_turn_queue`) process one turn at a time.
* Decouple the transfer into a detached daemon using `remote_transfer.py start` and report progress asynchronously.

### 4. Socket Hardening & Keepalives
Silent Wi-Fi drops and target sleep events do not emit TCP `FIN`/`RST` packets. Prevent socket hangs by enforcing strict keepalives:
```bash
-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o BatchMode=yes
```

---

## 🛠️ CLI Quick Reference

The skill includes a production runner at `/workspace/tools/remote_transfer.py` (symlinked in `scripts/transfer.py`):

### 1. Launch Decoupled Background Transfer
```bash
# Push from Source Host to Steam Deck (throttled + idle-inhibited)
python3 /workspace/tools/remote_transfer.py start \
  --source-host "<user>@<source_host> -p <port>" \
  --src "/storage/games/roms/gamecube/F-Zero GX (USA).rvz" \
  --dest "deck@<target_host>:/run/media/deck/UUID/Emulation/roms/gamecube/" \
  --bwlimit 30000 \
  --desc "F-Zero GX ROM transfer"
```

### 2. Check Transfer Status
```bash
# Query specific transfer or list all active/recent
python3 /workspace/tools/remote_transfer.py status --id <transfer_id>
python3 /workspace/tools/remote_transfer.py list
```

### 3. Cancel Running Transfer
```bash
python3 /workspace/tools/remote_transfer.py cancel --id <transfer_id>
```

---

## 📋 Pre-Flight & Post-Flight Checklist

Before launching any large remote transfer:
1. **Target Mount Point:** Verify the destination partition is actively mounted (e.g. Steam Deck MicroSD automounts in Desktop Mode under `/run/media/deck/...`).
2. **Disk Capacity:** Check free space (`df -h <mount>`) to ensure at least 15% headroom.
3. **Power Profile:** Verify target is on AC power or has $>50\%$ battery.
4. **Post-Sync Integrity:** After completion, confirm destination file size matches source before notifying the user or triggering emulators/ROM scrapers.
