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
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
TOOLS_DIR = Path(__file__).resolve().parent
MEMORY_DIR = Path("/workspace/memory")
PUB_DIR = MEMORY_DIR / "public"
PRIV_DIR = MEMORY_DIR / "private"
VAULT_DIR = MEMORY_DIR / "vault"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_PUB_INDEX = MEMORY_DIR / "MEMORY_PUBLIC.md"
MEMORY_PRIV_INDEX = MEMORY_DIR / "MEMORY_PRIVATE.md"
MEMORY_VAULT_INDEX = VAULT_DIR / "VAULT.md"
CHANNEL_HISTORY_PATH = Path("/workspace/data/channel_history.json")
CC_DECISIONS_FILE = MEMORY_DIR / "crab_cavern" / "decisions.md"
USER_RYAN_FILE = PRIV_DIR / "user_ryan.md"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

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
    
    Dual-Write / Mirroring Architecture:
    - Purely personal/confidential domains ('user', 'finances', 'family', 'social', 'preference', 'credentials')
      write exclusively to memory/private/.
    - Engineering/technical domains ('architecture', 'engineering', 'protocol', 'pattern', 'tool', 'scar', 'reference')
      automatically execute a Dual-Write when tier='auto':
      1. Write the full-fidelity, unredacted original to memory/private/<name>.
      2. Automatically scrub homelab IPs, NAS volume paths, and personal PII via promote_memory.scrub_content().
      3. Verify the scrubbed content against validate_commit_safety.validate_scrubbed_safety().
      4. If clean, write the sanitized mirror to memory/public/<name> so Public Zero has immediate access.
    
    Args:
        name: filename (e.g. 'arch_rolling_compaction.md' or 'user_preferences.md')
        title: Title of the memory document
        description: 1-line description
        category: category tag ('architecture', 'engineering', 'protocol', 'user', 'project', etc.)
        content: Markdown content
        tier: 'public', 'private', or 'auto' (automatically dual-writes engineering knowledge)
    """
    if not name.endswith(".md"):
        name += ".md"

    # Enforce naming prefix if appropriate
    valid_prefixes = ("user_", "project_", "reference_", "feedback_", "arch_", "scar_", "pattern_", "tool_")
    if not any(name.startswith(p) for p in valid_prefixes):
        if category in ("user", "project", "reference", "feedback", "arch", "scar", "pattern", "tool"):
            name = f"{category}_{name}"

    now_str = datetime.now(PT).strftime("%Y-%m-%d")

    def format_document(t_title: str, t_desc: str, t_cat: str, t_body: str, source_tag: str | None = None) -> str:
        fm = [
            "---",
            f"name: {t_title}",
            f"description: \"{t_desc}\"",
            f"category: {t_cat}",
        ]
        if source_tag:
            fm.append(f"source: {source_tag}")
        fm.extend([
            f"updated: {now_str}",
            "---",
            "",
            t_body.strip(),
            ""
        ])
        return "\n".join(fm)

    private_categories = ("user", "finances", "family", "private_email", "sms", "preference", "social", "credentials")
    is_personal = category in private_categories or name.startswith(("user_", "deep_", "private_", "security_"))

    written_files = []
    mirrored_public = False
    primary_file = None
    primary_tier = "private"

    if tier == "vault":
        # Strictly vault memory (isolated, no public/private mirror, no root symlink, independent VAULT.md index)
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        vault_file = VAULT_DIR / name
        vault_file.write_text(format_document(title, description, category, content), encoding="utf-8")
        written_files.append(str(vault_file))
        primary_tier = "vault"
        primary_file = vault_file
        rebuild_indexes()
        return {
            "ok": True,
            "file": str(primary_file),
            "tier": primary_tier,
            "mirrored_public": False,
            "files": written_files,
            "indexed": True
        }

    elif tier == "private" or is_personal:
        # Strictly private (ground-truth only, no public mirror)
        PRIV_DIR.mkdir(parents=True, exist_ok=True)
        priv_file = PRIV_DIR / name
        priv_file.write_text(format_document(title, description, category, content), encoding="utf-8")
        written_files.append(str(priv_file))
        primary_tier = "private"
        primary_file = priv_file

    elif tier == "public":
        # Strictly public explicit write (must pass security validation raw)
        for pat, desc in _get_security_rules():
            m = re.search(pat, content)
            if m:
                raise ValueError(f"Security validation failed for public memory: Matched {desc} ('{m.group(0)}')")
        PUB_DIR.mkdir(parents=True, exist_ok=True)
        pub_file = PUB_DIR / name
        pub_file.write_text(format_document(title, description, category, content), encoding="utf-8")
        written_files.append(str(pub_file))
        primary_tier = "public"
        primary_file = pub_file

    else:
        # tier == "auto" for engineering / technical content: DUAL-WRITE
        # 1. Write full-fidelity unredacted original to memory/private/
        PRIV_DIR.mkdir(parents=True, exist_ok=True)
        priv_file = PRIV_DIR / name
        priv_file.write_text(format_document(title, description, category, content), encoding="utf-8")
        written_files.append(str(priv_file))
        primary_tier = "dual"
        primary_file = priv_file

        # 2. In-flight scrubbing for sanitized public mirror
        try:
            if str(TOOLS_DIR) not in sys.path:
                sys.path.insert(0, str(TOOLS_DIR))
            import promote_memory
            scrubbed_content = promote_memory.scrub_content(content)
            scrubbed_title = promote_memory.scrub_content(title)
            scrubbed_desc = promote_memory.scrub_content(description)

            violations = promote_memory.validate_scrubbed_safety(f"{scrubbed_title}\n{scrubbed_desc}\n{scrubbed_content}")
            if not violations:
                PUB_DIR.mkdir(parents=True, exist_ok=True)
                pub_file = PUB_DIR / name
                pub_file.write_text(format_document(scrubbed_title, scrubbed_desc, category, scrubbed_content, source_tag=f"private/{name}"), encoding="utf-8")
                written_files.append(str(pub_file))
                mirrored_public = True
            else:
                log.warning(f"Public mirror skipped for {name}: unscrubbed tokens detected: {violations}")
        except Exception as e:
            log.warning(f"Error generating public mirror for {name}: {e}")

    # Create / update backward-compatible symlink at root of memory dir
    root_link = MEMORY_DIR / name
    if primary_file and root_link != primary_file:
        try:
            if root_link.is_symlink() or root_link.exists():
                root_link.unlink()
            root_link.symlink_to(Path(primary_file.parent.name) / name)
        except Exception as e:
            log.warning(f"Failed creating root symlink for {name}: {e}")

    # Rebuild indexes
    rebuild_indexes()

    return {
        "ok": True,
        "file": str(primary_file),
        "tier": primary_tier,
        "mirrored_public": mirrored_public,
        "files": written_files,
        "indexed": True
    }

def rebuild_indexes():
    """Rebuild MEMORY_PUBLIC.md, MEMORY_PRIVATE.md, unified MEMORY.md, and isolated VAULT.md."""
    PUB_DIR.mkdir(parents=True, exist_ok=True)
    PRIV_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_DIR.mkdir(parents=True, exist_ok=True)

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

    # Unified index (Public + Private only; strictly NO VAULT)
    uni_lines = [
        "# Zero Complete Memory Index (Unified)\n",
        "## 🌐 Public Engineering & Architecture Memory (`memory/public/`)\n"
    ]
    uni_lines.extend(pub_lines[1:])
    uni_lines.append("\n## 🔒 Private Homelab & Personal Memory (`memory/private/`)\n")
    uni_lines.extend(priv_lines[1:])
    MEMORY_INDEX.write_text("\n".join(uni_lines) + "\n", encoding="utf-8")

    # Isolated Vault Index (Vault files only)
    vault_lines = ["# Zero Vault Secret Memory Index (Strictly Isolated Enclave)\n"]
    for f in sorted(VAULT_DIR.glob("*.md")):
        if f.name in ("VAULT.md", "MEMORY.md"):
            continue
        title, desc = get_info(f)
        vault_lines.append(f"- [{title}]({f.name}) — {desc}")
    if len(vault_lines) == 1:
        vault_lines.append("- *(Vault is currently empty. No secret records active.)*")
    MEMORY_VAULT_INDEX.write_text("\n".join(vault_lines) + "\n", encoding="utf-8")

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

IGNORED_CHANNELS = {
    "1210466877835313155",  # seerr-notifications
    "1330447543477338202",  # server-updates
}

PUBLIC_CHANNELS = {
    "1534436119888793750",  # the-banana-stand
    "1534452820995080192",  # lounge
}

SCORING_PROMPT = """You are the Memory Consolidation Evaluator for Zero (autonomous AI engineer).
Evaluate the importance (1-10) of this conversation episode for long-term durable memory retention.

IMPORTANCE RUBRIC:
- 9-10 (Critical / Durable System Knowledge):
  * Permanent architectural decisions, ratified protocol specs (e.g. AGORA market pricing rules, tunnel ingress).
  * Explicit core user preference corrections / operational invariants (e.g. "Don't self-narrate", "Always Pacific Time").
  * System outage post-mortems / root-cause debugging scars (e.g. SQLite concurrency deadlock, CLI process hang).
- 7-8 (Substantive Architecture & Decisions):
  * Clarifications of system harness internals (e.g. ephemeral worker vs daemon lifecycle).
  * Major feature/tool enhancements (e.g. new classification rubric, dreaming pipeline redesign).
  * Homelab hardware/device topology changes.
- 5-6 (Routine Work in Progress / Minor Technical Chat):
  * Active task coordination, standard debugging iterations, test pass/fail reports with no systemic takeaways.
- 3-4 (Minor Updates / Informational):
  * Brief acknowledgments, routine cron outputs, simple status checks.
- 1-2 (Noise / Casual Banter):
  * Greetings, social jokes, reaction GIFs, "sounds good", "lgtm".

Conversation Excerpt ({channel_name} | {tier}):
{transcript}

Task:
1. Score from 1 to 10.
2. If score >= 7, specify CATEGORY, TITLE (3-6 words), and SUMMARY (1-3 clear sentences of the durable takeaway).
   Categories:
   - PUBLIC_DECISION (ratified multi-agent, protocol, or open engineering decisions)
   - PRIVATE_PREFERENCE (Ryan's explicit feedback, directives, habits, communication rules)
   - PRIVATE_SCAR (homelab debugging post-mortem, tricky root causes, system fixes)
3. If score < 7, specify CATEGORY: DISCARD and SUMMARY: <short reason>.

Format strictly as:
SCORE: <number>
CATEGORY: <DISCARD | PUBLIC_DECISION | PRIVATE_PREFERENCE | PRIVATE_SCAR>
TITLE: <title>
SUMMARY: <distilled memory takeaway or discard reason>
"""

def _parse_msg_ts(m: dict) -> datetime | None:
    ts_str = m.get("timestamp")
    if not ts_str:
        return None
    try:
        if "UTC" in ts_str:
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None

def _segment_channel_messages(messages: list[dict], max_gap_seconds: int = 300) -> list[list[dict]]:
    episodes = []
    curr = []
    last_ts = None
    sorted_msgs = sorted([m for m in messages if _parse_msg_ts(m)], key=lambda x: _parse_msg_ts(x))
    for m in sorted_msgs:
        ts = _parse_msg_ts(m)
        if last_ts and (ts - last_ts).total_seconds() > max_gap_seconds:
            if curr:
                episodes.append(curr)
                curr = []
        curr.append(m)
        last_ts = ts
    if curr:
        episodes.append(curr)
    return episodes

def _evaluate_episode_llm(ep: list[dict], ch_name: str, is_public: bool) -> dict:
    transcript_lines = []
    for m in ep[:20]:
        author = m.get("author", "Unknown")
        content = m.get("content", "").strip()
        content_clean = re.sub(r"```(?:handoff)?\n.*?\n```", "[HANDOFF_ENVELOPE]", content, flags=re.DOTALL)
        if content_clean:
            transcript_lines.append(f"{author}: {content_clean[:350]}")
    
    transcript_text = "\n".join(transcript_lines)
    tier = "PUBLIC" if is_public else "PRIVATE"
    prompt = SCORING_PROMPT.format(channel_name=ch_name, tier=tier, transcript=transcript_text)
    
    try:
        res = subprocess.run([
            "agy",
            "--model=gemini-3.8-flash-low",
            "--disable-slash-commands",
            f"-p={prompt}"
        ], capture_output=True, text=True, timeout=35)
        out = res.stdout.strip()
    except Exception as e:
        return {"score": 0.0, "category": "ERROR", "title": "", "summary": str(e)}

    score = 0.0
    category = "DISCARD"
    title = ""
    summary = ""

    m_score = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)", out)
    if m_score:
        score = float(m_score.group(1))

    m_cat = re.search(r"CATEGORY:\s*([A-Z_]+)", out)
    if m_cat:
        category = m_cat.group(1).strip()

    m_title = re.search(r"TITLE:\s*([^\n]+)", out)
    if m_title:
        title = m_title.group(1).strip()

    m_sum = re.search(r"SUMMARY:\s*([\s\S]+)$", out)
    if m_sum:
        summary = m_sum.group(1).strip()

    return {"score": score, "category": category, "title": title, "summary": summary}

def _to_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:40]

def run_dreaming_consolidation(dry_run: bool = False, hours_back: int = 24) -> tuple[bool, str]:
    """3-Stage Dreaming Pass:
    Stage 1: Public stream (Crab Cavern) -> extracts decisions/specs -> memory/public/
    Stage 2: Private stream (Home turf) -> extracts preferences/scars -> memory/private/
    Stage 3: Cross-tier promotion & FTS5 index synchronization.
    """
    now_dt = datetime.now(PT)
    now_pt = now_dt.strftime("%Y-%m-%d %I:%M %p PT")
    consolidated_items = []
    
    # 1. Ingest Channel History
    if CHANNEL_HISTORY_PATH.exists():
        try:
            with open(CHANNEL_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)

            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
            episodes_to_eval = []

            for cid, msgs in history.items():
                if cid in IGNORED_CHANNELS:
                    continue
                
                recent_msgs = [m for m in msgs if _parse_msg_ts(m) and _parse_msg_ts(m) >= cutoff]
                if not recent_msgs:
                    continue

                ch_name = recent_msgs[0].get("channel_name", cid)
                is_thread = ch_name.startswith("🧵")
                is_public = (cid in PUBLIC_CHANNELS) or ("banana" in ch_name.lower()) or ("lounge" in ch_name.lower())

                if is_thread and len(recent_msgs) <= 25:
                    total_len = sum(len(m.get("content", "")) for m in recent_msgs)
                    if total_len > 150:
                        episodes_to_eval.append((recent_msgs, ch_name, is_public))
                else:
                    eps = _segment_channel_messages(recent_msgs, max_gap_seconds=300)
                    for ep in eps:
                        total_len = sum(len(m.get("content", "")) for m in ep)
                        if len(ep) >= 2 and total_len > 150:
                            episodes_to_eval.append((ep, ch_name, is_public))

            log.info(f"Dreaming evaluating {len(episodes_to_eval)} substantive episodes over past {hours_back}h")

            for ep, ch_name, is_public in episodes_to_eval:
                res = _evaluate_episode_llm(ep, ch_name, is_public)
                score = res.get("score", 0.0)
                category = res.get("category", "DISCARD")
                title = res.get("title", "")
                summary = res.get("summary", "")

                if score < 7.0 or category == "DISCARD" or not summary:
                    continue

                slug = _to_slug(title) or f"item_{int(datetime.now().timestamp())}"
                t0_str = _parse_msg_ts(ep[0]).astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT")

                # Stage 1: Public Decision
                if category == "PUBLIC_DECISION" and is_public:
                    if CC_DECISIONS_FILE.exists():
                        dec_content = CC_DECISIONS_FILE.read_text(encoding="utf-8", errors="replace")
                        if slug not in dec_content and title.lower() not in dec_content.lower():
                            block = f"""
---

### Resolution: `{slug}` ({t0_str})
* **Author**: Zero (consolidated via dreaming)
* **Kind**: `decision` / `architecture`
* **Title**: {title}
* **Summary**: 🍌 {summary}
"""
                            if not dry_run:
                                CC_DECISIONS_FILE.write_text(dec_content.rstrip() + block + "\n", encoding="utf-8")
                            consolidated_items.append(f"Public Decision logged: `{title}` (Crab Cavern)")
                    else:
                        if not dry_run:
                            memory_write(f"arch_{slug}.md", title, summary[:100], "architecture", summary, tier="public")
                        consolidated_items.append(f"Public Architecture created: `arch_{slug}.md`")

                # Stage 2: Private Preference or Scar
                elif category == "PRIVATE_PREFERENCE":
                    if USER_RYAN_FILE.exists():
                        u_content = USER_RYAN_FILE.read_text(encoding="utf-8", errors="replace")
                        if title.lower() not in u_content.lower() and slug not in u_content.lower():
                            rule_bullet = f"\n- **{title} ({t0_str}):** {summary}"
                            if not dry_run:
                                USER_RYAN_FILE.write_text(u_content.rstrip() + rule_bullet + "\n", encoding="utf-8")
                            consolidated_items.append(f"Ryan Preference updated: `{title}`")
                    else:
                        if not dry_run:
                            memory_write(f"user_pref_{slug}.md", title, summary[:100], "user", summary, tier="private")
                        consolidated_items.append(f"Private Preference created: `user_pref_{slug}.md`")

                elif category == "PRIVATE_SCAR":
                    scar_name = f"scar_{slug}.md"
                    scar_path = PRIV_DIR / scar_name
                    if not scar_path.exists():
                        if not dry_run:
                            memory_write(scar_name, title, summary[:100], "scar", summary, tier="private")
                        consolidated_items.append(f"Private Debugging Scar logged: `{scar_name}`")

        except Exception as e:
            log.warning(f"Error during episode dreaming evaluation: {e}")

    # File Modifications Check
    for target in [Path("/workspace/agents.md"), Path("/workspace/memory/private/user_ryan.md")]:
        if target.exists() and (datetime.now(PT) - datetime.fromtimestamp(target.stat().st_mtime, tz=PT)).total_seconds() < (hours_back * 3600):
            consolidated_items.append(f"Rules/Preferences updated in `{target.name}`")

    tools_dir = Path("/workspace/tools")
    if tools_dir.exists():
        for tf in tools_dir.glob("*.py"):
            if (datetime.now(PT) - datetime.fromtimestamp(tf.stat().st_mtime, tz=PT)).total_seconds() < (hours_back * 3600):
                consolidated_items.append(f"Tool enhancements in `{tf.name}`")

    # Stage 3: Automated Promotion Pass
    try:
        import promote_memory
        for cand in promote_memory.find_candidates():
            if (datetime.now(PT) - datetime.fromtimestamp(cand.stat().st_mtime, tz=PT)).total_seconds() < (hours_back * 3600):
                dest_file = PUB_DIR / cand.name
                if not dest_file.exists() or cand.stat().st_mtime > dest_file.stat().st_mtime:
                    ok, msg = promote_memory.promote_file(cand, dry_run=dry_run)
                    if ok:
                        consolidated_items.append(f"Promoted engineering scar: `{cand.name}` -> `public/{cand.name}`")
    except Exception as e:
        log.warning(f"Dreaming promotion scan error: {e}")

    if not dry_run:
        rebuild_indexes()

    if not consolidated_items:
        return False, f"Dream pass complete ({hours_back}h window): no new long-term memories required consolidation."

    report = (
        f"💭 **Memory Consolidation (Dreaming)** — {now_pt} (Window: {hours_back}h)\n"
        f"Consolidated learnings into durable memory:\n"
        + "\n".join(f"- {it}" for it in sorted(set(consolidated_items)))
        + "\nMemory indexes rebuilt and synced with SQLite FTS5."
    )
    return True, report

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if action == "doctor":
        ok, msg = run_memory_doctor()
        print(msg)
    elif action == "dream":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        ok, msg = run_dreaming_consolidation(hours_back=hours)
        print(msg)
    elif action == "catchup":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        ok, msg = run_dreaming_consolidation(hours_back=days * 24)
        print(msg)
    elif action == "rebuild":
        rebuild_indexes()
        print("Indexes rebuilt successfully.")
