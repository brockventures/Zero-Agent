---
trigger: always_on
glob: "*"
description: Hard constraint against server-wide or top-level unconstrained grepping and recursive searches.
---

# Safe Search & Scoped Grep Policy (Crash Prevention)

## Hard Constraints:
1. **NEVER Execute Root or Server-Wide Searches:**
   - NEVER call `grep_search` or `find_by_name` on root `/`, `/workspace`, `/workspace/data`, `/workspace/memory`, `/root`, `/docker`, or any top-level directory.
   - Doing so traverses multi-gigabyte data exports (e.g., `/workspace/data` >20GB of Google Takeout archives and media), causing catastrophic memory exhaustion, tool timeouts, and container hangs/crashes.

2. **ALWAYS Scope Searches to Specific Subdirectories or Files:**
   - Always target the exact directory needed:
     - Tools/Scripts: `SearchPath: "/workspace/tools"`
     - Configuration: `SearchPath: "/workspace/config"`
     - Documentation: `SearchPath: "/workspace/docs"`
     - Public Memory: `SearchPath: "/workspace/memory/public"`
     - Specific file: `SearchPath: "/workspace/tools/bridge.py"`

3. **ALWAYS Provide `Includes` Filters with `grep_search`:**
   - When calling `grep_search`, always supply the `Includes` parameter to restrict file matching (e.g., `Includes: ["*.py"]`, `Includes: ["*.json"]`).

4. **NEVER Recursively Grep `/workspace/data/` or `/archives/`:**
   - Raw Takeout archives and bulk binary exports are isolated under `/archives/`.
   - If a specific data file must be inspected, access it directly using `view_file` or a scoped python script, never recursive grep.

5. **Bound All Shell Search Commands (`run_command`):**
   - Never run unbounded `grep -r`, `rg`, or `find /` from shell.
   - Always specify explicit subdirectories, use `-maxdepth <N>`, and bound outputs (e.g., `head -n 20`).

6. **Prohibition of Binary Scanning & Reverse-Engineering:**
   - NEVER run `grep`, `strings`, `hexdump`, `objdump`, or disassembly against compiled system binaries (e.g. `/usr/local/bin/agy`, `/usr/bin/*`) or multi-megabyte binary blobs.
   - NEVER write python scratch scripts to read system binaries into memory for regex or symbol scraping during conversational turns. Check documentation, CLI `--help` flags, or system APIs instead.

---

## 🛡️ Active PreToolUse Safety Gate (Deterministic Enforcement)
- **Hook Engine:** [`/workspace/.agents/hooks/search_guard.py`](file:///workspace/.agents/hooks/search_guard.py)
- **Configuration:** Synchronized in [`/workspace/.agents/hooks.json`](file:///workspace/.agents/hooks.json) and global `~/.gemini/config/hooks.json`.
- **Enforcement:** Every `grep_search`, `find_by_name`, and `run_command` is intercepted before execution. Calls targeting root paths or unbounded shell search patterns are physically denied before hitting the disk.

