---
name: scar-to-skill
description: >-
  Use this skill after solving complex debugging incidents or operational breakdowns to evaluate whether to codify the solution into a permanent executable skill.
  Enforces strict anti-slop filters (recurrence >= 3, deterministic automation, high blast radius) and audits existing skills for pruning.
---

# 🧠 Conservative Scar-to-Skill Transformation & Skill Janitor

The **Scar-to-Skill** engine transforms hard-won forensic debugging breakthroughs into reusable, executable skills—while enforcing **strict anti-slop filters** so our skills directory stays lean, high-signal, and free of one-off junk.

---

## 🛡️ Anti-Slop Thresholds (Conservative Admission Policy)

A debugging incident or architectural scar is **ONLY** eligible to become a skill if it passes ALL 4 criteria:

1. **High Recurrence Probability ($\ge 3$ expected encounters):** The problem is a systemic pattern (e.g. secret leakage, crontab collisions, schema drift), NOT a one-off typo or temporary upstream glitch.
2. **Automated Verification / Remediation Possible:** The skill contains deterministic, runnable scripts (), not just passive text. *(Passive notes belong in , not )*.
3. **High Blast Radius / Cost of Failure:** An outage or regression caused by this failure mode would waste significant time or compromise safety.
4. **Generalizable Across Projects:** The solution is modular and decoupled from hardcoded temporary variables.

---

## 🧹 Periodic Skill Janitor & Pruning Proposal

To prevent skill rot, the Janitor audits all installed skills and tracks usage metrics.



---

## 📋 Transformation Workflow


