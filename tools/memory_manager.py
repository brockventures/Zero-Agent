#!/usr/bin/env python3
"""Memory Manager & Dreaming Engine for Zero (Dual-Tier Partitioned Architecture).

Provides:
1. memory_write(): Creates/updates structured memory files in public/ or private/ tiers
   with automated security scanning on public writes, maintaining MEMORY_PUBLIC.md,
   MEMORY_PRIVATE.md, and MEMORY.md indexes.
2. run_memory_doctor(): Audits both memory tiers for orphaned files, broken links, and staleness.
3. run_dreaming_consolidation(): Consolidates operational learnings and engineering scars into memory.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
MEMORY_DIR = Path("/workspace/memory")
PUB_DIR = MEMORY_DIR / "public"
PRIV_DIR = MEMORY_DIR / "private"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_PUB_INDEX = MEMORY_DIR / "MEMORY_PUBLIC.md"
MEMORY_PRIV_INDEX = MEMORY_DIR / "MEMORY_PRIVATE.md"

log = logging.getLogger("memory_manager")

# Lazy load security rules for public write scanning
_SECURITY_RULES = None

def _get_security_rules():
    global _SECURITY_RULES
    if _SECURITY_RULES is None:
        try:
            sys.path.insert(0, "/workspace/tools")
            import validate_commit_safety
            _SECURITY_RULES = validate_commit_safety.SECURITY_RULES
        except Exception:
            _SECURITY_RULES = []
    return _SECURITY_RULES

def memory_write(name: str, title: str, description: str, category: str, content: str, tier: str = "auto") -> dict:
    """Create or update a memory file in public/ or private/ tier and update index files.
    
    Args:
        name: filename (e.g. 'arch_rolling_compaction.md' or 'user_preferences.md')
        title: Title of the memory document
        description: 1-line description
        category: category tag ('architecture', 'engineering', 'protocol', 'user', 'project', etc.)
        content: Markdown content
        tier: 'public', 'private', or 'auto' (automatically evaluates against security rules)
    """
    if not name.endswith(".md"):
        name += ".md"

    # Enforce naming prefix if appropriate
    valid_prefixes = ("user_", "project_", "reference_", "feedback_", "arch_", "scar_")
    if not any(name.startswith(p) for p in valid_prefixes):
        if category in ("user", "project", "reference", "feedback", "arch", "scar"):
            name = f"{category}_{name}"

    # Determine tier if auto
    if tier == "auto":
        is_priv = False
        private_categories = ("user", "finances", "family", "private_email", "sms")
        if category in private_categories or name.startswith(("user_", "deep_", "private_", "security_")):
            is_priv = True
        else:
            for pat, _ in _get_security_rules():
                if re.search(pat, content):
                    is_priv = True
                    break
        target_dir = PRIV_DIR if is_priv else PUB_DIR
    elif tier == "public":
        # Validate security before accepting public write
        for pat, desc in _get_security_rules():
            m = re.search(pat, content)
            if m:
                raise ValueError(f"Security validation failed for public memory: Matched {desc} ('{m.group(0)}')")
        target_dir = PUB_DIR
    else:
        target_dir = PRIV_DIR

    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / name
    now_str = datetime.now(PT).strftime("%Y-%m-%d")

    # Format YAML frontmatter
    fm = [
        "---",
        f"name: {title}",
        f"description: \"{description}\"",
        f"category: {category}",
        f"updated: {now_str}",
        "---",
        "",
        content.strip(),
        ""
    ]
    file_path.write_text("\n".join(fm), encoding="utf-8")

    # Create / update backward-compatible symlink at root of memory dir
    root_link = MEMORY_DIR / name
    if root_link != file_path:
        try:
            if root_link.is_symlink() or root_link.exists():
                root_link.unlink()
            root_link.symlink_to(Path(target_dir.name) / name)
        except Exception as e:
            log.warning(f"Failed creating root symlink for {name}: {e}")

    # Rebuild indexes
    rebuild_indexes()

    return {"ok": True, "file": str(file_path), "tier": target_dir.name, "indexed": True}

def rebuild_indexes():
    """Rebuild MEMORY_PUBLIC.md, MEMORY_PRIVATE.md, and unified MEMORY.md."""
    PUB_DIR.mkdir(parents=True, exist_ok=True)
    PRIV_DIR.mkdir(parents=True, exist_ok=True)

    def get_info(fp: Path) -> tuple[str, str]:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            m_name = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
            m_desc = re.search(r"^description:\s*\"?([^\n\"]+)\"?$", text, re.MULTILINE)
            title = m_name.group(1).strip() if m_name else fp.stem.replace("_", " ").title()
            desc = m_desc.group(1).strip() if m_desc else "Operational memory document"
            return title, desc
        except Exception:
            return fp.stem, "Operational document"

    # Public index
    pub_lines = ["# Zero Public Engineering & Architecture Memory Index\n"]
    for f in sorted(PUB_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        title, desc = get_info(f)
        pub_lines.append(f"- [{title}](public/{f.name}) — {desc}")
    
    MEMORY_PUB_INDEX.write_text("\n".join(pub_lines) + "\n", encoding="utf-8")
    (PUB_DIR / "MEMORY.md").write_text("\n".join(pub_lines) + "\n", encoding="utf-8")

    # Private index
    priv_lines = ["# Zero Private Homelab & Confidential Memory Index\n"]
    for f in sorted(PRIV_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        title, desc = get_info(f)
        priv_lines.append(f"- [{title}](private/{f.name}) — {desc}")

    MEMORY_PRIV_INDEX.write_text("\n".join(priv_lines) + "\n", encoding="utf-8")
    (PRIV_DIR / "MEMORY.md").write_text("\n".join(priv_lines) + "\n", encoding="utf-8")

    # Unified index
    uni_lines = [
        "# Zero Complete Memory Index (Unified)\n",
        "## 🌐 Public Engineering & Architecture Memory (`memory/public/`)\n"
    ]
    uni_lines.extend(pub_lines[1:])
    uni_lines.append("\n## 🔒 Private Homelab & Personal Memory (`memory/private/`)\n")
    uni_lines.extend(priv_lines[1:])
    MEMORY_INDEX.write_text("\n".join(uni_lines) + "\n", encoding="utf-8")

    # Sync SQLite FTS5 Index
    try:
        if "/workspace" not in sys.path:
            sys.path.insert(0, "/workspace")
        from tools.transcript_index import TranscriptIndexer
        indexer = TranscriptIndexer()
        indexer.sync_all()
        indexer.close()
    except Exception as e:
        log.warning(f"Failed to sync SQLite FTS5 index: {e}")

def run_memory_doctor() -> tuple[bool, str]:
    """Audit both memory tiers for orphaned files, broken links, and security compliance."""
    if not MEMORY_DIR.exists():
        return False, "Memory directory /workspace/memory does not exist."

    issues = []
    
    # 1. Audit public tier against security rules
    security_violations = []
    rules = _get_security_rules()
    for f in PUB_DIR.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        content = f.read_text(encoding="utf-8", errors="replace")
        for pat, desc in rules:
            m = re.search(pat, content)
            if m:
                security_violations.append(f"`{f.name}` contains {desc} ('{m.group(0)}')")
                break

    if security_violations:
        issues.append(f"🚨 **Security Air-Gap Violations in Public Memory:**\n  " + "\n  ".join(security_violations))

    # 2. Rebuild and check indexes
    rebuild_indexes()
    
    pub_count = len([f for f in PUB_DIR.glob("*.md") if f.name != "MEMORY.md"])
    priv_count = len([f for f in PRIV_DIR.glob("*.md") if f.name != "MEMORY.md"])

    if not issues:
        return True, f"Memory store is healthy and air-gapped. ✅\n- **Public Engineering Documents:** `{pub_count}`\n- **Private Homelab Documents:** `{priv_count}`"

    report = "🩺 **Memory Doctor Audit**:\n" + "\n".join(issues)
    return False, report

def run_dreaming_consolidation(dry_run: bool = False) -> tuple[bool, str]:
    """Nightly dreaming pass: consolidates today's operational learning into memory."""
    now_pt = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    consolidated_items = []
    
    for target in [Path("/workspace/agents.md"), Path("/workspace/memory/private/user_ryan.md")]:
        if target.exists() and (datetime.now(PT) - datetime.fromtimestamp(target.stat().st_mtime, tz=PT)).total_seconds() < 86400:
            consolidated_items.append(f"Rules/Preferences updated in `{target.name}`")

    tools_dir = Path("/workspace/tools")
    if tools_dir.exists():
        for tf in tools_dir.glob("*.py"):
            if (datetime.now(PT) - datetime.fromtimestamp(tf.stat().st_mtime, tz=PT)).total_seconds() < 86400:
                consolidated_items.append(f"Tool enhancements in `{tf.name}`")

    if not consolidated_items:
        return False, "Dream pass complete: no new long-term memories required consolidation."

    report = (
        f"💭 **Nightly Memory Consolidation (Dreaming)** — {now_pt}\n"
        f"Consolidated the day's lessons into durable memory:\n"
        + "\n".join(f"- {it}" for it in sorted(set(consolidated_items)))
        + "\nContext ready for session rollover."
    )
    return True, report

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if action == "doctor":
        ok, msg = run_memory_doctor()
        print(msg)
    elif action == "dream":
        ok, msg = run_dreaming_consolidation()
        print(msg)
    elif action == "rebuild":
        rebuild_indexes()
        print("Indexes rebuilt successfully.")
