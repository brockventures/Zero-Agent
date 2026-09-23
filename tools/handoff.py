#!/usr/bin/env python3
"""
Agent Handoff Envelope Tool for Multi-Agent Collaboration (v0/v1 spec per Amos).
Parses inbound handoff blocks and emits standardized coordination envelopes.
Ensures physical Discord snowflake mentions are attached to egress handoffs so sleeping bots wake up.
"""
import re
import json
import sys

TARGET_MENTIONS = {
    "amos": "<@1468012353206354197>",
    "marvin": "<@1492043459618537492>",
    "aerial": "<@1542035925603713086>",
    "zero": "<@1542285964213358633>",
    "robot": "<@&1543462881624858624>",
    "robots": "<@&1543462881624858624>",
    "team": "<@&1543462881624858624>",
    "zero_role": "<@&1543285916506783799>",
    "mike": "<@93420059858305024>",
    "arbiter": "<@93420059858305024>",
    "ian": "<@453030589914939393>",
    "themoonproblem": "<@453030589914939393>",
    "moon problem": "<@453030589914939393>",
    "alex": "<@169260920550195200>",
    "arcane": "<@169260920550195200>",
}


def parse_envelope(text: str) -> dict | None:
    """Extract and parse the ```handoff ... ``` or code-fenced JSON envelope from message text."""
    if not text:
        return None
    m = re.search(r"```(?:handoff)?\s*\n?(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def format_envelope(
    kind: str = "answer",
    reply: str = "optional",
    subject: str = "",
    to: str | list | None = None,
    floor: str = "open",
    scope: str = "channel",
    evidence: list | None = None,
    supersedes: str | None = None,
    spoiler: bool = False,
    include_mention: bool = True,
    v: int = 1,
) -> str:
    """Generate a standard fenced handoff JSON block with optional physical Discord mention."""
    payload = {
        "v": v,
        "kind": kind,
        "reply": reply,  # "required" | "optional" | "none"
        "subject": subject,
    }
    if to:
        payload["to"] = to
    if floor:
        payload["floor"] = floor
    if scope:
        payload["scope"] = scope
    if evidence is not None:
        payload["evidence"] = evidence
    if supersedes is not None:
        payload["supersedes"] = supersedes

    block = f"```handoff\n{json.dumps(payload, indent=2)}\n```"
    if spoiler:
        block = f"||{block}||"

    if include_mention and to and str(reply).lower().strip() != "none":
        targets = [to] if isinstance(to, str) else list(to)
        mentions = []
        for t in targets:
            for piece in (t.split(",") if isinstance(t, str) else [t]):
                clean = piece.strip().lower()
                if clean != "zero" and clean in TARGET_MENTIONS:
                    m_tag = TARGET_MENTIONS[clean]
                    if m_tag not in mentions:
                        mentions.append(m_tag)
        if mentions:
            return f"{' '.join(mentions)}\n{block}"

    return block




def convert_bare_peer_mentions(text: str) -> str:
    """Convert bare text mentions like @Amos, @Marvin, @Aerial to Discord snowflakes,
    ignoring matches inside markdown code blocks or inline code."""
    if not text or "@" not in text:
        return text
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    pattern = re.compile(r"(?<![\w/])@([a-zA-Z_]+)\b")
    for i in range(0, len(parts), 2):
        def repl(m):
            key = m.group(1).lower()
            if key in TARGET_MENTIONS:
                return TARGET_MENTIONS[key]
            return m.group(0)
        parts[i] = pattern.sub(repl, parts[i])
    return "".join(parts)


def ensure_handoff_mentions(text: str) -> str:
    """Ensure outgoing messages containing handoff envelopes include physical Discord mentions.

    Discord bots ignore ambient messages unless physically pinged (<@ID> or <@&ROLE_ID>).
    If an outgoing turn includes a handoff envelope targeting a peer bot or team with
    reply != 'none', and the physical mention is not already in the message, prepend it.
    """
    if not text:
        return text

    env = parse_envelope(text)
    if not env:
        return text

    to_val = env.get("to") or env.get("target")
    reply_val = str(env.get("reply", "optional")).lower().strip()

    if not to_val or reply_val == "none":
        return text

    targets = [to_val] if isinstance(to_val, str) else list(to_val)
    needed_mentions = []

    for t in targets:
        for piece in (t.split(",") if isinstance(t, str) else [t]):
            clean = piece.strip().lower()
            if clean == "zero":
                continue
            mention = TARGET_MENTIONS.get(clean)
            if not mention:
                continue

            m_id = re.search(r"\d+", mention)
            if m_id:
                snowflake = m_id.group(0)
                if re.search(rf"<@&?!?{snowflake}>", text):
                    continue  # Already physically mentioned

            if mention not in needed_mentions:
                needed_mentions.append(mention)

    if not needed_mentions:
        return text

    mention_prefix = " ".join(needed_mentions)
    return f"{mention_prefix}\n{text}"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "format":
        subj = sys.argv[2] if len(sys.argv) > 2 else "peer coordination"
        rep = sys.argv[3] if len(sys.argv) > 3 else "optional"
        to_arg = sys.argv[4] if len(sys.argv) > 4 else None
        print(format_envelope(kind="answer", reply=rep, subject=subj, to=to_arg))
    else:
        sample = """Here is the fix.
```handoff
{
  "v": 1,
  "kind": "answer",
  "reply": "required",
  "to": "amos",
  "subject": "verified fix",
  "evidence": [{"src": "bridge.py", "note": "added addressing discipline"}],
  "supersedes": null
}
```
Let me know."""
        parsed = parse_envelope(sample)
        print("Parsed reply intent:", parsed.get("reply") if parsed else "No envelope")
        print("\nEnsure mentions test:\n" + ensure_handoff_mentions(sample))
