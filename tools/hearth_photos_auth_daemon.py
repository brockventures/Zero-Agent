#!/usr/bin/env python3
"""Interactive Playwright Google Photos Auth & Album Sync Daemon for Brock Hearth.

Listens on 127.0.0.1:8098 (configurable via HEARTH_PHOTOS_DAEMON_HOST) so
hearth-ical-bridge (/hearth-api/photos/interactive/*) can render a live
interactive Google Sign-In session directly inside the ApoloSign Android
tablet's WebView without X-Frame-Options restrictions.
"""

import base64
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

try:
  from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
  sync_playwright = None

SECRETS_ENV_PATH = "/secrets/env.json"
PROFILE_DIR = "/workspace/data/hearth_gphotos_profile"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8098
HEARTH_IMPORT_URL = "http://127.0.0.1:8099/hearth-api/photos/sync"
ALLOWED_AUTH_HOSTS = frozenset({"photos.google.com", "accounts.google.com"})


def _load_secrets_env() -> dict:
  try:
    if os.path.exists(SECRETS_ENV_PATH):
      with open(SECRETS_ENV_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        if isinstance(data, dict):
          return data
  except Exception:
    pass
  return {}


def _get_config(key: str, default: str = "") -> str:
  val = os.environ.get(key)
  if val is not None and str(val).strip():
    return str(val).strip()
  secrets = _load_secrets_env()
  sval = secrets.get(key)
  if sval is not None and str(sval).strip():
    return str(sval).strip()
  return default


def _build_auth_url(album_url: str = "") -> str:
  base = album_url or _get_config("HEARTH_GPHOTOS_ALBUM_URL", ALBUM_URL)
  if not base:
    return ""
  sep = "&" if "?" in base else "?"
  obfsgid = _get_config("GOOGLE_PHOTOS_OBFSGID", "")
  email = _get_config("GOOGLE_PHOTOS_EMAIL", "")
  return f"{base}{sep}obfsgid={obfsgid}&email={email}"


def _is_allowed_auth_url(url: str) -> bool:
  if not isinstance(url, str) or not url:
    return False
  if any(ch.isspace() for ch in url):
    return False
  try:
    parsed = urllib.parse.urlparse(url)
    port = parsed.port
  except ValueError:
    return False
  if parsed.scheme.lower() != "https":
    return False
  if parsed.username is not None or parsed.password is not None:
    return False
  if port is not None and port != 443:
    return False
  hostname = (parsed.hostname or "").lower()
  return hostname in ALLOWED_AUTH_HOSTS


def _get_auth_tokens() -> list[str]:
  tokens = []
  for key in ("HEARTH_PHOTOS_AUTH_TOKEN", "ZERO_HEALTH_ADMIN_TOKEN"):
    val = _get_config(key, "")
    if val and val not in tokens:
      tokens.append(val)
  return tokens


def _is_authorized(auth_header: str) -> bool:
  if not isinstance(auth_header, str):
    return False
  parts = auth_header.strip().split(" ", 1)
  if len(parts) != 2 or parts[0].lower() != "bearer":
    return False
  provided = parts[1].strip()
  if not provided:
    return False
  expected_tokens = _get_auth_tokens()
  if not expected_tokens:
    return False
  return any(hmac.compare_digest(provided, tok) for tok in expected_tokens)


def get_bind_address() -> tuple[str, int]:
  host = _get_config("HEARTH_PHOTOS_DAEMON_HOST", DEFAULT_BIND_HOST) or DEFAULT_BIND_HOST
  try:
    port = int(_get_config("HEARTH_PHOTOS_DAEMON_PORT", str(DEFAULT_BIND_PORT)))
  except ValueError:
    port = DEFAULT_BIND_PORT
  return host, port


ALBUM_URL = _get_config("HEARTH_GPHOTOS_ALBUM_URL", "")
ALBUM_TITLE = _get_config("HEARTH_GPHOTOS_ALBUM_TITLE", "Family Album")
AUTH_URL = _build_auth_url(ALBUM_URL)

PHOTO_PAT = re.compile(
    r'https://lh3\.googleusercontent\.com/(?:pw|p)/[A-Za-z0-9_\-]+(?![A-Za-z0-9_\-=])'
)

cmd_queue = []
state_lock = threading.Lock()
latest_state = {
    "active": False,
    "url": "",
    "title": "",
    "screenshot_b64": "",
    "synced_count": 0,
    "synced": False,
    "error": "",
    "updated_at": 0,
}


def extract_photos_from_html(html_text: str) -> list[str]:
  if not html_text:
    return []
  normalized = (
      html_text.replace("\\u003d", "=")
      .replace("\\u0026", "&")
      .replace("\\/", "/")
  )
  seen = set()
  out = []
  for m in PHOTO_PAT.finditer(normalized):
    raw = m.group(0)
    if len(raw) < 55:
      continue
    sized = f"{raw}=w1920-h1080"
    if sized not in seen:
      seen.add(sized)
      out.append(sized)
  return out


def push_to_hearth(photos: list[str]) -> bool:
  album_url = _get_config("HEARTH_GPHOTOS_ALBUM_URL", ALBUM_URL)
  album_title = _get_config("HEARTH_GPHOTOS_ALBUM_TITLE", ALBUM_TITLE)
  payload = json.dumps({
      "photos": photos,
      "album_url": album_url,
      "title": album_title,
  }).encode("utf-8")
  req = urllib.request.Request(
      HEARTH_IMPORT_URL,
      data=payload,
      headers={"Content-Type": "application/json"},
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=15) as resp:
      return resp.status == 200
  except Exception as e:
    print("Failed to push to Hearth:", e)
    return False


def browser_worker():
  if sync_playwright is None:
    raise RuntimeError("playwright is not installed")
  os.makedirs(PROFILE_DIR, exist_ok=True)
  with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=True,
        viewport={"width": 900, "height": 620},
        user_agent=(
            "Mozilla/5.0 (Linux; Android 16; ApoloSign) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    while True:
      cmd = None
      with state_lock:
        if cmd_queue:
          cmd = cmd_queue.pop(0)

      if cmd:
        action = cmd.get("action")
        try:
          if action == "start":
            target = cmd.get("url") or _build_auth_url() or AUTH_URL
            if not _is_allowed_auth_url(target):
              with state_lock:
                latest_state["error"] = "disallowed auth URL"
                latest_state["active"] = False
              cmd = None
            else:
              page.goto(target, wait_until="domcontentloaded", timeout=25000)
              page.wait_for_timeout(1200)
          elif action == "click":
            x = float(cmd.get("x", 450))
            y = float(cmd.get("y", 310))
            page.mouse.click(x, y)
            page.wait_for_timeout(1000)
          elif action == "type":
            text = str(cmd.get("text", ""))
            submit = bool(cmd.get("submit", True))
            if cmd.get("clear"):
              page.keyboard.press("Control+A")
              page.keyboard.press("Backspace")
            if text:
              page.keyboard.type(text, delay=25)
            if submit:
              page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
          elif action == "key":
            key = str(cmd.get("key", "Enter"))
            page.keyboard.press(key)
            page.wait_for_timeout(800)
        except Exception as e:
          with state_lock:
            latest_state["error"] = str(e)

      # Periodically snapshot if active or just received a command
      with state_lock:
        is_active = latest_state["active"]

      if is_active or cmd:
        try:
          cur_url = page.url
          title = page.title()
          html = page.content()
          photos = extract_photos_from_html(html)
          synced = False
          if photos:
            push_to_hearth(photos)
            synced = True
          png = page.screenshot(type="jpeg", quality=75)
          b64 = base64.b64encode(png).decode("ascii")
          with state_lock:
            latest_state["active"] = not synced
            latest_state["url"] = cur_url
            latest_state["title"] = title
            latest_state["screenshot_b64"] = b64
            if photos:
              latest_state["synced_count"] = len(photos)
              latest_state["synced"] = True
            latest_state["updated_at"] = time.time()
        except Exception as e:
          with state_lock:
            latest_state["error"] = str(e)

      time.sleep(0.4 if is_active else 1.0)


class Handler(BaseHTTPRequestHandler):

  def _json(self, code, data):
    raw = json.dumps(data).encode("utf-8")
    self.send_response(code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def _require_auth(self) -> bool:
    auth_header = self.headers.get("Authorization", "") if self.headers else ""
    if not _is_authorized(auth_header):
      self._json(401, {"error": "unauthorized"})
      return False
    return True

  def do_GET(self):
    if not self._require_auth():
      return
    if self.path.startswith("/status"):
      with state_lock:
        self._json(200, dict(latest_state))
      return
    self._json(404, {"error": "not found"})

  def do_POST(self):
    if not self._require_auth():
      return
    try:
      length = int(self.headers.get("Content-Length", "0") or "0")
      raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
      body = json.loads(raw_body or "{}")
    except (ValueError, json.JSONDecodeError):
      self._json(400, {"error": "invalid json"})
      return
    if not isinstance(body, dict):
      self._json(400, {"error": "invalid payload"})
      return
    if self.path.startswith("/action"):
      action = body.get("action", "start")
      if action == "start":
        target = body.get("url") or _build_auth_url() or AUTH_URL
        if not _is_allowed_auth_url(target):
          self._json(400, {"error": "disallowed auth URL"})
          return
      with state_lock:
        if action == "start":
          latest_state["active"] = True
          latest_state["synced"] = False
          latest_state["error"] = ""
        elif action == "stop":
          latest_state["active"] = False
        cmd_queue.append(body)
      # Wait briefly for command to process
      time.sleep(1.4)
      with state_lock:
        self._json(200, dict(latest_state))
      return
    self._json(404, {"error": "not found"})

  def log_message(self, fmt, *args):
    return


if __name__ == "__main__":
  host, port = get_bind_address()
  t = threading.Thread(target=browser_worker, daemon=True)
  t.start()
  server = HTTPServer((host, port), Handler)
  print(f"Hearth Photos Auth Daemon listening on {host}:{port}")
  server.serve_forever()
