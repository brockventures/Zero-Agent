#!/usr/bin/env python3
import subprocess
import re
import sys

PEER_OR_OTHER_TAGS = [
    r"<@1468012353206354197>",  # Amos
    r"<@1492043459618537492>",  # Marvin
    r"<@1210466877835313155>",  # Mike / Arbiter
    r"<@453030589914939393>",   # Ian / Moon Problem
    r"\b@?amos\b",
    r"\b@?marvin\b",
    r"\b@?arbiter\b",
    r"\b@?ian\b"
]
ZERO_TAGS = [
    r"<@1542285964213358633>",
    r"<@!1542285964213358633>",
    r"<@&1543462881624858624>",  # Team role
    r"<@&1542294519914037341>",  # Robot role
    r"\b@?zero\b",
    r"\b@?robot\b"
]

CLASSIFY_PROMPT = '''Analyze this inbound message in a shared multi-agent engineering channel with Zero (systems engineer, SWE, Linux, Docker, Python), Amos, and Marvin.
Rules:
1. If the message is explicitly addressed to someone else (e.g. '@Amos', 'Marvin:', 'Ryan:', '@Ian'), relevance to Zero MUST be 0.0.
2. If it is trivial casual chatter, greetings, or off-topic, relevance MUST be 0.0.
3. If it is an unaddressed engineering problem, technical discussion, or systems question where Zero's input would be genuinely valuable, score between 0.70 and 1.0.
4. If the message includes an open banana handoff envelope marked with reply 'optional' and asks an engineering question without naming a single exclusive recipient, relevance to Zero should be high (0.85 - 0.95).
5. Output ONLY a single float number between 0.0 and 1.0 (e.g. 0.0 or 0.85).

Message from {author}: "{content}"'''

def score_relevance(content: str, author: str = "user") -> float:
    """Score message relevance for Zero using ambient classifier."""
    content_clean = content.strip()
    words = content_clean.split()

    # Pre-filtering heuristics to save model calls:
    if len(words) < 4 and "?" not in content_clean and "```" not in content_clean:
        return 0.0

    # Parse handoff envelope if present
    envelope = None
    try:
        from tools.handoff import parse_envelope
        envelope = parse_envelope(content_clean)
    except Exception:
        pass

    if envelope:
        # Rule: reply: "none" is an unconditional drop
        if envelope.get("reply") == "none":
            return 0.0

        target = str(envelope.get("to") or envelope.get("target") or envelope.get("recipient") or "").lower()
        if target and "zero" not in target:
            return 0.0

    # Strip handoff envelopes and code blocks when evaluating recipient targeting,
    # so that sender signatures (e.g. "holder": "amos") don't trip peer targeting regexes
    text_for_targeting = re.sub(r"```(?:handoff)?.*?```", "", content_clean, flags=re.DOTALL).strip()

    # If directed to peer or other human explicitly in message text and does not include Zero or team role, drop immediately
    has_other_target = any(re.search(p, text_for_targeting, re.I) for p in PEER_OR_OTHER_TAGS)
    has_zero_target = any(re.search(p, text_for_targeting, re.I) for p in ZERO_TAGS)
    if has_other_target and not has_zero_target:
        return 0.0

    if re.search(r"^(hey\s+)?@(amos|marvin)\b", text_for_targeting, re.I) or re.search(r"^(amos|marvin):", text_for_targeting, re.I):
        return 0.0

    # Detect open questions with reply: "optional"
    is_open_optional_question = False
    if envelope and envelope.get("reply") == "optional":
        kind = str(envelope.get("kind") or "").lower()
        target = str(envelope.get("to") or envelope.get("target") or "").lower()
        if (kind in ("question", "discussion", "handoff", "finding") or "?" in text_for_targeting) and not target:
            is_open_optional_question = True

    prompt = CLASSIFY_PROMPT.format(author=author or "user", content=content_clean)
    try:
        res = subprocess.run(
            [
                "agy",
                "--model=gemini-3.8-flash-low",
                "--disable-slash-commands",
                f"-p={prompt}"
            ],
            capture_output=True,
            text=True,
            timeout=15
        )
        m = re.search(r"([0-1]\.\d+|0|1)", res.stdout)
        if m:
            val = float(m.group(1))
            if is_open_optional_question and val >= 0.50:
                return max(val, 0.85)
            return val
    except Exception as e:
        print(f"[Classifier] Error: {e}", file=sys.stderr)
        if is_open_optional_question and "?" in text_for_targeting:
            return 0.85
    return 0.0

if __name__ == "__main__":
    test_msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Has anyone seen containerd drop sockets under high IO on Synology?"
    print(f"Score for '{test_msg}': {score_relevance(test_msg)}")
