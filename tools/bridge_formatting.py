"""
Zero Discord Bridge - Formatting & Text Sanitization Module
Encapsulates all pure text transformations, LaTeX cleanup, Discord chunking,
presentation sanitization, and thread titling.

Re-exports stream and safety symbols for complete backward compatibility.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

# Re-export pure security, leak detection, code-span shielding, and secret scrubbing symbols
from tools.bridge_safety import (
    CLI_LEAK_LINE_PATTERNS,
    _CODE_SPAN_RE,
    _get_scrub_targets,
    dedup_repetitive_patterns,
    is_internal_cli_leak,
    mask_code_spans,
    restore_code_spans,
    scrub_credentials,
    strip_internal_cli_chatter,
)

# Re-export stream-json protocol parser, transcript harvesting, and error formatting
from tools.bridge_stream import (
    AgyStreamParser,
    format_agy_error_message,
    format_command_preview,
    harvest_transcript_response,
    parse_agy_error,
)


def convert_markdown_tables(text: str) -> str:
    """Convert raw markdown pipe tables into mobile-friendly Discord card lists with subtext.
    Shields inline code spans before splitting on '|' so pipes inside backticks never break cells."""
    if not text or "|" not in text:
        return text

    masked_text, table_spans = mask_code_spans(text, prefix="\x00TABLE_SPAN_")
    lines = masked_text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect markdown table header followed by separator (|---|---|)
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*[-:]+[-| :]*$", lines[i + 1]):
            headers = [
                re.sub(r"^\*\*|\*\*$", "", c.replace(r"\|", "|")).strip()
                for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))
            ]
            i += 2  # skip header and separator
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [
                    c.replace(r"\|", "|").strip()
                    for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))
                ]
                table_rows.append(cells)
                i += 1

            if not table_rows:
                continue

            # Check if column 0 is a Section/Group and column 1 is the item/metric
            has_section_col = False
            if len(headers) >= 4 and headers[0].lower() in ("segment", "section", "category", "group", "type"):
                has_section_col = True

            # Detect if this is a multi-column comparison table vs a compact attribute/status summary table
            is_comparison = False
            attr_words = {
                "count", "status", "notes", "note", "details", "detail", "description",
                "value", "val", "cost", "price", "port", "host", "ip", "url", "link",
                "date", "time", "type", "size", "action", "result", "finding", "resolution",
                "state", "reason", "error", "percent", "percentage", "progress", "unit",
                "pattern", "meaning", "summary", "info"
            }

            check_headers = headers[2:] if has_section_col else headers[1:]
            other_headers = [re.sub(r"[^a-z0-9_-]", "", h.lower().strip()) for h in check_headers]

            if has_section_col:
                is_comparison = True
            elif len(headers) >= 3 and not any(oh in attr_words for oh in other_headers):
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
    return restore_code_spans("\n".join(out), table_spans, prefix="\x00TABLE_SPAN_")


def format_for_discord(text: str) -> str:
    """Format markdown for clean Discord presentation using a single outer code-span shield:
    - Strips internal system task envelopes and intermediate progress chatter
    - Shields all fenced code blocks (```...```) and N-backtick inline code spans (`...`, ``...``)
    - Strips file:/// markdown links which render broken on Discord
    - Collapses link previews (<https://...>) and normalizes hyperlink brackets
    - Converts GitHub alerts (> [!NOTE]) to clean emoji callouts
    - Converts markdown pipe tables into clean mobile cards
    - Converts explicit LaTeX notation into Unicode symbols without touching prose $ prices/tickers
    - Restores shielded code spans and positions reaction GIFs / handoff mentions
    """
    if not text:
        return ""

    # 0a. Strip internal agent task lifecycle envelopes, CLI leak lines, and intermediate progress chatter first
    text = strip_internal_cli_chatter(text)
    if not text:
        return ""

    # 0b. Pre-shield link hygiene for accidental backticks around bare URLs or inside http(s) link labels:
    # - Single-backticked bare URLs: `<https://...>` or `https://...` -> <https://...>
    # - Backticked labels on web links: [`label`](https://...) -> [label](https://...)
    text = re.sub(r"(?<!`)`<?(https?://[^\s`>]+)>?`(?!`)", r"<\1>", text)
    text = re.sub(r"\[\s*`+([^`\]\n]+)`+\s*\]\((<?https?://[^\)\n]+)\)", r"[\1](\2)", text)

    # 0c. Global Code-Span Shield: mask all fenced code blocks and N-backtick inline code spans
    # so zero prose regexes can mutate code, regex patterns, or bash snippets.
    text, code_spans = mask_code_spans(text, prefix="\x00DISCORD_SPAN_")

    def _add_protected_span(val: str) -> str:
        idx = len(code_spans)
        code_spans.append(val)
        return f"\x00DISCORD_SPAN_{idx}\x00"

    # 1. Clean file:/// links: [`/path`](file:///path) -> `/path`, [path](file:///path) -> `path`, bare file:///path -> `/path`
    def clean_file_link(m):
        inner = m.group(1).strip()
        if inner.startswith("\x00DISCORD_SPAN_") and inner.endswith("\x00"):
            return inner
        if inner.startswith("`") and inner.endswith("`"):
            return _add_protected_span(inner)
        return _add_protected_span(f"`{inner}`")

    text = re.sub(r"\[([^\]]+)\]\(file://[^\)]*\)", clean_file_link, text)
    text = re.sub(
        r"(?<![\w`\(])file://(/[^\s\)\>]+?)(?=[.,;:?!]?(?:\s|$|\)))",
        lambda m: _add_protected_span(f"`{m.group(1)}`"),
        text,
    )

    # 1b. Normalize Discord hyperlinks:
    # Discord breaks when bold/italics wrap the outside of brackets (**[label](url)**)
    # or when emojis are inside brackets. Also, raw URLs with underscores inside parentheses
    # can trigger italic markdown unless wrapped in angle brackets (< >).
    def normalize_discord_links(m):
        label = m.group(2).strip()
        raw_url = m.group(3).strip()
        suffix_colon = m.group(4) or ""
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
        clean_inner = inner_label.strip("*_`").strip()

        # If label is identical to the target URL, collapse redundant [url](<url>) into clean <url>
        if clean_inner.lower() == clean_url.lower() or clean_inner.lower().rstrip("/") == clean_url.lower().rstrip("/"):
            return f"<{clean_url}>{suffix_colon}"

        link_str = f"[{clean_inner}](<{clean_url}>){suffix_colon}"

        if lead_emoji:
            return f"{lead_emoji} {link_str}"
        return link_str

    pattern = r"(\*\*|\*)?\[([^\]]+)\]\(<?(https?://[^\)>]+)>?\)(:?)(\*\*|\*)?"
    text = re.sub(pattern, normalize_discord_links, text)

    # 1c. Collapse bare URLs (<https://...>) by default to suppress bloated link preview cards
    # Code blocks are already shielded by \x00DISCORD_SPAN_; preserve markdown links, already bracketed URLs, and GIFs.
    def collapse_bare_urls(content: str) -> str:
        md_links = []
        def save_md(m):
            md_links.append(m.group(0))
            return f"\x00MD_LINK_{len(md_links)-1}\x00"
        s = re.sub(r"\[[^\]]+\]\([^\)]+\)", save_md, content)

        bracketed = []
        def save_bracket(m):
            bracketed.append(m.group(0))
            return f"\x00BRACKETED_{len(bracketed)-1}\x00"
        s = re.sub(r"<https?://[^>]+>", save_bracket, s)

        def wrap_bare(m):
            url = m.group(1)
            punct = m.group(2) or ""
            if "tenor.com/view/" in url or "giphy.com/gifs/" in url:
                return f"{url}{punct}"
            return f"<{url}>{punct}"

        url_pattern = r"(https?://[^\s<>\(\)\[\]\x00]+?)([.,;:?!]?)(?=[.,;:?!]?(?:\s|$|\)))"
        s = re.sub(url_pattern, wrap_bare, s)

        for i, b in enumerate(bracketed):
            s = s.replace(f"\x00BRACKETED_{i}\x00", b)
        for i, m in enumerate(md_links):
            s = s.replace(f"\x00MD_LINK_{i}\x00", m)

        return s

    text = collapse_bare_urls(text)

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

    # 3b. Collapse unsupported h4+ headers (####+) to h3 (###) so Discord renders them as headings
    text = re.sub(r"^(#{4,})\s*(.*)$", r"### \2", text, flags=re.MULTILINE)

    # 4. Convert markdown pipe tables to clean Discord mobile cards
    text = convert_markdown_tables(text)

    # 4b. Normalize literal Unicode bullets (•) to native Discord markdown list syntax (-)
    text = re.sub(r"^([ \t]*(?:>[ \t]*)*)•[ \t]*", r"\1- ", text, flags=re.MULTILINE)

    # 4b-ii. Strip redundant numbering inside bulleted list items (e.g. "- **1. Gold:**" -> "- **Gold:**" or "- 1. Gold" -> "- Gold")
    text = re.sub(r"^([ \t]*[-*]\s+\*\*)\d+\.\s+", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^([ \t]*[-*]\s+)\d+\.\s+", r"\1", text, flags=re.MULTILINE)

    # 4c. Tighten loose lists where a parent list item is followed by an empty line before its sub-bullets
    text = re.sub(r"(^[ \t]*(?:[-*]|\d+\.)\s+[^\n]+)\n\n+([ \t]{2,}(?:[-*]|\d+\.)\s+)", r"\1\n\2", text, flags=re.MULTILINE)

    # 5. Convert LaTeX notation into clean Discord markdown and Unicode symbols (while code spans remain masked)
    text = clean_discord_latex(text)

    # 6. Convert bare @peer mentions and repair any LLM-stuttered snowflake IDs while code spans are masked
    try:
        from tools.handoff import convert_bare_peer_mentions, repair_corrupted_snowflakes
        text = convert_bare_peer_mentions(text)
        text = repair_corrupted_snowflakes(text)
    except Exception:
        pass

    # 7. Restore all shielded fenced code blocks and inline code spans
    text = restore_code_spans(text, code_spans, prefix="\x00DISCORD_SPAN_")

    # 8. Sanitize reaction GIFs (verify Tenor links are live HTTP 200 and position at end before [CHOICES: ...])
    text = sanitize_reaction_gifs(text)

    # 9. Ensure handoff envelopes include physical Discord mentions for peer bots
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
    very end of the message (immediately before any [CHOICES: ...] / [OPTIONS: ...] block).
    Ignores GIF URLs inside fenced or inline code spans."""
    if not text or ("tenor.com/view/" not in text and "giphy.com/gifs/" not in text):
        return text

    masked_text, code_spans = mask_code_spans(text, prefix="\x00GIF_SPAN_")
    if "tenor.com/view/" not in masked_text and "giphy.com/gifs/" not in masked_text:
        return text

    gif_url_pattern = r"https?://(?:www\.)?(?:tenor\.com/view/[a-zA-Z0-9_\-]+|giphy\.com/gifs/[a-zA-Z0-9_\-]+)"
    gif_urls = re.findall(gif_url_pattern, masked_text)
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
            masked_text = masked_text.replace(url, final_dest_url)
            url = final_dest_url

        if not is_ok:
            try:
                from tools.gif_tool import get_contextual_gif
                fallback = get_contextual_gif("shrug")
                if fallback and fallback.get("url"):
                    masked_text = masked_text.replace(url, fallback["url"])
                    url = fallback["url"]
                else:
                    masked_text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", masked_text)
                    continue
            except Exception:
                masked_text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", masked_text)
                continue

    # Find remaining valid URLs
    remaining_urls = re.findall(gif_url_pattern, masked_text)
    if not remaining_urls:
        return restore_code_spans(masked_text.strip(), code_spans, prefix="\x00GIF_SPAN_")

    # Deduplicate while preserving original appearance order
    unique_urls = list(dict.fromkeys(remaining_urls))

    # Regex matching markdown links, angle-bracketed URLs, or bare GIF URLs
    full_gif_re = re.compile(
        r"\[[^\]]*\]\(\s*<?" + gif_url_pattern + r">?\s*\)|"
        r"<" + gif_url_pattern + r">|"
        r"(?<!\(|<|\[)" + gif_url_pattern
    )

    # Strip existing GIF links from lines
    lines = masked_text.split("\n")
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
    choices_pattern = re.compile(
        r"(\n*\s*\[(?:CHOICES|OPTIONS):\s*(?:[^\[\]]|\[[^\]]*\])+\]\s*)$", re.IGNORECASE
    )
    choices_match = choices_pattern.search(collapsed_body)
    if choices_match:
        content_before = collapsed_body[:choices_match.start()].rstrip()
        choice_block = choices_match.group(1).strip()
        if content_before:
            out_text = f"{content_before}\n\n{gif_block}\n\n{choice_block}"
        else:
            out_text = f"{gif_block}\n\n{choice_block}"
    else:
        if collapsed_body:
            out_text = f"{collapsed_body}\n\n{gif_block}"
        else:
            out_text = gif_block

    return restore_code_spans(out_text, code_spans, prefix="\x00GIF_SPAN_")


def strip_reaction_gifs(text: str) -> str:
    """Completely remove all Tenor, Giphy, and reaction GIF links from text,
    stripping orphan lines and collapsing surplus whitespace while preserving code spans."""
    if not text or ("tenor.com" not in text and "giphy.com" not in text and ".gif" not in text.lower()):
        return text.strip()

    masked_text, code_spans = mask_code_spans(text, prefix="\x00STRIP_GIF_SPAN_")

    gif_url_pattern = r"https?://(?:www\.)?(?:tenor\.com/(?:view/)?[a-zA-Z0-9_\-]+|media\.tenor\.com/[a-zA-Z0-9_\-/]+|giphy\.com/gifs/[a-zA-Z0-9_\-]+|[^\s\)\>\]\x00]+\.gif\b)"
    full_gif_re = re.compile(
        r"\[[^\]]*\]\(\s*<?" + gif_url_pattern + r">?\s*\)|"
        r"<" + gif_url_pattern + r">|"
        r"(?<!\(|<|\[)" + gif_url_pattern,
        re.IGNORECASE,
    )

    lines = masked_text.split("\n")
    cleaned_lines = []
    for line in lines:
        line_stripped = line.strip()
        if full_gif_re.fullmatch(line_stripped):
            continue
        cleaned_line = full_gif_re.sub("", line).rstrip()
        if cleaned_line.strip() or not line_stripped:
            cleaned_lines.append(cleaned_line)

    raw_body = "\n".join(cleaned_lines)
    collapsed = re.sub(r"\n{3,}", "\n\n", raw_body).strip()
    return restore_code_spans(collapsed, code_spans, prefix="\x00STRIP_GIF_SPAN_")


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
    # Discord's native single-message limit is 2000. If the whole text fits, never split.
    if len(cleaned) <= 2000:
        return [cleaned]

    # Boundary squeeze: if barely over limit (<=2100 chars), collapse whitespace to fit in 1 message
    if len(cleaned) <= 2100:
        condensed = re.sub(r"\n{3,}", "\n\n", cleaned)
        condensed = re.sub(r"[ \t]+\n", "\n", condensed).strip()
        if len(condensed) <= 2000:
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


_KNOWN_LATEX_CMD_RE = re.compile(
    r"\\(?:alpha|beta|gamma|delta|epsilon|zeta|eta|theta|lambda|mu|pi|rho|sigma|tau|phi|omega"
    r"|Delta|Theta|Lambda|Sigma|Omega|cdot|times|div|leq?|geq?|neq|approx|sim|pm|to|rightarrow|leftarrow"
    r"|infty|partial|nabla|in|notin|subset|subseteq|frac|text|mathrm|mathbf|left|right|quad|[,;!%])"
)


def clean_discord_latex(text: str) -> str:
    """Convert explicit LaTeX math notation into clean, native Discord markdown and Unicode symbols.
    - Shields all CommonMark fenced code blocks and N-backtick inline code spans first.
    - Converts block math ($$...$$, \\[...\\]) and explicit inline math (\\(...\\)).
    - Converts $...$ ONLY when the interior is a valid mathematical expression (single variable,
      superscript/subscript term, known LaTeX command, or explicit equation), never matching
      tickers ($SOL), prices ($200), shell variables ($PATH), or regexes ($foo\\s+bar$).
    """
    if not text:
        return text

    text, code_spans = mask_code_spans(text, prefix="\x00LATEX_SPAN_")

    def _add_latex_span(val: str) -> str:
        idx = len(code_spans)
        code_spans.append(val)
        return f"\x00LATEX_SPAN_{idx}\x00"

    symbol_map = {
        r"\\alpha\b": "α", r"\\beta\b": "β", r"\\gamma\b": "γ", r"\\delta\b": "δ",
        r"\\epsilon\b": "ε", r"\\zeta\b": "ζ", r"\\eta\b": "η", r"\\theta\b": "θ",
        r"\\lambda\b": "λ", r"\\mu\b": "μ", r"\\pi\b": "π", r"\\rho\b": "ρ",
        r"\\sigma\b": "σ", r"\\tau\b": "τ", r"\\phi\b": "φ", r"\\omega\b": "ω",
        r"\\Delta\b": "Δ", r"\\Theta\b": "Θ", r"\\Lambda\b": "Λ", r"\\Sigma\b": "Σ",
        r"\\Omega\b": "Ω",
        r"\\cdot\b": "·", r"\\times\b": "×", r"\\div\b": "÷",
        r"\\leq?\b": "≤", r"\\geq?\b": "≥", r"\\neq\b": "≠", r"\\approx\b": "≈",
        r"\\sim\b": "~", r"\\pm\b": "±", r"\\to\b": "→", r"\\rightarrow\b": "→", r"\\leftarrow\b": "←",
        r"\\infty\b": "∞", r"\\partial\b": "∂", r"\\nabla\b": "∇",
        r"\\in\b": "∈", r"\\notin\b": "∉", r"\\subset\b": "⊂", r"\\subseteq\b": "⊆",
        r"\\quad\b": " ", r"\\,": " ", r"\\;": " ", r"\\!": "", r"\\%": "%",
    }

    superscripts = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}
    subscripts = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉"}

    def _transform_math_body(inner: str, is_block: bool = False) -> str:
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"\1/\2", inner)
        inner = re.sub(r"\\(?:text|mathrm|mathbf)\{([^}]+)\}", r"\1", inner)
        inner = re.sub(r"\\(?:left|right)\b", "", inner)
        for k, v in superscripts.items():
            inner = inner.replace(f"^{k}", v)
        for k, v in subscripts.items():
            inner = inner.replace(f"_{k}", v)
        if not is_block:
            inner = re.sub(r"\s+", " ", inner).strip()
            if len(inner) == 1 and inner.isascii() and inner.isalpha():
                return f"*{inner}*"
        return inner

    # 1. Convert block math $$...$$ and \[ ... \] and shield resulting code blocks immediately
    def replace_block_math(match):
        inner = _transform_math_body(match.group(1).strip(), is_block=True)
        return _add_latex_span(f"\n```\n{inner}\n```\n")

    text = re.sub(r"\$\$(.+?)\$\$", replace_block_math, text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", replace_block_math, text, flags=re.DOTALL)

    # 2. Convert explicit inline math \( ... \)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: _transform_math_body(m.group(1)), text)

    # 3. Closed single numbers enclosed in dollar signs like $450$ or $200$ (LaTeX single-number notation)
    text = re.sub(r"(?<![\w\$\\~])\$(\d+(?:\.\d+)?)\$(?![\w\$])", r"\1", text)

    # 4. Whitelist-only inline $...$ conversion:
    # Only convert $inner$ if there is no leading/trailing space inside the $ delimiters AND
    # inner is a recognized mathematical expression (never arbitrary prose, tickers, paths, or regexes).
    def _is_valid_inline_latex(inner: str) -> bool:
        s = inner.strip()
        if not s:
            return False
        # (a) Contains an explicit known LaTeX command (e.g. \alpha, \frac, \text, \sim, \cdot)
        if _KNOWN_LATEX_CMD_RE.search(s):
            # Reject if it also contains non-LaTeX regex/path escapes like \s, \d, \w, \n, \b
            stripped_known = _KNOWN_LATEX_CMD_RE.sub("", s)
            if "\\" in stripped_known:
                return False
            return True
        # Reject any other string containing backslashes (e.g. regexes \s, \d or Windows paths)
        if "\\" in s:
            return False
        # (b) Single ASCII variable (e.g. $x$, $d$, $n$)
        if len(s) == 1 and s.isascii() and s.isalpha():
            return True
        # (c) Algebraic variable/number with superscript or subscript (e.g. $x^2$, $10^6$, $R^2$, $v_0$)
        if re.match(r"^[a-zA-Z0-9]+(?:\^[0-9a-zA-Z+\-]+|_[0-9a-zA-Z+\-]+)+$", s):
            return True
        # (d) Clean mathematical equation/comparison (must contain =, ^, <, >, ±, ×, ÷, ≤, ≥, ≠, or ≈, and no slashes/colons/tildes)
        if (
            re.match(r"^[a-zA-Z0-9\s\+\-\*=\^_\(\)\.,<>±×÷≤≥≠≈]+$", s)
            and any(op in s for op in ("=", "^", "<", ">", "±", "×", "÷", "≤", "≥", "≠", "≈"))
        ):
            return True
        return False

    def _replace_inline_dollar_math(m: re.Match) -> str:
        inner = m.group(1)
        if _is_valid_inline_latex(inner):
            return _transform_math_body(inner)
        return m.group(0)

    text = re.sub(
        r"(?<![\w\$\\~])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w\$])",
        _replace_inline_dollar_math,
        text,
    )

    # 5. Clean HTML tags into Discord markdown (no raw <br> or <b>)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"<(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"<code>", "`", text, flags=re.IGNORECASE)
    text = re.sub(r"</code>", "`", text, flags=re.IGNORECASE)

    return restore_code_spans(text, code_spans, prefix="\x00LATEX_SPAN_")


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


_CODE_OR_REACT_RE = re.compile(
    r"(```[\s\S]*?```|``[^`\n]+``|`[^`\n]+`)|(\[(?:REACT|REACTION):\s*([^\[\]]+)\])",
    re.IGNORECASE,
)

_CODE_OR_CHOICES_RE = re.compile(
    r"(```[\s\S]*?```|``[^`\n]+``|`[^`\n]+`)|(\[(?:CHOICES|OPTIONS):\s*((?:[^\[\]]|\[[^\]]*\])+)\])",
    re.IGNORECASE,
)


def extract_react_directives(text: str) -> tuple[str, list[str]]:
    """Extract and strip [REACT: <emoji>] tags outside of code blocks/inline backticks.

    Returns (cleaned_text, reactions_to_add). Ignores backticked prose references like
    `[REACT: <emoji>]` or `` `[REACT: ...]` `` so empty backticks are never left behind.
    """
    if not text:
        return "", []
    masked, spans = mask_code_spans(text, prefix="\x00REACT_SPAN_")
    reactions: list[str] = []

    def _repl(m: re.Match) -> str:
        if m.group(1) is not None:
            return m.group(1)
        raw_emojis = (m.group(3) or "").strip()
        parts = [p.strip() for p in re.split(r"[, ]+", raw_emojis) if p.strip()]
        valid_parts = [
            p for p in parts
            if p not in ("...", "…", "<emoji>", "<emojis>") and not (p.startswith("<") and p.endswith(">") and ":" not in p)
        ]
        if not valid_parts:
            return m.group(0)
        reactions.extend(valid_parts)
        return ""

    cleaned = _CODE_OR_REACT_RE.sub(_repl, masked).strip()
    return restore_code_spans(cleaned, spans, prefix="\x00REACT_SPAN_"), reactions


def strip_unquoted_choices(text: str) -> str:
    """Strip [CHOICES: ...] / [OPTIONS: ...] tags only when outside of inline/fenced code blocks."""
    if not text:
        return ""
    masked, spans = mask_code_spans(text, prefix="\x00CHOICES_SPAN_")
    cleaned = _CODE_OR_CHOICES_RE.sub(lambda m: m.group(1) if m.group(1) is not None else "", masked).strip()
    return restore_code_spans(cleaned, spans, prefix="\x00CHOICES_SPAN_")


def parse_interactive_choices(text: str, quick_choice_view_cls=None, button_choice_fn=None) -> tuple[str, any]:
    """Parse [CHOICES: opt1 | opt2] tag from text (ignoring code spans) and return (cleaned_text, choice_view)."""
    if not text:
        return text, None
    masked, spans = mask_code_spans(text, prefix="\x00CHOICES_SPAN_")
    matches = [m for m in _CODE_OR_CHOICES_RE.finditer(masked) if m.group(2) is not None]
    parsed_choices = []
    valid_match = None
    for m in matches:
        raw_choices = m.group(3).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [
            restore_code_spans(c, spans, prefix="\x00CHOICES_SPAN_").strip().strip("'\"`“”‘’").strip()
            for c in raw_choices.split(delim)
            if c.strip()
        ]
        choices = [c for c in choices if c]
        if choices and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in choices):
            parsed_choices = choices
            valid_match = m

    if valid_match and parsed_choices and quick_choice_view_cls:
        clean_text = strip_unquoted_choices(text)
        view = quick_choice_view_cls(parsed_choices, callback_fn=button_choice_fn)
        return clean_text, view
    return text, None


def scrub_ephemeral_directives(text: str) -> str:
    """Scrub ephemeral operational directives (Banana Watcher, watchdog alerts, loop warnings)
    from in-flight tasks before constructing a mid-turn steering prompt.
    Prevents autonomous NO_REPLY rules or bot probes from contaminating conversational turns.
    """
    if not text:
        return ""
    # Strip CRITICAL OPERATIONAL DIRECTIVE blocks
    text = re.sub(
        r"\[CRITICAL OPERATIONAL DIRECTIVE[^\]]*\].*?(?=(?:\n\s*\n\[|\Z))",
        "",
        text,
        flags=re.DOTALL,
    )
    # Strip Banana Watcher headers
    text = re.sub(
        r"🍌 \*\*(?:Topic Stalled|Loop Warning|Consensus Reached)\*\*:[^\n]*",
        "",
        text,
    )
    # Strip orphaned NO_REPLY directives
    text = re.sub(r"(?i)output \[NO_REPLY\][^\n]*", "", text)
    return text.strip()


def format_steering_prompt(
    new_directive: str,
    original_task: str | None = None,
    author_name: str | None = None,
    is_group: bool = False,
) -> str:
    """Format a mid-turn steering prompt preserving original in-flight context."""
    orig_section = ""
    if original_task and str(original_task).strip():
        clean_orig = scrub_ephemeral_directives(str(original_task))
        if clean_orig:
            orig_section = (
                f"\n[ORIGINAL IN-FLIGHT TASK BEING EXECUTED]:\n"
                f"\"{clean_orig}\"\n\n"
            )

    if is_group:
        sender = f"from {author_name}" if author_name else "in the channel"
        return (
            f"[MID-TURN GROUP CONVERSATION UPDATE]\n"
            f"{orig_section}"
            f"While you were drafting your reply, a new message arrived in the channel {sender}:\n"
            f"\"{new_directive.strip()}\"\n\n"
            f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
            f"1. Absorb this new context immediately. If your in-progress thought is now obsolete, answered, or contradicted, pivot cleanly or yield.\n"
            f"2. If the new message is an addition, follow-up, or related question, synthesize both naturally in your reply.\n"
            f"3. If you were emitting a ```handoff block, ensure it is completely valid, closed JSON or omitted entirely.\n"
            f"4. Keep your output concise (strictly under 2,000 chars) and respond naturally to the CURRENT state of the room."
        )

    return (
        f"[USER MID-TURN STEERING UPDATE]\n"
        f"{orig_section}"
        f"[NEW DIRECTIVE RECEIVED MID-TURN]:\n"
        f"\"{new_directive.strip()}\"\n\n"
        f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
        f"1. Absorb the new directive immediately in the context of the original in-flight task.\n"
        f"2. If the new directive is an addition, clarification, or follow-up question, SYNTHESIZE BOTH: address the new directive while fulfilling the original task (answer both questions or combine them).\n"
        f"3. If the new directive explicitly contradicts, cancels, or supersedes the prior plan, abort the prior task and pivot cleanly to the new directive.\n"
        f"4. Seamlessly incorporate this guidance into your response without restarting from scratch unless requested."
    )



