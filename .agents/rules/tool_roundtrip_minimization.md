---
trigger: always_on
glob: "*"
description: Enforces tool roundtrip minimization, broad-slice file reads, and deterministic batch execution.
---

# Tool Roundtrip Minimization & Batch Execution Invariant

## Hard Constraints:

1. **Full-File & Broad-Slice Reading (Ban 50-Line Slicing):**
   - `view_file` natively supports loading up to **800 lines** in a single call.
   - **NEVER** inspect files in small 30–60 line sequential slices. Omit `StartLine` and `EndLine` for files under 800 lines to load in one shot.
   - For larger files, inspect targeted logical sections in wide blocks (300–800 lines).

2. **Multi-File Diagnostic Batching (Scratch Scripts First):**
   - When diagnosing issues across multiple files, **NEVER** run serial chains of `view_file` or single-line `cat`/`grep`/`ls` across 4+ files.
   - Bundle multi-file inspections, AST checks, regex searches, or log triages into a single Python scratch script in `scratch/` and execute via `run_command`.

3. **Compound Shell Commands:**
   - Single-line bash inspection commands (`cat`, `ls`, `grep`, `docker ps`, `uptime`) must be bundled using `&&`, `;`, or compound one-liners rather than fired in individual serial calls.

4. **Multi-Edit Batching:**
   - Avoid long serial chains of `replace_file_content` on the same file. Use `write_to_file` with `Overwrite: true` or a script for extensive changes. Always run tests after edits before chaining further replacements.

5. **Targeted Pytest over Full-Suite Runs (Ban Unscoped Full-Suite Pytest):**
   - In active interactive PR turns, **NEVER** run unconstrained full repository test suites (bare `pytest` or `pytest tests/`).
   - **ALWAYS** scope test execution strictly to the relevant test file or function under test (`pytest tests/test_feature.py`). Rely on CI for full matrix regressions.

6. **One-Shot Git & PR Pipeline Batching:**
   - When staging, committing, pushing, and opening pull requests, **NEVER** execute serial micro-tool calls (`add` -> `commit` -> `push` -> `pr`).
   - **ALWAYS** bundle the git lifecycle into a single compound shell command:
     `git add <files> && git commit -m "<msg>" && git push -u origin <branch> && gh pr create --title "<title>" --body "<body>"`.

7. **Conversational & Deliberation Zero-Tool Fast-Path:**
   - When responding to pure conversational, opinion, strategy, design feedback, or consensus prompts that do not require live file edits or diagnostics, **ZERO** tool calls should be made. Deliver the response in a single inference pass.
8. **Compound Verification & Pre-Flight One-Liners:**
   - Instead of running linters, test suites, and git status across separate tool steps, bundle pre-flight checks into a single compound shell command:
     `ruff check <path> && pytest <test_path> && git status -s`
   - Never stage or push until compound verification passes cleanly.

9. **Bundled Failure Diagnostics:**
   - When a test, command, or script fails, **NEVER** conduct serial investigative probing (e.g. check error -> view file -> check logs -> view another file).
   - Bundle diagnostics into a single scratch script in `scratch/` or a compound bash command that extracts stderr, inspects the failing line range, and checks process or log state in one shot.

10. **Post-PR Compaction Discipline:**
   - Every completed PR marks the end of an isolated working memory cycle. Compact or recycle the session immediately upon PR merge/closure per `.agents/rules/autonomous_github_management.md` to prevent cumulative token ballooning on subsequent tool turns.

*Note: Enforced deterministically by `batch_guard.py`.*
