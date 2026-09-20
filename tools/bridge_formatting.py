"""
Zero Discord Bridge - Formatting & Text Sanitization Module
Encapsulates all pure text transformations, LaTeX cleanup, Discord chunking,
credential scrubbing, and thread titling.
"""

import html
import json
import os
import re
import urllib.request
from pathlib import Path


def format_command_preview(cmd_raw: str, max_len: int = 80) -> str:
    """Format command string for Discord status previews, stripping SSH boilerplate and showing host."""
    lines_list = cmd_raw.strip().splitlines()
    first_line = lines_list[0].strip() if lines_list else ""
    host_1 = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

    if first_line.startswith("ssh "):
        host_tag = ""
        if host_1 in first_line:
            host_tag = f"[{host_1}]"
        elif host_2 in first_line:
            host_tag = f"[{host_2}]"

        parts = re.split(rf"(?:{re.escape(host_1)}|{re.escape(host_2)})\s+", first_line, maxsplit=1)
        if len(parts) > 1:
            inner_cmd = parts[1].strip().strip('"').strip("'")
            snip = inner_cmd[:max_len]
            return f"Running {host_tag}: {snip}..."

    snip = first_line[:max_len]
    return f"Running: {snip}..."


def convert_markdown_tables(text: str) -> str:
    """Convert raw markdown pipe tables into mobile-friendly Discord card lists with subtext."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect markdown table header followed by separator (|---|---|)
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*[-:]+[-| :]*$", lines[i + 1]):
            headers = [re.sub(r"^\*\*|\*\*$", "", c).strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip header and separator
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                table_rows.append(cells)
                i += 1

            if not table_rows:
                continue

            # Check if column 0 is a Section/Group and column 1 is the item/metric
            has_section_col = False
            if len(headers) >= 4 and headers[0].lower() in ("segment", "section", "category", "group", "type"):
                has_section_col = True

            # Detect if this is a true comparison table
            # True comparison: (1) header 0 is a feature/criteria/capability keyword, OR (2) header 1..N are product/system/option names (not attribute words)
            is_comparison = False
            attr_words = {
                "count", "status", "notes", "note", "details", "detail", "description",
                "value", "val", "cost", "price", "port", "host", "ip", "url", "link",
                "date", "time", "type", "size", "action", "result", "finding", "resolution",
                "state", "reason", "error", "percent", "percentage", "progress", "unit"
            }
            comparison_lead_words = {
                "feature", "criteria", "aspect", "comparison", "vs", "versus",
                "capability", "dimension", "spec", "specification", "category",
                "attribute", "service", "item", "area", "property", "function", "metric", "target"
            }

            check_headers = headers[2:] if has_section_col else headers[1:]
            h0 = re.sub(r"[^a-z0-9_-]", "", headers[0].lower())
            other_headers = [re.sub(r"[^a-z0-9_-]", "", h.lower().strip()) for h in check_headers]

            if has_section_col:
                is_comparison = True
            elif len(headers) >= 3:
                if h0 in comparison_lead_words:
                    if not any(oh in attr_words for oh in other_headers):
                        is_comparison = True
                elif not any(oh in attr_words for oh in other_headers):
                    is_comparison = True

            current_group = ""
            for row in table_rows:
                if not row or not any(row):
                    continue

                if has_section_col:
                    group_cell = re.sub(r"^\*\*|\*\*$", "", row[0]).strip() if len(row) > 0 else ""
                    if group_cell:
                        current_group = group_cell
                        out.append(f"\n### {current_group}")

                    metric_name = re.sub(r"^\*\*|\*\*$", "", row[1]).strip() if len(row) > 1 else ""
                    if not metric_name:
                        continue

                    # Multi-system comparison sub-items
                    vals = []
                    for col_idx in range(2, len(headers)):
                        if col_idx < len(row):
                            val = row[col_idx].strip()
                            clean_col = re.sub(r"^\*\*|\*\*$", "", headers[col_idx]).strip()
                            if val and val not in ("—", "-"):
                                vals.append(f"*{clean_col}*: {val}")
                    if vals:
                        out.append(f"- **{metric_name}**: " + " · ".join(vals))
                    else:
                        out.append(f"- **{metric_name}**")
                else:
                    first = re.sub(r"^\*\*|\*\*$", "", row[0]).strip()
                    if not first:
                        continue

                    if is_comparison and len(headers) >= 3:
                        out.append(f"- **{first}**:")
                        for col_idx in range(1, len(headers)):
                            if col_idx < len(row):
                                val = row[col_idx].strip()
                                clean_col = re.sub(r"^\*\*|\*\*$", "", headers[col_idx]).strip()
                                if val and val not in ("—", "-"):
                                    out.append(f"  - *{clean_col}*: {val}")
                    elif len(headers) == 2 or len(row) == 2:
                        val = row[1].strip() if len(row) > 1 else ""
                        out.append(f"- **{first}**: {val}")
                    else:
                        second = row[1].strip() if len(row) > 1 else ""
                        if second in ("—", "-"):
                            second = ""
                        notes_parts = [c.strip() for c in row[2:] if c.strip() and c.strip() not in ("—", "-")]
                        notes = " · ".join(notes_parts)

                        if second and notes:
                            if "(" not in second and len(second) <= 25:
                                out.append(f"- **{first}** ({second}): {notes}")
                            else:
                                out.append(f"- **{first}**: {second} · {notes}")
                        elif second:
                            out.append(f"- **{first}** ({second})")
                        elif notes:
                            out.append(f"- **{first}**: {notes}")
                        else:
                            out.append(f"- **{first}**")
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


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


def format_for_discord(text: str) -> str:
    """Format markdown for clean Discord presentation:
    - Strips file:/// markdown links which render broken on Discord
    - Converts GitHub alerts (> [!NOTE]) to clean emoji callouts
    - Converts broken markdown pipe tables into clean mobile cards
    - Preserves standard https:// links
    - Strips internal system task envelopes and intermediate progress chatter
    """
    if not text:
        return ""

    # 1. Clean file:/// links: [`/path`](file:///path) -> `/path`, [path](file:///path) -> `path`, bare file:///path -> `/path`
    def clean_file_link(m):
        inner = m.group(1).strip()
        if inner.startswith("`") and inner.endswith("`"):
            return inner
        return f"`{inner}`"

    text = re.sub(r"\[([^\]]+)\]\(file://[^\)]*\)", clean_file_link, text)
    text = re.sub(r"(?<![\w`\(])file://(/[^\s\)\>]+?)(?=[.,;:?!]?(?:\s|$|\)))", r"`\1`", text)

    # 1b. Normalize Discord hyperlinks:
    # Discord breaks when bold/italics wrap the outside of brackets (**[label](url)**)
    # or when emojis are inside brackets. Also, raw URLs with underscores inside parentheses
    # can trigger italic markdown unless wrapped in angle brackets (< >).
    def normalize_discord_links(m):
        prefix_outer = m.group(1) or ""
        label = m.group(2).strip()
        raw_url = m.group(3).strip()
        clean_url = raw_url.strip("<>").strip()

        # Preserve Tenor/Giphy links so Discord media player can embed them
        if "tenor.com/view/" in clean_url or "giphy.com/gifs/" in clean_url:
            return f"[{label}]({clean_url})"

        # If label starts with an emoji, move it outside the bracket for clean parsing
        emoji_match = re.match(r"^([\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50]+|\:\w+\:)\s*(.*)$", label)
        if emoji_match and emoji_match.group(2).strip():
            lead_emoji = emoji_match.group(1)
            inner_label = emoji_match.group(2).strip()
        else:
            lead_emoji = ""
            inner_label = label

        # Discord's link parser requires plain text inside brackets ([]).
        # Any bold (**), italic (*), or backtick (`) formatting inside or outside brackets breaks the link.
        clean_inner = inner_label.strip("*_`").strip()

        # If label is identical to the target URL, collapse redundant [url](<url>) into clean <url>
        if clean_inner.lower() == clean_url.lower() or clean_inner.lower().rstrip("/") == clean_url.lower().rstrip("/"):
            return f"<{clean_url}>"

        link_str = f"[{clean_inner}](<{clean_url}>)"

        if lead_emoji:
            return f"{lead_emoji} {link_str}"
        return link_str

    pattern = r"(\*\*|\*)?\[([^\]]+)\]\(<?(https?://[^\)>]+)>?\)(?:\1)?"
    text = re.sub(pattern, normalize_discord_links, text)

    # 2. Strip internal action/progress pseudo-tags (e.g. <Action: ...>)
    text = re.sub(r"<\s*action:[^>]+>", "", text, flags=re.IGNORECASE)

    # 3. Convert GitHub-style alerts to emoji blockquotes
    alerts = {
        "NOTE": "ℹ️ **Note:**",
        "TIP": "💡 **Tip:**",
        "IMPORTANT": "📌 **Important:**",
        "WARNING": "⚠️ **Warning:**",
        "CAUTION": "🛑 **Caution:**",
    }
    for alert, emoji in alerts.items():
        text = re.sub(rf"^>\s*\[!{alert}\]", f"> {emoji}", text, flags=re.MULTILINE | re.IGNORECASE)

    # 3. Collapse unsupported h4+ headers (####+) to h3 (###) so Discord renders them as headings
    text = re.sub(r"^(#{4,})\s*(.*)$", r"### \2", text, flags=re.MULTILINE)

    # 4. Convert markdown pipe tables to clean Discord mobile cards
    text = convert_markdown_tables(text)

    # 4b. Normalize literal Unicode bullets (•) to native Discord markdown list syntax (-)
    # Discord treats literal • as plain paragraph text, breaking mobile hanging indents and creating blank line gaps before sublists.
    text = re.sub(r"^([ \t]*(?:>[ \t]*)*)•[ \t]*", r"\1- ", text, flags=re.MULTILINE)

    # 4c. Tighten loose lists where a parent list item is followed by an empty line before its sub-bullets
    text = re.sub(r"(^[ \t]*(?:[-*]|\d+\.)\s+[^\n]+)\n\n+([ \t]{2,}(?:[-*]|\d+\.)\s+)", r"\1\n\2", text, flags=re.MULTILINE)

    # 5. Strip internal agent task lifecycle envelopes, CLI leak lines, and intermediate progress chatter
    text = strip_internal_cli_chatter(text)

    # 6. Sanitize reaction GIFs: verify Tenor links are live (HTTP 200) and replace 404s with working fallbacks
    text = sanitize_reaction_gifs(text)

    # 8. Ensure handoff envelopes include physical Discord mentions for peer bots
    try:
        from tools.handoff import ensure_handoff_mentions
        text = ensure_handoff_mentions(text)
    except Exception:
        pass

    return text.strip()


def sanitize_reaction_gifs(text: str) -> str:
    """Probe Tenor and Giphy links in Discord output. If a reaction GIF returns HTTP 404,
    replace it with a live dynamic GIF or strip the line so Discord never renders broken previews.
    Normalizes all GIF links to [GIF](url) format and repositions the GIF hyperlink to the
    very end of the message (immediately before any [CHOICES: ...] / [OPTIONS: ...] block)."""
    if not text or ("tenor.com/view/" not in text and "giphy.com/gifs/" not in text):
        return text

    gif_url_pattern = r"https?://(?:www\.)?(?:tenor\.com/view/[a-zA-Z0-9_\-]+|giphy\.com/gifs/[a-zA-Z0-9_\-]+)"
    gif_urls = re.findall(gif_url_pattern, text)
    filler_words = {"gif", "gifs", "the", "a", "an", "and", "or", "of", "in", "to", "for", "with", "view", "hd"}

    for url in set(gif_urls):
        is_ok = False
        final_dest_url = url
        try:
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    raw_geturl = resp.geturl() if hasattr(resp, "geturl") else url
                    final_url = raw_geturl if isinstance(raw_geturl, str) else url
                    if "tenor.com/view/" in url and final_url != url:
                        orig_slug = url.split("/view/")[-1].lower()
                        final_slug = final_url.split("/view/")[-1].lower()
                        orig_words = {w for w in re.findall(r"[a-z0-9]+", orig_slug) if w not in filler_words and not w.isdigit()}
                        final_words = {w for w in re.findall(r"[a-z0-9]+", final_slug) if w not in filler_words and not w.isdigit()}
                        overlap = orig_words & final_words
                        if not overlap and (orig_words or final_words):
                            print(f"[BridgeFormatting] ⚠️ Hallucinated Tenor URL rejected: {url} redirected to {final_url} (0 word overlap)", file=sys.stderr)
                            is_ok = False
                        else:
                            is_ok = True
                            final_dest_url = final_url
                    else:
                        is_ok = True
        except Exception:
            is_ok = False

        if is_ok and final_dest_url != url:
            text = text.replace(url, final_dest_url)
            url = final_dest_url

        if not is_ok:
            try:
                from tools.gif_tool import get_contextual_gif
                fallback = get_contextual_gif("shrug")
                if fallback and fallback.get("url"):
                    text = text.replace(url, fallback["url"])
                    url = fallback["url"]
                else:
                    text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", text)
                    continue
            except Exception:
                text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", text)
                continue

    # Find remaining valid URLs
    remaining_urls = re.findall(gif_url_pattern, text)
    if not remaining_urls:
        return text.strip()

    # Deduplicate while preserving original appearance order
    unique_urls = list(dict.fromkeys(remaining_urls))

    # Regex matching markdown links, angle-bracketed URLs, or bare GIF URLs
    full_gif_re = re.compile(
        r"\[[^\]]*\]\(\s*<?" + gif_url_pattern + r">?\s*\)|"
        r"<" + gif_url_pattern + r">|"
        r"(?<!\(|<|\[)" + gif_url_pattern
    )

    # Strip existing GIF links from lines
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        line_stripped = line.strip()
        if full_gif_re.fullmatch(line_stripped):
            continue
        cleaned_line = full_gif_re.sub("", line).rstrip()
        if cleaned_line.strip() or not line_stripped:
            cleaned_lines.append(cleaned_line)

    raw_body = "\n".join(cleaned_lines)
    collapsed_body = re.sub(r"\n{3,}", "\n\n", raw_body).strip()
    gif_block = "\n".join(f"[GIF]({u})" for u in unique_urls)

    # Place GIF immediately before trailing [CHOICES: ...] or [OPTIONS: ...] block, or at the end
    choices_pattern = re.compile(r"(\n*\s*\[(?:CHOICES|OPTIONS):\s*[^\]]+\]\s*)$", re.IGNORECASE)
    choices_match = choices_pattern.search(collapsed_body)
    if choices_match:
        content_before = collapsed_body[:choices_match.start()].rstrip()
        choice_block = choices_match.group(1).strip()
        if content_before:
            return f"{content_before}\n\n{gif_block}\n\n{choice_block}"
        else:
            return f"{gif_block}\n\n{choice_block}"
    else:
        if collapsed_body:
            return f"{collapsed_body}\n\n{gif_block}"
        else:
            return gif_block


def strip_reaction_gifs(text: str) -> str:
    """Completely remove all Tenor, Giphy, and reaction GIF links from text,
    stripping orphan lines and collapsing surplus whitespace."""
    if not text or ("tenor.com" not in text and "giphy.com" not in text and ".gif" not in text.lower()):
        return text.strip()

    gif_url_pattern = r"https?://(?:www\.)?(?:tenor\.com/(?:view/)?[a-zA-Z0-9_\-]+|media\.tenor\.com/[a-zA-Z0-9_\-/]+|giphy\.com/gifs/[a-zA-Z0-9_\-]+|[^\s\)\>\]]+\.gif\b)"
    full_gif_re = re.compile(
        r"\[[^\]]*\]\(\s*<?" + gif_url_pattern + r">?\s*\)|"
        r"<" + gif_url_pattern + r">|"
        r"(?<!\(|<|\[)" + gif_url_pattern,
        re.IGNORECASE,
    )

    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        line_stripped = line.strip()
        if full_gif_re.fullmatch(line_stripped):
            continue
        cleaned_line = full_gif_re.sub("", line).rstrip()
        if cleaned_line.strip() or not line_stripped:
            cleaned_lines.append(cleaned_line)

    raw_body = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", raw_body).strip()


def harvest_transcript_response(conv_id: str | None) -> str | None:
    """Harvest completed response from transcript files if stdout was truncated or cut off."""
    try:
        from tools.bridge_runner import harvest_transcript_response as _htr
        return _htr(conv_id)
    except Exception:
        return None


class AgyStreamParser:
    """Deterministic state-machine parser for agy stream-json output."""

    def __init__(self, conv_id: str | None = None):
        self.conv_id = conv_id
        self.accumulated_segment: list[str] = []
        self.last_substantive_response: str = ""
        self.final_result_response: str = ""
        self.error_response: str = ""
        self.is_explicit_silence: bool = False

    @staticmethod
    def _is_silence_or_placeholder(text: str) -> bool:
        if not text or not isinstance(text, str):
            return True
        return is_internal_cli_leak(text)

    def process_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return

        ev_type = event.get("event") or event.get("type")

        # 1. Init event: extract conversation_id if present
        if ev_type == "init" or "conversation_id" in event:
            cid = event.get("conversation_id")
            if cid:
                self.conv_id = cid

        # 2. Result event: contains overall turn summary
        if ev_type == "result" or "result" in event:
            res = event.get("result", {})
            if isinstance(res, dict):
                if res.get("response"):
                    self.final_result_response = res["response"]
                if res.get("error"):
                    self.error_response = f"Error: {res.get('error')}"
                if res.get("conversation_id"):
                    self.conv_id = res["conversation_id"]
            elif isinstance(event.get("response"), str):
                self.final_result_response = event["response"]

        # 3. Step update event
        elif ev_type == "step_update" or "step_update" in event:
            step = event.get("step_update", {})
            if not isinstance(step, dict):
                return

            stype = step.get("step_type")
            tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")

            if stype in ("tool",) or tname:
                # Tool execution: clear pre-tool narration
                self.accumulated_segment.clear()

            elif stype in ("system_message", "system"):
                # Asynchronous system event: preserve substantive content generated prior
                curr = "".join(self.accumulated_segment).strip()
                if curr and not self._is_silence_or_placeholder(curr):
                    self.last_substantive_response = curr
                self.accumulated_segment.clear()

            elif stype == "agent_response":
                delta = step.get("text_delta") or step.get("text") or step.get("content")
                if delta and isinstance(delta, str):
                    self.accumulated_segment.append(delta)

                if step.get("state") == "DONE":
                    curr = "".join(self.accumulated_segment).strip()
                    if curr:
                        if self._is_silence_or_placeholder(curr):
                            if curr.lower() in ("[no_reply]", "no_reply", "[no_op]", "no_op", "reply:none", "reply: none", "none") or is_internal_cli_leak(curr):
                                self.is_explicit_silence = True
                        else:
                            self.last_substantive_response = curr

        elif ev_type in ("tool", "tool_call", "tool_use"):
            self.accumulated_segment.clear()

        elif ev_type in ("system_message", "system"):
            curr = "".join(self.accumulated_segment).strip()
            if curr and not self._is_silence_or_placeholder(curr):
                self.last_substantive_response = curr
            self.accumulated_segment.clear()

        elif ev_type in ("content", "message", "text", "delta"):
            content = event.get("content") or event.get("text") or event.get("delta")
            if content and isinstance(content, str):
                self.accumulated_segment.append(content)

    def process_line(self, line: str) -> bool:
        line_s = line.strip()
        if not line_s:
            return False

        # Find JSON boundaries
        start = line_s.find("{")
        end = line_s.rfind("}") + 1
        if start != -1 and end > start:
            json_substr = line_s[start:end]
            try:
                ev = json.loads(json_substr)
                self.process_event(ev)
                return True
            except Exception:
                decoder = json.JSONDecoder()
                idx = start
                parsed_any = False
                while idx < len(line_s):
                    while idx < len(line_s) and line_s[idx] != "{":
                        idx += 1
                    if idx >= len(line_s):
                        break
                    try:
                        ev, end_idx = decoder.raw_decode(line_s, idx)
                        self.process_event(ev)
                        parsed_any = True
                        idx = end_idx
                    except Exception:
                        idx += 1
                return parsed_any
        return False

    def get_final_response(self, fallback_result: str = "") -> str:
        curr = "".join(self.accumulated_segment).strip()

        # Check if the current segment is an explicit silence request or internal CLI leak
        if curr and (curr.lower() in ("[no_reply]", "no_reply", "[no_op]", "no_op", "reply:none", "reply: none", "none") or is_internal_cli_leak(curr)):
            if not self.last_substantive_response or self._is_silence_or_placeholder(self.last_substantive_response):
                return "[NO_REPLY]"

        # 1. Check current segment after last tool
        if curr and not self._is_silence_or_placeholder(curr):
            clean = re.sub(r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", curr, flags=re.IGNORECASE).strip()
            clean = strip_internal_cli_chatter(clean)
            if clean and not self._is_silence_or_placeholder(clean):
                return clean

        # 2. Check last substantive response before an asynchronous system message
        if self.last_substantive_response and not self._is_silence_or_placeholder(self.last_substantive_response):
            clean = re.sub(r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", self.last_substantive_response, flags=re.IGNORECASE).strip()
            clean = strip_internal_cli_chatter(clean)
            if clean and not self._is_silence_or_placeholder(clean):
                return clean

        # 3. Check final result response from agy
        frr = self.final_result_response.strip() or fallback_result.strip()
        if frr:
            if frr.lower() in ("[no_reply]", "no_reply", "[no_op]", "no_op", "reply:none", "reply: none", "none") or is_internal_cli_leak(frr):
                if not self.last_substantive_response or self._is_silence_or_placeholder(self.last_substantive_response):
                    return "[NO_REPLY]"
            clean_frr = re.sub(r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", frr, flags=re.IGNORECASE).strip()
            clean_frr = strip_internal_cli_chatter(clean_frr)
            if clean_frr and not self._is_silence_or_placeholder(clean_frr):
                return clean_frr

        if self.is_explicit_silence:
            return "[NO_REPLY]"

        if self.error_response:
            return self.error_response

        return ""


def extract_agent_response(raw_text: str, conv_id: str | None = None) -> str:
    """Extract clean response text from agy output, supporting plain text, json, or stream-json."""
    if not raw_text:
        if conv_id:
            harvested = harvest_transcript_response(conv_id)
            if harvested and not is_internal_cli_leak(harvested):
                return format_for_discord(harvested)
        return "*(Response completed, but no text output was generated)*"

    # Thoroughly strip ANSI escape codes and terminal controls
    text = re.sub(r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw_text)

    # Check if this output is plain text (no JSON events present)
    has_json = False
    for line in text.splitlines():
        line_s = line.strip()
        if (line_s.startswith("{") and line_s.endswith("}")) or "\"event\":\"" in line_s or "\"conversation_id\":\"" in line_s:
            has_json = True
            break

    if not has_json:
        if is_internal_cli_leak(text):
            return "[NO_REPLY]"
        return format_for_discord(text)

    parser = AgyStreamParser(conv_id=conv_id)
    for line in text.splitlines():
        parser.process_line(line)

    final_resp = parser.get_final_response()

    if is_internal_cli_leak(final_resp):
        return "[NO_REPLY]"

    if final_resp:
        return format_for_discord(final_resp)

    # Fallback to on-disk transcript if conv_id is known
    target_cid = conv_id or parser.conv_id
    if target_cid:
        harvested = harvest_transcript_response(target_cid)
        if harvested and not is_internal_cli_leak(harvested):
            return format_for_discord(harvested)

    # Fallback filter for plain text outside JSON lines
    clean_lines = []
    for l in text.splitlines():
        l_str = l.strip()
        if not l_str or (l_str.startswith("{") and l_str.endswith("}")) or (l_str.startswith("[") and l_str.endswith("]")):
            continue
        if l_str.startswith("===") and l_str.endswith("==="):
            continue
        if "\"event\":\"" in l_str or "\"step_update\":\"" in l_str or "\"conversation_id\":\"" in l_str:
            continue
        clean_lines.append(l)

    if clean_lines:
        cand = format_for_discord("\n".join(clean_lines))
        if not is_internal_cli_leak(cand):
            return cand
        return "[NO_REPLY]"

    return "*(Response completed, but no text output was generated)*"


def chunk_text(text: str, max_len: int = 1980) -> list[str]:
    """Split text into Discord-safe chunks while preserving markdown code block integrity.
    - If text is slightly over limit (e.g. 1980-2100 chars), collapses redundant empty lines
      and trailing whitespace to squeeze it into a single message without splitting.
    - Preserves code block formatting across chunks.
    - If a code block crosses a split boundary, cleanly closes it in the first chunk and re-opens it in the next.
    - Prefers breaking before code blocks so blocks stay intact whenever possible.
    """
    cleaned = text.strip()
    if len(cleaned) <= max_len:
        return [cleaned]

    # Boundary squeeze: if barely over limit (<=2100 chars), collapse whitespace to fit in 1 message
    if len(cleaned) <= 2100:
        condensed = re.sub(r"\n{3,}", "\n\n", cleaned)
        condensed = re.sub(r"[ \t]+\n", "\n", condensed).strip()
        if len(condensed) <= max_len:
            return [condensed]

    lines = cleaned.splitlines(keepends=True)
    chunks = []
    current_chunk = []
    current_len = 0
    in_code_block = False
    code_lang = ""

    for idx, line in enumerate(lines):
        stripped = line.strip()
        is_code_fence = stripped.startswith("```")

        # If entering a code block, look ahead to closing fence to see if the entire block fits in current_chunk.
        # If it fits within the remaining budget of current_chunk, DO NOT break early (keeps handoff envelopes
        # and short code snippets intact with the preceding text).
        # Only break early if the block would overflow the current chunk AND current_chunk has text.
        if is_code_fence and not in_code_block and current_chunk:
            block_len = len(line)
            found_close = False
            for j in range(idx + 1, len(lines)):
                block_len += len(lines[j])
                if lines[j].strip().startswith("```"):
                    found_close = True
                    break
            if found_close and (current_len + block_len > max_len):
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0

        overhead = (len(code_lang) + 12) if in_code_block else 0

        # If a single line exceeds max_len on its own, flush current chunk and split the line
        if len(line) + overhead > max_len:
            if current_chunk:
                if in_code_block:
                    current_chunk.append("\n```\n")
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0
                if in_code_block:
                    prefix = f"```{code_lang}\n"
                    current_chunk.append(prefix)
                    current_len = len(prefix)

            rem = line
            effective_max = max_len - (len(code_lang) + 12 if in_code_block else 0)
            while len(rem) > effective_max:
                split_idx = rem.rfind(" ", 0, effective_max)
                if split_idx <= 0:
                    split_idx = effective_max
                sub_part = rem[:split_idx].rstrip()
                if in_code_block:
                    chunks.append(f"{sub_part}\n```")
                    rem = f"```{code_lang}\n" + rem[split_idx:].lstrip()
                else:
                    chunks.append(sub_part)
                    rem = rem[split_idx:].lstrip()

            if rem and rem.strip():
                current_chunk.append(rem)
                current_len += len(rem)
            continue

        if current_len + len(line) + overhead > max_len:
            if current_chunk:
                if in_code_block:
                    current_chunk.append("\n```\n")
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0
                if in_code_block:
                    prefix = f"```{code_lang}\n"
                    current_chunk.append(prefix)
                    current_len = len(prefix)

        if is_code_fence:
            if not in_code_block:
                in_code_block = True
                code_lang = stripped[3:].strip()
            else:
                in_code_block = False
                code_lang = ""

        current_chunk.append(line)
        current_len += len(line)

    if current_chunk:
        chunks.append("".join(current_chunk).strip())

    return [c for c in chunks if c]


def convert_markdown_to_mobile_html(md_text: str) -> str:
    """Render markdown to mobile-friendly, dark-mode HTML that opens natively in Chrome on Android/Pixel."""
    try:
        import markdown
        body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])
    except Exception:
        body = f"<pre>{html.escape(md_text)}</pre>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Zero Report</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    max-width: 850px;
    margin: 0 auto;
    padding: 16px 18px 40px 18px;
    background-color: #1e1f22;
    color: #dbdee1;
    font-size: 15px;
  }}
  h1, h2, h3, h4 {{
    color: #f2f3f5;
    font-weight: 600;
    margin-top: 1.4em;
    margin-bottom: 0.5em;
  }}
  h1 {{ font-size: 1.5em; border-bottom: 1px solid #35363c; padding-bottom: 8px; }}
  h2 {{ font-size: 1.3em; }}
  h3 {{ font-size: 1.1em; }}
  p {{ margin: 0.6em 0; }}
  code {{
    background: #2b2d31;
    color: #ebedef;
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'JetBrains Mono', 'Fira Code', Consolas, Monaco, monospace;
    font-size: 0.9em;
  }}
  pre {{
    background: #2b2d31;
    padding: 14px;
    border-radius: 8px;
    overflow-x: auto;
    border: 1px solid #35363c;
  }}
  pre code {{ background: none; padding: 0; font-size: 0.85em; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 16px 0;
    display: block;
    overflow-x: auto;
  }}
  th, td {{
    border: 1px solid #3f4147;
    padding: 8px 12px;
    text-align: left;
  }}
  th {{ background: #2b2d31; color: #ffffff; font-weight: 600; }}
  tr:nth-child(even) {{ background-color: #232428; }}
  a {{ color: #5865f2; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  blockquote {{
    border-left: 4px solid #5865f2;
    margin: 0;
    padding-left: 12px;
    color: #949ba4;
  }}
</style>
</head>
<body>
{body}
</body>
</html>"""


_SCRUB_TARGETS = None


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


def clean_discord_latex(text: str) -> str:
    """Convert raw LaTeX math notation into clean, native Discord markdown and Unicode symbols."""
    if not text:
        return text

    symbol_map = {
        r"\\alpha": "α", r"\\beta": "β", r"\\gamma": "γ", r"\\delta": "δ",
        r"\\epsilon": "ε", r"\\zeta": "ζ", r"\\eta": "η", r"\\theta": "θ",
        r"\\lambda": "λ", r"\\mu": "μ", r"\\pi": "π", r"\\rho": "ρ",
        r"\\sigma": "σ", r"\\tau": "τ", r"\\phi": "φ", r"\\omega": "ω",
        r"\\Delta": "Δ", r"\\Theta": "Θ", r"\\Lambda": "Λ", r"\\Sigma": "Σ",
        r"\\Omega": "Ω",
        r"\\cdot": "·", r"\\times": "×", r"\\div": "÷",
        r"\\leq?": "≤", r"\\geq?": "≥", r"\\neq": "≠", r"\\approx": "≈",
        r"\\pm": "±", r"\\to": "→", r"\\rightarrow": "→", r"\\leftarrow": "←",
        r"\\infty": "∞", r"\\partial": "∂", r"\\nabla": "∇",
        r"\\in": "∈", r"\\notin": "∉", r"\\subset": "⊂", r"\\subseteq": "⊆"
    }

    # 1. Convert block math $$...$$ and \[ ... \]
    def replace_block_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"\1/\2", inner)
        inner = re.sub(r"\\(?:text|mathrm|mathbf)\{([^}]+)\}", r"\1", inner)
        inner = re.sub(r"\\(?:left|right)", "", inner)
        return f"\n```\n{inner}\n```\n"

    text = re.sub(r"\$\$(.+?)\$\$", replace_block_math, text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", replace_block_math, text, flags=re.DOTALL)

    # 2. Convert inline math \( ... \)
    text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text)

    # 3. Convert inline math $...$ (ignoring currency like $50 or $100.00)
    def replace_inline_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"\1/\2", inner)
        inner = re.sub(r"\\(?:text|mathrm|mathbf)\{([^}]+)\}", r"\1", inner)
        inner = re.sub(r"\\(?:left|right)", "", inner)
        inner = inner.replace("\\", "")

        # Single variable or letter: render as italics (*d*)
        if len(inner) == 1 and inner.isalpha():
            return f"*{inner}*"
        return inner

    text = re.sub(r"(?<![\w\$])\$(?!\d)([^$\n]+?)\$(?![\w\$])", replace_inline_math, text)

    # 4. Clean HTML tags into Discord markdown (no raw <br> or <b>)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"<(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"<code>", "`", text, flags=re.IGNORECASE)
    text = re.sub(r"</code>", "`", text, flags=re.IGNORECASE)

    return text


def generate_concise_thread_title(prompt: str, target_words: int = 6) -> str:
    """Generate a clean, synthesized 5-6 word semantic thread title from the original prompt subject."""
    if not prompt:
        return "General Task Execution"

    clean = re.sub(r"^(thread:|parallel:|\/goal|\/plan|\/deep-research)\s*", "", prompt, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\[Attached file\(s\)[^\]]+\]", "", clean).strip()
    clean = re.sub(r"\[PREVIOUS SESSION CARRY-FORWARD CONTEXT\]:.*?(?=\[CURRENT USER PROMPT\]:|$)", "", clean, flags=re.DOTALL)
    clean = re.sub(r"\[CURRENT USER PROMPT\]:\s*", "", clean).strip()
    clean = re.sub(r"[#*_`~]", "", clean).strip()

    # Conversational Intent Cleaner
    clean_stripped = re.sub(
        r"^(we talked about|i'm concerned because|immediately some of those are|can you|could you|please|i need|i want to|i want|how do we|why would|what about|did we get|hey also|ok so|let's|lets|can we|we should|i think|is there a way to)\s+",
        "",
        clean,
        flags=re.IGNORECASE
    ).strip()

    low = clean_stripped.lower()

    # High-Confidence Domain Intent Mappings (5-6 words)
    if any(k in low for k in ["birthday", "birthdays", "bday", "friends and family", "friends & family"]):
        return "Friends and Family Contacts and Birthdays"
    elif any(k in low for k in ["ev9", "kia ev9", "marketcheck"]):
        return "Kia EV9 Dealership Listings Market Monitor"
    elif any(k in low for k in ["compaction", "rolling context", "context size", "wedged", "turn counter", "prefill"]):
        return "Context Compaction and Rolling Memory Architecture"
    elif any(k in low for k in ["tautulli", "plex status", "plex transcode", "plex down", "pms"]):
        return "Plex Media Server Alerts and Transcoding"
    elif any(k in low for k in ["sonarr", "radarr", "prowlarr", "indexer"]):
        return "Arr Media Indexer and Server Alerts"
    elif any(k in low for k in ["youtube music", "liked songs", "music playlist", "prime416"]):
        return "YouTube Music Playlist Sync and Discovery"
    elif any(k in low for k in ["google sheet", "sheets api", "push friends to sheets"]):
        return "Google Sheets Friends and Family Sync"
    elif any(k in low for k in ["openmessage", "sms", "rcs", "google message"]):
        return "Google Messages RCS and SMS Integration"
    elif any(k in low for k in ["d&d", "dungeons and dragons", "tabletop", "campaign lore"]):
        return "Dungeons and Dragons Lore and Notes"
    elif any(k in low for k in ["memory doctor", "doctor audit", "audit sidecar"]):
        return "Homelab Memory Store Health and Audit"
    elif any(k in low for k in ["reboot", "restart", "beacon", "in-flight"]):
        return "Bridge Lifecycle and Restart Architecture Engine"
    elif any(k in low for k in ["baseball", "big board", "stat blast", "scrapegurus"]):
        return "Baseball Analytics and Big Board Scraping"
    elif any(k in low for k in ["thread naming", "naming triggers", "auto-rename", "threaded convos"]):
        return "Thread Naming and Escalation Timeout Tuning"

    stopwords = {
        "ok", "so", "heres", "here", "a", "an", "the", "new", "issue", "problem",
        "question", "look", "looks", "like", "just", "well", "now", "hey", "can",
        "could", "would", "should", "please", "tell", "me", "my", "we", "our",
        "you", "your", "that", "this", "it", "its", "was", "were", "is", "are",
        "have", "has", "had", "do", "does", "did", "to", "for", "in", "on", "at",
        "from", "with", "about", "all", "of", "and", "or", "but", "if", "then",
        "when", "why", "how", "what", "which", "who", "run", "perform", "check",
        "analyze", "generate", "build", "investigate", "test", "minor", "comment",
        "wrong", "right", "good", "bad", "too", "also", "much", "many", "really",
        "still", "got", "get", "tried", "try", "seeing", "see", "think", "give",
        "want", "need", "make", "take", "using", "use"
    }

    words = [w for w in re.findall(r"[a-zA-Z0-9]+", clean_stripped) if len(w) > 1]
    meaningful = [w.capitalize() for w in words if w.lower() not in stopwords]

    if len(meaningful) >= 4:
        return " ".join(meaningful[:target_words])
    elif meaningful:
        context_words = [w.capitalize() for w in words if len(w) > 1 and w.capitalize() not in meaningful]
        combined = meaningful + context_words
        return " ".join(combined[:target_words])
    elif words:
        return " ".join([w.capitalize() for w in words[:target_words]])
    return "General Task Execution"


def parse_thread_title_tag(text: str) -> tuple[str, str | None]:
    """Parse [THREAD_TITLE: ...] tag from text and return (cleaned_text, title)."""
    match = re.search(r"\[THREAD_TITLE:\s*([^\]]+)\]", text, flags=re.IGNORECASE)
    if match:
        clean_text = re.sub(r"\[THREAD_TITLE:\s*([^\]]+)\]", "", text, flags=re.IGNORECASE).strip()
        title = match.group(1).strip()
        return clean_text, title
    return text, None


def synthesize_thread_title(prompt: str, response: str, max_words: int = 6) -> str:
    """
    Synthesize a sharp 4-6 word semantic thread title from the agent's answer/solution.
    Prioritizes explicit [THREAD_TITLE: ...] tags, leading headers/bold summaries from response,
    and falls back to synthesized response-prompt semantics.
    """
    if not response:
        return generate_concise_thread_title(prompt, target_words=max_words)

    # 1. Explicit tag
    _, explicit_tag = parse_thread_title_tag(response)
    if explicit_tag:
        clean_tag = re.sub(r"[#*_`~]", "", explicit_tag).strip()
        words = clean_tag.split()
        if words:
            return " ".join(words[:max_words])

    # 2. Extract leading bold text or markdown headers from response (ignoring generic status)
    status_prefixes = (
        "zero is online", "what was just deployed", "restart briefing", "task execution",
        "migrating deliverable", "online and ready", "lead with the result"
    )

    lines = [l.strip() for l in response.split("\n") if l.strip()]
    candidate = None

    for line in lines[:8]:
        # Markdown heading ### Header
        h_match = re.match(r"^#{1,3}\s+(?:\d+[\.\)]\s+)?([^\n]+)", line)
        if h_match:
            cand_text = re.sub(r"[#*_`~]", "", h_match.group(1)).strip()
            if not any(sp in cand_text.lower() for sp in status_prefixes) and len(cand_text.split()) >= 2:
                candidate = cand_text
                break

        # Leading bold **Header/Title**
        b_match = re.match(r"^\*\*(?:\d+[\.\)]\s+)?([^\*]+)\*\*", line)
        if b_match:
            cand_text = re.sub(r"[#*_`~]", "", b_match.group(1)).strip()
            if not any(sp in cand_text.lower() for sp in status_prefixes) and len(cand_text.split()) >= 2:
                candidate = cand_text
                break

    if candidate:
        words = [w for w in re.findall(r"[a-zA-Z0-9_-]+", candidate) if len(w) > 1]
        if len(words) >= 3:
            return " ".join([w.capitalize() for w in words[:max_words]])

    # 3. Fall back to clean prompt generator
    return generate_concise_thread_title(prompt, target_words=max_words)


def parse_interactive_choices(text: str, quick_choice_view_cls=None, button_choice_fn=None) -> tuple[str, any]:
    """Parse [CHOICES: opt1 | opt2] tag from text and return (cleaned_text, choice_view)."""
    matches = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", text))
    parsed_choices = []
    valid_match = None
    for m in matches:
        raw_choices = m.group(1).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [c.strip().strip("'\"`“”‘’").strip() for c in raw_choices.split(delim) if c.strip()]
        choices = [c for c in choices if c]
        if choices and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in choices):
            parsed_choices = choices
            valid_match = m

    if valid_match and parsed_choices and quick_choice_view_cls:
        clean_text = re.sub(r"\[CHOICES:\s*([^\]]+)\]", "", text).strip()
        view = quick_choice_view_cls(parsed_choices, callback_fn=button_choice_fn)
        return clean_text, view
    return text, None


def parse_agy_error(text: str) -> dict | None:
    """Extract and parse structured AGY_ERROR payload from output or stderr stream.

    The Antigravity CLI v1.2.6+ emits:
    AGY_ERROR: {"canonical_status":..., "code":..., "retryable":..., "error_id":..., "short_error":...}
    on stderr and exits with code 3 on agent/model API failures.
    """
    if not text:
        return None

    for line in text.splitlines():
        if "AGY_ERROR:" in line:
            payload_str = line.split("AGY_ERROR:", 1)[1].strip()
            try:
                data = json.loads(payload_str)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            m = re.search(r"\{.*?\}", payload_str)
            if m:
                try:
                    data = json.loads(m.group(0))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
    return None


def format_agy_error_message(err: dict, elapsed_sec: int = 0, pid_str: str = "") -> str:
    """Format structured AGY_ERROR payload into an actionable Discord diagnostic."""
    canonical = err.get("canonical_status") or err.get("status") or "API_FAILURE"
    code = err.get("code") or err.get("http_code") or err.get("grpc_code") or ""
    retryable = err.get("retryable")
    error_id = err.get("error_id") or err.get("id") or ""
    msg = (
        err.get("short_error")
        or err.get("message")
        or err.get("error")
        or "Upstream model API failure"
    )

    header = "⚠️ **Model API Failure (CLI Exit Code 3):**"
    lines = [header, f"\n{msg}\n"]

    status_str = f"`{canonical}`"
    if code:
        status_str += f" (Code {code})"
    lines.append(f"• **Status:** {status_str}")

    if error_id:
        lines.append(f"• **Error ID:** `{error_id}`")
    if retryable is not None:
        lines.append(f"• **Retryable:** {'Yes' if retryable else 'No'}")
    if elapsed_sec:
        lines.append(f"• **Elapsed:** {elapsed_sec}s")
    if pid_str:
        lines.append(f"• **Process:** `{pid_str}`")

    return "\n".join(lines)

