#!/usr/bin/env python3
"""Interactive Playwright Google Photos Auth & Album Sync Daemon for Brock Hearth.

Listens on 0.0.0.0:8098 so hearth-ical-bridge (/hearth-api/photos/interactive/*)
can render a live interactive Google Sign-In session directly inside the
ApoloSign Android tablet's WebView without X-Frame-Options restrictions.
"""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import threading
import time
import urllib.request
from playwright.sync_api import sync_playwright

PROFILE_DIR = "/workspace/data/hearth_gphotos_profile"
ALBUM_URL = "https://photos.google.com/share/AF1QipP4bhMA5NcQrYuOJ9orqYIf7PDRZklHk4zMtdKaREch057I8BUy9Jwu5bsNapl-ew?key=b21uM2VDMWtaWFd4M1EtWUpINll6MzBwT3VHR3BR"
AUTH_URL = (
    ALBUM_URL + f"&obfsgid={os.environ.get('GOOGLE_PHOTOS_OBFSGID', '')}&email={os.environ.get('GOOGLE_PHOTOS_EMAIL', '')}"
)
HEARTH_IMPORT_URL = "http://127.0.0.1:8099/hearth-api/photos/sync"

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
  payload = json.dumps({
      "photos": photos,
      "album_url": ALBUM_URL,
      "title": "Rosie and Isaac",
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
            target = cmd.get("url") or AUTH_URL
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
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def do_GET(self):
    if self.path.startswith("/status"):
      with state_lock:
        self._json(200, dict(latest_state))
      return
    self._json(404, {"error": "not found"})

  def do_POST(self):
    length = int(self.headers.get("Content-Length", "0") or "0")
    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
    if self.path.startswith("/action"):
      action = body.get("action", "start")
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
  t = threading.Thread(target=browser_worker, daemon=True)
  t.start()
  server = HTTPServer(("0.0.0.0", 8098), Handler)
  print("Hearth Photos Auth Daemon listening on 0.0.0.0:8098")
  server.serve_forever()
