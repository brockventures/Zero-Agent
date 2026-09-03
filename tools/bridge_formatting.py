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

            # Detect if this is a comparison or complex multi-column table
            is_comparison = False
            if len(headers) >= 3:
                h0 = headers[0].lower()
                if h0 in ("feature", "attribute", "metric", "criteria", "aspect", "comparison", "spec", "parameter", "vs"):
                    is_comparison = True
                elif any(len(r) > 1 and (len(r[1]) > 25 or "(" in r[1]) for r in table_rows):
                    is_comparison = True
                elif any(len(r) > 2 and (len(r[2]) > 25 or "(" in r[2]) for r in table_rows):
                    is_comparison = True

            for row in table_rows:
                if not row or not any(row):
                    continue
                first = re.sub(r"^\*\*|\*\*$", "", row[0]).strip()

                if is_comparison and len(headers) >= 3:
                    out.append(f"• **{first}**:")
                    for col_idx in range(1, len(headers)):
                        if col_idx < len(row) and row[col_idx]:
                            clean_col = re.sub(r"^\*\*|\*\*$", "", headers[col_idx]).strip()
                            val = row[col_idx]
                            out.append(f"  - *{clean_col}*: {val}")
                elif len(row) == 2 or (len(headers) == 2 and len(row) >= 2):
                    val = row[1].strip()
                    out.append(f"• **{first}**: {val}")
                else:
                    second = row[1] if len(row) > 1 else ""
                    notes = " · ".join(c for c in row[2:] if c) if len(row) > 2 else ""
                    if second and notes:
                        out.append(f"• **{first}** ({second}): {notes}")
                    elif second:
                        out.append(f"• **{first}** ({second})")
                    elif notes:
                        out.append(f"• **{first}**: {notes}")
                    else:
                        out.append(f"• **{first}**")
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

    # 1. Clean file:/// links: [`/path`](file:///path) -> `/path`, [path](file:///path) -> `path`
    def clean_file_link(m):
        inner = m.group(1).strip()
        if inner.startswith("`") and inner.endswith("`"):
            return inner
        return f"`{inner}`"

    text = re.sub(r"\[([^\]]+)\]\(file://[^\)]*\)", clean_file_link, text)

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

    # 5. Strip internal agent task lifecycle envelopes and echoed system progress headers
    text = re.sub(
        r"<SYSTEM_MESSAGE>[\s\S]*?</SYSTEM_MESSAGE>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"An asynchronous task has completed:\s*[^\n]+\s*\(State:\s*[^\)]+\)\s*Result payload:\s*\d+\s*Task output:.*?(?=\n\n(?:[A-Z#•*-]|\Z)|\Z)",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"Tool is running as a background task with task id:\s*[^\n]+(?:\nTask Description:[^\n]+)?(?:\nTask logs are available at:[^\n]+)?(?:\nYOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:[^\n]+)?(?:\n\s*DO NOTHING ELSE\.)?",
        "",
        text,
        flags=re.DOTALL,
    )

    # 6. Strip intermediate background task wait and launch self-narration chatter
    text = re.sub(
        r"(?:^|\n+)(?:I (?:have\s+)?(?:initiated|launched|started|spawned|triggered)[^\n]+?(?:as soon as the (?:background\s+)?task\s+(?:completes|finishes)|when the (?:command|task)\s+finishes|and (?:will\s+)?wait for it to finish|waiting for PID 1 to consume)[^\n]*\.?)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:^|\n+)(?:I will (?:review|inspect|check|analyze) the results (?:as soon as|when|once) the (?:background\s+)?task (?:completes|finishes)\.?)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:^|\n+)(?:I have launched [^\n]+? and will wait for it to finish\.?)",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 7. Sanitize reaction GIFs: verify Tenor links are live (HTTP 200) and replace 404s with working fallbacks
    text = sanitize_reaction_gifs(text)

    return text.strip()


def sanitize_reaction_gifs(text: str) -> str:
    """Probe Tenor and Giphy links in Discord output. If a reaction GIF returns HTTP 404,
    replace it with a live dynamic GIF or strip the line so Discord never renders broken previews."""
    if not text or ("tenor.com/view/" not in text and "giphy.com/gifs/" not in text):
        return text

    gif_urls = re.findall(r"https?://(?:www\.)?(?:tenor\.com/view/[a-zA-Z0-9_\-]+|giphy\.com/gifs/[a-zA-Z0-9_\-]+)", text)
    for url in set(gif_urls):
        is_ok = False
        try:
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    is_ok = True
        except Exception:
            is_ok = False

        if not is_ok:
            try:
                from tools.gif_tool import get_contextual_gif
                fallback = get_contextual_gif("shrug")
                if fallback and fallback.get("url"):
                    text = text.replace(url, fallback["url"])
                else:
                    text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", text)
            except Exception:
                text = re.sub(rf"(?:^|\n)[^\n]*{re.escape(url)}[^\n]*(?:\n|$)", "\n", text)

    return text


def extract_agent_response(raw_text: str) -> str:
    """Extract clean response text from agy output, supporting plain text, json, or stream-json."""
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
        return format_for_discord(text)

    # Parse JSON if events are present
    accumulated_content = []
    final_result_response = ""
    error_response = ""

    for line in text.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        start = line_str.find("{")
        end = line_str.rfind("}") + 1
        if start != -1 and end > start:
            json_substr = line_str[start:end]
            try:
                event = json.loads(json_substr)
                if isinstance(event, dict):
                    if "response" in event and event["response"]:
                        final_result_response = event["response"]

                    ev_type = event.get("event") or event.get("type")
                    if ev_type == "result" or "result" in event:
                        res = event.get("result", {})
                        if isinstance(res, dict):
                            if "response" in res and res["response"]:
                                final_result_response = res["response"]
                            elif "error" in res and res.get("error", ""):
                                error_response = f"Error: {res.get('error', '')}"
                    elif ev_type == "step_update" or "step_update" in event:
                        step = event.get("step_update", {})
                        if isinstance(step, dict):
                            stype = step.get("step_type")
                            if stype == "tool" or step.get("tool_name") or "tool_info" in step:
                                # Discard intermediate thought/narration emitted before tool execution
                                accumulated_content.clear()
                            elif stype == "agent_response" and step.get("text_delta"):
                                accumulated_content.append(step["text_delta"])
                    elif ev_type in ("tool", "tool_call", "tool_use"):
                        accumulated_content.clear()
                    elif ev_type in ("content", "message", "text", "delta"):
                        content = event.get("content") or event.get("text") or event.get("delta")
                        if content and isinstance(content, str):
                            accumulated_content.append(content)
            except Exception:
                pass

    if final_result_response:
        return format_for_discord(final_result_response)
    if error_response:
        return format_for_discord(error_response)
    if accumulated_content:
        return format_for_discord("".join(accumulated_content))

    # Fallback filter for JSON metadata lines
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
        return format_for_discord("\n".join(clean_lines))

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

    for line in lines:
        stripped = line.strip()
        is_code_fence = stripped.startswith("```")

        # If entering a code block and current chunk already has substantial text (>half max_len),
        # break early so the entire code block starts cleanly on a new message.
        if is_code_fence and not in_code_block and current_len > (max_len // 2):
            if current_chunk:
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0

        overhead = (len(code_lang) + 12) if in_code_block else 0

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
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_-]{20,}", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"ya29\.[A-Za-z0-9_-]+", "[REDACTED_OAUTH_TOKEN]", text)
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
    elif any(k in low for k in ["sonarr", "radarr", "prowlarr", "indexer", "rate limit", "429"]):
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


def parse_interactive_choices(text: str, quick_choice_view_cls=None, button_choice_fn=None) -> tuple[str, any]:
    """Parse [CHOICES: opt1 | opt2] tag from text and return (cleaned_text, choice_view)."""
    matches = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", text))
    parsed_choices = []
    valid_match = None
    for m in matches:
        raw_choices = m.group(1).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [c.strip() for c in raw_choices.split(delim) if c.strip()]
        if choices and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in choices):
            parsed_choices = choices
            valid_match = m

    if valid_match and parsed_choices and quick_choice_view_cls:
        clean_text = re.sub(r"\[CHOICES:\s*([^\]]+)\]", "", text).strip()
        view = quick_choice_view_cls(parsed_choices, callback_fn=button_choice_fn)
        return clean_text, view
    return text, None

