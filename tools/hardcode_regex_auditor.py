#!/usr/bin/env python3
"""Hardcoded Rule & Brittle Regex Auditor for Zero (Ivy-AG).

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
from typing import Any, Dict, List

DEFAULT_TARGET_DIR = Path("/workspace/tools")

# Regex rules for scanning source code
PATTERNS = [
    {
        "id": "WORD_COUNT_HEURISTIC",
        "category": "Heuristic Filter",
        "severity": "HIGH",
        "regex": re.compile(r"len\([^)]+\.split\(\)\)\s*(?:<=?|<)\s*\d+"),
        "description": "Arbitrary word-count prefilter may drop valid engineering directives (e.g. 'run tests', 'ship it').",
        "rec_type": "LLM Reasoning / Intent Routing",
        "recommendation": "Bypass prefilter for actionable intent directives or route directly to model classification."
    },
    {
        "id": "UNANCHORED_FLOAT_REGEX",
        "category": "Fragile Regex",
        "severity": "HIGH",
        "custom_check": lambda line: (
            re.search(r"r?[\"'][^\"']*(?<!\d\+\.)\\d\+\\\.\\d\+[^\"']*[\"']", line)
            and not re.search(r"\\d\+\\\.\\d\+\\\.\\d\+", line)  # Ignore IP addresses
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


def format_audit_report(findings: List[Dict[str, Any]], target_path: Path) -> str:
    """Format findings into a high-signal markdown audit report."""
    if not findings:
        return (
            f"🔍 **Hardcoded Rule & Brittle Regex Audit** (`{target_path}`)\n"
            f"✅ **Zero Fragile Heuristics Detected.** Codebase adheres to dynamic state and LLM intent routing protocols."
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
