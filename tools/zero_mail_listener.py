#!/usr/bin/env python3
"""Zero Dedicated Inbound Email Listener Daemon (Hardened with Anti-Flood / Anti-DDoS Circuit Breaker).

Maintains a fast polling loop against Gmail for emails addressed to zero@example.com.
Features:
- Prompt injection & zero-width character sanitization.
- Flood / DDoS Circuit Breaker (collapses surges > 5 emails/min into a single digest).
- Discord rate limit pacing (max 3 individual alerts per cycle, 1.2s delay).
- Human-in-the-loop operational guardrails.
"""

import json
import os
import sys
import time
import re
import html
import unicodedata
import urllib.request
import urllib.parse
from datetime import datetime
from collections import Counter

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", os.environ.get("GOOGLE_OAUTH_SECRETS", "/secrets/google_oauth.json"))
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

ENV_PATH = os.environ.get("ENV_PATH", "/workspace/.env")
STATE_DIR = os.environ.get("DATA_DIR", "/workspace/data")
SEEN_FILE = os.path.join(STATE_DIR, "seen_zero_emails.json")
LOG_FILE = os.environ.get("ZERO_MAIL_LOG", os.path.join(STATE_DIR, "zero_mail_listener.log"))

POLL_INTERVAL = 15  # seconds
TIMEOUT = 15

# Anti-Flood & Rate Limiting Thresholds
MAX_INDIVIDUAL_PER_CYCLE = 3   # Max individual Discord alerts per 15s tick
FLOOD_SURGE_THRESHOLD = 5      # If >5 new emails in a tick, collapse into a single digest alert
DISCORD_POST_DELAY = 1.2       # Sleep between Discord calls to avoid HTTP 429

# Security regexes
INVISIBLE_CHARS_RE = re.compile(
    r"[\u200B-\u200D\uFEFF\u00AD\u2060\u202A-\u202E\u2066-\u2069\u0000-\u0008\u000B\u000C\u000E-\u001F]"
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
DISCORD_PING_RE = re.compile(r"@(everyone|here|&[0-9]+|[0-9]+)")

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def sanitize_text(text: str, max_len: int = 150) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE_CHARS_RE.sub("", text)
    text = html.unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    text = DISCORD_PING_RE.sub(r"@-\1", text)
    text = text.replace("`", "'")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len-3] + "..."
    return text

def load_credentials(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds

def load_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'").strip('"')
    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                env.update(json.load(f))
        except Exception:
            pass
    for k, v in os.environ.items():
        if k not in env or env[k] == "":
            env[k] = v
    return env

_cached_token = None
_token_expiry = 0

def get_access_token() -> str:
    global _cached_token, _token_expiry
    now = time.time()
    if _cached_token and now < _token_expiry - 60:
        return _cached_token
    
    creds = load_credentials(SECRETS_PATH)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(creds.get("token_uri", "https://oauth2.googleapis.com/token"), data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        tokens = json.loads(resp.read().decode())
        _cached_token = tokens["access_token"]
        _token_expiry = now + tokens.get("expires_in", 3600)
        return _cached_token

def load_seen() -> set:
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        trimmed = list(seen)[-1000:]
        with open(SEEN_FILE, "w") as f:
            json.dump(trimmed, f)
    except Exception as e:
        log(f"Error saving seen file: {e}")

def post_discord(bot_token: str, channel_id: str, content: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "ZeroMailListener/1.0"
        }
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status in (200, 201)

def main():
    log("Starting Zero Inbound Email Listener Daemon (Anti-Flood Guard Active)...")
    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID", "1542081375287640084")

    if not bot_token:
        log("ERROR: DISCORD_BOT_TOKEN missing in .env. Exiting.")
        sys.exit(1)

    seen_ids = load_seen()
    is_initial_sync = (len(seen_ids) == 0)

    if is_initial_sync:
        log("Initial run: Seeding existing zero@example.com emails...")
        try:
            tok = get_access_token()
            query = urllib.parse.quote("to:zero@example.com")
            url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults=30"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                for m in data.get("messages", []):
                    seen_ids.add(m["id"])
            save_seen(seen_ids)
            log(f"Seeded {len(seen_ids)} existing messages. Now listening for new mail.")
        except Exception as e:
            log(f"Error during seeding: {e}")

    log(f"Listener active (polling every {POLL_INTERVAL}s for 'to:zero@example.com is:unread')...")

    while True:
        try:
            tok = get_access_token()
            query = urllib.parse.quote("to:zero@example.com is:unread")
            url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults=20"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                messages = data.get("messages", [])

            # Filter out already seen IDs
            new_msgs = [m for m in messages if m["id"] not in seen_ids]

            if new_msgs:
                log(f"Detected {len(new_msgs)} new unread email(s) for Zero.")

                # CIRCUIT BREAKER: Surge / Flood Detection
                if len(new_msgs) >= FLOOD_SURGE_THRESHOLD:
                    log(f"⚠️ SURGE DETECTED: {len(new_msgs)} emails in single tick. Collapsing into flood digest.")
                    
                    # Fetch metadata for summary
                    senders = []
                    for m in new_msgs:
                        mid = m["id"]
                        seen_ids.add(mid)
                        try:
                            m_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=metadata"
                            m_req = urllib.request.Request(m_url, headers={"Authorization": f"Bearer {tok}"})
                            with urllib.request.urlopen(m_req, timeout=TIMEOUT) as m_resp:
                                m_data = json.loads(m_resp.read().decode())
                                headers = {h["name"].lower(): h["value"] for h in m_data.get("payload", {}).get("headers", [])}
                                senders.append(sanitize_text(headers.get("from", "Unknown"), 40))
                        except Exception:
                            pass

                    save_seen(seen_ids)

                    # Tally top senders
                    top_senders = Counter(senders).most_common(3)
                    senders_str = ", ".join([f"`{s}` ({cnt})" for s, cnt in top_senders])

                    surge_msg = (
                        f"🛡️ **Inbound Email Surge Suppressed (`zero@example.com`)**\n"
                        f"• **Volume:** `{len(new_msgs)}` new emails received in last {POLL_INTERVAL}s.\n"
                        f"• **Top Senders:** {senders_str}\n"
                        f"• **Action Taken:** Individual alert cards suppressed to protect Discord channel. Details preserved for Nightly Assistant.\n\n"
                        f"-# 🔒 *Anti-DDoS Circuit Breaker Active.*"
                    )
                    try:
                        post_discord(bot_token, channel_id, surge_msg)
                    except Exception as de:
                        log(f"Failed to post surge digest: {de}")

                else:
                    # Normal volume: post individual cards (capped at MAX_INDIVIDUAL_PER_CYCLE)
                    for m in new_msgs[:MAX_INDIVIDUAL_PER_CYCLE]:
                        mid = m["id"]
                        m_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=metadata"
                        m_req = urllib.request.Request(m_url, headers={"Authorization": f"Bearer {tok}"})
                        with urllib.request.urlopen(m_req, timeout=TIMEOUT) as m_resp:
                            m_data = json.loads(m_resp.read().decode())

                        snippet = m_data.get("snippet", "")
                        headers = {h["name"].lower(): h["value"] for h in m_data.get("payload", {}).get("headers", [])}
                        sender = headers.get("from", "Unknown")
                        subject = headers.get("subject", "(No Subject)")
                        to_addr = headers.get("to", "")

                        delivered = headers.get("delivered-to", "").lower()
                        if "zero@example.com" not in to_addr.lower() and "zero@example.com" not in delivered:
                            seen_ids.add(mid)
                            continue

                        clean_sender = sanitize_text(sender, 50)
                        clean_subj = sanitize_text(subject, 70)
                        clean_snippet = sanitize_text(snippet, 120)

                        log(f"New email from {clean_sender}: '{clean_subj}'")

                        msg_body = (
                            f"📬 **New Email Received for Zero** (`zero@example.com`)\n"
                            f"• **From:** `{clean_sender}`\n"
                            f"• **Subject:** *\"{clean_subj}\"*\n"
                            f"• **Preview:** {clean_snippet}\n\n"
                            f"-# 🔒 *Operational Guardrail Active: Presented for human review. No autonomous actions taken.*"
                        )

                        try:
                            post_discord(bot_token, channel_id, msg_body)
                            log(f"Discord notification posted for message {mid}")
                        except Exception as de:
                            log(f"Failed to post to Discord: {de}")

                        seen_ids.add(mid)
                        time.sleep(DISCORD_POST_DELAY)

                    save_seen(seen_ids)

        except Exception as e:
            log(f"Polling loop exception: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
