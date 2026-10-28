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
    r"<@!?93420059858305024>",    # Mike / Arbiter
    r"<@!?453030589914939393>",   # Ian / Moon Problem
    r"<@!?169260920550195200>",   # Alex / Arcane
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
    if any(re.search(p, text, re.I) for p in PEER_SNOWFLAKE_TAGS):
        return True
    if any(re.search(p, text, re.I) for p in PEER_VOCATIVE_PATTERNS):
        return True
    return False


ZERO_TAGS = [
    r"<@1542285964213358633>",
    r"<@!1542285964213358633>",
    r"<@&1543462881624858624>",  # Team role
    r"<@&1543285916506783799>",  # Robot role (Crab Cavern)
    r"<@&1542294519914037341>",  # Robot role (legacy/alt)
    r"(?:@zero\b|\b(?:hey|hi|hello)\s+zero\b|(?:^|[\n.!?\s,;])zero\s*[:,-]|\bzero\s*[?!]|\b(?:ask|tag|tell|ping|cc)\s+zero\b|(?:^|[\n.!?]\s*)zero\b(?:\s+(?!(?:day|shot|downtime|errors?|detections?|latency|tolerance|cost|percent|sum|crossing|emission|point|index|out|wrapping|layout|config)\b)\S+|$))",
    r"(?:@robot\b|\b(?:hey|hi|hello)\s+@?robot\b|^\s*@?robot\s*[:,-])",
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
- Most ambient messages need NO reaction. However, if the message is a genuinely funny joke/roast, a shipped milestone/win, or a warm acknowledgment where a single native Discord emoji reaction from Zero would be natural (e.g. 😂, 💀, 🔥, 🎉, 👍, 👀, 🍻), you may append ` | REACT: <single_emoji>` after the float.
- Never attach a reaction to routine chatter or peer-to-peer handoffs.

Output ONLY a single float between 0.0 and 1.0, optionally followed by ` | REACT: <emoji>` (e.g. `0.25`, `0.30 | REACT: 😂`, `0.85`).

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
    lines = [line.strip() for line in cleaned_output.strip().splitlines() if line.strip()]
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
    except Exception:
        return False
    return True


class WarmClassifierDaemon:
    """Persistent warm agy worker for low-latency ambient classification."""

    def __init__(self, work_dir: str = "/tmp/agy-classifier", max_turns: int = 40):
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
            stderr=subprocess.PIPE,
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
                except Exception:
                    if hasattr(proc, "kill"):
                        proc.kill()
                        proc.wait(timeout=1.0)
        except Exception as e:
            print(f"[ClassifierDaemon] Warning during shutdown: {e}", file=sys.stderr)

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
            if self.proc is None or (hasattr(self.proc, "poll") and self.proc.poll() is not None):
                self._start_locked()
            if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
                return None

            payload = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
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
                except Exception:
                    fd = None

            buf = ""
            while True:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    print(f"[ClassifierDaemon] Timeout after {timeout}s", file=sys.stderr)
                    self._shutdown_locked()
                    return None

                if "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
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
                        if isinstance(step, dict) and step.get("step_type") == "agent_response":
                            delta = step.get("text_delta") or step.get("text") or step.get("content")
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
                    print(f"[ClassifierDaemon] select timeout after {timeout}s", file=sys.stderr)
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
                        print("[ClassifierDaemon] EOF or empty readline on stdout", file=sys.stderr)
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
        except Exception as e:
            print(f"[ClassifierDaemon] Error in classify: {e}", file=sys.stderr)
            try:
                self._shutdown_locked()
            except Exception:
                pass
            return None
        finally:
            self.lock.release()


warm_classifier = WarmClassifierDaemon()


def score_relevance(content: str, author: str = "user", use_warm: bool | None = None) -> float:
    """Score message relevance for Zero using ambient classifier."""
    global _LAST_AMBIENT_REACTION
    _LAST_AMBIENT_REACTION = None
    content_clean = content.strip()

    # Fast-path: Explicit Zero mentions or @Robot snowflake tags immediately score 1.0
    if any(re.search(tag, content_clean, re.I) for tag in ZERO_TAGS):
        return 1.0

    words = content_clean.split()

    # Pre-filtering heuristics to save model calls:
    is_directive = bool(
        re.search(
            r"^(?:ship|run|deploy|push|update|test|proceed|rollback|revert|fix|merge|build|restart|reload|check|verify|clean|sync|retry|stop|start|cancel|go ahead|lgtm|approved|do it)\b",
            content_clean,
            re.I,
        )
    )
    if (
        len(words) < 4
        and "?" not in content_clean
        and "```" not in content_clean
        and not is_directive
    ):
        print("[Classifier] Drop reason=short_non_directive", file=sys.stderr)
        return 0.0

    # Parse handoff envelope if present
    envelope = None
    try:
        from tools.handoff import parse_envelope

        envelope = parse_envelope(content_clean)
    except Exception:
        pass

    if envelope:
        target = str(
            envelope.get("to") or envelope.get("target") or envelope.get("recipient") or ""
        ).lower()
        if target and "zero" not in target and "team" not in target:
            print(f"[Classifier] Drop reason=envelope_target_other target={target}", file=sys.stderr)
            return 0.0

        floor_state = str(envelope.get("floor") or "").lower()
        # floor: closed is the explicit state indicating "do not reply"
        if floor_state == "closed" and "zero" not in target:
            print("[Classifier] Drop reason=envelope_floor_closed", file=sys.stderr)
            return 0.0

    # Strip handoff envelopes and code blocks when evaluating recipient targeting,
    # so that sender signatures (e.g. "holder": "amos") don't trip peer targeting regexes
    text_for_targeting = re.sub(
        r"```(?:handoff)?.*?```", "", content_clean, flags=re.DOTALL
    ).strip()

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

    prompt = CLASSIFY_PROMPT.format(author=author or "user", content=content_clean)

    # 1. Warm persistent classifier path
    decide_warm = use_warm if use_warm is not None else _should_use_warm_classifier()
    if decide_warm:
        try:
            warm_resp = warm_classifier.classify(prompt)
            if warm_resp is not None:
                val = extract_classifier_score(warm_resp)
                if val is not None:
                    _LAST_AMBIENT_REACTION = extract_classifier_reaction(warm_resp)
                    return val
                print("[Classifier] Fallback reason=warm_parse_null", file=sys.stderr)
            else:
                print("[Classifier] Fallback reason=warm_returned_none", file=sys.stderr)
        except Exception as we:
            print(f"[Classifier] Fallback reason=warm_error err={we}", file=sys.stderr)

    # 2. Cold subprocess fallback path
    try:
        res = subprocess.run(
            [
                "agy",
                "--model=gemini-3.8-flash-low",
                "--disable-slash-commands",
                f"-p={prompt}",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        _LAST_AMBIENT_REACTION = extract_classifier_reaction(res.stdout)
        val = extract_classifier_score(res.stdout)
        if val is not None:
            return val
        print("[Classifier] Fallback reason=parse_null", file=sys.stderr)
    except Exception as e:
        print(f"[Classifier] Fallback reason=subprocess_error err={e}", file=sys.stderr)

    if is_open_optional_question and "?" in text_for_targeting:
        print("[Classifier] Escalating open optional question to 0.85 on fallback", file=sys.stderr)
        return 0.85
    return 0.0


if __name__ == "__main__":
    test_msg = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Has anyone seen containerd drop sockets under high IO on Synology?"
    )
    print(f"Score for '{test_msg}': {score_relevance(test_msg)}")
