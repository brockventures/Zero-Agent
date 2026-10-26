---
name: zero-medic
description: Use this skill whenever Zero Prime (discord-antigravity-agent on Host 2) or Ivy (discord-ivy-agent) is unresponsive, wedged on a stuck turn, throwing bridge errors, out of sync with git, or needs remote diagnostics/recovery from the laptop standby replica.
---

# Zero Prime Medic & Standby Recovery Skill (`zero-medic`)

When operating from Ryan's Windows laptop (`c:\Users\ryanb\zero-agent`), you act as **Zero Medic** — the out-of-band first-responder capable of inspecting, unwedging, patching, and restarting **Zero Prime** (`discord-antigravity-agent` on Host 2 `.84`) and **Host 1** (`.82`).

## Core Safety Rules
1. **NEVER Run Unbounded `docker logs` on Synology Hosts:** Synology DSM uses a custom SQLite log driver (`log.db`) that hangs indefinitely when supervisor processes hold stdout open. Always inspect bind-mounted log files on disk (`$NAS_DOCKER_ROOT/discord-agy-agent/logs/bridge.log`) via `py tools/prime_medic.py logs` or `status`.
2. **Use `py` on Windows:** Always invoke Python scripts locally using `py` (e.g., `py tools/prime_medic.py status`).
3. **Preserve Air-Gapped Commit Safety:** Before pushing any emergency fix to `origin/main`, run `py tools/validate_commit_safety.py .` to ensure zero hardcoded LAN IPs or tokens are committed.

---

## Primary Medic Workflows

### 1. Full Telemetry & Triage (`status`)
Run a single-shot SSH probe against Host 2 and Host 1 to inspect container health, internal `ps -ef` processes, `in_flight_turn.json`, turn queues, git commit alignment, and the tail of `bridge.log`:
```powershell
py tools/prime_medic.py status
```

### 2. Tail Live Disk Logs (`logs`)
Read the bind-mounted bridge log directly from `$NAS_DOCKER_ROOT/discord-agy-agent/logs/bridge.log`:
```powershell
py tools/prime_medic.py logs -n 100
```

### 3. Surgically Unwedge a Stuck Turn (`clear-stuck`)
If Zero Prime is alive in Discord but stuck on a turn (`in_flight_turn.json` is populated and a zombie `agy` child process is hung):
```powershell
py tools/prime_medic.py clear-stuck
```
This terminates hung `agy` processes inside `discord-antigravity-agent` and backs up/clears `data/in_flight_turn.json` without restarting the container.

### 4. Restart Zero Prime or Ivy (`restart`)
If the Discord gateway connection or `bridge.py` event loop is dead:
```powershell
# Standard container restart
py tools/prime_medic.py restart

# Full compose recreate
py tools/prime_medic.py restart --compose

# Restart Ivy (discord-ivy-agent)
py tools/prime_medic.py restart --ivy
```

### 5. Deploy a Hotfix from Laptop to Zero Prime
When fixing a bug in `tools/` or `.agents/`:
1. Edit and test locally on the laptop (`py -m pytest tools/test_<module>.py`).
2. Run `py tools/validate_commit_safety.py` to verify no secrets or private IPs leaked.
3. Commit and push to `origin/main`.
4. Pull the fix onto Zero Prime and restart:
```powershell
py tools/prime_medic.py sync-git
py tools/prime_medic.py restart
```

### 6. Refresh Local Standby Memory & State (`sync_standby.py`)
To pull the latest `origin/main` code plus an air-gapped snapshot of Zero Prime's `memory/` and `data/` files over SSH:
```powershell
py tools/sync_standby.py --force
```
