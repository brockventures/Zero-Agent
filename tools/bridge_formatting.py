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

# Re-export pure security, leak detection, and secret scrubbing symbols
from tools.bridge_safety import (
    CLI_LEAK_LINE_PATTERNS,
    _get_scrub_targets,
    dedup_repetitive_patterns,
    is_internal_cli_leak,
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

    # 1c. Collapse bare URLs (<https://...>) by default to suppress bloated link preview cards
    # Preserves code blocks, markdown links, already bracketed URLs, and visual GIFs
    def collapse_bare_urls(content: str) -> str:
        code_blocks = []
        def save_code(m):
            code_blocks.append(m.group(0))
            return f"__CODE_BLOCK_{len(code_blocks)-1}__"

        s = re.sub(r"```[\s\S]*?```|`[^`\n]+`", save_code, content)

        md_links = []
        def save_md(m):
            md_links.append(m.group(0))
            return f"__MD_LINK_{len(md_links)-1}__"
        s = re.sub(r"\[[^\]]+\]\([^\)]+\)", save_md, s)

        bracketed = []
        def save_bracket(m):
            bracketed.append(m.group(0))
            return f"__BRACKETED_{len(bracketed)-1}__"
        s = re.sub(r"<https?://[^>]+>", save_bracket, s)

        def wrap_bare(m):
            url = m.group(1)
            punct = m.group(2) or ""
            if "tenor.com/view/" in url or "giphy.com/gifs/" in url:
                return f"{url}{punct}"
            return f"<{url}>{punct}"

        url_pattern = r"(https?://[^\s<>\(\)\[\]]+?)([.,;:?!]?)(?=[.,;:?!]?(?:\s|$|\)))"
        s = re.sub(url_pattern, wrap_bare, s)

        for i, b in enumerate(bracketed):
            s = s.replace(f"__BRACKETED_{i}__", b)
        for i, m in enumerate(md_links):
            s = s.replace(f"__MD_LINK_{i}__", m)
        for i, c in enumerate(code_blocks):
            s = s.replace(f"__CODE_BLOCK_{i}__", c)

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
        from tools.handoff import convert_bare_peer_mentions, ensure_handoff_mentions
        text = convert_bare_peer_mentions(text)
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

