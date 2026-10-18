---
trigger: always_on
description: "Strictly prohibits hardcoding private LAN IPs, custom SSH ports, NAS hostnames, Synology volume paths, or personal PII in tracked code, skills, or tests."
---

# Zero Hardcoded Topology & PII Invariant

Because `/workspace` backs the public `brockventures/Zero-Agent` repository, all tracked tools, skills, rules, and tests must remain 100% free of hardcoded private network topology and personal identifiers.

## Hard Constraints

1. **Dynamic Topology Resolution (`tools/nas_config.py`):**
   - **NEVER** hardcode private RFC 1918 subnet IPs (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`), custom SSH ports, or NAS hostnames in Python scripts.
   - **ALWAYS** import `/workspace/tools/nas_config.py` (`nas_config.HOST_1_IP`, `nas_config.HOST_2_IP`, `nas_config.SSH_PORT`, `nas_config.SSH_USER`, `nas_config.DOCKER_ROOT`, `nas_config.run_ssh()`) or resolve values from environment variables / `/secrets/env.json` with neutral fallback defaults (`127.0.0.1`, port `22`).

2. **Documentation, Skills & Rules Placeholders:**
   - In `.agents/skills/**/SKILL.md`, `.agents/rules/*.md`, and `docs/`, **NEVER** write literal LAN IPs, custom ports, or Synology `/volume1/...` paths.
   - Use canonical placeholders (`<HOST_1_IP>`, `<HOST_2_IP>`, `<GATEWAY_IP>`, `<DEVICE_LAN_IP>`, `$NAS_DOCKER_ROOT`) or shorthand suffixes (`.82`, `.84`).

3. **Test Fixtures (`RFC 5737` Documentation Subnets Only):**
   - In unit tests (`tools/test_*.py`, `tests/test_*.py`), **NEVER** use real `192.168.1.x` or `172.18.0.x` container IPs or real NAS hostnames in mock payloads.
   - Use RFC 5737 documentation addresses (`198.51.100.x` or `203.0.113.x`) and generic hostnames (`NasHost01`, `AppHost02`).

4. **Zero-Knowledge Secret & PII Scanners (`/secrets/pii_rules.json`):**
   - **NEVER** hardcode literal tokens, Wi-Fi passwords, street addresses, phone numbers, or personal emails inside pre-commit hooks or scanner scripts (`validate_commit_safety.py`, `scan_secrets_and_pii.py`, `promote_memory.py`).
   - Store all literal private regex patterns exclusively in `/secrets/pii_rules.json` (mounted outside the Git worktree) and load them dynamically at runtime.
