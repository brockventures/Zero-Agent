import re
import html
import unicodedata

# Dangerous zero-width, bidirectional overrides, and control characters
INVISIBLE_CHARS_RE = re.compile(
    r"[\u200B-\u200D\uFEFF\u00AD\u2060\u202A-\u202E\u2066-\u2069\u0000-\u0008\u000B\u000C\u000E-\u001F]"
)

# HTML stripping regex patterns
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
SCRIPT_STYLE_RE = re.compile(r"<(script|style|iframe|object|embed)[^>]*>.*?</\1>", flags=re.DOTALL | re.IGNORECASE)
HIDDEN_STYLE_RE = re.compile(r'style=[\'"][^\'"]*(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0)[^\'"]*[\'"]', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")

# Discord format disarming regex
DISCORD_PING_RE = re.compile(r"@(everyone|here|&[0-9]+|[0-9]+)")

def sanitize_email_text(raw_text: str, max_length: int = 4000) -> str:
    """Sanitize inbound email text against hidden text, injection, and formatting exploits."""
    if not raw_text:
        return ""
    
    # 1. Normalize Unicode and remove invisible/directional characters
    text = unicodedata.normalize("NFKC", raw_text)
    text = INVISIBLE_CHARS_RE.sub("", text)
    
    # 2. Strip dangerous HTML elements and hidden blocks
    text = HTML_COMMENT_RE.sub("", text)
    text = SCRIPT_STYLE_RE.sub("", text)
    text = HIDDEN_STYLE_RE.sub("", text)
    
    # 3. Unescape HTML entities, then strip remaining tags
    text = html.unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    
    # 4. Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    
    # 5. Length ceiling truncation
    if len(text) > max_length:
        text = text[: max_length - 3] + "..."
        
    return text

def sanitize_discord_display(text: str, max_len: int = 120) -> str:
    """Sanitize email snippets for safe rendering in Discord chat."""
    clean = sanitize_email_text(text, max_length=max_len)
    # Neutralize pings
    clean = DISCORD_PING_RE.sub(r"@-\1", clean)
    # Neutralize unclosed backticks
    clean = clean.replace("`", "'")
    # Neutralize markdown quote and subtext spoofing at line starts
    clean = re.sub(r"^([>#\-\*]+)", r" \1", clean, flags=re.MULTILINE)
    return clean.strip()
