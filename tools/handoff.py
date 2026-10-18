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
    "ryan": "<@179407724335988736>",
}

CANONICAL_SNOWFLAKES = {
    "1468012353206354197": "<@1468012353206354197>",  # Amos
    "1492043459618537492": "<@1492043459618537492>",  # Marvin
    "1542035925603713086": "<@1542035925603713086>",  # Aerial
    "1542285964213358633": "<@1542285964213358633>",  # Zero
    "1543462881624858624": "<@&1543462881624858624>", # Team/Robot
    "1543285916506783799": "<@&1543285916506783799>", # Zero Role
    "93420059858305024": "<@93420059858305024>",      # Mike
    "453030589914939393": "<@453030589914939393>",    # Dr. Coley
    "169260920550195200": "<@169260920550195200>",    # Alex
    "179407724335988736": "<@179407724335988736>",    # Ryan
    "1541205716948353074": "<@1541205716948353074>",  # Ivy
}


def _edit_distance_le_2(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two digit strings, capped at 3."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        new_dp = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            new_dp.append(min(new_dp[-1] + 1, dp[j] + 1, dp[j - 1] + cost))
        dp = new_dp
        if min(dp) > 2:
            return 3
    return dp[-1]


def repair_corrupted_snowflakes(text: str) -> str:
    """Detect and repair LLM-corrupted Discord snowflake tags (<@ID> or <@&ID>)
    that are within edit distance <= 2 of a canonical Crab Cavern / Brock Guild snowflake.
    Prevents token-generation digit stuttering from rendering as @unknown-user on Discord."""
    if not text or "<@" not in text:
        return text
    from tools.bridge_safety import mask_code_spans, restore_code_spans

    masked, spans = mask_code_spans(text, prefix="\x00SNOWFLAKE_SPAN_")

    def _fix_match(m: re.Match) -> str:
        prefix = m.group(1)  # '<@', '<@!', or '<@&'
        digits = m.group(2)
        if digits in CANONICAL_SNOWFLAKES:
            return m.group(0)
        best_id = None
        best_dist = 3
        for canon_id in CANONICAL_SNOWFLAKES:
            dist = _edit_distance_le_2(digits, canon_id)
            if dist < best_dist:
                best_dist = dist
                best_id = canon_id
        if best_id is not None and best_dist <= 2:
            if "&" in prefix:
                return f"<@&{best_id}>"
            return CANONICAL_SNOWFLAKES[best_id]
        return m.group(0)

    masked = re.sub(r"(<@!?&?)(\d{15,22})>", _fix_match, masked)
    return restore_code_spans(masked, spans, prefix="\x00SNOWFLAKE_SPAN_")



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
    v: int | float = 1.1,
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




PROSE_SKIP_MENTIONS = {"robot", "robots", "team", "zero", "zero_role"}


def convert_bare_peer_mentions(text: str) -> str:
    """Convert bare text mentions like @Amos, @Marvin, @Aerial to Discord snowflakes,
    ignoring matches inside markdown code blocks or inline code, and never converting
    group role tags (@robot, @team) or self-mentions (@zero) in free-form prose."""
    if not text or "@" not in text:
        return text
    from tools.bridge_safety import mask_code_spans, restore_code_spans

    masked, spans = mask_code_spans(text, prefix="\x00HANDOFF_SPAN_")
    pattern = re.compile(r"(?<![\w/])@([a-zA-Z_]+)\b")

    def repl(m):
        key = m.group(1).lower()
        if key in TARGET_MENTIONS and key not in PROSE_SKIP_MENTIONS:
            return TARGET_MENTIONS[key]
        return m.group(0)

    masked = pattern.sub(repl, masked)
    return restore_code_spans(masked, spans, prefix="\x00HANDOFF_SPAN_")


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
