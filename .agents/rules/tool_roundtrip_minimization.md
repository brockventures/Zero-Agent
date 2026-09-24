---
trigger: always_on
glob: "*"
description: Enforces tool roundtrip minimization, broad-slice file reads, and deterministic batch execution.
---

# Tool Roundtrip Minimization & Batch Execution Invariant

## Hard Constraints:

1. **Full-File & Broad-Slice Reading (Ban 50-Line Slicing):**
   - `view_file` natively supports loading up to **800 lines** in a single call.
   - **NEVER** inspect files in small 30–60 line sequential slices (e.g. lines 1–50, then 51–100, then 101–150). Doing so multiplies latency by 10x–20x.
   - For any file under 800 lines, **omit `StartLine` and `EndLine`** entirely to load the whole file in a single tool call.
   - For larger files, inspect targeted logical sections in wide blocks (300–800 lines) rather than iterative crawling.

2. **Multi-File Diagnostic Batching (Scratch Scripts First):**
   - When diagnosing an issue across multiple files, **NEVER** run serial chains of `view_file` or single-line `cat`/`grep`/`ls` across 4+ files.
   - Bundle multi-file inspections, AST checks, regex searches, or log triages into a single Python scratch script in the conversation artifact directory (`<appDataDir>/brain/<conv_id>/scratch/` or `/workspace/scratch/`) and execute it in one shot via `run_command`.

3. **Compound Shell Commands:**
   - Single-line bash inspection commands (`cat`, `ls`, `grep`, `docker ps`, `ps aux`, `uptime`) must be bundled using `&&`, `;`, or compound one-liners rather than fired in individual serial tool calls.

4. **Multi-Edit Batching:**
   - Avoid long serial chains of `replace_file_content` on the same file.
   - If making extensive changes across a file, use `write_to_file` with `Overwrite: true` or a Python script to apply changes cleanly in a single pass.
   - Always run test suites (`pytest`, unit tests) after making edits before chaining further replacements.

5. **Targeted Pytest over Full-Suite Runs (Ban Unscoped Full-Suite Pytest):**
   - In active interactive PR turns, **NEVER** run unconstrained full repository test suites (e.g. bare `pytest` or `pytest tests/`) if the suite spans multiple unrelated modules or takes >10s.
   - **ALWAYS** scope test execution strictly to the relevant test file or function under test (e.g. `pytest tests/test_vessel_absorb.py` or `pytest -k "test_hull_index"`).
   - Rely on asynchronous CI (GitHub Actions) to execute the complete regression test matrix in the cloud once the PR is pushed.

6. **One-Shot Git & PR Pipeline Batching:**
   - When staging, committing, pushing, and opening pull requests, **NEVER** execute serial micro-tool calls (e.g. `git add` -> `git commit` -> `git push` -> `gh pr create`).
   - **ALWAYS** bundle the git lifecycle into a single compound shell command:
     `git add <files> && git commit -m "<msg>" && git push -u origin <branch> && gh pr create --title "<title>" --body "<body>"` (or a single Python script).

7. **Conversational & Deliberation Zero-Tool Fast-Path:**
   - When responding to pure conversational, opinion, strategy, design feedback, or consensus prompts that do not require live file edits or diagnostic verification, **ZERO** tool calls should be made.
   - Deliver the conversational response directly in a single inference pass without running exploratory bash checks (`git status`, directory listings, grep).

---

## 🛡️ Active PreToolUse Safety Gate (Deterministic Enforcement)
- **Hook Engine:** [`/workspace/.agents/hooks/batch_guard.py`](file:///workspace/.agents/hooks/batch_guard.py)
- **Configuration:** Synchronized in [`/workspace/.agents/hooks.json`](file:///workspace/.agents/hooks.json) and global `~/.gemini/config/hooks.json`.
- **Enforcement:** Intercepts `run_command`, `view_file`, `replace_file_content`, and `write_to_file`:
  - Blocks on 4th consecutive single-line shell inspection command.
  - Blocks on 3rd consecutive `view_file` call to the same file.
  - Blocks on 5th consecutive serial `view_file` call across different files.
  - Blocks on 4th consecutive `replace_file_content` call to the same file.
  - Blocks on unscoped full repository test execution (bare `pytest` / `pytest tests/` without target file).
  - Blocks on 3rd consecutive unbatched single-line git lifecycle command (`git add`, `git commit`, `git push`, `gh pr`).
