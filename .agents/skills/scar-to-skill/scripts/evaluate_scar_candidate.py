#!/usr/bin/env python3
"""Evaluate candidate scar against strict anti-slop admission rubric."""
import argparse, sys

def evaluate(title: str, recurrence: int, has_automation: bool, blast_radius: str) -> dict:
    score = 0
    reasons = []
    
    # 1. Recurrence score (max 35)
    if recurrence >= 4:
        score += 35
        reasons.append("✅ High recurrence frequency (+35)")
    elif recurrence >= 3:
        score += 25
        reasons.append("✅ Moderate recurrence frequency (+25)")
    elif recurrence >= 2:
        score += 10
        reasons.append("⚠️ Low recurrence (+10)")
    else:
        reasons.append("❌ One-off occurrence (0)")

    # 2. Automation potential (max 35)
    if has_automation:
        score += 35
        reasons.append("✅ Deterministic executable tooling provided (+35)")
    else:
        reasons.append("❌ No executable scripts provided — belongs in memory/ (0)")

    # 3. Blast radius (max 30)
    br_upper = blast_radius.upper()
    if br_upper in ("CRITICAL", "HIGH"):
        score += 30
        reasons.append(f"✅ {blast_radius} blast radius / high outage cost (+30)")
    elif br_upper == "MEDIUM":
        score += 15
        reasons.append("⚠️ Medium blast radius (+15)")
    else:
        reasons.append("⚠️ Low impact (+5)")
        score += 5

    approved = score >= 75
    print("=" * 60)
    print(f"🔍 SCAR-TO-SKILL ADMISSION EVALUATION: '{title}'")
    print("=" * 60)
    print(f"• Overall Qualification Score: {score}/100")
    print(f"• Status: {'🎉 APPROVED FOR SKILL CREATION' if approved else '🚫 REJECTED (Store in /workspace/memory/ instead)'}")
    print("\nDetailed Breakdown:")
    for r in reasons:
        print(f"  {r}")
    print("=" * 60)
    return {"score": score, "approved": approved}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True, help="Scar title")
    parser.add_argument("--recurrence", type=int, default=1, help="Expected occurrence frequency")
    parser.add_argument("--has_automation", action="store_true", help="Whether deterministic scripts exist")
    parser.add_argument("--blast_radius", choices=["Low", "Medium", "High", "Critical"], default="Medium")
    args = parser.parse_args()
    res = evaluate(args.title, args.recurrence, args.has_automation, args.blast_radius)
    sys.exit(0 if res["approved"] else 1)
