#!/usr/bin/env python3
"""Hardcoded Rule & Brittle Regex Auditor for Zero.

Forensically scans Python scripts, sidecars, and tools for:
1. Brittle heuristics (e.g. word count thresholds `len(split()) < 4`).
2. Fragile regexes (e.g. unanchored numeric floats, generic tag strippers `<[^>]+>`).
3. Static state and frozen agendas (e.g. hardcoded templates, hardcoded identities in branches).
4. Keyword collisions in routing/titling logic.

Provides actionable architectural recommendations:
- Where to replace heuristics with LLM Reasoning (semantic intent, classification, summarization).
- Where to replace brittle patterns with Robust Deterministic Logic (AST parsing, anchored boundaries, dynamic state queries).
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TARGET_DIR = Path("/workspace/tools")

# Regex rules for scanning source code
PATTERNS = [
    {
        "id": "WORD_COUNT_HEURISTIC",
        "category": "Heuristic Filter",
        "severity": "HIGH",
        "regex": re.compile(r"len\([^)]+\.split\(\)\)\s*(?:<=?|<)\s*\d+"),
        "custom_check": lambda line: (
            bool(re.search(r"len\([^)]+\.split\(\)\)\s*(?:<=?|<)\s*\d+", line))
            and "is_directive" not in line
            and "is_coref" not in line
            and "is_short" not in line
            and "l_strip" not in line
        ),
        "description": "Arbitrary word-count prefilter may drop valid engineering directives (e.g. 'run tests', 'ship it').",
        "rec_type": "LLM Reasoning / Intent Routing",
        "recommendation": "Bypass prefilter for actionable intent directives or route directly to model classification."
    },
    {
        "id": "UNANCHORED_FLOAT_REGEX",
        "category": "Fragile Regex",
        "severity": "HIGH",
        "custom_check": lambda line: (
            bool(re.search(r"r?[\"'][^\"']*(?<!\d\+\.)\\d\+\\\.\\d\+[^\"']*[\"']", line))
            and not re.search(r"\\d\+\\\.\\d\+\\\.\\d\+", line)  # Ignore IP addresses
            and not re.search(r"\\d\+\\\.\\d\+\(\?:\\\.\\d\+\)\*", line)  # Ignore semver extractors
            and not re.search(r"\d+\.\d+\.\d+", line)
        ),
        "description": "Unanchored float/decimal regex may collide with token counts or memory metadata instead of confidence scores.",
        "rec_type": "Robust Deterministic Logic",
        "recommendation": "Anchor score patterns to explicit keys (e.g. r'(?:Score|Relevance):\s*([0-1](?:\.\d+)?)') and word boundaries."
    },
    {
        "id": "NAIVE_TAG_STRIPPER",
        "category": "Fragile Regex",
        "severity": "MEDIUM",
        "regex": re.compile(r"r?[\"']<\[\^>\]\+>[\"']"),
        "description": "Generic HTML tag stripping regex drops generic types (Vector<T>, Map<K,V>) and math inequalities.",
        "rec_type": "Robust Deterministic Logic",
        "recommendation": "Use an explicit tag whitelist or HTML parser (e.g. BeautifulSoup / targeted regex with tag names)."
    },
    {
        "id": "HARDCODED_YEAR_TEMPORAL",
        "category": "Temporal Hardcoding",
        "severity": "MEDIUM",
        "regex": re.compile(r"[\"']202[0-5]-[0-1]\d-[0-3]\d[\"']"),
        "description": "Hardcoded past year date string in active execution paths.",
        "rec_type": "Robust Deterministic Logic",
        "recommendation": "Derive reference dates dynamically from system/Pacific time (datetime.now(PT))."
    },
    {
        "id": "HARDCODED_NAME_BRANCHING",
        "category": "Hardcoded Identity",
        "severity": "MEDIUM",
        "regex": re.compile(r"(?:if|elif)\s+.*?(?:==|in)\s*\[?[\"'](?:Ryan|Amos|Alice|Bob)[\"']"),
        "description": "Hardcoded person identity in conditional routing instead of dynamic metadata lookup.",
        "rec_type": "Robust Deterministic Logic / Dynamic Query",
        "recommendation": "Query user metadata, relationship graphs, or contacts dynamically."
    },
    {
        "id": "STATIC_FALLBACK_AGENDA",
        "category": "Frozen Template",
        "severity": "MEDIUM",
        "regex": re.compile(r"[\"'].*?Amos\s*—\s*Ledger DDL.*?[\"']"),
        "description": "Frozen template/mock milestone string detected in codebase.",
        "rec_type": "LLM Reasoning / Dynamic State",
        "recommendation": "Synthesize standing agenda and milestones dynamically from git commits and PR states."
    }
]


class CodebaseAuditor:
    """Scans Python files for anti-patterns and generates forensic findings."""

    def __init__(self, target_dir: Path):
        self.target_dir = target_dir

    def scan_file(self, file_path: Path) -> List[Dict[str, Any]]:
        findings = []
        if not file_path.is_file() or file_path.suffix != ".py":
            return findings

        # Skip regression test files and self
        if "test_" in file_path.name or file_path.name == "hardcode_regex_auditor.py":
            return findings

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return findings

        lines = content.splitlines()

        for idx, line in enumerate(lines, start=1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            for rule in PATTERNS:
                matched = False
                if "custom_check" in rule:
                    matched = bool(rule["custom_check"](line))
                elif "regex" in rule:
                    matched = bool(rule["regex"].search(line))

                if matched:
                    findings.append({
                        "file": str(file_path),
                        "filename": file_path.name,
                        "line": idx,
                        "content": stripped[:120],
                        "rule_id": rule["id"],
                        "category": rule["category"],
                        "severity": rule["severity"],
                        "description": rule["description"],
                        "rec_type": rule["rec_type"],
                        "recommendation": rule["recommendation"]
                    })

        return findings

    def run_audit(self) -> List[Dict[str, Any]]:
        all_findings = []
        if self.target_dir.is_file():
            all_findings.extend(self.scan_file(self.target_dir))
        elif self.target_dir.is_dir():
            for p in sorted(self.target_dir.rglob("*.py")):
                all_findings.extend(self.scan_file(p))
        return all_findings


def run_llm_semantic_audit(target_path: Path, static_findings: List[Dict[str, Any]]) -> str:
    """Invoke Gemini 4 Argon (High) to perform semantic architecture review over tools/sidecars."""
    try:
        from tools.llm_sidecar import call_sidecar_llm, get_sidecar_model
        model_id = get_sidecar_model("monthly_hardcode_regex_audit", "gemini-4-argon-high")
        py_files = sorted(target_path.glob("*.py")) if target_path.is_dir() else [target_path]
        # Gather compact AST / heuristic signatures across recently modified scripts
        py_files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        summaries = []
        for p in py_files[:15]:
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                elif_chains = len(re.findall(r"^\s*elif\s+", src, flags=re.M))
                regex_compiles = len(re.findall(r"re\.(?:compile|search|match|sub)\(", src))
                if elif_chains >= 4 or regex_compiles >= 5:
                    summaries.append({
                        "file": p.name,
                        "lines": len(src.splitlines()),
                        "elif_branches": elif_chains,
                        "regex_calls": regex_compiles,
                    })
            except Exception:
                pass

        prompt = (
            f"You are Zero's Staff Architecture Auditor ({model_id}) conducting the Monthly Hardcoded Rule & Brittle Regex Audit.\n\n"
            f"Static Regex Findings ({len(static_findings)}):\n{json.dumps(static_findings[:10], indent=2)}\n\n"
            f"High-Branching / Regex-Dense Modules in `{target_path}`:\n{json.dumps(summaries, indent=2)}\n\n"
            "Write a concise 3-bullet architectural assessment identifying where brittle heuristics, "
            "canned templates, or fragile regexes should be replaced with dynamic state queries or LLM reasoning. "
            "Use standard `- **Label:** description` bullets without em dashes."
        )
        return call_sidecar_llm("monthly_hardcode_regex_audit", prompt, timeout=75)
    except Exception:
        return ""


def collect_module_density_summaries(target_path: Path, limit: int = 15) -> List[Dict[str, Any]]:
    """Collect AST/regex branching density stats for recently modified Python modules."""
    py_files = sorted(target_path.glob("*.py")) if target_path.is_dir() else [target_path]
    py_files = [p for p in py_files if "test_" not in p.name and p.name != "hardcode_regex_auditor.py"]
    py_files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    summaries: List[Dict[str, Any]] = []
    for p in py_files[:limit]:
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            elif_chains = len(re.findall(r"^\s*elif\s+", src, flags=re.M))
            regex_compiles = len(re.findall(r"re\.(?:compile|search|match|sub)\(", src))
            if elif_chains >= 4 or regex_compiles >= 5:
                summaries.append({
                    "file": p.name,
                    "path": f"tools/{p.name}",
                    "lines": len(src.splitlines()),
                    "elif_branches": elif_chains,
                    "regex_calls": regex_compiles,
                })
        except Exception:
            pass
    return summaries


def build_audit_forge_plan(
    findings: List[Dict[str, Any]],
    summaries: List[Dict[str, Any]],
    origin_channel_id: int = 1544953279664889888,
) -> Dict[str, Any]:
    """Synthesize a concrete multi-chunk ForgePlan JSON from static findings and high-density modules."""
    chunks: List[Dict[str, Any]] = []
    seen_files = set()

    for f in findings[:3]:
        fname = f["filename"]
        rel_path = f"tools/{fname}"
        if rel_path in seen_files:
            continue
        seen_files.add(rel_path)
        test_file = f"tools/test_{fname}"
        target_files = [rel_path]
        if (DEFAULT_TARGET_DIR / f"test_{fname}").exists():
            target_files.append(test_file)
        chunks.append({
            "chunk_index": len(chunks),
            "title": f"Remediate {f['rule_id']} in {fname}",
            "repo": "/workspace",
            "worker_model": "gemini-4-hydrogen-high",
            "target_files": target_files,
            "target_symbols": [rel_path],
            "prohibited_files": ["tools/bridge.py"],
            "shared_context": [f"Line {f['line']}: {f['content']}"],
            "instructions": (
                f"Replace {f['rule_id']} ({f['category']}) at {rel_path}:{f['line']} per audit guidance: "
                f"{f['recommendation']} Add or update unit tests to lock in the behavior."
            ),
            "verification_cmd": f"python3 -m py_compile {rel_path}" + (f" && pytest {test_file}" if len(target_files) > 1 else ""),
        })

    for s in summaries:
        rel_path = s["path"]
        if rel_path in seen_files or len(chunks) >= 3:
            continue
        seen_files.add(rel_path)
        fname = s["file"]
        test_file = f"tools/test_{fname}"
        target_files = [rel_path]
        if (DEFAULT_TARGET_DIR / f"test_{fname}").exists():
            target_files.append(test_file)
        chunks.append({
            "chunk_index": len(chunks),
            "title": f"Simplify branching & regex density in {fname}",
            "repo": "/workspace",
            "worker_model": "gemini-4-hydrogen-high",
            "target_files": target_files,
            "target_symbols": [rel_path],
            "prohibited_files": ["tools/bridge.py"],
            "shared_context": [
                f"{fname} currently has {s['elif_branches']} elif branches and {s['regex_calls']} regex calls across {s['lines']} lines."
            ],
            "instructions": (
                f"Refactor brittle procedural elif chains or overlapping regex rules in {rel_path} into "
                "declarative dispatch tables or structured helpers without altering public function signatures."
            ),
            "verification_cmd": f"python3 -m py_compile {rel_path}" + (f" && pytest {test_file}" if len(target_files) > 1 else ""),
        })

    return {
        "title": "Hardcoded Rule & Brittle Regex Audit Remediation",
        "objective": "Eliminate fragile static heuristics, high-density regex chains, and procedural elif branching identified by the monthly architecture audit.",
        "origin_channel_id": origin_channel_id,
        "origin_thread_id": None,
        "repo": "/workspace",
        "tier": 2 if len(chunks) <= 1 else 3,
        "touches_core_bridge": False,
        "context_fact_sheet": [
            f"Static findings detected: {len(findings)}",
            f"High-density modules identified: {len(summaries)}",
        ],
        "shared_context": [
            "Preserve all existing public function signatures and unit test contracts."
        ],
        "chunks": chunks,
    }


def stage_audit_decision_card(
    findings: List[Dict[str, Any]],
    target_path: Path,
    channel_id: int = 1544953279664889888,
    post_to_discord: bool = True,
) -> Optional[str]:
    """Write the audit ForgePlan JSON to disk and stage an inline FORGE Decision Card."""
    summaries = collect_module_density_summaries(target_path)
    plan = build_audit_forge_plan(findings, summaries, origin_channel_id=channel_id)
    if not plan.get("chunks"):
        return None

    plan_dir = Path("/workspace/data/forge_plans")
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / "hardcode_regex_audit_plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    chunk_titles = ", ".join(c["title"] for c in plan["chunks"])
    summary_text = (
        f"Staged {len(plan['chunks'])}-chunk remediation plan ({chunk_titles}). "
        "Tap Approve to enqueue the refactor build to #forge."
    )
    try:
        from tools.forge import stage_decision
        decision_id = stage_decision(
            category="forge",
            title="Enqueue Hardcode & Regex Audit Remediation Plan",
            summary=summary_text,
            handler="forge_enqueue",
            payload={"plan_file": str(plan_file)},
            channel_id=channel_id,
            post_to_discord=post_to_discord,
        )
        return decision_id
    except Exception:
        return None


def format_audit_report(
    findings: List[Dict[str, Any]],
    target_path: Path,
    stage_card: bool = False,
    channel_id: int = 1544953279664889888,
) -> str:
    """Format findings into a high-signal markdown audit report with Action Plan and optional Decision Card."""
    summaries = collect_module_density_summaries(target_path)
    llm_review = run_llm_semantic_audit(target_path, findings)
    llm_block = f"\n\n### 🧠 Semantic Architecture Review (`gemini-4-argon-high`)\n{llm_review}" if llm_review else ""

    plan = build_audit_forge_plan(findings, summaries, origin_channel_id=channel_id)
    plan_lines: List[str] = []
    if plan.get("chunks"):
        plan_lines.append("\n### 📋 Proposed Action Plan")
        for idx, c in enumerate(plan["chunks"], start=1):
            tfiles = ", ".join(f"`{tf}`" for tf in c.get("target_files", []))
            plan_lines.append(f"{idx}. **{c['title']}** ({tfiles}): {c['instructions']}")

    decision_id = None
    if stage_card and plan.get("chunks"):
        decision_id = stage_audit_decision_card(findings, target_path, channel_id=channel_id, post_to_discord=True)
        if decision_id:
            plan_lines.append(f"\n✅ **Decision Card Staged (`{decision_id}`):** Tap **Approve** on the card to enqueue this plan to <#1562899941595160657>.")

    plan_block = "\n".join(plan_lines)

    if not findings:
        return (
            f"🔍 **Hardcoded Rule & Brittle Regex Audit** (`{target_path}`)\n"
            f"✅ **Zero Fragile Static Heuristics Detected.** Codebase adheres to dynamic state and LLM intent routing protocols."
            f"{llm_block}{plan_block}"
        )

    high_count = sum(1 for f in findings if f["severity"] == "HIGH")
    med_count = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low_count = sum(1 for f in findings if f["severity"] == "LOW")

    lines = [
        f"🔍 **Hardcoded Rule & Brittle Regex Audit** (`{target_path}`)",
        f"⚠️ **Findings Detected:** {len(findings)} total ({high_count} High, {med_count} Medium, {low_count} Low)\n"
    ]

    for f in findings:
        icon = "🔴" if f["severity"] == "HIGH" else "🟡"
        lines.append(f"### {icon} [{f['severity']}] `{f['filename']}:{f['line']}` — {f['category']}")
        lines.append(f"- **Pattern:** `{f['rule_id']}`")
        lines.append(f"- **Code:** `{f['content']}`")
        lines.append(f"- **Diagnostic:** {f['description']}")
        lines.append(f"- **Architectural Fix:** **{f['rec_type']}** — {f['recommendation']}\n")

    if llm_block:
        lines.append(llm_block.strip())
    if plan_block:
        lines.append(plan_block.strip())

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Audit tools and scripts for brittle hardcoding and regex.")
    parser.add_argument("--path", type=Path, default=DEFAULT_TARGET_DIR, help="Path to audit (file or directory).")
    parser.add_argument("--json", action="store_true", help="Output findings in JSON format.")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output if no high/medium findings.")
    parser.add_argument("--severity", choices=["HIGH", "MEDIUM", "LOW"], default="LOW", help="Minimum severity threshold.")

    args = parser.parse_args()

    auditor = CodebaseAuditor(args.path)
    findings = auditor.run_audit()

    # Filter severity
    sev_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    min_rank = sev_rank[args.severity]
    filtered_findings = [f for f in findings if sev_rank.get(f["severity"], 1) >= min_rank]

    if args.quiet and not filtered_findings:
        return 0

    if args.json:
        print(json.dumps(filtered_findings, indent=2))
    else:
        print(format_audit_report(filtered_findings, args.path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
