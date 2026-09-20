"""
bridge_safety.py — Security, Leak Filtering & Sanitization for Zero Discord Bridge.

Core Responsibilities:
1. Leak Detection: Authoritatively detect internal Antigravity CLI artifacts,
   task wait chatter, and model prompt leakage before text is sent to Discord.
2. Repetition Deduplication: Collapse runaway repetitive LLM generation loops.
3. Chatter Stripping: Strip internal CLI notifications, subagent blocks, and task IDs.
4. Credential Scrubbing: Redact passwords, tokens, API keys, homelab private IPs,
   and OAuth credentials from outbound messages.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

CLI_LEAK_LINE_PATTERNS = [
    r"^\s*error:\s*interrupted\s*$",
    r"^\s*error:\s*the connection to the agent was interrupted[^\n]*$",
    r"^\s*No tools called(?:\.|\s+Waiting for [^\n]+|\s*$)",
    r"^\s*Waiting for (?:the\s+)?(?:background\s+)?(?:task|command|subagent|process)[s]?(?:-[a-zA-Z0-9_-]+)?(?:\s+to\s+(?:complete|finish)|\s+finishes|\s+completes)?(?:\.\.\.|\.)?\s*$",
    r"^\s*Waiting for task-[a-zA-Z0-9_-]+[^\n]*$",
    r"^\s*I (?:will|have)\s+wait(?:ed|ing)? for [^\n]+?(?:to complete|to finish|finish|complete)\.?\s*$",
    r"^\s*I (?:have\s+)?(?:initiated|launched|started|spawned|triggered)[^\n]+?(?:as soon as the (?:background\s+)?task\s+(?:completes|finishes)|when the (?:command|task)\s+finishes|and (?:will\s+)?wait for it to finish|the moment it completes|waiting for PID 1 to consume)[^\n]*\.?\s*$",
    r"^\s*I will (?:review|inspect|check|analyze) the results (?:as soon as|when|once|the moment) the (?:background\s+)?task (?:completes|finishes)[^\n]*\.?\s*$",
    r"^\s*I am pausing tool calls to allow [^\n]+? to complete in the background[^\n]*\.?\s*$",
    r"^\s*The system will resume execution automatically once [^\n]+\.?\s*$",
    r"^\s*I have launched [^\n]+? and will wait for it to finish[^\n]*\.?\s*$",
    r"^\s*Tool execution was canceled\.?\s*$",
    r"^\s*No content generated yet\.?\s*$",
    r"^\s*\*\(\s*Response completed, but no text output was generated\s*\)\*\s*$",
    r"^\s*⚠️\s*\*\(\s*Recovered from session transcript following process cutoff\s*\)\*\s*$",
    r"^\s*Log:\s*file://[^\n]+$",
    r"^\s*An async(?:hronous)? task has completed[^\n]*$",
    r"^\s*Task ID:\s*[^\n]+$",
    r"^\s*Task Exit Code:\s*\d+\s*$",
    r"^\s*Task Output:\s*$",
    r"^\s*<end of task output>\s*$",
    r"^\s*root agent idle; waiting up to \d+s for \d+ background task\(s\)\s*$",
    r"^\s*terminating \d+ background task\(s\) on exit\s*$",
    r"^\s*A subagent has completed[^\n]*$",
    r"^\s*Subagent (?:ID|Status|Output):\s*[^\n]*$",
    r"^\s*<end of subagent output>\s*$",
    r"^\s*\[Message\]\s+timestamp=[^\n]*$",
    r"^\s*\[Task Update\]\s+Task\s+[^\n]*$",
    r"^\s*</?RECEIVED_TASK_NOTIFICATION>\s*$",
    r"^\s*Exit code:\s*\d+\s*$",
    r"^\s*Stdout:\s*$",
    r"^\s*Stderr:\s*$",
    r"^\s*To authenticate, visit:\s*$",
    r"^\s*Process\s+[a-f0-9-]+/task-[a-zA-Z0-9_-]+\s+completed with exit code\s+\d+\.?\s*(?:Output:)?\s*$",
    r"^\s*Process\s+[^\n]+?\s+completed with exit code\s+\d+\.?\s*(?:Output:)?\s*$",
    r"^\s*\[BridgeDaemon\][^\n]+$",
    r"^\s*\[BridgeState\][^\n]+$",
    r"^\s*Ran \d+ tests? in [0-9.]+s\s*$",
    r"^\s*OK\s*$",
    r"^\s*FAILED\s*\(.*?\)\s*$",
    r"^\s*-{6,}\s*$",
    r"^\s*\.{4,}\s*$",
    r"^\s*Task Description:\s*[^\n]+$",
    r"^\s*Task logs are available at:[^\n]+$",
    r"^\s*Tool is running as a background task with task id:[^\n]*$",
    r"^\s*YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[^\n]*$",
    r"^\s*DO NOTHING ELSE\.?\s*$",
    r"^\s*\.\.\.\s*\[repetitive (?:lines|output) truncated\]\s*\.\.\.\s*$",
]


def dedup_repetitive_patterns(text: str, max_repeats: int = 3) -> str:
    """Collapse runaway repetitive single lines or multi-line patterns produced by degenerative LLM loops."""
    if not text:
        return ""
    lines = text.split("\n")
    n = len(lines)
    if n < 6:
        return text

    # Check for repeating blocks of length k (1 to 4)
    for k in range(1, 5):
        i = 0
        new_lines = []
        pattern_collapsed = False
        while i < len(lines):
            block = [l.strip() for l in lines[i : i + k]]
            if any(len(l) > 3 for l in block) and i + k <= len(lines):
                repeats = 1
                while i + (repeats + 1) * k <= len(lines):
                    next_block = [l.strip() for l in lines[i + repeats * k : i + (repeats + 1) * k]]
                    if next_block == block:
                        repeats += 1
                    else:
                        break
                if repeats > max_repeats:
                    new_lines.extend(lines[i : i + max_repeats * k])
                    new_lines.append("... [repetitive output truncated] ...")
                    i += repeats * k
                    pattern_collapsed = True
                    continue
            new_lines.append(lines[i])
            i += 1
        if pattern_collapsed:
            lines = new_lines

    return "\n".join(lines)


def strip_internal_cli_chatter(text: str) -> str:
    """Strip Antigravity CLI system messages, background task envelopes, and wait chatter."""
    if not text:
        return ""

    # Collapse repetitive patterns first to prevent runaway repetitive loops
    text = dedup_repetitive_patterns(text, max_repeats=3)

    # 1. Block-level removals (anchored to line start so inline mentions are not eaten to EOF)
    text = re.sub(
        r"(?m)^\s*The following is a\s*<\s*SYSTEM_MESSAGE\s*>[\s\S]*?(?:<\s*/\s*SYSTEM_MESSAGE\s*>|\}\s*(?=[A-Z#])|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^\s*<\s*SYSTEM_MESSAGE\s*>[\s\S]*?<\s*/\s*SYSTEM_MESSAGE\s*>\s*\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^\s*<\s*RECEIVED_TASK_NOTIFICATION\s*>[\s\S]*?<\s*/\s*RECEIVED_TASK_NOTIFICATION\s*>\s*\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Unclosed standalone system / task notification blocks on their own line extending to EOF
    text = re.sub(
        r"(?m)^\s*<\s*(?:SYSTEM_MESSAGE|RECEIVED_TASK_NOTIFICATION)\s*>\s*\n[\s\S]*?$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^The following is a <SYSTEM_MESSAGE>[^\n]*\n*",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    text = re.sub(r"(?:^|\n+)\s*The following is a\s*(?=\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?m)^\s*Process\s+[^\n]+?\s+completed with exit code\s+\d+\.?\s*(?:Output:)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^\s*\[BridgeDaemon\] Worker (?:for\s+)?#[a-zA-Z0-9_-]+ (?:exited cleanly|terminated)[^\n]*\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Tool is running as a background task with task id:\s*[^\n]+(?:\nTask Description:[^\n]+)?(?:\nTask logs are available at:[^\n]+)?(?:\nYOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[^\n]+)?(?:\n\s*DO NOTHING ELSE\.?)?",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[\s\S]*?(?:DO NOTHING ELSE\.?|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Task id\s+[\"'][^\"']+[\"']\s+(?:was\s+canceled|finished|completed|failed)[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"Task logs are available at:\s*file://[^\n]+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"An async(?:hronous)? task has completed[\s\S]*?<end of task output>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"An async(?:hronous)? task has completed:\s*[^\n]+(?:\s*\(State:[^\)]+\))?(?:\s*Result payload:\s*\d+)?(?:\s*Task output:\s*(?:\[[^\]\r\n]*\]|[^\r\n]*))?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"A subagent has completed[\s\S]*?<end of subagent output>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"root agent idle; waiting up to \d+s for \d+ background task\(s\)[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"terminating \d+ background task\(s\) on exit[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Subagent execution in progress\.\.\.[\s\S]*?(?:If you call a tool now[^\n]*|wait for tasks or subagents\.\.?|DO NOTHING ELSE\.?)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^\s*(?:No content generated yet\.?\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:^|\n+)\s*No content generated yet\.?\s*(?=\n|$)",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Line-by-line scrubbing for chatter / sentinel leaks
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        matched = False
        for pat in CLI_LEAK_LINE_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                matched = True
                break
        if not matched:
            cleaned_lines.append(line)

    res = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", res).strip()


def is_internal_cli_leak(text: str) -> bool:
    """Return True if text contains only internal CLI artifacts, task wait chatter, or empty sentinels."""
    if not text or not isinstance(text, str):
        return True
    orig_lower = text.lower().strip()
    if re.match(r"^process\s+[^\n]+?\s+completed with exit code", orig_lower):
        return True
    if orig_lower.startswith("tool is running as a background task with task id"):
        return True
    if orig_lower.startswith("an async task has completed") or orig_lower.startswith("an asynchronous task has completed"):
        return True

    cleaned = strip_internal_cli_chatter(text).strip()
    if not cleaned:
        return True
    lower = cleaned.lower().strip()
    if lower in (
        "[no_reply]",
        "no_reply",
        "[no_op]",
        "no_op",
        "reply:none",
        "reply: none",
        "none",
        "*(response completed, but no text output was generated)*",
        "no content generated yet",
        "no content generated yet.",
        "... [repetitive lines truncated] ...",
        "... [repetitive output truncated] ...",
    ):
        return True
    if lower.startswith("error: interrupted") or lower == "interrupted":
        return True

    # Standalone pure CLI status, process completion, or task completion notices
    if re.match(r"^process\s+[^\n]+?\s+completed with exit code", lower):
        return True
    if lower.startswith("an async task has completed") or lower.startswith("an asynchronous task has completed"):
        return True

    # If remaining lines are exclusively internal daemon/system/test lines without user-facing content
    remaining_lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    if remaining_lines and all(
        re.match(
            r"^(\[BridgeDaemon\]|\[BridgeState\]|Process\s+[^\n]+completed|Ran \d+ test|OK$|FAILED|\.\.\.|-{5,}|<end of|<RECEIVED_TASK_NOTIFICATION)",
            l,
            re.IGNORECASE,
        )
        for l in remaining_lines
    ):
        return True

    # Standalone pure CLI status or task completion notices without substantive user content
    words = [w for w in cleaned.split() if any(c.isalnum() for c in w)]
    if len(words) <= 15:
        if "root agent idle; waiting up to" in lower:
            return True
        if "terminating" in lower and "background task(s) on exit" in lower:
            return True
        if lower.startswith("task id") and ("canceled" in lower or "completed" in lower or "failed" in lower):
            return True
        if "waiting for task" in lower:
            return True

    return False


_SCRUB_TARGETS: Optional[set[str]] = None


def _get_scrub_targets() -> set[str]:
    """Retrieve sensitive strings to scrub from environment and credential mounts."""
    global _SCRUB_TARGETS
    if _SCRUB_TARGETS is not None:
        return _SCRUB_TARGETS

    targets = set()
    env_keys = [
        "DISCORD_BOT_TOKEN", "HA_ACCESS_TOKEN", "TAUTULLI_API_KEY",
        "MARKETCHECK_API_KEY", "CLOUDFLARE_API_TOKEN", "UPTIMEROBOT_API_KEY",
        "SERPAPI_API_KEY", "ATT_WIFI_PASSWORD", "ATT_ACCESS_CODE",
        "ZERO_EMAIL_PASSWORD"
    ]
    for k in env_keys:
        val = os.getenv(k, "").strip()
        if val and len(val) >= 6:
            targets.add(val)

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                for v in d.values():
                    if isinstance(v, str) and len(v.strip()) >= 6 and not v.startswith("http"):
                        targets.add(v.strip())
        except Exception:
            pass

    if os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                tok = d.get("token", "")
                if tok and len(tok) >= 6:
                    targets.add(tok.strip())
        except Exception:
            pass

    if os.path.exists("/workspace/.env"):
        try:
            with open("/workspace/.env") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        _, v = line.split("=", 1)
                        v_clean = v.strip().strip("'").strip('"')
                        if len(v_clean) >= 6:
                            targets.add(v_clean)
        except Exception:
            pass

    for oauth_path in ["/secrets/google_oauth.json", "/secrets/youtube_oauth.json"]:
        if os.path.exists(oauth_path):
            try:
                with open(oauth_path) as f:
                    content = f.read()
                for line in content.splitlines():
                    line = line.strip().rstrip(",")
                    if ":" in line:
                        _, v = line.split(":", 1)
                        v_clean = v.strip().strip('"').strip("'")
                        if len(v_clean) >= 12 and not v_clean.startswith("http"):
                            targets.add(v_clean)
            except Exception:
                pass

    _SCRUB_TARGETS = targets
    return _SCRUB_TARGETS


def scrub_credentials(text: str) -> str:
    """Scrub internal tokens, passwords, API keys, and homelab private IPs from outbound text."""
    if not text:
        return text

    # Redact private subnet IPs first for consistent classification
    text = re.sub(r"\b192\.168\.1\.\d{1,3}\b", "[internal-ip]", text)

    for val in _get_scrub_targets():
        if val in text:
            text = text.replace(val, "[REDACTED_SECRET]")

    # Redact common credential patterns
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_-]{20,}", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"\bya29\.[A-Za-z0-9_-]+", "[REDACTED_OAUTH_TOKEN]", text)
    text = re.sub(r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "[REDACTED_JWT]", text)

    return text
