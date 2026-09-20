---
name: consultation-panel
description: >-
  Use this skill whenever deciding on complex, ambiguous, or high-stakes architectural choices, trade-offs, refactors, or security/infrastructure crossroads.
  Dynamically synthesizes and launches a multi-perspective panel of specialized adversarial subagents tailored to the problem domain, conducts rigorous debate, and reconciles the perspectives into a decisive consensus recommendation.
---

# 🏛️ Consultation & Deliberation Panel Skill

This skill governs assembling and orchestrating an on-demand, multi-perspective **adversarial consultation panel** using concurrent subagents (`invoke_subagent`). It stress-tests difficult architectural choices, identifies hidden edge cases, and delivers a battle-tested consensus recommendation before code is written.

---

## 🎯 When to Use This Skill

Activate this skill when:
1. **High-Risk Architectural Decisions:** Database migrations, auth protocol overhauls, reverse proxy and ingress topologies, or air-gap security boundaries.
2. **Complex Trade-off Crossroads:** Situations with multiple viable approaches where each path carries distinct latency, complexity, maintenance, or security implications.
3. **Multi-Agent Protocol Design:** Designing consensus rules, lock/mutex mechanisms, turn lifecycles, or message queue topologies (e.g. Crab Cavern, Banana protocol).
4. **Refactoring vs. Rewriting:** Evaluating whether to refactor a brittle system in-place or rebuild with a new paradigm.

---

## 🧩 Dynamic Persona Synthesis Framework

Rather than relying on static, generic personas, the panel **dynamically generates 2 to 4 specialized adversarial roles** specifically tailored to the fault lines of the task.

### 1. Persona Domain Generation Catalog

| Problem Domain | Dynamically Synthesized Personas | Primary Mandate & Stance |
|---|---|---|
| **Homelab & Network Infrastructure** *(VLANs, DNS, Reverse Proxies)* | • **Network Security & Air-Gap Auditor**<br>• **Operational Simplicity & Latency Hawk**<br>• **Homelab Client UX & Discovery Advocate** | Attack attack surfaces, WAN leaks, and firewall holes.<br>Fight multi-hop routing latency, double-NAT, and debugging friction.<br>Protect mobile roaming, mDNS/SSDP discovery, and household UX. |
| **Multi-Agent Coordination & Protocols** *(Crab Cavern, Agora, Mutex)* | • **Game-Theoretic & Consensus Strategist**<br>• **Resource & Token Cost Optimizer**<br>• **Protocol Determinism & Deadlock Engineer** | Model Byzantine actors, starvation, race conditions, and incentives.<br>Minimize context window blowup, polling waste, and invocation costs.<br>Enforce strict state machine idempotency, atomic locks, and recovery. |
| **Software Architecture & API Design** *(Async rewrites, SDKs, Tooling)* | • **Concurrency & Performance Hawk**<br>• **Clean Architecture & Maintainability Lead**<br>• **Failure-Mode & Chaos Red Teamer** | Audit event loop starvation, thread safety, GIL contention, and memory leaks.<br>Enforce API ergonomics, type safety, modularity, and low cognitive load.<br>Formulate destructive failure tests, network resets, and corrupted inputs. |
| **Data Lifecycle & Datastores** *(SQLite, Postgres, Cache, Search)* | • **Data Integrity & ACID Purist**<br>• **Minimalist Deployment Engineer**<br>• **Query Latency & I/O Optimizer** | Protect transactional consistency, write amplification, and backup safety.<br>Fight operational complexity and stateful container sprawl on NAS storage.<br>Optimize index cardinality, FTS5 retrieval latency, and controller queue depth. |

---

## 🛠️ Step-by-Step Execution Runbook

### Step 1: Problem Decomposition & Tension Identification
Clearly formulate the decision problem and extract the 2–3 core technical tensions (e.g., *Security Isolation vs. Local Discovery Friction*, or *In-Memory Caching Speed vs. Distributed Consistency*).

### Step 2: Concurrent Subagent Dispatch
Dispatch all panel members in a **single tool turn** using `invoke_subagent`.

```json
{
  "Subagents": [
    {
      "TypeName": "research",
      "Role": "Security & Air-Gap Auditor",
      "Model": "flash",
      "Prompt": "You are the Security & Air-Gap Auditor on an architectural consultation panel.\n\nEVALUATE THIS PROPOSAL:\n<Proposal details>\n\nYOUR MANDATE:\n1. Attack the proposal from a pure security, threat-modeling, and network isolation perspective.\n2. Identify specific exploit vectors, configuration leakage, or unauthenticated surface areas.\n3. Propose mandatory security invariants if this path is taken.\n4. Deliver a concise verdict (Strong Approve / Approve with Constraints / Reject) with concrete technical justification."
    },
    {
      "TypeName": "research",
      "Role": "Operational Simplicity & Latency Hawk",
      "Model": "flash",
      "Prompt": "You are the Operational Simplicity & Latency Hawk on an architectural consultation panel.\n\nEVALUATE THIS PROPOSAL:\n<Proposal details>\n\nYOUR MANDATE:\n1. Attack the proposal from a simplicity, maintainability, latency, and debuggability perspective.\n2. Identify unnecessary operational complexity, failure propagation, or ongoing maintenance tax.\n3. Challenge over-engineering and provide the cleanest minimal alternative.\n4. Deliver a concise verdict with concrete technical justification."
    }
  ]
}
```

### Step 3: Adversarial Review & Cross-Examination
Collect subagent evaluations silently. Compare their arguments, identify where one persona successfully exposes a flaw in another's assumption, and extract unhandled edge cases.

### Step 4: Executive Consensus & Decision Matrix Synthesis
Synthesize the debate into a single, cohesive deliverable. Never dump raw disconnected outputs.

Deliver:
1. **Decision Matrix:** Structured comparison of options across Key Criteria (Performance, Security, Maintenance Complexity, Blast Radius / Reversibility).
2. **Key Tensions Reconciled:** How the opposing perspectives were balanced without falling into false compromises.
3. **The Unshakeable Recommendation:** Decisive verdict with concrete next steps and required guardrails.

---

## 🔒 Operational Invariants

1. **Batch Dispatch Invariant:** All panel personas MUST be invoked in parallel within a single `invoke_subagent` call to minimize roundtrip latency (~6-8s total). Never spawn them serially.
2. **No Strawman Arguments:** Every persona must be prompted to formulate the strongest possible technical case for their perspective (steelmanning).
3. **Actionable Closure:** Every panel run must conclude with a clear, definitive recommendation. Never end in an ambiguous "it depends" stalemate.
