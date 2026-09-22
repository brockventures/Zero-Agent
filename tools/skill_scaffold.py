#!/usr/bin/env python3
"""On-demand developer scaffolding tool for handcrafted Antigravity skills."""

import argparse
import os
import re
import stat
import sys
from pathlib import Path

SKILLS_DIR = Path("/workspace/.agents/skills")

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

## Overview
{description}

## When to Use
- Trigger 1: Describe the primary scenario or operational trigger.
- Trigger 2: Describe secondary conditions or alternative triggers.

## Invariants & Guardrails
- **Invariant 1:** Critical invariant or safety boundary.
- **Invariant 2:** Logging, error handling, or fallback behavior.

## Execution Workflow
1. **Step 1:** Initial discovery or validation.
2. **Step 2:** Core execution step.
3. **Step 3:** State verification and evidence capture.

## Verification & Receipts
- Detail deterministic commands or steps to verify success before concluding.
"""

SCRIPT_TEMPLATE = """#!/usr/bin/env python3
\"\"\"Executable helper for {name} skill.\"\"\"

import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="Helper for {name}")
    parser.add_argument("--dry-run", action="store_true", help="Run without mutating state")
    args = parser.parse_args()
    
    print(f"Executing {name} helper (dry_run={{args.dry_run}})...")
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""

def scaffold_skill(name: str, description: str, with_script: bool = False, force: bool = False) -> int:
    # Validate name (kebab-case)
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        print(f"❌ Error: Skill name '{name}' must be lowercase kebab-case (e.g., 'crontab-verify', 'shopping-advisor').")
        return 1

    target_dir = SKILLS_DIR / name
    if target_dir.exists() and not force:
        print(f"❌ Error: Target skill directory already exists: {target_dir}")
        print("   Use --force to overwrite existing files.")
        return 1

    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate human title
    title = name.replace("-", " ").title()
    
    # Write SKILL.md
    skill_md = target_dir / "SKILL.md"
    skill_content = SKILL_TEMPLATE.format(name=name, description=description.strip(), title=title)
    skill_md.write_text(skill_content, encoding="utf-8")
    print(f"✅ Created manifest: {skill_md}")

    # Optional script
    if with_script:
        scripts_dir = target_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_file = scripts_dir / f"{name.replace('-', '_')}.py"
        script_content = SCRIPT_TEMPLATE.format(name=name)
        script_file.write_text(script_content, encoding="utf-8")
        
        # chmod +x
        st = script_file.stat()
        script_file.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"✅ Created executable script: {script_file}")

    print(f"\n🎉 Skill '{name}' successfully scaffolded at {target_dir}")
    print("   Next step: Edit SKILL.md to tailor workflows and instructions.")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scaffold a new handcrafted Antigravity skill.")
    parser.add_argument("name", help="Kebab-case skill name (e.g. 'nas-backup-auditor')")
    parser.add_argument("--desc", required=True, help="Skill description for frontmatter discovery")
    parser.add_argument("--with-script", action="store_true", help="Include scripts/ directory with executable template")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files in target directory")
    
    args = parser.parse_args()
    sys.exit(scaffold_skill(args.name, args.desc, with_script=args.with_script, force=args.force))
