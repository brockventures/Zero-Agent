#!/usr/bin/env python3
import subprocess
import re
import sys

PEER_SNOWFLAKE_TAGS = [
    r"<@!?1468012353206354197>",  # Amos
    r"<@!?1492043459618537492>",  # Marvin
    r"<@!?93420059858305024>",    # Mike / Arbiter
    r"<@!?453030589914939393>",   # Ian / Moon Problem
]

PEER_NAMES = r"(?:amos|marvin|arbiter|ian|mike)"

# Patterns where a peer is specifically addressed in a vocative position (NOT prepositional/referential)
PEER_VOCATIVE_PATTERNS = [
    # Explicit @ mentions: @amos, @marvin, etc.
    rf"@\s*{PEER_NAMES}\b",
    # Vocative greetings or openers with punctuation: "hey amos,", "hi marvin:", "amos:", "amos,"
    rf"(?:^|[\n.!?]\s*)(?:hey\s+|hi\s+|hello\s+)?{PEER_NAMES}\s*[:,]",
    # Starting the message directly with the name: "amos what do you think", "marvin check this"
    rf"^(?:hey\s+|hi\s+|hello\s+)?{PEER_NAMES}\s+",
]

PEER_OR_OTHER_TAGS = PEER_SNOWFLAKE_TAGS + PEER_VOCATIVE_PATTERNS

def is_explicitly_addressed_to_other(content: str) -> bool:
    """Check if the text is explicitly addressed to a peer agent or collaborator (not Zero)."""
    text = content.strip()
    if any(re.search(p, text, re.I) for p in PEER_SNOWFLAKE_TAGS):
        return True
    if any(re.search(p, text, re.I) for p in PEER_VOCATIVE_PATTERNS):
        return True
    return False
ZERO_TAGS = [
    r"<@1542285964213358633>",
    r"<@!1542285964213358633>",
    r"<@&1543462881624858624>",  # Team role
    r"<@&1542294519914037341>",  # Robot role
    r"\b@?zero\b",
    r"\b@?robot\b"
]

CLASSIFY_PROMPT = '''You are evaluating inbound chat messages in a shared multi-agent engineering channel (Crab Cavern) with Zero (systems engineer, SWE, Linux, Docker, Python), Amos, and Marvin.

Evaluate the relevance score (0.0 to 1.0) of this message to Zero based on:
1. TOPICAL ALIGNMENT (0.0 - 0.4):
   - 0.0: Casual social banter, personal jokes, food/greetings, off-topic.
   - 0.1 - 0.2: Tangential dev talk, unrelated repos, or peer bots discussing their own internal configs.
   - 0.3 - 0.4: Directly involves Zero's core stack (Docker, Linux, Python, backend state, repo architecture, bridge, AGORA market sandbox).
2. CONVERSATIONAL OBLIGATION / TARGETING (0.0 - 0.4):
   - 0.0: Addressed exclusively to someone else (e.g. '@Amos', 'Marvin:', human-to-human) with no ambiguity.
   - 0.1 - 0.2: Addressed to another entity, BUT mentions or questions systems/code that Zero specifically owns or built.
   - 0.3 - 0.4: Addressed to the open room/channel, or an unassigned engineering question/blocker.
3. ACTIONABILITY / URGENCY (0.0 - 0.2):
   - 0.0: Passive observation, generic acknowledgment ("nice", "noted"), or informational progress statement.
   - 0.1: Discussion or design debate seeking consensus.
   - 0.2: Active blocker, error/outage report, direct question, or call to execute.

Score Guidance:
- 0.0 - 0.2: Social noise, peer-only internal banter, or pure chatter.
- 0.3 - 0.5: Topical discussions that are either addressed to others or purely informational updates.
- 0.6 - 0.7: Open architectural/technical discussions affecting the shared codebase.
- 0.8 - 1.0: Actionable technical questions, broken builds, or blockers where Zero's input is needed.

Output ONLY a single float between 0.0 and 1.0 (e.g. 0.35, 0.65, 0.85).

Message from {author}: "{content}"'''

def extract_classifier_score(output: str) -> float | None:
    """Extract relevance score float safely without matching token counts or metadata numbers."""
    if not output:
        return None
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    # 1. Look for score on a line by itself or prefixed with 'score:' (starting from bottom)
    for line in reversed(lines):
        m = re.search(r"^(?:score:?\s*)?(0(?:\.\d+)?|1(?:\.0+)?)$", line, re.IGNORECASE)
        if m:
            return float(m.group(1))
    # 2. Look for trailing score at the end of a line
    for line in reversed(lines):
        m = re.search(r"(?:^|[\s:])\b(0(?:\.\d+)?|1(?:\.0+)?)\s*$", line, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None

def score_relevance(content: str, author: str = "user") -> float:
    """Score message relevance for Zero using ambient classifier."""
    content_clean = content.strip()
    words = content_clean.split()

    # Pre-filtering heuristics to save model calls:
    is_directive = bool(re.search(r"^(?:ship|run|deploy|push|update|test|proceed|rollback|revert|fix|merge|build|restart|reload|check|verify|clean|sync|retry|stop|start|cancel|go ahead|lgtm|approved|do it)\b", content_clean, re.I))
    if len(words) < 4 and "?" not in content_clean and "```" not in content_clean and not is_directive:
        return 0.0

    # Parse handoff envelope if present
    envelope = None
    try:
        from tools.handoff import parse_envelope
        envelope = parse_envelope(content_clean)
    except Exception:
        pass

    if envelope:
        target = str(envelope.get("to") or envelope.get("target") or envelope.get("recipient") or "").lower()
        if target and "zero" not in target:
            return 0.0

        floor_state = str(envelope.get("floor") or "").lower()
        if envelope.get("reply") == "none" and floor_state not in ("open", "free", "any") and "zero" not in target:
            return 0.0

    # Strip handoff envelopes and code blocks when evaluating recipient targeting,
    # so that sender signatures (e.g. "holder": "amos") don't trip peer targeting regexes
    text_for_targeting = re.sub(r"```(?:handoff)?.*?```", "", content_clean, flags=re.DOTALL).strip()

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
            timeout=25
        )
        val = extract_classifier_score(res.stdout)
        if val is not None:
            return val
    except Exception as e:
        print(f"[Classifier] Error: {e}", file=sys.stderr)
        if is_open_optional_question and "?" in text_for_targeting:
            return 0.85
    return 0.0

if __name__ == "__main__":
    test_msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Has anyone seen containerd drop sockets under high IO on Synology?"
    print(f"Score for '{test_msg}': {score_relevance(test_msg)}")
