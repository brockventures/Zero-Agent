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
    r"^\s*error:\s*invalid model selection[^\n]*$",
    r"^\s*error:\s*the connection to the agent was interrupted[^\n]*$",
    r"^\s*No tools called(?:\.|\s+Waiting for [^\n]+|\s*$)",
    r"^\s*(?:-\s*)?a?wait(?:ing)?\s+(?:for\s+)?[^\n]+(?:results|execution|completion|finish|output|task|process|search|eval|run)[^\n]*\.?\s*$",
    r"^\s*(?:-\s*)?awaiting\s+[^\n]+(?:\.|\s*$)",
    r"^\s*Wait(?:ing)? for (?:the\s+)?(?:background\s+)?(?:task|command|subagent|process)[s]?(?:-[a-zA-Z0-9_-]+)?(?:\s+to\s+(?:complete|finish)|\s+finishes|\s+completes)?(?:\.\.\.|\.)?\s*$",
    r"^\s*Wait(?:ing)? for task-[a-zA-Z0-9_-]+[^\n]*$",
    r"^\s*(?:I (?:will|have)\s+)?wait(?:ed|ing)? for [^\n]+?(?:to complete|to finish|finish|complete)\.?\s*$",
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
    r"^\s*An async(?:hronous)? task (?:[a-zA-Z0-9_\-\/]+ )?has completed[^\n]*$",
    r"^\s*An async(?:hronous)? (?:command|task|job) or scheduled task is still running[^\n]*$",
    r"^\s*I(?:'ll| will) continue once I receive an update\.?\s*$",
    r"^\s*I will continue (?:once|when|after) [^\n]+\.?\s*$",
    r"^\s*Log file content:\s*$",
    r"^\s*Task id\s+[\"'][^\"']+[\"']\s+finished with result:\s*$",
    r"^\s*The command exited with code\s+-?\d+\.?\s*$",
    r"^\s*Task ID:\s*[^\n]+$",
    r"^\s*Task Exit Code:\s*\d+\s*$",
    r"^\s*Task Output:\s*$",
    r"^\s*<end of task output>\s*$",
    r"^\s*(?:[A-Z][a-zA-Z0-9_\- ]+?)\s+in the background(?:\.\.\.|\.)?\s*$",
    r"^\s*root agent idle; waiting up to \d+s for \d+ background task\(s\)\s*$",
    r"^\s*terminating \d+ background task\(s\) on exit\s*$",
    r"^\s*A subagent has completed[^\n]*$",
    r"^\s*Subagent (?:ID|Status|Output):\s*[^\n]*$",
    r"^\s*<end of subagent output>\s*$",
    r"^\s*\[Message\]\s+timestamp=[^\n]*$",
    r"^\s*(?:\[|\*\*)?Task Update(?:\]|\*\*)?:?\s+Task\s+[^\n]*$",
    r"^\s*(?:\[|\*\*)?Task Update(?:\]|\*\*)?:?\s*Background task is running[^\n]*$",
    r"^\s*(?:\[|\*\*)?Task Update(?:\]|\*\*)?:?\s*[^\n]*$",
    r"^\s*(?:Background task is running\.?\s*)?Please stand by\.?\s*$",
    r"^\s*</?RECEIVED_TASK_NOTIFICATION>\s*$",
    r"^\s*</?WAITING_FOR_TASKS_OUTPUT>\s*$",
    r"^\s*Wait for at least one of the background tasks to complete:?\s*$",
    r"^\s*-\s+[a-zA-Z0-9_-]+/task-[a-zA-Z0-9_-]+.*$",
    r"^\s*-\s+task-[a-zA-Z0-9_-]+.*$",
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
    r"^\s*Task log:\s*`?(?:file://)?[^\n`]+`?\s*$",
    r"^\s*Task:\s*[^\n]+,\s*Status:\s*[^\n]+$",
    r"^\s*Match:\s*b?['\"][^\n]*$",
    r"^\s*An async(?:hronous)? command is running[^\n]*$",
    r"^\s*The system will automatically resume execution when the command completes[^\n]*$",
    r"^\s*Do not poll or call additional tools until the background task finishes[^\n]*$",
    r"^\s*Tool is running as a background task with task id:[^\n]*$",
    r"^\s*YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[^\n]*$",
    r"^\s*DO NOTHING ELSE\.?\s*$",
    r"^\s*\.\.\.\s*\[repetitive (?:lines|output) truncated\]\s*\.\.\.\s*$",
    r"^\s*Wait(?:ing)?\s+for\s+(?:task|background\s+task|at\s+least\s+one)[^\n]*$",
    r"^\s*Wait(?:ing)?\s+for\s+[a-f0-9\-]+(?:/task-\d+)?\s+to\s+(?:complete|finish)[^\n]*$",
    r"^\s*Wait(?:ing)?\s+for\s+task[:\s][^\n]*$",
    r"^\s*An async(?:hronous)?\s+(?:background\s+)?(?:command|task|job|process)\s+(?:[a-zA-Z0-9_\-\/:]+\s+)?(?:is\s+)?(?:currently\s+|already\s+|still\s+)?running[^\n]*$",
    r"^\s*Continuing\s+(?:silent\s+)?(?:background\s+)?(?:execution|waiting)[^\n]*$",
    r"^\s*(?:Please\s+)?(?:do\s+not|don't)\s+call\s+(?:any\s+more\s+|additional\s+)?tools\s+(?:until\s+[^\n]+)?\.?\s*$",
    r"^\s*Wait\s+silently\s+(?:for\s+[^\n]+)?\.?\s*$",
    r"^\s*WaitMsBeforeAsync:[^\n]+$",
    r"^\s*No\s+response\s+text\s+needed\s+while\s+waiting[^\n]*$",
    r"^\s*Wait(?:ing)?\s+for\s+(?:command|process)\s+to\s+(?:finish|complete)[^\n]*$",
    r"^\s*No other work to do\.?\s*$",
    r"^\s*(?:Queries completed with no tool calls|Roundtrip complete)\.\s*Raising signal for pending messages.*$",
    r"^\s*Background messages arrived during execution:\s*$",
    r"^\s*Floated messages complete\.\s*Continuing execution\.?\s*$",
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


_CODE_SPAN_RE = re.compile(
    r"(?m)((?<!`)(?P<fence>`{3,})[a-zA-Z0-9_+-]*[ \t]*\n[\s\S]*?(?:^[ \t>]*(?P=fence)`*[ \t]*$|\Z))"
    r"|((?<!`)(?P<ticks>`+)(?!`)(?:[^\n]*?(?<!`)(?P=ticks)(?!`)))"
)


def mask_code_spans(text: str, prefix: str = "\x00CODE_SPAN_") -> tuple[str, list[str]]:
    """Replace all CommonMark fenced code blocks and N-backtick inline code spans with immutable placeholders.

    Correctly handles:
    - Multi-line fenced code blocks (```...``` or ````...````), even when interior contains single/double backticks.
    - Single-line N-backtick code spans (`...`, ``...``, ```...```), even when interior contains shorter/longer
      backtick runs (e.g. `` `foo` `` or `r"(...```handoff...```\\s*$)"`).
    """
    if not text or "`" not in text:
        return text, []

    spans: list[str] = []

    def _save(m: re.Match) -> str:
        idx = len(spans)
        spans.append(m.group(0))
        return f"{prefix}{idx}\x00"

    masked = _CODE_SPAN_RE.sub(_save, text)
    return masked, spans


def restore_code_spans(text: str, spans: list[str], prefix: str = "\x00CODE_SPAN_") -> str:
    """Restore placeholders created by mask_code_spans back to their original code spans."""
    if not text or not spans:
        return text
    for idx, span in enumerate(spans):
        text = text.replace(f"{prefix}{idx}\x00", span)
    return text


_MONOLOGUE_INDICATORS = (
    "calibrating response...",
    "physical mention override invariant",
    "[crab cavern multi-agent collaboration environment]",
    "[gif cadence tracker",
    "[bridge execution invariant",
    "[active memory context]",
    "let's check the constraints:",
    "deliver the response in a single inference pass",
    "let's write a punchy one-liner",
    "let's make it a lethal ",
    "wait—rule ",
    "wait: rule ",
    "wait - rule ",
)


def strip_leaked_chain_of_thought(text: str) -> str:
    """Strip leaked model chain-of-thought, prompt-rule deliberation, and pre-tool monologue.

    Handles cases where low-effort model variants (e.g. gemini-4-argon-low) fail to open a
    separate thinking channel and spill internal deliberation into visible content before
    emitting the final user-facing reply.
    """
    if not text:
        return ""

    # 1. Strip explicit <thought>...</thought> or <thinking>...</thinking> blocks and orphaned closing tags
    text = re.sub(
        r"(?is)<\s*(?:thought|thinking)\s*>[\s\S]*?<\s*/\s*(?:thought|thinking)\s*>\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?is)^[\s\S]*?<\s*/\s*(?:thought|thinking)\s*>\s*",
        "",
        text,
    )

    lower = text.lower()
    has_monologue_marker = any(ind in lower for ind in _MONOLOGUE_INDICATORS) or bool(
        re.match(
            r"^\s*(?:[^\w\s]*|\S+/)?\s*(?:calibrating response\.\.\.|wait[—\-:]\s*(?:let's|rule\s+\d+|can\s+we))",
            text,
            re.IGNORECASE,
        )
    )

    if has_monologue_marker:
        # 2a. Self-dispatch cue ("Let's send it!", "Let's post it!", "Let's ship it!") followed by the final reply
        dispatch_matches = list(
            re.finditer(
                r"(?is)(?:let's\s+(?:send|post|deliver|emit|ship)\s+(?:it|this|that)|here\s+goes)\s*[!.:]*\s*",
                text,
            )
        )
        if dispatch_matches:
            last_m = dispatch_matches[-1]
            prefix_lower = text[: last_m.start()].lower()
            remainder = text[last_m.end() :].strip()
            if remainder and any(ind in prefix_lower for ind in _MONOLOGUE_INDICATORS):
                return remainder

        # 2b. Quoted draft-then-emit suffix duplication (model drafts "X" in monologue, then emits X at end)
        stripped_text = text.strip()
        lines = [ln.strip() for ln in stripped_text.splitlines() if ln.strip()]
        if len(lines) >= 2:
            tail = lines[-1]
            if len(tail) >= 15:
                prefix_body = stripped_text[: -len(tail)]
                if f'"{tail}"' in prefix_body or f"“{tail}”" in prefix_body:
                    return tail

        # 2c. Pure unfinished pre-tool monologue (e.g. "Wait—let's get a reaction GIF via gif_tool.py ... Let's run gif_tool.py.")
        if re.match(
            r"(?is)^\s*(?:[^\w\s]*|\S+/)?\s*(?:calibrating response\.\.\.|wait[—\-:]\s*let's\b)[\s\S]*?(?:let's\s+(?:run|check|call|fetch|get|use)\s+[^\n]+)\s*$",
            stripped_text,
        ):
            return ""

    return text


def strip_internal_cli_chatter(text: str) -> str:
    """Strip Antigravity CLI system messages, background task envelopes, and wait chatter."""
    if not text:
        return ""

    # Collapse repetitive patterns first to prevent runaway repetitive loops
    text = dedup_repetitive_patterns(text, max_repeats=3)

    # Strip leaked model chain-of-thought / prompt-rule deliberation before code-span masking
    text = strip_leaked_chain_of_thought(text)
    if not text:
        return ""

    # 1. System / task XML envelope removals (anchored to line start so inline mentions are not eaten to EOF)
    text = re.sub(
        r"(?is)(?:Queries completed with no tool calls|Roundtrip complete)\.\s*Raising signal for pending messages\.\.\.[\s\S]*?Floated messages complete\.\s*Continuing execution\.?\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?is)\[MESSAGE FROM BACKGROUND TASK\s*\(id:[^\)]+\)\][\s\S]*?(?:Floated messages complete\.\s*Continuing execution\.?\s*|(?=\n\s*#{1,6}\s)|\Z)",
        "",
        text,
    )
    text = re.sub(
        r"(?m)^\s*The following is a\s*<\s*SYSTEM_MESSAGE\s*>[\s\S]*?(?:<\s*/\s*SYSTEM_MESSAGE\s*>|\}\s*(?=[A-Z#])|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^\s*<\s*(?:SYSTEM_MESSAGE|RECEIVED_TASK_NOTIFICATION|NOTIFICATION|WAITING_FOR_TASKS_OUTPUT)\s*>[\s\S]*?<\s*/\s*(?:SYSTEM_MESSAGE|RECEIVED_TASK_NOTIFICATION|NOTIFICATION|WAITING_FOR_TASKS_OUTPUT)\s*>\s*\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Unclosed standalone system / task notification blocks on their own line extending to EOF
    text = re.sub(
        r"(?m)^\s*<\s*(?:SYSTEM_MESSAGE|RECEIVED_TASK_NOTIFICATION|NOTIFICATION|WAITING_FOR_TASKS_OUTPUT)\s*>\s*\n?[\s\S]*?$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"An async(?:hronous)? task (?:[a-zA-Z0-9_\-\/]+ )?has completed:\s*[^\n]+(?:\s*\(State:[^\)]+\))?(?:\s*Result payload:\s*\d+)?(?:\s*Task output:\s*(?:\[[^\]\r\n]*\]|[^\r\n]*))?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?is)(?:^|\n)\s*An async(?:hronous)? task (?:[a-zA-Z0-9_\-\/]+ )?has completed"
        r"(?:"
        r"[\s\S]*?<end of task output>"
        r"|\s+with exit code:?\s*-?\d+\.?[\s\S]*?(?:(?:Log:\s*file://[^\n]+(?:\n|$))|(?:\n\s*\}\s*\n+(?=[A-Za-z#*📸🍌]))|(?:\n\s*\]\s*\n+(?=[A-Za-z#*📸🍌]))|(?=\n\s*(?:#{1,6}\s|\*\*|📸|🍌|✅|⚠️))|\Z)"
        r")",
        "",
        text,
    )
    text = re.sub(
        r"A subagent has completed[\s\S]*?<end of subagent output>",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Shield all fenced code blocks and inline code spans before line/phrase chatter scrubbing
    text, code_spans = mask_code_spans(text, prefix="\x00SAFETY_SPAN_")

    text = re.sub(
        r"(?m)^\s*Wait for at least one of the background tasks to complete:?[\s\S]*?(?=\n\n|\Z)",
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
        r"(?m)^\s*YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[\s\S]*?(?:DO NOTHING ELSE\.?|\n\s*\n)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^\s*An async(?:hronous)? command is running\.[\s\S]*?(?:Do not poll or call additional tools until the background task finishes\.?\s*\n?)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?m)^\s*Task log:\s*`?(?:file://)?[^\n`]+`?\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*Task:\s*[^\n]+,\s*Status:\s*[^\n]+\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*(?:Match:\s*[^\n]+\n?)+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"Task id\s+[\"'][^\"']+[\"']\s+(?:was\s+canceled|finished|completed|failed)[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"An async(?:hronous)? task (?:[a-zA-Z0-9_\-\/]+ )?has completed:\s*[^\n]+(?:\s*\(State:[^\)]+\))?(?:\s*Result payload:\s*\d+)?(?:\s*Task output:\s*(?:\[[^\]\r\n]*\]|[^\r\n]*))?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip raw command stdout sentinels and trailing script result JSON blocks
    text = re.sub(
        r"(?m)^\s*===+[^=\n]+===+\s*\n\s*\{[\s\S]*?\n\}\s*\n*",
        "",
        text,
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
    res = restore_code_spans(res, code_spans, prefix="\x00SAFETY_SPAN_")
    return re.sub(r"\n{3,}", "\n\n", res).strip()


def is_internal_cli_leak(text: str) -> bool:
    """Return True if text contains only internal CLI artifacts, task wait chatter, or empty sentinels."""
    if not text or not isinstance(text, str):
        return True
    orig_lower = text.lower().strip()
    if "waiting_for_tasks_output" in orig_lower:
        cleaned_wait = strip_internal_cli_chatter(text).strip()
        if not cleaned_wait or is_internal_cli_leak(cleaned_wait):
            return True
    if "wait for at least one of the background tasks to complete" in orig_lower:
        cleaned_wait = strip_internal_cli_chatter(text).strip()
        if not cleaned_wait or is_internal_cli_leak(cleaned_wait):
            return True
    if re.match(r"^process\s+[^\n]+?\s+completed with exit code", orig_lower):
        return True
    if orig_lower.startswith("tool is running as a background task with task id"):
        return True
    # Option B background execution placeholders
    if re.search(r"\b(?:task update|background task(?: is)? running|please stand by)\b", orig_lower):
        is_substantive = len(text.strip()) > 300 and ("\n\n" in text or "###" in text)
        if not is_substantive:
            return True
    # Asynchronous task / background execution wait leaks
    if re.search(r"\ban async(?:hronous)?\s+(?:background\s+)?(?:command|task|job|process)\b", orig_lower):
        if any(w in orig_lower for w in ("running", "wait", "silent", "tools", "completed", "finish")):
            is_substantive = len(text.strip()) > 300 and ("\n\n" in text or "###" in text)
            if not is_substantive:
                return True
    if (
        "continuing silent background execution" in orig_lower
        or "waitmsbeforeasync:" in orig_lower
        or "no response text needed while waiting" in orig_lower
        or "wait silently for the next event" in orig_lower
        or "please do not call any more tools until the task finishes" in orig_lower
        or "do not poll or call additional tools" in orig_lower
    ):
        is_substantive = len(text.strip()) > 300 and ("\n\n" in text or "###" in text)
        if not is_substantive:
            return True

    if (
        orig_lower.startswith("an async task has completed")
        or orig_lower.startswith("an asynchronous task has completed")
        or re.match(r"^an async(?:hronous)? task (?:[a-zA-Z0-9_\-\/]+ )?has completed", orig_lower)
    ):
        return True
    if orig_lower.startswith("task id ") and "finished with result" in orig_lower:
        return True
    if orig_lower.startswith("an async command is running") or "do not poll or call additional tools until the background task finishes" in orig_lower:
        return True
    if orig_lower.startswith("task log:") or re.match(r"^task log:\s*`?(?:file://)?", orig_lower):
        return True

    # Upstream Model Routing & API Gateway Failures
    if (
        'not found for api version' in orig_lower
        or 'modelservice.listmodels' in orig_lower
        or ('error: not_found' in orig_lower and 'models/' in orig_lower)
        or 'invalid model selection' in orig_lower
        or 'not recognized as a known model' in orig_lower
    ):
        return True

    # Antigravity CLI Google Auth & Eligibility Failures
    if (
        "eligibility check failed" in orig_lower
        or "failed to get profile picture" in orig_lower
        or "failed to get user info" in orig_lower
        or "lh3.googleusercontent.com" in orig_lower
        or "googleapis.com/oauth2/v2/userinfo" in orig_lower
        or "net/http: tls handshake timeout" in orig_lower
    ):
        is_substantive = len(text.strip()) > 300 and ("\n\n" in text or "###" in text)
        if not is_substantive:
            return True

    # System message echoes and malformed tool call artifacts
    if "<system_message>" in orig_lower or "the following is a <system_message>" in orig_lower:
        cleaned_msg = strip_internal_cli_chatter(text).strip()
        words_msg = [w for w in cleaned_msg.split() if any(c.isalnum() for c in w)]
        if len(words_msg) < 5:
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
        "null",
        "}",
        "]",
        ")",
        ">",
        "{",
        "[",
        "(",
        "<",
        "}}",
        "]}",
        "})",
        "*(response completed, but no text output was generated)*",
        "no content generated yet",
        "no content generated yet.",
        "... [repetitive lines truncated] ...",
        "... [repetitive output truncated] ...",
    ):
        return True
    if lower.startswith("error: interrupted") or lower == "interrupted":
        return True

    # Agent runtime meta-reasoning leaks
    if (
        "in this agent runtime" in lower
        or "waiting silently without text output" in lower
        or "if we output visible text, the turn ends" in lower
        or "and the task finishes:" in lower
    ):
        return True

    # Standalone pure CLI status, process completion, or task completion notices
    if re.match(r"^process\s+[^\n]+?\s+completed with exit code", lower):
        return True
    if lower.startswith("an async task has completed") or lower.startswith("an asynchronous task has completed"):
        return True

    # Option B chatter on cleaned text (suppress only if not substantive)
    is_substantive = len(cleaned.strip()) > 300 and ("\n\n" in cleaned or "###" in cleaned)
    if not is_substantive:
        lines_non_empty = [l.strip() for l in cleaned.splitlines() if l.strip()]
        if lines_non_empty and all(
            re.match(r"^(?:-\s*)?(?:a?waiting|wait)\s+", l, re.IGNORECASE)
            for l in lines_non_empty
        ):
            return True
        if re.search(r"\ba?wait(?:ing)?\s+for\s+(?:at\s+least\s+one\s+of\s+the\s+)?(?:background\s+)?task", lower):
            return True
        if re.search(r"\ba?wait(?:ing)?\s+for\s+task[:\s]", lower):
            return True
        if re.search(r"\ba?wait(?:ing)?\s+for\s+[a-f0-9\-]+(?:/task-\d+)?\s+to\s+(?:complete|finish)", lower):
            return True
        if re.search(r"\ba?wait(?:ing)?\s+for\s+(?:command|process)\s+to\s+(?:finish|complete)", lower):
            return True
        if re.search(r"\ba?waiting\s+(?:isolated|execution|grid|search|task|command|background|process)\b", lower):
            return True

    # If remaining lines are exclusively internal daemon/system/test lines without user-facing content
    remaining_lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    if remaining_lines and all(
        re.match(
            r"^(\[BridgeDaemon\]|\[BridgeState\]|Process\s+[^\n]+completed|Ran \d+ test|OK$|FAILED|\.\.\.|-{5,}|<end of|<RECEIVED_TASK_NOTIFICATION|</?WAITING_FOR_TASKS_OUTPUT|Wait for at least one)",
            l,
            re.IGNORECASE,
        )
        for l in remaining_lines
    ):
        return True

    # Standalone pure CLI status or task completion notices without substantive user content
    words = [w for w in cleaned.split() if any(c.isalnum() for c in w)]
    if not words and not any(ord(c) > 127 for c in cleaned):
        return True
    # Stray CJK / punctuation empty-turn sampler tokens (e.g. "即是", "...") with no ASCII words or emojis
    if len(cleaned) <= 12 and not any(c.isascii() and c.isalnum() for c in cleaned):
        if all(not (0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF) for c in cleaned):
            return True
    if len(words) <= 25:
        if "awaiting" in lower and any(k in lower for k in ("results", "execution", "task", "search", "grid", "eval")):
            return True
        if "root agent idle; waiting up to" in lower:
            return True
        if re.search(r"(?<!`)\bterminating\s+\d+\s+background\s+task\(s\)\s+on\s+exit\b", lower):
            return True
        if lower.startswith("task id") and ("canceled" in lower or "completed" in lower or "failed" in lower):
            return True
        if "waiting for task" in lower or "wait for task" in lower or "wait for background task" in lower or "awaiting task" in lower:
            return True
        if "no other work to do" in lower:
            return True
        if lower.startswith("wait for") and ("complete" in lower or "finish" in lower):
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


def is_transient_auth_error(text: str) -> bool:
    """Return True if text contains transient Google auth or eligibility check failures."""
    if not text or not isinstance(text, str):
        return False
    lower = text.lower()
    return any(sig in lower for sig in (
        "eligibility check failed",
        "failed to get profile picture",
        "failed to get user info",
        "lh3.googleusercontent.com",
        "googleapis.com/oauth2/v2/userinfo",
        "authentication failed or timed out",
    ))
