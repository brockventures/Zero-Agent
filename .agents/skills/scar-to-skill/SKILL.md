---
name: scar-to-skill
description: >-
  Framework for evaluating operational scars, enforcing the 3-Strike Rule against skill slop, scaffolding handcrafted skills, and auditing installed skills via the Janitor.
---

# 🧠 Conservative Scar-to-Skill Transformation & Skill Janitor

The **Scar-to-Skill** framework protects our agent context window and tool selection surface by enforcing **strict anti-slop filters** and the **3-Strike Rule**. Every installed skill injects token overhead into system prompt context on every single turn; therefore, skills are reserved strictly for high-recurrence, high-leverage workflows and are authored intentionally by hand.

---

## 🛡️ The 3-Strike Rule for Operational Scars

When debugging incidents, outages, or architectural breakdowns occur, do NOT immediately create a skill. Follow the progressive 3-strike discipline:

1. **Strike 1 (First Encounter):** 
   - Perform silent forensic root-cause analysis.
   - Document the autopsy and recovery receipts in durable memory (`/workspace/memory/public/` or `/workspace/memory/private/`).
   - *Cost:* 0 active tokens. Indexed immediately in SQLite FTS5 BM25 search.
2. **Strike 2 (Recurrence / Pattern Confirmation):**
   - Update the existing memory document with cross-references, edge cases, and failure mode variations.
   - If deterministic verification commands or fix scripts exist, package them as standard workspace tools in `/workspace/tools/`.
3. **Strike 3 (Systemic Operational Workflow):**
   - If the exact same multi-step workflow recurs a 3rd time *and* it requires specialized prompt instructions, execution protocols, or dedicated sub-scripts, evaluate for promotion to `.agents/skills/`.

---

## ⚖️ Admission Rubric: Memory vs. Tool vs. Skill

| Destination | Criteria & Best Use | Token Cost |
|---|---|---|
| **Durable Memory** (`memory/`) | Root causes, architectural decisions (ADRs), post-mortems, hardware quirks, API idiosyncrasies. | **0 tokens** (retrieved via search on demand) |
| **Workspace Tool** (`tools/`) | Deterministic scripts, CLI utilities, single-purpose automation, data processors. | **0 tokens** (executed via shell) |
| **Agent Skill** (`.agents/skills/`) | Multi-step interactive workflows, specialized reasoning protocols, complex domain tasks with dedicated guidance. | **Active discovery tokens on every turn** |

To programmatically test a candidate against the admission rubric:
```bash
python3 /workspace/.agents/skills/scar-to-skill/scripts/evaluate_scar_candidate.py \
  --title "NAS Snapshot Lockout" \
  --recurrence 3 \
  --has_automation \
  --blast_radius High
```

---

## 🛠️ Handcrafted Authoring Workflow

Autonomous skill-generation loops are strictly banned. All skills are handcrafted during active pairing:

1. **Scaffold the Skill:**
   Use the developer scaffolding utility to initialize a clean, schema-compliant directory:
   ```bash
   # Instruction-only skill
   python3 /workspace/tools/skill_scaffold.py <skill-name> --desc "Clear trigger description"

   # Executable tool skill with script template
   python3 /workspace/tools/skill_scaffold.py <skill-name> --desc "Clear trigger description" --with-script
   ```
2. **Author Workflows:**
   Define specific operational triggers, execution workflows, invariants, and verification steps in `SKILL.md`.
3. **Verify Schema & Health:**
   Run the Skill Janitor to verify compliance across the workspace.

---

## 🧹 Skill Janitor (`scripts/audit_and_prune_skills.py`)

The Skill Janitor audits all installed skills in `.agents/skills/` against Antigravity requirements:
- **Validates YAML Frontmatter:** Ensures `name` matches directory and `description` is descriptive.
- **First-Class Instruction Skills:** Fully recognizes methodology/instruction-only skills (`systematic-debugging`, `consultation-panel`, etc.) alongside executable tool skills.
- **Python Syntax Compilation:** Verifies all `.py` files inside `scripts/` compile without syntax errors.
- **Zero False Pruning:** Never recommends removing healthy skills. Flags only unparseable frontmatter, broken scripts, or empty stubs.

To run the Janitor audit on demand:
```bash
python3 /workspace/.agents/skills/scar-to-skill/scripts/audit_and_prune_skills.py
```
