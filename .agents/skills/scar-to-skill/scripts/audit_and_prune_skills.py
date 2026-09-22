#!/usr/bin/env python3
"""Audit installed skills and validate against Antigravity schema."""

import argparse
import os
import py_compile
import re
import sys
import time
from pathlib import Path

SKILLS_DIR = Path("/workspace/.agents/skills")

def parse_frontmatter(content: str):
    """Parse YAML frontmatter without external dependencies."""
    if not content.startswith("---"):
        return None, content
    
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    
    fm_raw = parts[1]
    body = parts[2].strip()
    
    metadata = {}
    
    # Extract name
    name_match = re.search(r"^name:\s*['\"]?([a-zA-Z0-9_\-\.]+)['\"]?", fm_raw, re.MULTILINE)
    if name_match:
        metadata["name"] = name_match.group(1).strip()
        
    # Extract description (handle single-line, folded >, or block |)
    desc_match = re.search(r"^description:\s*([>|]?)(.*)", fm_raw, re.MULTILINE)
    if desc_match:
        indicator = desc_match.group(1)
        inline_desc = desc_match.group(2).strip()
        
        # If block indicator or empty inline, read indented lines following
        start_pos = desc_match.end()
        following_lines = fm_raw[start_pos:].splitlines()
        desc_lines = []
        if inline_desc:
            desc_lines.append(inline_desc)
            
        for line in following_lines:
            if line.startswith("  ") or line.startswith("\t"):
                desc_lines.append(line.strip())
            elif not line.strip() and desc_lines:
                desc_lines.append("")
            else:
                break
                
        metadata["description"] = " ".join(desc_lines).strip()
        
    return metadata, body

def audit(verbose: bool = False):
    print("=" * 68)
    print("🧹 SKILL DIRECTORY AUDIT & PRUNING JANITOR (Antigravity Schema)")
    print("=" * 68)
    
    if not SKILLS_DIR.exists():
        print(f"❌ Skills directory not found at {SKILLS_DIR}")
        return 1
        
    skill_dirs = sorted([d for d in SKILLS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")])
    print(f"Auditing {len(skill_dirs)} installed skills in {SKILLS_DIR}...\n")
    
    executable_skills = []
    instruction_skills = []
    warnings = []
    broken_skills = []
    
    for s in skill_dirs:
        skill_md = s / "SKILL.md"
        scripts_dir = s / "scripts"
        
        if not skill_md.exists():
            broken_skills.append((s.name, "Missing SKILL.md manifest", "Remove orphaned directory"))
            continue
            
        try:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            broken_skills.append((s.name, f"Unreadable SKILL.md: {e}", "Inspect or delete"))
            continue
            
        mtime = skill_md.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        
        metadata, body = parse_frontmatter(content)
        
        if metadata is None:
            broken_skills.append((s.name, "Invalid YAML frontmatter (missing closing ---)", "Fix frontmatter"))
            continue
            
        skill_name = metadata.get("name")
        description = metadata.get("description")
        
        if not skill_name:
            broken_skills.append((s.name, "Missing 'name' in frontmatter", "Add name to frontmatter"))
            continue
            
        if not description:
            broken_skills.append((s.name, "Missing 'description' in frontmatter", "Add description to frontmatter"))
            continue
            
        if skill_name != s.name:
            warnings.append((s.name, f"Frontmatter name '{skill_name}' does not match directory '{s.name}'"))
            
        if len(body) < 80:
            warnings.append((s.name, f"SKILL.md body is very short ({len(body)} chars)"))
            
        # Script analysis & compilation check
        py_scripts = list(scripts_dir.glob("*.py")) if scripts_dir.exists() else []
        sh_scripts = list(scripts_dir.glob("*.sh")) if scripts_dir.exists() else []
        all_scripts = py_scripts + sh_scripts
        
        script_errors = []
        for py_file in py_scripts:
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as err:
                script_errors.append(f"{py_file.name}: Syntax error ({err.msg})")
            except Exception as err:
                script_errors.append(f"{py_file.name}: {err}")
                
        if script_errors:
            broken_skills.append((s.name, f"Script compilation errors: {', '.join(script_errors)}", "Fix syntax"))
            continue
            
        if all_scripts:
            executable_skills.append({
                "dir": s.name,
                "name": skill_name,
                "scripts": len(all_scripts),
                "body_len": len(body),
                "age_days": age_days,
            })
        else:
            instruction_skills.append({
                "dir": s.name,
                "name": skill_name,
                "body_len": len(body),
                "age_days": age_days,
            })
            
    print(f"🛠️  EXECUTABLE & TOOL SKILLS ({len(executable_skills)} installed):")
    for item in executable_skills:
        print(f"  • ✅ {item['dir']:<30} | {item['scripts']} script(s) | {item['body_len']:5d} bytes | Modified {item['age_days']:.1f}d ago")
        
    print(f"\n📖 METHODOLOGY & INSTRUCTION SKILLS ({len(instruction_skills)} installed):")
    for item in instruction_skills:
        print(f"  • 📘 {item['dir']:<30} | Pure instructions | {item['body_len']:5d} bytes | Modified {item['age_days']:.1f}d ago")
        
    if warnings:
        print(f"\n⚠️  SCHEMA & LINT WARNINGS ({len(warnings)} found):")
        for dir_name, issue in warnings:
            print(f"  • ⚠️  {dir_name}: {issue}")
            
    if broken_skills:
        print(f"\n🚨 DEFECTS & PRUNING RECOMMENDATIONS ({len(broken_skills)} found):")
        for dir_name, reason, action in broken_skills:
            print(f"  • 🗑️  Propose Fix/Remove: {dir_name}")
            print(f"      Reason: {reason}")
            print(f"      Action: {action}")
    else:
        print("\n✨ All 26 installed skills conform to Antigravity schema. Zero broken skills detected!")

    print("=" * 68)
    return 1 if broken_skills else 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit and validate installed Antigravity skills.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    sys.exit(audit(verbose=args.verbose))
