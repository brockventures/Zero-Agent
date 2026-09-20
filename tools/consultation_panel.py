#!/usr/bin/env python3
"""Consultation & Deliberation Panel Orchestration Tool for Zero.

Dynamically synthesizes domain-tailored adversarial personas and formats
multi-subagent deliberation runs for high-stakes architectural decisions.
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional


CORE_DOMAINS = {
    "network_infrastructure": [
        {
            "role": "Network Security & Air-Gap Auditor",
            "mandate": "Attack attack surfaces, VLAN egress leaks, unauthenticated ports, and firewall holes. Enforce strict zero-trust isolation.",
        },
        {
            "role": "Operational Simplicity & Latency Hawk",
            "mandate": "Fight multi-hop routing latency, double-NAT, fragile proxy cascades, and operational maintenance overhead. Champion minimal viable architecture.",
        },
        {
            "role": "Homelab UX & Discovery Advocate",
            "mandate": "Evaluate mobile roaming, mDNS/SSDP discovery, Home Assistant latency, and client usability.",
        },
    ],
    "multi_agent_protocols": [
        {
            "role": "Game-Theoretic & Consensus Strategist",
            "mandate": "Model Byzantine actors, incentive compatibility, starvation, deadlocks, and multi-agent game-theory dynamics.",
        },
        {
            "role": "Resource & Token Cost Optimizer",
            "mandate": "Analyze context window blowup, polling waste, API rate limits, and subagent invocation overhead.",
        },
        {
            "role": "Protocol Determinism & State Machine Engineer",
            "mandate": "Enforce idempotent transitions, race condition immunity, atomic lock lifecycles, and deterministic message delivery.",
        },
    ],
    "software_architecture": [
        {
            "role": "Concurrency & Performance Hawk",
            "mandate": "Audit event loop blocking, thread safety, memory leak vectors, GIL locks, and CPU cache efficiency.",
        },
        {
            "role": "Clean Architecture & Maintainability Lead",
            "mandate": "Enforce API ergonomics, separation of concerns, testability, type safety, and low cognitive overhead.",
        },
        {
            "role": "Failure-Mode & Chaos Red Teamer",
            "mandate": "Formulate destructive failure tests, network dropouts, corrupted state recovery, and cascading timeout handling.",
        },
    ],
    "data_lifecycle": [
        {
            "role": "Data Integrity & ACID Purist",
            "mandate": "Protect transactional boundaries, write amplification, data corruption risks, and point-in-time recovery.",
        },
        {
            "role": "Minimalist Deployment Engineer",
            "mandate": "Evaluate container sprawl, RAM/disk footprint on Synology NAS, and ongoing backup/maintenance friction.",
        },
        {
            "role": "Query Latency & I/O Optimizer",
            "mandate": "Optimize index cardinality, SQLite FTS5 vs Postgres BM25 retrieval latency, and NAS disk controller queue depth.",
        },
    ],
}


def detect_domain(topic: str) -> str:
    """Infer the most relevant technical domain from a topic description."""
    t = topic.lower()
    if any(k in t for k in ["vlan", "subnet", "firewall", "router", "gateway", "unifi", "dns", "nginx", "proxy", "port"]):
        return "network_infrastructure"
    elif any(k in t for k in ["agent", "consensus", "crab cavern", "banana", "turn", "channel", "classifier", "subagent", "coordination"]):
        return "multi_agent_protocols"
    elif any(k in t for k in ["db", "database", "sqlite", "postgres", "sql", "migration", "schema", "table", "index", "storage"]):
        return "data_lifecycle"
    else:
        return "software_architecture"


def generate_panel_specs(topic: str, domain: Optional[str] = None, model: str = "flash") -> List[Dict[str, Any]]:
    """Dynamically generate tailored subagent specs for a deliberation panel."""
    chosen_domain = domain if domain in CORE_DOMAINS else detect_domain(topic)
    archetypes = CORE_DOMAINS.get(chosen_domain, CORE_DOMAINS["software_architecture"])

    specs = []
    for arch in archetypes:
        prompt = (
            f"You are the **{arch['role']}** on an architectural consultation panel.\n\n"
            f"TECHNICAL PROPOSAL / DECISION AT HAND:\n{topic}\n\n"
            f"YOUR SPECIFIC MANDATE:\n{arch['mandate']}\n\n"
            f"REQUIRED OUTPUT STRUCTURE:\n"
            f"1. **Core Assessment:** Strong Approve / Approve with Constraints / Reject.\n"
            f"2. **Critical Failure Modes & Risks:** The exact technical vulnerabilities or maintenance traps you see.\n"
            f"3. **Proposed Alternative / Required Invariants:** Concrete architectural requirements if this path is taken.\n"
            f"4. **Bottom-Line Technical Take:** 2-3 sentence punchy summary of your stance."
        )
        specs.append({
            "TypeName": "research",
            "Role": arch["role"],
            "Model": model,
            "Prompt": prompt,
        })
    return specs


def format_decision_matrix(
    options: List[str],
    evaluations: Dict[str, Dict[str, Any]],
) -> str:
    """Format a clean markdown decision matrix from evaluated options."""
    lines = [
        "### 📊 Architectural Decision Matrix",
        "",
        "| Option | Performance & Latency | Security & Blast Radius | Maintenance Complexity | Consensus Rating |",
        "|---|---|---|---|---|",
    ]
    for opt in options:
        ev = evaluations.get(opt, {})
        perf = ev.get("performance", "N/A")
        sec = ev.get("security", "N/A")
        maint = ev.get("maintenance", "N/A")
        score = ev.get("rating", "N/A")
        lines.append(f"| **{opt}** | {perf} | {sec} | {maint} | **{score}** |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Zero Consultation Panel Tool")
    subparsers = parser.add_subparsers(dest="command")

    p_plan = subparsers.add_parser("plan", help="Generate subagent panel dispatch plan for a topic")
    p_plan.add_argument("topic", help="The architectural decision or technical question")
    p_plan.add_argument("--domain", choices=list(CORE_DOMAINS.keys()), help="Force specific domain")
    p_plan.add_argument("--model", default="flash", choices=["flash", "pro"], help="Subagent model tier")
    p_plan.add_argument("--json", action="store_true", help="Output raw JSON for invoke_subagent")

    args = parser.parse_args()

    if args.command == "plan":
        specs = generate_panel_specs(args.topic, domain=args.domain, model=args.model)
        if args.json:
            print(json.dumps({"Subagents": specs}, indent=2))
        else:
            print(f"🏛️ Consultation Panel Plan for: '{args.topic}'")
            print(f"Domain Detected: {args.domain or detect_domain(args.topic)}")
            print(f"Synthesized Personas ({len(specs)}):")
            for s in specs:
                print(f"  • [{s['Model'].upper()}] {s['Role']}")
            print("\nReady for single-turn batch dispatch via `invoke_subagent`.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
