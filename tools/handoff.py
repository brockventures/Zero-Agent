#!/usr/bin/env python3
"""
Agent Handoff Envelope Tool for Multi-Agent Collaboration (v0 spec per Amos).
Parses inbound handoff blocks and emits standardized coordination envelopes.
"""
import re, json, sys

def parse_envelope(text: str) -> dict | None:
    """Extract and parse the ```handoff ... ``` JSON envelope from message text."""
    m = re.search(r"```handoff\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None

def format_envelope(kind: str = "answer", reply: str = "optional", subject: str = "", evidence: list = None, supersedes: str = None, spoiler: bool = False) -> str:
    """Generate a standard fenced handoff JSON block."""
    payload = {
        "v": 0,
        "kind": kind,
        "reply": reply,  # "required" | "optional" | "none"
        "subject": subject,
        "evidence": evidence or [],
        "supersedes": supersedes
    }
    block = f"```handoff\n{json.dumps(payload, indent=2)}\n```"
    if spoiler:
        return f"||{block}||"
    return block

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "format":
        subj = sys.argv[2] if len(sys.argv) > 2 else "peer coordination"
        rep = sys.argv[3] if len(sys.argv) > 3 else "optional"
        print(format_envelope(kind="answer", reply=rep, subject=subj))
    else:
        # Self-test parse
        sample = """Here is the fix.
```handoff
{
  "v": 0,
  "kind": "answer",
  "reply": "none",
  "subject": "verified fix",
  "evidence": [{"src": "bridge.py", "note": "added addressing discipline"}],
  "supersedes": null
}
```
Let me know."""
        parsed = parse_envelope(sample)
        print("Parsed reply intent:", parsed.get("reply") if parsed else "No envelope")
