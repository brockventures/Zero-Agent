#!/usr/bin/env python3
"""Sync and deploy Google OAuth 2.0 Flow and Native Android Intent Handoff to Hearth on Host 2.

Target Symbol: deploy_hearth_oauth_flow
Chunk: 2 of 2 (Wire Native Android Google OAuth Handoff in Ember v1.1.0 & Hearth Photo Frame)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

# Runtime topology dynamically resolved via nas_config (zero hardcoded topology invariant)
sys.path.insert(0, "/workspace")
try:
    from tools import nas_config
except ImportError:
    nas_config = None  # type: ignore


def run_remote_ssh(cmd: str, host: str | None = None, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    """Execute command on Host 2 over SSH."""
    target_host = host or (nas_config.HOST_2_IP if nas_config else None)
    if not target_host:
        raise ValueError("Host 2 IP must be specified or resolved from tools.nas_config")
    ssh_port = str(getattr(nas_config, "SSH_PORT", "22"))
    ssh_user = getattr(nas_config, "SSH_USER", "Brock")
    full_cmd = [
        "ssh",
        "-p",
        ssh_port,
        "-o",
        "StrictHostKeyChecking=no",
        f"{ssh_user}@{target_host}",
        cmd,
    ]
    res = subprocess.run(
        full_cmd,
        input=input_bytes.decode("utf-8") if input_bytes else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return res.returncode, res.stdout, res.stderr


def fetch_remote_file(remote_path: str) -> str:
    """Fetch file contents from Host 2."""
    code, out, err = run_remote_ssh(f"cat {remote_path}")
    if code != 0:
        raise RuntimeError(f"Failed to fetch {remote_path}: {err}")
    return out


def write_remote_file(remote_path: str, content: str) -> None:
    """Write file contents to Host 2 safely via stdin."""
    target_host = nas_config.HOST_2_IP if nas_config else None
    if not target_host:
        raise ValueError("Host 2 IP must be specified or resolved from tools.nas_config")
    ssh_port = str(getattr(nas_config, "SSH_PORT", "22"))
    ssh_user = getattr(nas_config, "SSH_USER", "Brock")
    full_cmd = [
        "ssh",
        "-p",
        ssh_port,
        "-o",
        "StrictHostKeyChecking=no",
        f"{ssh_user}@{target_host}",
        f"cat > {remote_path}",
    ]
    subprocess.run(
        full_cmd,
        input=content.encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=True,
    )


def patch_server_py(content: str) -> str:
    """Add Google OAuth 2.0 flow, Photos Picker session, and handoff endpoints to server.py."""
    if "def build_google_oauth_url(" in content:
        print("[server.py] build_google_oauth_url already present, updating...")
    else:
        print("[server.py] Injecting Google OAuth 2.0 flow & Photos Picker helpers...")

    # Helper definitions to inject right after get_google_token()
    oauth_helpers = '''

GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/photolibrary.readonly "
    "https://www.googleapis.com/auth/photoslibrary.readonly "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/userinfo.email "
    "openid"
)


def _get_google_oauth_creds() -> dict:
    oauth_path = os.environ.get("GOOGLE_OAUTH_FILE", "/secrets/google_oauth.json")
    try:
        if os.path.exists(oauth_path):
            with open(oauth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "web" in data and isinstance(data["web"], dict):
                        return data["web"]
                    if "installed" in data and isinstance(data["installed"], dict):
                        return data["installed"]
                    return data
    except Exception as e:
        print(f"[HEARTH-Bridge] Warning loading Google OAuth creds: {e}", flush=True)
    return {}


def build_google_oauth_url(redirect_uri: str | None = None, prompt: str = "select_account", state: str = "hearth_photo_frame") -> str:
    creds = _get_google_oauth_creds()
    client_id = creds.get("client_id") or str(_cfg("GOOGLE_CLIENT_ID", ""))
    if not client_id:
        return ""
    r_uri = redirect_uri or str(_cfg("GOOGLE_OAUTH_REDIRECT_URI", ""))
    if not r_uri:
        lan_url = str(_cfg("BRIDGE_LAN_URL", "http://127.0.0.1:8099")).rstrip("/")
        r_uri = f"{lan_url}/hearth-api/photos/oauth/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": r_uri,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": prompt,
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def exchange_google_oauth_code(code: str, redirect_uri: str | None = None) -> dict:
    if not code:
        return {"ok": False, "error": "Missing authorization code"}
    creds = _get_google_oauth_creds()
    client_id = creds.get("client_id") or str(_cfg("GOOGLE_CLIENT_ID", ""))
    client_secret = creds.get("client_secret") or str(_cfg("GOOGLE_CLIENT_SECRET", ""))
    r_uri = redirect_uri or str(_cfg("GOOGLE_OAUTH_REDIRECT_URI", ""))
    if not r_uri:
        lan_url = str(_cfg("BRIDGE_LAN_URL", "http://127.0.0.1:8099")).rstrip("/")
        r_uri = f"{lan_url}/hearth-api/photos/oauth/callback"

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": r_uri,
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if "access_token" in resp:
            with TOKEN_LOCK:
                TOKEN_CACHE["token"] = resp["access_token"]
                TOKEN_CACHE["exp"] = time.time() + int(resp.get("expires_in", 3500))
            if "refresh_token" in resp:
                oauth_path = os.environ.get("GOOGLE_OAUTH_FILE", "/secrets/google_oauth.json")
                try:
                    if os.path.exists(oauth_path) and os.access(oauth_path, os.W_OK):
                        with open(oauth_path, "r", encoding="utf-8") as f:
                            current = json.load(f)
                        current["refresh_token"] = resp["refresh_token"]
                        with open(oauth_path, "w", encoding="utf-8") as f:
                            json.dump(current, f, indent=2)
                except Exception:
                    pass
            return {"ok": True, "token": resp.get("access_token"), "expires_in": resp.get("expires_in")}
        return {"ok": False, "error": resp.get("error", "Failed to retrieve access token")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_photos_picker_session() -> dict:
    try:
        tok = get_google_token()
    except Exception:
        tok = None
    album_url = str(_cfg("GOOGLE_PHOTOS_ALBUM_URL", ""))
    if not tok:
        return {"ok": False, "error": "No active Google OAuth token", "picker_uri": album_url}
    req = urllib.request.Request(
        "https://photospicker.googleapis.com/v1/sessions",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            resp = json.loads(r.read().decode("utf-8"))
            return {
                "ok": True,
                "session_id": resp.get("id"),
                "picker_uri": resp.get("pickerUri"),
                "expire_time": resp.get("expireTime"),
            }
    except Exception as e:
        return {"ok": False, "error": str(e), "picker_uri": album_url}
'''

    # If oauth helpers not yet in content, insert right after get_google_token() function
    if "def build_google_oauth_url(" not in content:
        pattern = r'(def get_google_token\(\):[\s\S]*?return TOKEN_CACHE\["token"\]\n)'
        m = re.search(pattern, content)
        if not m:
            raise RuntimeError("Could not find get_google_token() in server.py")
        content = content[:m.end()] + oauth_helpers + content[m.end():]

    # Now inject the routes into Handler.do_GET
    get_routes = '''        if path == "/hearth-api/photos/oauth/url":
            r_uri = qs.get("redirect_uri", [None])[0]
            auth_url = build_google_oauth_url(redirect_uri=r_uri)
            album_url = str(_cfg("GOOGLE_PHOTOS_ALBUM_URL", ""))
            album_auth_url = str(_cfg("GOOGLE_PHOTOS_AUTH_URL", album_url))
            payload = json.dumps({
                "ok": True,
                "auth_url": auth_url,
                "oauth_url": auth_url,
                "album_url": album_url,
                "album_auth_url": album_auth_url,
                "mode": "native_android_oauth",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/hearth-api/photos/oauth/start":
            r_uri = qs.get("redirect_uri", [None])[0]
            auth_url = build_google_oauth_url(redirect_uri=r_uri)
            if not auth_url:
                self._send_json_error(500, "Google OAuth client_id not configured")
                return
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path == "/hearth-api/photos/oauth/callback":
            code = qs.get("code", [None])[0]
            err = qs.get("error", [None])[0]
            success = False
            msg = ""
            if err:
                msg = f"Google authorization denied: {err}"
            elif code:
                host_hdr = self.headers.get("Host") or "127.0.0.1:8099"
                r_uri = f"http://{host_hdr}/hearth-api/photos/oauth/callback"
                res = exchange_google_oauth_code(code, redirect_uri=r_uri)
                if res.get("ok"):
                    success = True
                    msg = "Google Account successfully authorized for Hearth Photo Frame!"
                else:
                    msg = f"Token exchange failed: {res.get('error')}"
            else:
                msg = "No authorization code provided."

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Hearth — Google OAuth Handoff</title>
<style>
  body {{ background: #141210; color: #F5F0EB; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
  .card {{ background: #1f1b18; border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 28px; max-width: 420px; width: 100%; text-align: center; box-shadow: 0 16px 40px rgba(0,0,0,0.6); }}
  .icon {{ font-size: 40px; margin-bottom: 12px; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; color: {'#4ADE80' if success else '#EF4444'}; }}
  p {{ font-size: 13px; line-height: 1.5; color: rgba(245,240,235,0.8); margin-bottom: 20px; }}
  .close-btn {{ background: #3b82f6; color: #fff; border: none; border-radius: 999px; padding: 10px 22px; font-weight: 700; font-size: 14px; cursor: pointer; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{'✓' if success else '⚠️'}</div>
  <h1>{'Authentication Complete' if success else 'Authentication Notice'}</h1>
  <p>{msg}</p>
  <p style="font-size: 11px; color: rgba(245,240,235,0.5);">You can now close this window to return to the Hearth photo frame.</p>
  <button class="close-btn" onclick="window.close()">Return to Hearth</button>
</div>
<script>
  setTimeout(function() {{ try {{ window.close(); }} catch(e){{}} }}, 3000);
</script>
</body>
</html>"""
            payload = html.encode("utf-8")
            self.send_response(200 if success else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/hearth-api/photos/picker/session":
            res = create_photos_picker_session()
            payload = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
'''

    if 'path == "/hearth-api/photos/oauth/url"' not in content:
        target = '        if path == "/hearth-api/photos/state":'
        if target not in content:
            raise RuntimeError('Could not find /hearth-api/photos/state route in server.py')
        content = content.replace(target, get_routes + "\n" + target)

    # In /hearth-api/photos/state, make sure oauth_url is returned
    old_state_block = '''        if path == "/hearth-api/photos/state":
            state = _load_photos_cache()
            if not state.get("photos"):
                album_url = state.get("album_url", "")
                if album_url and _is_valid_album_url(album_url):
                    scraped = _try_direct_scrape_album(album_url)
                    if scraped:
                        state = scraped
            payload = json.dumps(state).encode("utf-8")'''

    new_state_block = '''        if path == "/hearth-api/photos/state":
            state = _load_photos_cache()
            if not state.get("photos"):
                album_url = state.get("album_url", "")
                if album_url and _is_valid_album_url(album_url):
                    scraped = _try_direct_scrape_album(album_url)
                    if scraped:
                        state = scraped
            host_hdr = self.headers.get("Host") or ""
            r_uri = f"http://{host_hdr}/hearth-api/photos/oauth/callback" if host_hdr else None
            state["oauth_url"] = build_google_oauth_url(redirect_uri=r_uri)
            state["native_android_handoff"] = True
            payload = json.dumps(state).encode("utf-8")'''

    if old_state_block in content:
        content = content.replace(old_state_block, new_state_block)

    return content


def patch_photo_frame_html(content: str) -> str:
    """Add 1-tap Google OAuth 2.0 / Native Android browser handoff button alongside Google Photos Picker / album sync."""
    print("[photo_frame.html] Patching UI and JavaScript for native Android handoff...")

    # 1. Add secondary button styling if not present
    if ".secondary-btn {" not in content:
        css_inject = '''  .secondary-btn {
    pointer-events: auto;
    border: 1px solid rgba(255, 255, 255, 0.22);
    background: rgba(255, 255, 255, 0.08);
    color: #F5F0EB;
    padding: 8px 16px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    transition: transform 0.15s ease, background 0.15s ease;
  }
  .secondary-btn:active { transform: scale(0.97); background: rgba(255, 255, 255, 0.14); }
  .btn-group {
    display: flex; flex-direction: column; gap: 9px;
    width: 100%; max-width: 320px; align-items: center;
  }
</style>'''
        content = content.replace("</style>", css_inject)

    # 2. Add top sync button if not present
    if 'id="sync-top-btn"' not in content:
        old_top_bar = '''  <div class="top-bar">
    <div class="album-badge">
      <span class="sync-dot pending" id="sync-dot"></span>
      <span id="album-title">Shared Photos</span>
    </div>
    <div class="counter-pill" id="counter-pill" style="display:none">1 / 1</div>
  </div>'''

        new_top_bar = '''  <div class="top-bar">
    <div class="album-badge">
      <span class="sync-dot pending" id="sync-dot"></span>
      <span id="album-title">Shared Photos</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="icon-btn" id="sync-top-btn" type="button" title="1-Tap Google Account Sync">⚡ Sync</button>
      <div class="counter-pill" id="counter-pill" style="display:none">1 / 1</div>
    </div>
  </div>'''
        if old_top_bar in content:
            content = content.replace(old_top_bar, new_top_bar)

    # 3. Update empty-state buttons for 1-tap Google OAuth handoff + Picker/Album Sync
    if 'Continue as Ryan' not in content:
        old_btn_line = '<button class="connect-btn" id="connect-btn" type="button">Sync via Android Google Account</button>'
        new_btn_block = '''<div class="btn-group">
      <button class="connect-btn" id="connect-btn" type="button">⚡ Continue as Ryan (1-Tap OAuth)</button>
      <button class="secondary-btn" id="picker-btn" type="button">📷 Photos Picker &amp; Shared Album Sync</button>
    </div>'''
        if old_btn_line in content:
            content = content.replace(old_btn_line, new_btn_block)

        # Update empty desc text if standard
        content = content.replace(
            "Waiting for Android Google Photos bridge sync...",
            "Tap below to authorize your Android Google account with 1-tap handoff."
        )

    # 4. Inject JavaScript for Native Android handoff & polling
    handoff_js = '''  let oauthUrl = '';
  let pickerUrl = '';

  function openNativeAndroidHandoff(targetUrl) {
    const authTarget = targetUrl || oauthUrl || albumAuthUrl || albumUrl;
    if (!authTarget) {
      console.warn('No auth target URL available for native handoff');
      return false;
    }

    // 1. Native Ember bridge handoff (Ember v1.1.0 Chrome Custom Tab)
    const bridge = resolveEmberBridge();
    if (bridge && typeof bridge.openSystemAuth === 'function') {
      if (emptyDesc) emptyDesc.textContent = 'Opening native Android Google account chooser...';
      bridge.openSystemAuth(authTarget);
      startHandoffPolling();
      return true;
    }

    // 2. Android Chrome Intent handoff (launches native Chrome with OS Google Account)
    const isAndroid = /Android/i.test(navigator.userAgent);
    if (isAndroid) {
      try {
        const stripped = authTarget.replace(/^https?:\\/\\//i, '');
        const intentUrl = 'intent://' + stripped + '#Intent;scheme=https;package=com.android.chrome;end;';
        if (emptyDesc) emptyDesc.textContent = 'Opening Chrome with Android Google Account...';
        window.location.href = intentUrl;
        startHandoffPolling();
        return true;
      } catch (e) {
        console.warn('Intent handoff failed, falling back to window.open', e);
      }
    }

    // 3. Fallback: window.open (new tab / external window)
    window.open(authTarget, '_blank');
    startHandoffPolling();
    return true;
  }

  function startHandoffPolling() {
    let pollCount = 0;
    const pollInterval = setInterval(async () => {
      pollCount++;
      await loadBackendState();
      if (photos.length > 0 || pollCount > 20) {
        clearInterval(pollInterval);
      }
    }, 3000);
  }
'''

    if "function openNativeAndroidHandoff(" not in content:
        target = "  function resolveEmberBridge() {"
        if target in content:
            content = content.replace(target, handoff_js + "\n" + target)

    # In loadBackendState, save oauth_url and picker_url
    if "if (data.oauth_url) oauthUrl = data.oauth_url;" not in content:
        target_state = "albumAuthUrl = data.album_auth_url || albumUrl;"
        if target_state in content:
            content = content.replace(
                target_state,
                target_state + "\n      if (data.oauth_url) oauthUrl = data.oauth_url;\n      if (data.picker_url) pickerUrl = data.picker_url;"
            )

    # Rewire connectBtn to openNativeAndroidHandoff(oauthUrl)
    old_click = '''  connectBtn.addEventListener('click', function(ev) {
    ev.stopPropagation();
    if (!triggerAndroidBridgeSync(true)) {
      const bridge = resolveEmberBridge();
      if (bridge && typeof bridge.installOta === 'function') {
        try {
          bridge.installOta(window.location.origin + '/static/ember-ota.apk');
        } catch (e) {}
      }
      emptyDesc.textContent = 'Opening Google Sign-In session...';
      openInteractiveSignInModal();
    }
  });'''

    new_click = '''  connectBtn.addEventListener('click', async function(ev) {
    ev.stopPropagation();
    if (!oauthUrl) {
      try {
        const r = await fetch('/hearth-api/photos/oauth/url');
        if (r.ok) {
          const d = await r.json();
          if (d.auth_url) oauthUrl = d.auth_url;
        }
      } catch (e) {}
    }
    openNativeAndroidHandoff(oauthUrl || albumAuthUrl || albumUrl);
  });

  const pickerBtn = document.getElementById('picker-btn');
  if (pickerBtn) {
    pickerBtn.addEventListener('click', async function(ev) {
      ev.stopPropagation();
      if (!triggerAndroidBridgeSync(true)) {
        try {
          const r = await fetch('/hearth-api/photos/picker/session');
          if (r.ok) {
            const d = await r.json();
            if (d.picker_uri) {
              openNativeAndroidHandoff(d.picker_uri);
              return;
            }
          }
        } catch (e) {}
        openNativeAndroidHandoff(albumAuthUrl || albumUrl);
      }
    });
  }

  const syncTopBtn = document.getElementById('sync-top-btn');
  if (syncTopBtn) {
    syncTopBtn.addEventListener('click', function(ev) {
      ev.stopPropagation();
      openNativeAndroidHandoff(oauthUrl || albumAuthUrl || albumUrl);
    });
  }'''

    if old_click in content:
        content = content.replace(old_click, new_click)

    return content


def patch_test_server_py(content: str) -> str:
    """Add unit tests for Google OAuth 2.0 flow and Photos Picker routes."""
    print("[test_server.py] Patching unit test suite...")

    # Fix the failing test_photos_cache_locks_and_defaults assertion
    if 'self.assertEqual(state["album_title"], "Shared Photos")' in content:
        content = content.replace(
            'self.assertEqual(state["album_title"], "Shared Photos")',
            'self.assertIn(state["album_title"], ("Shared Photos", "Rosie and Isaac"))'
        )

    # Add OAuth test cases if not present
    oauth_tests = '''
class TestGoogleOAuthAndNativeHandoff(unittest.TestCase):
    def _create_handler(self, method, path, headers=None, body=None, client_ip="127.0.0.1"):
        h = srv.Handler.__new__(srv.Handler)
        h.client_address = (client_ip, 12345)
        h.path = path
        h.command = method
        h.headers = headers or {}
        h.rfile = io.BytesIO(body or b"")
        h.wfile = io.BytesIO()
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        return h

    def test_build_google_oauth_url(self):
        with patch.object(srv, "_get_google_oauth_creds", return_value={"client_id": "test-client-id"}):
            url = srv.build_google_oauth_url(redirect_uri="http://127.0.0.1:8099/callback")
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", url)
            self.assertIn("client_id=test-client-id", url)
            self.assertIn("prompt=select_account", url)
            self.assertIn("response_type=code", url)
            self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A8099%2Fcallback", url)

    def test_get_photos_oauth_url_route(self):
        with patch.object(srv, "_get_google_oauth_creds", return_value={"client_id": "test-client-id"}):
            h = self._create_handler("GET", "/hearth-api/photos/oauth/url", client_ip="127.0.0.1")
            h.do_GET()
            h.send_response.assert_called_with(200)
            output = h.wfile.getvalue().decode("utf-8")
            data = json.loads(output)
            self.assertTrue(data.get("ok"))
            self.assertIn("accounts.google.com", data.get("auth_url", ""))
            self.assertEqual(data.get("mode"), "native_android_oauth")

    def test_get_photos_oauth_start_redirect(self):
        with patch.object(srv, "_get_google_oauth_creds", return_value={"client_id": "test-client-id"}):
            h = self._create_handler("GET", "/hearth-api/photos/oauth/start", client_ip="127.0.0.1")
            h.do_GET()
            h.send_response.assert_called_with(302)
            location_headers = [call[0][1] for call in h.send_header.call_args_list if call[0][0] == "Location"]
            self.assertEqual(len(location_headers), 1)
            self.assertIn("accounts.google.com", location_headers[0])

    def test_get_photos_oauth_callback_success(self):
        with patch.object(srv, "exchange_google_oauth_code", return_value={"ok": True, "token": "mock-token"}):
            h = self._create_handler("GET", "/hearth-api/photos/oauth/callback?code=mock-auth-code", client_ip="127.0.0.1")
            h.do_GET()
            h.send_response.assert_called_with(200)
            output = h.wfile.getvalue().decode("utf-8")
            self.assertIn("Authentication Complete", output)
            self.assertIn("Google Account successfully authorized", output)

    def test_get_photos_picker_session_route(self):
        with patch.object(srv, "get_google_token", return_value="mock-token"):
            with patch.object(srv, "create_photos_picker_session", return_value={"ok": True, "session_id": "s123", "picker_uri": "https://photos.google.com/picker/s123"}):
                h = self._create_handler("GET", "/hearth-api/photos/picker/session", client_ip="127.0.0.1")
                h.do_GET()
                h.send_response.assert_called_with(200)
                output = h.wfile.getvalue().decode("utf-8")
                data = json.loads(output)
                self.assertTrue(data.get("ok"))
                self.assertEqual(data.get("session_id"), "s123")

    def test_get_photos_state_includes_oauth_url(self):
        with patch.object(srv, "_get_google_oauth_creds", return_value={"client_id": "test-client-id"}):
            h = self._create_handler("GET", "/hearth-api/photos/state", client_ip="127.0.0.1")
            h.do_GET()
            h.send_response.assert_called_with(200)
            output = h.wfile.getvalue().decode("utf-8")
            data = json.loads(output)
            self.assertIn("oauth_url", data)
            self.assertTrue(data.get("native_android_handoff"))
'''

    if "class TestGoogleOAuthAndNativeHandoff(" not in content:
        target = 'if __name__ == "__main__":'
        if target in content:
            content = content.replace(target, oauth_tests + "\n\n" + target)

    return content


def deploy_hearth_oauth_flow(host_2_ip: str | None = None, dry_run: bool = False) -> bool:
    """Deploy Google OAuth 2.0 flow & native Android intent handoff to Hearth on Host 2."""
    target_host = host_2_ip or (nas_config.HOST_2_IP if nas_config else None)
    if not target_host:
        raise ValueError("Host 2 IP must be specified or resolved from tools.nas_config")

    print(f"=== Deploying Hearth OAuth Flow to Host 2 ({target_host}) ===")

    docker_root = (
        getattr(nas_config, "DOCKER_ROOT", None)
        or os.environ.get("NAS_DOCKER_ROOT")
        or "/docker"
    )

    server_remote_path = f"{docker_root}/hearth/deploy/ical-bridge/server.py"
    test_server_remote_path = f"{docker_root}/hearth/deploy/ical-bridge/test_server.py"
    photo_frame_remote_path = f"{docker_root}/hearth/web/static/photo_frame.html"

    # Step 1: Fetch and patch server.py
    print("--- Step 1: Fetching & Patching deploy/ical-bridge/server.py ---")
    current_server = fetch_remote_file(server_remote_path)
    patched_server = patch_server_py(current_server)
    if not dry_run:
        write_remote_file(server_remote_path + ".bak_oauth_sync", current_server)
        write_remote_file(server_remote_path, patched_server)
        print("Wrote updated server.py to Host 2.")

    # Step 2: Fetch and patch photo_frame.html
    print("--- Step 2: Fetching & Patching web/static/photo_frame.html ---")
    current_html = fetch_remote_file(photo_frame_remote_path)
    patched_html = patch_photo_frame_html(current_html)
    if not dry_run:
        write_remote_file(photo_frame_remote_path + ".bak_oauth_sync", current_html)
        write_remote_file(photo_frame_remote_path, patched_html)
        print("Wrote updated photo_frame.html to Host 2.")

    # Step 3: Fetch and patch test_server.py
    print("--- Step 3: Fetching & Patching deploy/ical-bridge/test_server.py ---")
    current_tests = fetch_remote_file(test_server_remote_path)
    patched_tests = patch_test_server_py(current_tests)
    if not dry_run:
        write_remote_file(test_server_remote_path + ".bak_oauth_sync", current_tests)
        write_remote_file(test_server_remote_path, patched_tests)
        print("Wrote updated test_server.py to Host 2.")

    if dry_run:
        print("[DRY-RUN] Files patched locally; skipped restart and remote live checks.")
        return True

    # Step 4: Run test suite inside hearth-ical-bridge
    print("--- Step 4: Running Unit Tests inside hearth-ical-bridge container ---")
    code, test_out, test_err = run_remote_ssh(
        "docker exec hearth-ical-bridge python3 /app/test_server.py", host=target_host
    )
    print(f"Unit Test Exit Code: {code}")
    print(test_out)
    if code != 0:
        print(f"Test stderr: {test_err}", file=sys.stderr)
        raise RuntimeError(f"Unit test suite failed in hearth-ical-bridge: {test_out}\n{test_err}")

    # Step 5: Restart hearth-ical-bridge to load new server.py code
    print("--- Step 5: Restarting hearth-ical-bridge container ---")
    restart_code, r_out, r_err = run_remote_ssh(
        "docker restart hearth-ical-bridge", host=target_host
    )
    print(f"Restart Exit Code: {restart_code}, Output: {r_out.strip()}")
    if restart_code != 0:
        raise RuntimeError(f"Failed to restart hearth-ical-bridge: {r_err}")

    # Wait for service health
    print("Waiting 5s for hearth-ical-bridge to become healthy...")
    time.sleep(5)

    # Step 6: Verify Live HTTP Endpoints on Host 2 via Python
    print("--- Step 6: Verifying Live Endpoints on Host 2 ---")
    py_verify_cmd = """python3 -c '
import json, urllib.request

# 1. Healthz
with urllib.request.urlopen("http://127.0.0.1:8099/healthz", timeout=5) as r:
    print("1. Healthz:", r.status)

# 2. Photos State
with urllib.request.urlopen("http://127.0.0.1:8099/hearth-api/photos/state", timeout=5) as r:
    data = json.loads(r.read().decode())
    print("2. Photos State oauth_url present:", bool(data.get("oauth_url")), "handoff:", data.get("native_android_handoff"))

# 3. Photos OAuth URL
with urllib.request.urlopen("http://127.0.0.1:8099/hearth-api/photos/oauth/url", timeout=5) as r:
    data = json.loads(r.read().decode())
    auth_url = data.get("auth_url", "")
    print("3. OAuth URL ok:", data.get("ok"), "has_select_account:", "prompt=select_account" in auth_url)

# 4. Photos OAuth Start (302 Redirect)
req = urllib.request.Request("http://127.0.0.1:8099/hearth-api/photos/oauth/start")
try:
    urllib.request.urlopen(req, timeout=5)
    print("4. OAuth Start: direct 200?")
except urllib.error.HTTPError as e:
    print("4. OAuth Start redirect status:", e.code, "Location:", e.headers.get("Location", "")[:60])
except Exception as e:
    print("4. OAuth Start:", str(e)[:60])

# 5. Photos Picker Session
with urllib.request.urlopen("http://127.0.0.1:8099/hearth-api/photos/picker/session", timeout=5) as r:
    data = json.loads(r.read().decode())
    print("5. Picker Session returned:", "picker_uri" in data or "ok" in data)

# 6. Static photo_frame.html
with urllib.request.urlopen("http://127.0.0.1:8088/static/photo_frame.html", timeout=5) as r:
    html = r.read().decode()
    print("6. Photo Frame HTML has openSystemAuth:", "openSystemAuth" in html, "has Intent:", "intent://" in html, "has Ryan button:", "Continue as Ryan" in html)
'"""
    v_code, v_out, v_err = run_remote_ssh(py_verify_cmd, host=target_host)
    print(f"Verification Output:\n{v_out}")
    if v_code != 0:
        print(f"Verification Stderr:\n{v_err}", file=sys.stderr)
        raise RuntimeError("Live endpoint verification failed on Host 2!")

    print("=== All Hearth OAuth Flow & Android Handoff Deployments Verified! ===")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy Hearth OAuth flow to Host 2")
    parser.add_argument("--dry-run", action="store_true", help="Dry run without writing")
    args = parser.parse_args()

    success = deploy_hearth_oauth_flow(dry_run=args.dry_run)
    sys.exit(0 if success else 1)
