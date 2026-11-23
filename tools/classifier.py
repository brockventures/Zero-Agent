#!/usr/bin/env python3
import json
import os
import re
import select
import subprocess
import sys
import threading
import time

PEER_SNOWFLAKE_TAGS = [
    r"<@!?1468012353206354197>",  # Amos
    r"<@!?1492043459618537492>",  # Marvin
    r"<@!?1542035925603713086>",  # Aerial
    r"<@!?1545924520236290198>",  # Banana Watcher
    r"<@!?93420059858305024>",  # Mike / Arbiter
    r"<@!?453030589914939393>",  # Ian / Moon Problem
    r"<@!?169260920550195200>",  # Alex / Arcane
]

PEER_NAMES = r"(?:amos|marvin|aerial|arbiter|ian|mike|alex|arcane|bananawatcher|banana\s*watcher)"

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
    if any(re.search(p, text, re.IGNORECASE) for p in PEER_SNOWFLAKE_TAGS):
        return True
    return any(re.search(p, text, re.IGNORECASE) for p in PEER_VOCATIVE_PATTERNS)


NON_NAME_NOUNS = (
    r"(?:day|shot|downtime|errors?|detections?|latency|tolerance|cost|percent|sum|"
    r"crossing|emission|point|index|out|wrapping|layout|config|leakage|padding|margin|"
    r"trust|knowledge|defect|defects|risk|budget|drift|progress)\b"
)

_FENCED_CODE_BLOCK_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,}).*?(?:(?P=fence)|$)",
    re.DOTALL,
)


def strip_fenced_code_blocks(text: str) -> str:
    """Remove CommonMark fenced code blocks (```...``` / ~~~...~~~) and handoff envelopes."""
    if not text:
        return ""
    return _FENCED_CODE_BLOCK_RE.sub("", text)


_ZERO_VOCATIVE_RE = re.compile(
    r"(?:"
    r"@zero\b"
    r"|\b(?:hey|hi|hello|ask|tag|tell|ping|cc)\s+zero\b"
    r"|(?:^|[\n.!?])\s*zero\s*(?:[:,]|--?(?:\s+|$))"
    r"|\bzero\s*[?!]"
    r")",
    re.IGNORECASE,
)

_ROBOT_VOCATIVE_RE = re.compile(
    r"(?:@robot\b|\b(?:hey|hi|hello)\s+@?robot\b|^\s*@?robot\s*[:,-])",
    re.IGNORECASE,
)

_TEAM_VOCATIVE_RE = re.compile(
    r"(?:(?:^|[\s,;])@team\b|^(?:hey\s+)?team[:,\s])",
    re.IGNORECASE,
)


def contains_zero_mention(text: str) -> bool:
    """Check if text addresses Zero via @Zero or vocative greeting/punctuation outside fenced code blocks."""
    if not text:
        return False
    cleaned = strip_fenced_code_blocks(text)
    if not cleaned:
        return False
    return bool(_ZERO_VOCATIVE_RE.search(cleaned))


def contains_robot_mention(text: str) -> bool:
    """Check if text addresses @robot via vocative or tag outside fenced code blocks."""
    if not text:
        return False
    cleaned = strip_fenced_code_blocks(text)
    if not cleaned:
        return False
    return bool(_ROBOT_VOCATIVE_RE.search(cleaned))


def contains_team_mention(text: str) -> bool:
    """Check if text addresses @team via vocative or tag outside fenced code blocks."""
    if not text:
        return False
    cleaned = strip_fenced_code_blocks(text)
    if not cleaned:
        return False
    return bool(_TEAM_VOCATIVE_RE.search(cleaned))


ZERO_SNOWFLAKE_TAGS = [
    r"<@1542285964213358633>",
    r"<@!1542285964213358633>",
]

ZERO_TAGS = [
    r"<@1542285964213358633>",
    r"<@!1542285964213358633>",
    r"<@&1543462881624858624>",  # Team role
    r"<@&1543285916506783799>",  # Robot role (Crab Cavern)
    r"<@&1542294519914037341>",  # Robot role (legacy/alt)
]

CLASSIFY_PROMPT = '''You are evaluating inbound chat messages in a shared multi-agent engineering channel (Crab Cavern) with Zero (systems engineer, SWE, Linux, Docker, Python), Amos, Marvin, and Aerial.

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

Optional Lightweight Reaction (for scores < 0.80 ONLY):
- Most ambient messages need NO reaction. Restrict ` | REACT: <single_emoji>` strictly to: (a) direct closing acknowledgments to Zero when Zero just spoke in [Recent Room Context] (e.g. "sounds good", "thanks", "got it"), or (b) standout human humor/roasts and major celebrations.
- Dynamic, Context-Specific Emoji Selection: Whenever a reaction is warranted, dynamically pick a clever, interesting emoji tailored to the exact subject matter, domain, or joke in the message (e.g. 🖨️ for 3D printing, ⚾ for baseball, 🦀 for Crab Cavern, 🔌 or 🔋 for hardware/soldering, 💀 for a cursed bug, 🍻 for a toast, 👀 for intrigue) rather than defaulting to repetitive generic markers (🎯, 🚀, 🫡, 🎉, 👍).
- Never attach a reaction to third-party PR updates, issue lists, status checks, routine chatter, peer-to-peer handoffs, or bot messages.

Output ONLY a single float between 0.0 and 1.0, optionally followed by ` | REACT: <emoji>` (e.g. `0.25`, `0.30 | REACT: 💀`, `0.85`).

Message from {author}: "{content}"'''

_LAST_AMBIENT_REACTION: str | None = None


def pop_last_reaction() -> str | None:
    """Return and clear the most recent ambient reaction suggested by score_relevance."""
    global _LAST_AMBIENT_REACTION
    rx = _LAST_AMBIENT_REACTION
    _LAST_AMBIENT_REACTION = None
    return rx


def extract_classifier_reaction(output: str) -> str | None:
    """Extract optional lightweight emoji reaction from classifier output (e.g. '0.30 | REACT: 😂')."""
    if not output:
        return None
    m = re.search(r"\bREACT(?:ION)?:\s*([^\s|,;\[\]]+)", output, re.IGNORECASE)
    if not m:
        return None
    candidate = m.group(1).strip()
    if candidate.lower() in ("none", "null", "n/a", "<emoji>", "..."):
        return None
    # Ensure it contains a non-ASCII emoji character or custom Discord emoji format
    if any(ord(ch) > 127 for ch in candidate) or re.match(r"^<a?:\w+:\d+>$", candidate):
        return candidate
    return None


def extract_classifier_score(output: str) -> float | None:
    """Extract relevance score float safely without matching token counts or metadata numbers."""
    if not output:
        return None
    # Strip any optional '| REACT: ...' suffix before parsing score
    cleaned_output = re.sub(
        r"\s*\|?\s*\bREACT(?:ION)?:.*$", "", output, flags=re.IGNORECASE | re.MULTILINE
    )
    lines = [
        line.strip() for line in cleaned_output.strip().splitlines() if line.strip()
    ]
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


def _should_use_warm_classifier() -> bool:
    """Determine whether the warm classifier daemon should be used."""
    if os.getenv("TESTING") == "1":
        return False
    from unittest.mock import MagicMock, Mock

    if isinstance(subprocess.run, (Mock, MagicMock)):
        return False
    try:
        from tools.bridge_daemons import is_persistent_daemons_enabled

        if not is_persistent_daemons_enabled():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


class WarmClassifierDaemon:
    """Persistent warm agy worker for low-latency ambient classification."""

    def __init__(self, work_dir: str = "/tmp/agy-classifier", max_turns: int = 8):
        self.work_dir = work_dir
        self.max_turns = max_turns
        self.proc: subprocess.Popen | None = None
        self.turn_count = 0
        self.lock = threading.Lock()
        self._recycling = False

    @property
    def pid(self) -> int | None:
        if self.proc is None:
            return None
        if hasattr(self.proc, "poll") and self.proc.poll() is not None:
            return None
        return getattr(self.proc, "pid", None)

    def start(self):
        """Start the persistent agy classification process."""
        with self.lock:
            self._start_locked()

    def _start_locked(self):
        if self.proc is not None:
            if not hasattr(self.proc, "poll") or self.proc.poll() is None:
                return
            self._shutdown_locked()
        os.makedirs(self.work_dir, exist_ok=True)
        cmd = [
            "agy",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--model=gemini-3.8-flash-low",
            "--disable-slash-commands",
            "--print-timeout=4h",
            "--print=",
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.work_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.turn_count = 0

    def shutdown(self):
        """Shut down the persistent worker process cleanly."""
        with self.lock:
            self._shutdown_locked()

    def _shutdown_locked(self):
        if self.proc is None:
            return
        proc = self.proc
        self.proc = None
        self.turn_count = 0
        try:
            if hasattr(proc, "poll") and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    if hasattr(proc, "kill"):
                        proc.kill()
                        proc.wait(timeout=1.0)
        except Exception as e:  # noqa: BLE001
            print(f"[ClassifierDaemon] Warning during shutdown: {e}", file=sys.stderr)
        finally:
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(proc, stream_name, None)
                if stream is not None and hasattr(stream, "close"):
                    try:
                        stream.close()
                    except OSError:
                        pass

    def recycle(self):
        """Recycle the persistent worker process."""
        with self.lock:
            self._shutdown_locked()
            self._start_locked()

    def classify(self, prompt: str, timeout: float = 12.0) -> str | None:
        """Classify a prompt using the persistent stream-json worker."""
        if not self.lock.acquire(timeout=timeout):
            print("[ClassifierDaemon] Lock acquire timeout", file=sys.stderr)
            return None
        try:
            if self.proc is None or (
                hasattr(self.proc, "poll") and self.proc.poll() is not None
            ):
                self._start_locked()
            if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
                return None

            payload = (
                json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
            )
            try:
                self.proc.stdin.write(payload)
            except TypeError:
                self.proc.stdin.write(payload.encode("utf-8"))
            if hasattr(self.proc.stdin, "flush"):
                self.proc.stdin.flush()

            start_time = time.time()
            accumulated_deltas: list[str] = []
            final_response = ""

            from unittest.mock import MagicMock, Mock

            is_mock = isinstance(self.proc.stdout, (Mock, MagicMock))
            fd = None
            if not is_mock and hasattr(self.proc.stdout, "fileno"):
                try:
                    fno = self.proc.stdout.fileno()
                    if isinstance(fno, int):
                        fd = fno
                        os.set_blocking(fd, False)
                except (OSError, ValueError, TypeError):
                    fd = None

            buf = ""
            while True:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    print(
                        f"[ClassifierDaemon] Timeout after {timeout}s", file=sys.stderr
                    )
                    self._shutdown_locked()
                    return None

                if "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ev_type = ev.get("event") or ev.get("type")
                    if ev_type == "result" or "result" in ev:
                        res = ev.get("result", {})
                        if isinstance(res, dict):
                            resp = res.get("response")
                            if resp:
                                final_response = resp
                        elif isinstance(ev.get("response"), str):
                            final_response = ev["response"]
                        break
                    elif ev_type == "step_update" or "step_update" in ev:
                        step = ev.get("step_update", {})
                        if (
                            isinstance(step, dict)
                            and step.get("step_type") == "agent_response"
                        ):
                            delta = (
                                step.get("text_delta")
                                or step.get("text")
                                or step.get("content")
                            )
                            if delta and isinstance(delta, str):
                                accumulated_deltas.append(delta)
                    elif ev_type in ("content", "message", "text", "delta"):
                        content = ev.get("content") or ev.get("text") or ev.get("delta")
                        if content and isinstance(content, str):
                            accumulated_deltas.append(content)
                    continue

                select_target = [self.proc.stdout] if fd is None else [fd]
                rlist, _, _ = select.select(select_target, [], [], remaining)
                if not rlist:
                    print(
                        f"[ClassifierDaemon] select timeout after {timeout}s",
                        file=sys.stderr,
                    )
                    self._shutdown_locked()
                    return None

                if fd is not None:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        print("[ClassifierDaemon] EOF on stdout", file=sys.stderr)
                        self._shutdown_locked()
                        return None
                    buf += chunk.decode("utf-8", errors="replace")
                else:
                    line = self.proc.stdout.readline()
                    if not line:
                        print(
                            "[ClassifierDaemon] EOF or empty readline on stdout",
                            file=sys.stderr,
                        )
                        self._shutdown_locked()
                        return None
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    buf += line

            resp_text = (final_response or "".join(accumulated_deltas)).strip()
            if not resp_text:
                return None

            self.turn_count += 1
            if self.turn_count >= self.max_turns and not self._recycling:
                self._recycling = True

                def _do_recycle():
                    try:
                        self.recycle()
                    finally:
                        self._recycling = False

                threading.Thread(target=_do_recycle, daemon=True).start()

            return resp_text
        except Exception as e:  # noqa: BLE001
            print(f"[ClassifierDaemon] Error in classify: {e}", file=sys.stderr)
            try:
                self._shutdown_locked()
            except Exception:  # noqa: BLE001, S110
                pass
            return None
        finally:
            self.lock.release()


warm_classifier = WarmClassifierDaemon()


_DIRECTIVE_OR_FOLLOWUP_RE = re.compile(
    r"^(?:ship|run|deploy|push|update|test|proceed|rollback|revert|fix|merge|build|"
    r"restart|reload|check|verify|clean|sync|retry|stop|start|cancel|go ahead|lgtm|"
    r"approved|do it|yep|yeah|yes|sure|sounds good|please|pls|can you|could you|"
    r"what about|how about|also|no|nope|wait|try|use|make|let|show|tell|why|explain|"
    r"see|look|add|remove|delete|set|get|option|choice|step|rebase|pull|diff|rebuild|"
    r"stage|commit|pr|audit|diagnose|investigate|benchmark|done|status|ready|finished|"
    r"how'?s|is\s+(?:it|there|this|that)|did\s+(?:it|you|this|that|the)|any\s+update|"
    r"which|where|what|who|when|should\s+(?:we|you))\b",
    re.IGNORECASE,
)


def is_short_directive_or_followup(text: str) -> bool:
    """Return True if text begins with an actionable directive, confirmation, or follow-up query starter."""
    if not text:
        return False
    return bool(_DIRECTIVE_OR_FOLLOWUP_RE.search(text.strip()))


def score_relevance_with_reaction(
    content: str,
    author: str = "user",
    use_warm: bool | None = None,
    recent_context: str = "",
    zero_spoke_recently: bool = False,
) -> tuple[float, str | None]:
    """Score message relevance for Zero and return (score, optional_reaction) without global state."""
    content_clean = content.strip()
    text_for_targeting = strip_fenced_code_blocks(content_clean).strip()

    # 1. Explicit <@bot_id> Discord snowflake mentions override handoff envelopes and peer addressing
    if any(
        re.search(tag, text_for_targeting, re.IGNORECASE) for tag in ZERO_SNOWFLAKE_TAGS
    ):
        return 1.0, None

    # 2. Parse handoff envelope and enforce peer targeting / closed-floor drops BEFORE text vocative checks
    envelope = None
    try:
        from tools.handoff import parse_envelope

        envelope = parse_envelope(content_clean)
    except Exception:  # noqa: BLE001, S110
        pass

    if envelope:
        target = str(
            envelope.get("to")
            or envelope.get("target")
            or envelope.get("recipient")
            or ""
        ).lower()
        if target and "zero" not in target and "team" not in target:
            print(
                f"[Classifier] Drop reason=envelope_target_other target={target}",
                file=sys.stderr,
            )
            return 0.0, None

        floor_state = str(envelope.get("floor") or "").lower()
        # floor: closed is the explicit state indicating "do not reply"
        if floor_state == "closed" and "zero" not in target:
            print("[Classifier] Drop reason=envelope_floor_closed", file=sys.stderr)
            return 0.0, None

    # 3. Peer-addressing drop BEFORE text vocative checks
    if not zero_spoke_recently and is_explicitly_addressed_to_other(text_for_targeting):
        print("[Classifier] Drop reason=addressed_to_other", file=sys.stderr)
        return 0.0, None

    # 4. Fast-path: Role mentions, vocative Zero mentions, or @Robot mentions immediately score 1.0
    if (
        any(re.search(tag, text_for_targeting, re.IGNORECASE) for tag in ZERO_TAGS)
        or contains_zero_mention(content_clean)
        or contains_robot_mention(content_clean)
    ):
        return 1.0, None

    words = content_clean.split()

    # 5. Pre-filtering heuristics to save model calls (bypassed when Zero spoke recently or on directive/followup starters):
    is_directive = is_short_directive_or_followup(content_clean)
    if (
        len(words) < 4
        and "?" not in content_clean
        and "```" not in content_clean
        and not is_directive
        and not zero_spoke_recently
    ):
        print("[Classifier] Drop reason=short_non_directive", file=sys.stderr)
        return 0.0, None

    # Detect open questions with reply: "optional"
    is_open_optional_question = False
    if envelope and envelope.get("reply") == "optional":
        kind = str(envelope.get("kind") or "").lower()
        target = str(envelope.get("to") or envelope.get("target") or "").lower()
        if (
            kind in ("question", "discussion", "handoff", "finding", "proposal")
            or "?" in text_for_targeting
        ) and (not target or "team" in target):
            is_open_optional_question = True

    formatted_content = (
        f"[Recent Room Context]\n{recent_context.strip()}\n\n[New Message]\n{content_clean}"
        if recent_context and recent_context.strip()
        else content_clean
    )
    prompt = CLASSIFY_PROMPT.format(author=author or "user", content=formatted_content)

    # 6a. Warm persistent classifier path
    decide_warm = use_warm if use_warm is not None else _should_use_warm_classifier()
    if decide_warm:
        try:
            warm_resp = warm_classifier.classify(prompt)
            if warm_resp is not None:
                val = extract_classifier_score(warm_resp)
                if val is not None:
                    return val, extract_classifier_reaction(warm_resp)
                print("[Classifier] Fallback reason=warm_parse_null", file=sys.stderr)
            else:
                print(
                    "[Classifier] Fallback reason=warm_returned_none", file=sys.stderr
                )
        except Exception as we:  # noqa: BLE001
            print(f"[Classifier] Fallback reason=warm_error err={we}", file=sys.stderr)

    # 6b. Cold subprocess fallback path
    try:
        res = subprocess.run(
            [
                "agy",
                "--model=gemini-3.8-flash-low",
                "--disable-slash-commands",
                f"-p={prompt}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        val = extract_classifier_score(res.stdout)
        if val is not None:
            return val, extract_classifier_reaction(res.stdout)
        print("[Classifier] Fallback reason=parse_null", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[Classifier] Fallback reason=subprocess_error err={e}", file=sys.stderr)

    if is_open_optional_question and "?" in text_for_targeting:
        print(
            "[Classifier] Escalating open optional question to 0.85 on fallback",
            file=sys.stderr,
        )
        return 0.85, None
    return 0.0, None


def score_relevance(
    content: str,
    author: str = "user",
    use_warm: bool | None = None,
    recent_context: str = "",
    zero_spoke_recently: bool = False,
) -> float:
    """Score message relevance for Zero using ambient classifier (backwards-compatible wrapper)."""
    global _LAST_AMBIENT_REACTION
    score, reaction = score_relevance_with_reaction(
        content=content,
        author=author,
        use_warm=use_warm,
        recent_context=recent_context,
        zero_spoke_recently=zero_spoke_recently,
    )
    _LAST_AMBIENT_REACTION = reaction
    return score


_HANDOFF_TAGGED_BLOCK_RE = re.compile(
    r"(?:\|\|\s*)?```handoff\b.*?```(?:\s*\|\|)?",
    re.IGNORECASE | re.DOTALL,
)
_JSON_FENCED_BLOCK_RE = re.compile(
    r"(?:\|\|\s*)?```(?:json)?\s*\n?(\{.*?\})\s*```(?:\s*\|\|)?",
    re.IGNORECASE | re.DOTALL,
)
_HTML_ENVELOPE_RE = re.compile(
    r"<!--\s*(?:banana-)?envelope\b.*?-->",
    re.IGNORECASE | re.DOTALL,
)
_HANDOFF_ENVELOPE_KEYS = frozenset(
    {"v", "kind", "reply", "floor", "to", "target", "recipient", "subject"}
)
_LEADING_MENTIONS_RE = re.compile(
    r"^(?:\s*(?:<@[!&]?\d+>|@[A-Za-z0-9_.-]+)\s*[,:;—–-]*\s*)+"
)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:\s*(?:[,;.!]+|[—–]+|\s+[-]+\s+|\band\b)\s*)+", re.IGNORECASE
)
_REACT_TAG_RE = re.compile(r"\[REACT(?:ION)?\s*:", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_AGREEMENT_FILLER_WORDS = (
    r"(?:yes|yep|yup|yeah|yea|ya|oh|ah|totally|definitely|absolutely|completely|"
    r"honestly|actually|100%|\+1|agreed|right|exactly|true|fair|indeed)"
)
_AGREEMENT_CLAUSE_PATTERNS = (
    r"(?:\+1|100%|\^+|[👍💯☝️👆]+)",
    (
        r"(?:yes|yep|yup|yeah|yea|ya|aye|totally|definitely|absolutely|completely|exactly|"
        r"precisely|indeed|agreed|agree|same|this|true|fair|right|correct|word|ack|lgtm|sgtm|"
        r"seconded|co-?sign|concur|ditto|facts|solid|nice)"
    ),
    (
        r"(?:i\s+)?(?:totally\s+|fully\s+|completely\s+|strongly\s+|definitely\s+|100%?\s+|wholeheartedly\s+)?"
        r"(?:agree|concur)(?:\s+with\s+(?:that|this|you|the\s+above|@?[a-z0-9_.-]+))?(?:\s+on\s+(?:this|that))?"
    ),
    r"(?:that\s+|this\s+|it\s+)?(?:totally\s+|really\s+|completely\s+)?makes\s+sense(?:\s+to\s+me)?",
    r"(?:that\s+|it\s+)?checks\s+out",
    r"(?:a\s+|very\s+)?(?:good|great|fair|solid|valid|excellent|strong)\s+(?:point|catch|call|take)",
    r"(?:fair|good)\s+enough",
    r"spot\s+on",
    r"well\s+said",
    r"(?:that\s+)?sounds\s+(?:good|right|fair|great|solid|reasonable)(?:\s+to\s+me)?",
    r"(?:looks|seems)\s+good(?:\s+to\s+me)?",
    r"(?:came\s+here\s+to\s+say\s+this|took\s+the\s+words\s+right\s+out\s+of\s+my\s+mouth)",
    r"(?:nothing\s+(?:else\s+)?to\s+add|no\s+notes|no\s+objections)",
    r"(?:i'?m\s+on\s+board|count\s+me\s+in|on\s+board|same\s+here|me\s+too)",
    r"right\s+on",
    r"for\s+sure",
    r"you'?re\s+right",
    r"so\s+true",
)
_AGREEMENT_CLAUSE_RE = re.compile(
    rf"^(?:{_AGREEMENT_FILLER_WORDS}\s+)*(?:{'|'.join(_AGREEMENT_CLAUSE_PATTERNS)})(?:\s+(?:\+1|100%|[👍💯☝️👆]+))*$",
    re.IGNORECASE,
)


def _strip_handoff_envelopes(text: str) -> str:
    """Remove handoff fenced blocks and HTML banana-envelope comments while preserving non-handoff code blocks."""
    cleaned = _HANDOFF_TAGGED_BLOCK_RE.sub("", text)
    cleaned = _HTML_ENVELOPE_RE.sub("", cleaned)

    def _replace_json_handoff(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict) and any(
                k in parsed for k in _HANDOFF_ENVELOPE_KEYS
            ):
                return ""
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return match.group(0)

    return _JSON_FENCED_BLOCK_RE.sub(_replace_json_handoff, cleaned)


def is_bare_agreement_reply(text: str) -> bool:
    """Return True if text consists purely of bare agreement or backchanneling with no new substance."""
    if not text or not text.strip():
        return False

    working = _strip_handoff_envelopes(text).strip()
    if not working:
        return False

    # Preserve reactions, questions, URLs, and non-handoff code blocks unconditionally.
    if _REACT_TAG_RE.search(working) or "?" in working or _URL_RE.search(working):
        return False
    if "```" in working or "~~~" in working:
        return False

    working = _LEADING_MENTIONS_RE.sub("", working).strip()
    core = working.strip(" \t\r\n.!,:;—–-")
    if not core:
        return False

    clauses = [c.strip(" \t\r\n.!,:;—–-") for c in _CLAUSE_SPLIT_RE.split(core)]
    clauses = [c for c in clauses if c]
    if not clauses:
        return False

    return all(_AGREEMENT_CLAUSE_RE.match(clause) is not None for clause in clauses)


def is_additive_reply(text: str) -> bool:
    """Return True when a reply adds substance rather than empty text or bare agreement."""
    if not text or not text.strip():
        return False
    return not is_bare_agreement_reply(text)


if __name__ == "__main__":
    test_msg = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Has anyone seen containerd drop sockets under high IO on Synology?"
    )
    print(f"Score for '{test_msg}': {score_relevance(test_msg)}")
