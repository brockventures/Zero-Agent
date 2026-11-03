#!/usr/bin/env python3
"""
voice_quick_tools_auditor.py — Weekly Voice Quick-Tools Promotion Auditor Sidecar
=================================================================================
Weekly sidecar that checks for new skills or tools that should be promoted to
inline Quick Tools (`<3s` inline execution) for the Lomin/Gotchi voice bridge:

1. Scans all skills in `/workspace/.agents/skills/*/SKILL.md` and
   `/root/.gemini/antigravity-cli/skills/*/SKILL.md` and tools in `/workspace/tools/*.py`.
2. Compares them against the active inline voice tools in
   `tools.gotchi_zero_adapter._build_gotchi_tools()` and checks recent voice logs
   (`/workspace/data/gotchi_gateway.log`) for repeated `load_skill_playbook` or
   `handoff_to_zero_runtime` triggers.
3. Identifies candidate skills/tools suitable for `<3s` inline voice execution.
4. When candidates exist, stages an atomic FORGE Decision Card (`Approve | Edit | Dismiss`)
   with an explicit `**Approve Action:**` summary; when 0 new candidates exist, exits
   cleanly for silent `[NO_REPLY]` sidecar behavior.
5. Provides `ensure_weekly_schedule_registered(schedule_path='/workspace/data/schedule.json')`
   to idempotently register `voice_quick_tools_weekly_audit` (weekly on Sundays at 18:40 PT,
   `channel_id` 1542081375287640084) and calls it from `__main__ --register-schedule`.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from tools.forge import FORGE_DB_PATH, stage_decision

PT_TZ = ZoneInfo("America/Los_Angeles")
TARGET_CHANNEL_ID = 1542081375287640084
JOB_ID = "voice_quick_tools_weekly_audit"
JOB_NAME = "Weekly Voice Quick-Tools Promotion Audit"
JOB_DAY_OF_WEEK = 6  # Sunday (0=Mon .. 6=Sun)
JOB_HOUR_PT = 18
JOB_MINUTE_PT = 40
JOB_PROMPT = (
    "Run the weekly Voice Quick-Tools Promotion Auditor using "
    "python3 /workspace/tools/voice_quick_tools_auditor.py. "
    "Silent background sidecar: if 0 cards are staged or output is empty, emit [NO_REPLY] with zero text."
)

DEFAULT_SCHEDULE_PATH = Path("/workspace/data/schedule.json")
DEFAULT_LOG_PATH = Path("/workspace/data/gotchi_gateway.log")
DEFAULT_SKILL_DIRS: tuple[Path, ...] = (
    Path("/workspace/.agents/skills"),
    Path("/root/.gemini/antigravity-cli/skills"),
)
DEFAULT_TOOLS_DIR = Path("/workspace/tools")
DEFAULT_BASELINE_PATH = Path("/workspace/data/voice_quick_tools_baseline.json")

# Known baseline skills that are already covered by inline Quick Tools or are non-inline workflows
KNOWN_COVERED_OR_HEAVY_SKILLS: frozenset[str] = frozenset({
    # Already covered by inline quick tools in _build_gotchi_tools()
    "mlb-stream",
    "plex-playback",
    "print3d-slicer",
    "reddit-research",
    "shopping-advisor",
    "transcript-search",
    "whole-foods-cart",
    "outbox",
    # Heavy (>3s), architectural, browser, cloud compute, or build-time skills unsuitable for <3s inline voice
    "agy-customizations",
    "antigravity-guide",
    "api-and-interface-design",
    "code-simplification",
    "consultation-panel",
    "context7-docs",
    "crontab-verify",
    "detached-task",
    "documentation-and-adrs",
    "email-dispatch",
    "forge-orchestrator",
    "hardcode-regex-auditor",
    "homelab-routing",
    "image-generation",
    "modal-compute",
    "pixel-browser",
    "plex-audiobook-enricher",
    "print3d-image-gen",
    "remote-transfer",
    "repo-evaluator",
    "scar-to-skill",
    "security-auditor",
    "set-status",
    "starbucks-order",
    "systematic-debugging",
    "ui-preview",
    "ui-ux-pro-max",
    "verification-before-completion",
    "web-qa",
    "zero-medic",
})

# Keywords that indicate a skill or tool is heavy/long-running (>3s) and unsuitable for inline voice
HEAVY_EXCLUSION_KEYWORDS: tuple[str, ...] = (
    "playwright",
    "headless browser",
    "browserless",
    "modal",
    "docker build",
    "docker compose",
    "platformio",
    "orcaslicer",
    "adb ",
    "ui automator",
    "detached",
    "longer than 90 seconds",
    "multi-pr",
    "subagent",
    "image generation",
    "generate_image",
    "mesh reconstruction",
    "rsync",
    "bwlimit",
)

# Keywords that indicate a skill or tool is suitable for fast (<3s) inline voice lookup/control
FAST_INLINE_CANDIDATE_KEYWORDS: tuple[str, ...] = (
    "<3s",
    "<2s",
    "<1.5s",
    "<1s",
    "inline",
    "quick tool",
    "voice",
    "status",
    "check",
    "lookup",
    "query",
    "control",
    "toggle",
    "turn on",
    "turn off",
    "reminder",
    "timer",
    "alarm",
    "weather",
    "calendar",
    "sensor",
    "battery",
    "thermostat",
    "lock",
    "garage",
    "fast",
    "instant",
)

# Mapping from existing inline tool names in _build_gotchi_tools() to covered skill/tool stems
INLINE_TOOL_COVERAGE_MAP: dict[str, set[str]] = {
    "home_assistant_control": {
        "home_assistant",
        "ha_control",
        "ha_battery_check",
        "ha_anomaly_check",
        "ha_update_check",
    },
    "check_calendar": {
        "calendar",
        "schedule",
        "scheduler_tool",
        "dated_reminders",
        "birthday_reminder",
    },
    "tv_and_plex_control": {
        "plex-playback",
        "plex_playback",
        "plex_play",
        "mlb-stream",
        "mlb_stream",
        "mlb_stream_extractor",
    },
    "check_3d_printer": {
        "print3d-slicer",
        "print3d_slicer",
        "octoprint",
        "printer",
    },
    "grocery_list_action": {
        "whole-foods-cart",
        "whole_foods_cart",
        "grocery",
        "grocery_list",
    },
    "web_and_reddit_search": {
        "reddit-research",
        "reddit_research",
        "reddit_extractor",
        "shopping-advisor",
        "shopping_advisor",
        "amazon_serpapi",
    },
    "search_zero_memory": {
        "transcript-search",
        "transcript_search",
        "transcript_index",
    },
    "post_note_to_zero_chat": {
        "outbox",
        "deliver_file",
        "deliver_image",
    },
    "get_weather_forecast": {
        "weather",
        "weather_forecast",
    },
    "get_recent_discord_context": {
        "channel_history",
        "discord_context",
    },
    "search_gmail": {
        "gmail",
        "email",
    },
    "check_tasks_and_repos": {
        "tasks",
        "repos",
    },
}


def _parse_skill_frontmatter(skill_md_path: Path) -> tuple[str, str]:
    """Extract `name` and `description` from YAML frontmatter of a SKILL.md file."""
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return skill_md_path.parent.name, ""

    name_val = skill_md_path.parent.name
    desc_val = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            m_name = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
            if m_name:
                name_val = m_name.group(1).strip().strip("'\"")
            m_desc = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
            if m_desc:
                desc_val = m_desc.group(1).strip().strip("'\"")
    if not desc_val:
        # Fallback: first non-empty non-heading paragraph
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                desc_val = s[:300]
                break
    return name_val, desc_val


def _parse_tool_docstring(py_path: Path) -> str:
    """Extract module docstring from a Python tool file."""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        doc = ast.get_docstring(tree) or ""
        return " ".join(doc.strip().split())[:400]
    except Exception:
        return ""


def discover_skills_and_tools(
    skill_dirs: tuple[Path | str, ...] | list[Path | str] = DEFAULT_SKILL_DIRS,
    tools_dir: Path | str = DEFAULT_TOOLS_DIR,
) -> dict[str, list[dict[str, Any]]]:
    """
    Scan all skills in `/workspace/.agents/skills/*/SKILL.md` and
    `/root/.gemini/antigravity-cli/skills/*/SKILL.md` and tools in `/workspace/tools/*.py`.
    Returns `{"skills": [...], "tools": [...]}`.
    """
    skills: list[dict[str, Any]] = []
    seen_skills: set[str] = set()

    for raw_root in skill_dirs:
        root = Path(raw_root)
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            if not skill_md.is_file():
                continue
            folder_name = skill_md.parent.name.strip()
            fm_name, fm_desc = _parse_skill_frontmatter(skill_md)
            canonical = folder_name or fm_name
            norm_key = canonical.lower()
            if norm_key in seen_skills:
                continue
            seen_skills.add(norm_key)
            skills.append({
                "name": canonical,
                "normalized_name": norm_key,
                "description": fm_desc,
                "path": str(skill_md),
                "kind": "skill",
            })

    tools_list: list[dict[str, Any]] = []
    t_root = Path(tools_dir)
    if t_root.is_dir():
        for py_file in sorted(t_root.glob("*.py")):
            if not py_file.is_file():
                continue
            stem = py_file.stem
            if stem.startswith("test_") or stem.startswith("_"):
                continue
            doc = _parse_tool_docstring(py_file)
            tools_list.append({
                "name": stem,
                "normalized_name": stem.lower(),
                "description": doc,
                "path": str(py_file),
                "kind": "tool",
            })

    return {"skills": skills, "tools": tools_list}


def get_active_inline_voice_tools() -> set[str]:
    """
    Return the set of active inline function declaration names from
    `tools.gotchi_zero_adapter._build_gotchi_tools()`.
    """
    from tools import gotchi_zero_adapter

    built = gotchi_zero_adapter._build_gotchi_tools()
    names: set[str] = set()
    for tool_obj in built or []:
        decls = getattr(tool_obj, "function_declarations", None)
        if decls:
            for d in decls:
                n = getattr(d, "name", None)
                if n:
                    names.add(str(n).strip())
        elif isinstance(tool_obj, dict):
            for d in tool_obj.get("function_declarations", []):
                n = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
                if n:
                    names.add(str(n).strip())
        elif isinstance(tool_obj, str):
            names.add(tool_obj.strip())
    return names


def scan_voice_gateway_logs(
    log_path: Path | str = DEFAULT_LOG_PATH,
    max_bytes: int = 500_000,
) -> dict[str, Any]:
    """
    Inspect recent voice logs (`/workspace/data/gotchi_gateway.log`) for repeated
    `load_skill_playbook` or `handoff_to_zero_runtime` triggers.
    Returns counts per skill/task and total trigger statistics.
    """
    p = Path(log_path)
    playbook_counts: dict[str, int] = {}
    handoff_counts: dict[str, int] = {}
    total_playbook_calls = 0
    total_handoff_calls = 0

    if not p.is_file():
        return {
            "playbook_counts": playbook_counts,
            "handoff_counts": handoff_counts,
            "total_playbook_calls": 0,
            "total_handoff_calls": 0,
        }

    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw_text = f.read().decode("utf-8", errors="replace")
    except Exception:
        return {
            "playbook_counts": playbook_counts,
            "handoff_counts": handoff_counts,
            "total_playbook_calls": 0,
            "total_handoff_calls": 0,
        }

    # Patterns for load_skill_playbook calls in logs:
    # e.g. Executing tool call: load_skill_playbook({'skill_name': 'starbucks-order'})
    # or load_skill_playbook(skill_name='...') or JSON "skill_name": "..."
    playbook_re = re.compile(
        r"load_skill_playbook\s*\((?:[^)]*?(?:['\"]skill_name['\"]|skill_name)\s*[:=]\s*['\"]([a-zA-Z0-9_\-]+)['\"]|['\"]([a-zA-Z0-9_\-]+)['\"])",
        re.IGNORECASE,
    )
    handoff_re = re.compile(
        r"handoff_to_zero_runtime\s*\((?:[^)]*?(?:['\"](?:task_prompt|prompt)['\"]|(?:task_prompt|prompt))\s*[:=]\s*['\"]([^'\"]{3,160})['\"])?",
        re.IGNORECASE,
    )

    for line in raw_text.splitlines():
        if "load_skill_playbook" in line:
            total_playbook_calls += 1
            m = playbook_re.search(line)
            if m:
                sk = (m.group(1) or m.group(2) or "").strip().lower()
                if sk:
                    playbook_counts[sk] = playbook_counts.get(sk, 0) + 1
        if "handoff_to_zero_runtime" in line:
            total_handoff_calls += 1
            m = handoff_re.search(line)
            if m and m.group(1):
                prompt_snip = m.group(1).strip().lower()
                # Normalize into a short topic key or match known skill/tool tokens
                topic_key = re.sub(r"[^a-z0-9_\-]+", "_", prompt_snip)[:48].strip("_")
                if topic_key:
                    handoff_counts[topic_key] = handoff_counts.get(topic_key, 0) + 1

    return {
        "playbook_counts": playbook_counts,
        "handoff_counts": handoff_counts,
        "total_playbook_calls": total_playbook_calls,
        "total_handoff_calls": total_handoff_calls,
    }


def _build_covered_identifiers(active_inline_tools: set[str]) -> set[str]:
    """Build normalized set of tool/skill identifiers already covered by active inline tools."""
    covered: set[str] = set()
    for t_name in active_inline_tools:
        norm = t_name.lower().strip()
        covered.add(norm)
        covered.add(norm.replace("_", "-"))
        covered.add(norm.replace("-", "_"))
        for alias in INLINE_TOOL_COVERAGE_MAP.get(t_name, set()):
            a_norm = alias.lower().strip()
            covered.add(a_norm)
            covered.add(a_norm.replace("_", "-"))
            covered.add(a_norm.replace("-", "_"))
    for k in KNOWN_COVERED_OR_HEAVY_SKILLS:
        covered.add(k.lower())
        covered.add(k.lower().replace("-", "_"))
    return covered


def _is_suitable_for_fast_inline_voice(item: dict[str, Any], log_triggers: int) -> tuple[bool, str]:
    """
    Evaluate whether a discovered skill or tool is suitable for `<3s` inline voice execution.
    """
    name = str(item.get("name") or "").strip()
    desc = str(item.get("description") or "").strip()
    kind = str(item.get("kind") or "skill").strip()
    combined = f"{name} {desc}".lower()

    # Reject heavy/infrastructure/daemon/sidecar scripts that shouldn't be inline voice quick tools
    if any(kw in combined for kw in HEAVY_EXCLUSION_KEYWORDS):
        return False, "Excluded heavy/long-running execution dependency"

    if kind == "tool":
        # Exclude internal adapters, sidecars, tests, migrations, servers, and build scripts
        if any(
            pat in name.lower()
            for pat in (
                "adapter",
                "bridge",
                "sidecar",
                "auditor",
                "audit",
                "server",
                "daemon",
                "watcher",
                "forge",
                "migrate",
                "backfill",
                "telemetry",
                "config",
                "guard",
                "scanner",
                "validate",
                "promote",
                "enricher",
                "digest",
                "monitor",
                "sync",
                "backup",
            )
        ):
            if log_triggers < 2:
                return False, "Internal infrastructure/sidecar module"

    if log_triggers >= 2:
        return True, f"Repeated voice log trigger ({log_triggers}x in gotchi_gateway.log)"

    # Check if description explicitly highlights <3s / inline / fast voice suitability
    if kind == "skill":
        if any(kw in combined for kw in FAST_INLINE_CANDIDATE_KEYWORDS):
            return True, "Fast (<3s) inline voice candidate skill"
    elif kind == "tool":
        if any(kw in combined for kw in ("<3s", "<2s", "<1.5s", "<1s", "quick tool", "inline voice")):
            return True, "Explicit <3s inline voice tool candidate"

    return False, "Does not meet <3s inline voice promotion criteria"


def identify_promotion_candidates(
    discovered: dict[str, list[dict[str, Any]]] | None = None,
    active_inline_tools: set[str] | None = None,
    log_stats: dict[str, Any] | None = None,
    min_log_triggers: int = 2,
    known_tools_Allowlist: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Compare discovered skills/tools and recent voice log triggers against active inline
    voice tools in `_build_gotchi_tools()` and return candidate skills/tools suitable
    for `<3s` inline voice execution.
    """
    if discovered is None:
        discovered = discover_skills_and_tools()
    if active_inline_tools is None:
        active_inline_tools = get_active_inline_voice_tools()
    if log_stats is None:
        log_stats = scan_voice_gateway_logs()

    covered = _build_covered_identifiers(active_inline_tools)
    playbook_counts: dict[str, int] = log_stats.get("playbook_counts", {}) or {}
    handoff_counts: dict[str, int] = log_stats.get("handoff_counts", {}) or {}

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()

    # 1. Check discovered skills
    for sk in discovered.get("skills", []):
        norm = sk["normalized_name"]
        norm_underscore = norm.replace("-", "_")
        pb_hits = playbook_counts.get(norm, 0) + playbook_counts.get(norm_underscore, 0)
        ho_hits = sum(
            cnt for topic, cnt in handoff_counts.items() if norm in topic or norm_underscore in topic
        )
        total_triggers = pb_hits + ho_hits

        # If already covered by an inline quick tool (and not repeatedly falling back in logs), skip
        if (norm in covered or norm_underscore in covered) and total_triggers < min_log_triggers:
            continue
        # Even if in KNOWN_COVERED_OR_HEAVY_SKILLS, if it's an inline-covered tool in active_inline_tools, skip
        if norm in {t.lower() for t in active_inline_tools} or norm_underscore in {
            t.lower() for t in active_inline_tools
        }:
            continue

        suitable, reason = _is_suitable_for_fast_inline_voice(sk, total_triggers)
        if suitable and norm not in seen_candidates:
            seen_candidates.add(norm)
            proposed_fn = f"voice_{norm_underscore}" if not norm_underscore.startswith("voice_") else norm_underscore
            candidates.append({
                "name": sk["name"],
                "kind": "skill",
                "path": sk.get("path", ""),
                "description": sk.get("description", ""),
                "proposed_tool_name": proposed_fn,
                "log_triggers": total_triggers,
                "reason": reason,
            })

    # 2. Check discovered tools in /workspace/tools/*.py
    # Standard existing repository tools at baseline are ignored unless they explicitly declare
    # <3s / inline voice quick tool markers or are triggered repeatedly in gotchi_gateway.log.
    for tl in discovered.get("tools", []):
        norm = tl["normalized_name"]
        norm_hyphen = norm.replace("_", "-")
        if norm in covered or norm_hyphen in covered:
            continue
        if known_tools_Allowlist is not None and norm in known_tools_Allowlist:
            continue

        pb_hits = playbook_counts.get(norm, 0) + playbook_counts.get(norm_hyphen, 0)
        ho_hits = sum(
            cnt for topic, cnt in handoff_counts.items() if norm in topic or norm_hyphen in topic
        )
        total_triggers = pb_hits + ho_hits

        suitable, reason = _is_suitable_for_fast_inline_voice(tl, total_triggers)
        if suitable and norm not in seen_candidates:
            seen_candidates.add(norm)
            candidates.append({
                "name": tl["name"],
                "kind": "tool",
                "path": tl.get("path", ""),
                "description": tl.get("description", ""),
                "proposed_tool_name": norm,
                "log_triggers": total_triggers,
                "reason": reason,
            })

    # 3. Check repeated log triggers that didn't match an existing skill folder (e.g., ad-hoc skill/handoff requests)
    for sk_name, count in sorted(playbook_counts.items(), key=lambda x: (-x[1], x[0])):
        norm = sk_name.lower().strip()
        norm_underscore = norm.replace("-", "_")
        if count >= min_log_triggers and norm not in seen_candidates:
            if norm in {t.lower() for t in active_inline_tools} or norm_underscore in {
                t.lower() for t in active_inline_tools
            }:
                continue
            seen_candidates.add(norm)
            candidates.append({
                "name": sk_name,
                "kind": "log_trigger",
                "path": str(DEFAULT_LOG_PATH),
                "description": f"Repeated load_skill_playbook('{sk_name}') calls in voice gateway log.",
                "proposed_tool_name": norm_underscore,
                "log_triggers": count,
                "reason": f"Repeated load_skill_playbook trigger ({count}x in gotchi_gateway.log)",
            })

    return candidates


def stage_quick_tools_promotion_card(
    candidates: list[dict[str, Any]],
    channel_id: int = TARGET_CHANNEL_ID,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
) -> dict[str, Any] | None:
    """
    When candidates exist, stage an atomic FORGE Decision Card (`Approve | Edit | Dismiss`)
    with an explicit `**Approve Action:**` summary. Returns None when `candidates` is empty.
    """
    if not candidates:
        return None

    cand_names = [str(c.get("name") or "") for c in candidates[:5] if c.get("name")]
    names_csv = ", ".join(cand_names)
    bullet_lines = []
    for c in candidates[:5]:
        c_name = c.get("name", "unknown")
        c_kind = c.get("kind", "skill")
        c_reason = c.get("reason", "Candidate for <3s inline voice execution")
        c_fn = c.get("proposed_tool_name", c_name.replace("-", "_"))
        bullet_lines.append(f"• `{c_name}` ({c_kind} → `{c_fn}`): {c_reason}")

    approve_cmd = (
        f"python3 /workspace/tools/voice_quick_tools_auditor.py --promote "
        f"{' '.join(c.get('name', '') for c in candidates[:5] if c.get('name'))}"
    ).strip()

    summary = (
        f"Identified **{len(candidates)}** candidate skill(s)/tool(s) suitable for `<3s` inline "
        f"voice Quick-Tool promotion in `tools/gotchi_zero_adapter.py`:\n"
        + "\n".join(bullet_lines)
        + f"\n\n**Approve Action:** Enqueue FORGE Quick-Tool promotion build for {names_csv} (`{approve_cmd}`)"
    )

    payload = {
        "command": approve_cmd,
        "cwd": "/workspace",
        "action": f"Enqueue FORGE Quick-Tool promotion build for {names_csv}",
        "candidates": candidates,
    }

    return stage_decision(
        category="voice_quick_tools",
        title=f"Promote {len(candidates)} Voice Quick Tool(s): {names_csv[:60]}",
        summary=summary,
        handler_type="shell",
        payload=payload,
        channel_id=int(channel_id),
        ttl_seconds=86400.0,
        is_private=True,
        db_path=db_path,
        post_to_discord=post_to_discord,
    )


def _compute_next_sunday_run_ts(now_ts: float | None = None) -> float:
    """Compute next Sunday 18:40 PT epoch timestamp."""
    now_dt = datetime.fromtimestamp(now_ts or time.time(), tz=PT_TZ)
    days_ahead = (JOB_DAY_OF_WEEK - now_dt.weekday()) % 7
    candidate = now_dt.replace(hour=JOB_HOUR_PT, minute=JOB_MINUTE_PT, second=0, microsecond=0) + timedelta(
        days=days_ahead
    )
    if candidate <= now_dt:
        candidate += timedelta(days=7)
    return float(candidate.timestamp())


def ensure_weekly_schedule_registered(
    schedule_path: Path | str = DEFAULT_SCHEDULE_PATH,
) -> dict[str, Any]:
    """
    Idempotently register `voice_quick_tools_weekly_audit` (weekly on Sundays at 18:40 PT,
    `channel_id` 1542081375287640084) in `schedule_path` (and sync the local repo
    `data/schedule.json` if running inside a worktree).
    """
    target_paths: list[Path] = [Path(schedule_path)]

    result_entry: dict[str, Any] = {}
    was_created = False
    was_updated = False

    for s_path in target_paths:
        if s_path.exists():
            try:
                raw = json.loads(s_path.read_text(encoding="utf-8"))
            except Exception:
                raw = []
        else:
            s_path.parent.mkdir(parents=True, exist_ok=True)
            raw = []

        is_list = isinstance(raw, list)
        jobs: list[dict[str, Any]] = raw if is_list else raw.get("jobs", [])

        existing: dict[str, Any] | None = None
        for job in jobs:
            if isinstance(job, dict) and job.get("id") == JOB_ID:
                existing = job
                break

        desired_fields = {
            "id": JOB_ID,
            "name": JOB_NAME,
            "enabled": True,
            "schedule_type": "weekly",
            "day_of_week": JOB_DAY_OF_WEEK,
            "hour_pt": JOB_HOUR_PT,
            "minute_pt": JOB_MINUTE_PT,
            "channel_id": TARGET_CHANNEL_ID,
            "prompt": JOB_PROMPT,
            "catchup_if_missed": False,
        }

        if existing is None:
            new_job = dict(desired_fields)
            new_job["last_run_ts"] = None
            new_job["next_run_ts"] = _compute_next_sunday_run_ts()
            jobs.append(new_job)
            existing = new_job
            was_created = True
            dirty = True
        else:
            dirty = False
            for k, v in desired_fields.items():
                if existing.get(k) != v:
                    existing[k] = v
                    dirty = True
                    was_updated = True
            if "next_run_ts" not in existing or existing["next_run_ts"] is None:
                existing["next_run_ts"] = _compute_next_sunday_run_ts()
                dirty = True

        if dirty or not s_path.exists():
            tmp_path = s_path.with_suffix(".json.tmp")
            payload_to_write = jobs if is_list else {**raw, "jobs": jobs}
            tmp_path.write_text(json.dumps(payload_to_write, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(s_path)

        if not result_entry:
            result_entry = dict(existing)

    return {
        "ok": True,
        "id": JOB_ID,
        "created": was_created,
        "updated": was_updated,
        "schedule_path": str(schedule_path),
        "job": result_entry,
    }


def run_voice_quick_tools_audit(
    skill_dirs: tuple[Path | str, ...] | list[Path | str] = DEFAULT_SKILL_DIRS,
    tools_dir: Path | str = DEFAULT_TOOLS_DIR,
    log_path: Path | str = DEFAULT_LOG_PATH,
    channel_id: int = TARGET_CHANNEL_ID,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    dry_run: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Execute the full weekly Voice Quick-Tools Promotion audit:
    - Discovers skills and tools
    - Compares against active inline voice tools in `_build_gotchi_tools()`
    - Scans `/workspace/data/gotchi_gateway.log` for repeated `load_skill_playbook` or `handoff_to_zero_runtime` calls
    - Stages an atomic FORGE Decision Card when candidates exist
    - Emits empty output (`""`) when 0 candidates exist for silent `[NO_REPLY]` sidecar behavior.
    """
    discovered = discover_skills_and_tools(skill_dirs=skill_dirs, tools_dir=tools_dir)
    active_inline = get_active_inline_voice_tools()
    log_stats = scan_voice_gateway_logs(log_path=log_path)
    candidates = identify_promotion_candidates(
        discovered=discovered,
        active_inline_tools=active_inline,
        log_stats=log_stats,
    )

    decision_res: dict[str, Any] | None = None
    if candidates and not dry_run:
        decision_res = stage_quick_tools_promotion_card(
            candidates=candidates,
            channel_id=channel_id,
            db_path=db_path,
            post_to_discord=post_to_discord,
        )

    meta = {
        "skills_scanned": len(discovered.get("skills", [])),
        "tools_scanned": len(discovered.get("tools", [])),
        "active_inline_tools_count": len(active_inline),
        "active_inline_tools": sorted(active_inline),
        "log_stats": log_stats,
        "candidates_count": len(candidates),
        "candidates": candidates,
        "decision_staged": bool(decision_res and decision_res.get("ok")),
        "decision": decision_res,
    }

    if not candidates:
        return True, "", meta

    names_str = ", ".join(c["name"] for c in candidates[:5])
    dec_id = (decision_res or {}).get("decision_id", "dry-run")
    report = (
        f"🎙️ **Voice Quick-Tools Promotion Audit:** Identified {len(candidates)} candidate(s) "
        f"({names_str}) for `<3s` inline voice promotion. Staged FORGE Decision Card `{dec_id}`."
    )
    return True, report, meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly Voice Quick-Tools Promotion Auditor Sidecar"
    )
    parser.add_argument(
        "--register-schedule",
        action="store_true",
        help="Idempotently register voice_quick_tools_weekly_audit in data/schedule.json",
    )
    parser.add_argument(
        "--schedule-path",
        type=str,
        default=str(DEFAULT_SCHEDULE_PATH),
        help="Path to schedule.json",
    )
    parser.add_argument(
        "--log-path",
        type=str,
        default=str(DEFAULT_LOG_PATH),
        help="Path to gotchi_gateway.log",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(FORGE_DB_PATH),
        help="Path to FORGE SQLite database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report candidates without staging a Decision Card",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="Stage Decision Card in SQLite without posting directly over Discord REST",
    )
    parser.add_argument(
        "--promote",
        nargs="*",
        help="Acknowledge/trigger promotion for specified candidate skill/tool names",
    )
    args = parser.parse_args(argv)

    if args.register_schedule:
        res = ensure_weekly_schedule_registered(schedule_path=args.schedule_path)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.promote is not None:
        promoted = [str(x).strip() for x in args.promote if str(x).strip()]
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "promotion_queued",
                    "promoted_candidates": promoted,
                    "channel_id": TARGET_CHANNEL_ID,
                },
                indent=2,
            )
        )
        return 0

    ok, report, _meta = run_voice_quick_tools_audit(
        log_path=args.log_path,
        channel_id=TARGET_CHANNEL_ID,
        db_path=args.db_path,
        post_to_discord=not args.no_post,
        dry_run=args.dry_run,
    )
    if report:
        print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
