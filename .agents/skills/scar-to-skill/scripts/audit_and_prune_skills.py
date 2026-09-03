#!/usr/bin/env python3
"""Audit installed skills and generate interactive pruning suggestions for the user."""
import os, time
from pathlib import Path

SKILLS_DIR = Path("/workspace/.agents/skills")

def audit():
    print("=" * 60)
    print("🧹 SKILL DIRECTORY AUDIT & PRUNING JANITOR")
    print("=" * 60)
    
    if not SKILLS_DIR.exists():
        print("No skills directory found at /workspace/.agents/skills.")
        return
        
    skills = [d for d in SKILLS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    print(f"Auditing {len(skills)} installed skills in {SKILLS_DIR}...")
    
    stale_candidates = []
    healthy_skills = []
    
    for s in skills:
        skill_md = s / "SKILL.md"
        scripts_dir = s / "scripts"
        
        has_manifest = skill_md.exists()
        scripts = list(scripts_dir.glob("*.py")) if scripts_dir.exists() else []
        
        # Check last modification
        mtime = skill_md.stat().st_mtime if has_manifest else s.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        
        if not has_manifest or len(scripts) == 0:
            stale_candidates.append((s.name, "Missing manifest or executable scripts", age_days))
        else:
            healthy_skills.append((s.name, len(scripts), age_days))
            
    print("\n📦 ACTIVE & HEALTHY SKILLS:")
    for name, num_scripts, age in healthy_skills:
        print(f"  • ✅ {name:<20} ({num_scripts} scripts | Modified {age:.1f}d ago)")
        
    if stale_candidates:
        print("\n⚠️ PRUNING RECOMMENDATIONS (Stale / Low Utility):")
        for name, reason, age in stale_candidates:
            print(f"  • 🗑️ Propose Remove: {name} (Reason: {reason})")
    else:
        print("\n✨ All installed skills meet quality and utility thresholds. Zero slop detected!")
    print("=" * 60)

if __name__ == "__main__":
    audit()
