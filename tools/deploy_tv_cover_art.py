#!/usr/bin/env python3
"""Deploy 3D floorplan TV & media badge cover-art thumbnails to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.tv-cover-art.backup.js.
2. Idempotently patch brock-house-cycles-card.js with marker-delimited
   (/* TV_COVER_ART_..._BEGIN */ / /* TV_COVER_ART_..._END */) CSS, artwork
   extraction/update helpers, .run-badge <img> templates, sigParts change
   detection, and live DOM update hooks for TV (living_tv, office_tv) and
   media_sensor (apolosign_music) badges.
3. Validate patched JS syntax via `node --check`.
4. Deploy updated JS card to Host 1 Volume 2 NVMe, bump the HA Lovelace
   resource cache-buster via WebSocket, and verify HTTP 200 and live content.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools import nas_config

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.tv-cover-art.backup.js")

REQUIRED_MARKERS: tuple[str, ...] = (
    "/* TV_COVER_ART_CSS_BEGIN */",
    "/* TV_COVER_ART_CSS_END */",
    "/* TV_COVER_ART_HELPER_BEGIN */",
    "/* TV_COVER_ART_HELPER_END */",
    "/* TV_COVER_ART_HTML_BEGIN */",
    "/* TV_COVER_ART_HTML_END */",
    "/* TV_COVER_ART_SIG_BEGIN */",
    "/* TV_COVER_ART_SIG_END */",
    "/* TV_COVER_ART_UPDATE_BEGIN */",
    "/* TV_COVER_ART_UPDATE_END */",
)

TV_COVER_ART_CSS = """        /* TV_COVER_ART_CSS_BEGIN */
        .run-badge .run-icon[data-run-icon] {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .run-badge.has-thumb .run-icon[data-run-icon] {
          display: none;
        }
        .run-badge .run-thumb[data-run-thumb] {
          border-radius: 3px;
          object-fit: cover;
          flex-shrink: 0;
          border: 1px solid rgba(255, 255, 255, 0.24);
          background: rgba(0, 0, 0, 0.35);
        }
        .run-badge[data-run-kind="tv"] .run-thumb[data-run-thumb] {
          width: 16px;
          height: 22px;
          aspect-ratio: 2 / 3;
          object-position: center top;
        }
        .run-badge[data-run-kind="media_sensor"] .run-thumb[data-run-thumb] {
          width: 18px;
          height: 18px;
          aspect-ratio: 1 / 1;
          border-radius: 4px;
        }
        .run-badge.has-thumb[data-run-kind="tv"],
        .run-badge.has-thumb[data-run-kind="media_sensor"] {
          padding-top: 2px;
          padding-bottom: 2px;
        }
        .stage.exploded .run-badge[data-run-kind="tv"] .run-thumb[data-run-thumb] {
          width: 13px;
          height: 18px;
          border-radius: 2.5px;
        }
        .stage.exploded .run-badge[data-run-kind="media_sensor"] .run-thumb[data-run-thumb] {
          width: 15px;
          height: 15px;
          border-radius: 3px;
        }
        /* TV_COVER_ART_CSS_END */"""

TV_COVER_ART_HELPER_JS = """  /* TV_COVER_ART_HELPER_BEGIN */
  _sanitizeArtworkUrl(rawUrl) {
    const url = String(rawUrl || "").trim();
    if (!url) return "";
    const lower = url.toLowerCase();
    if (["none", "null", "undefined", "unknown", "unavailable"].includes(lower)) return "";
    if (
      url.startsWith("/") ||
      lower.startsWith("http://") ||
      lower.startsWith("https://") ||
      lower.startsWith("data:image/")
    ) {
      return url;
    }
    return "";
  }

  _extractMediaEntityArtwork(st, kind) {
    if (!st || !st.attributes) return "";
    const attrs = st.attributes;
    if (kind === "media_sensor") {
      return this._sanitizeArtworkUrl(
        attrs.entity_picture || attrs.entity_picture_local || attrs.artwork_url || attrs.cover_url || ""
      );
    }
    return this._sanitizeArtworkUrl(attrs.entity_picture || attrs.entity_picture_local || "");
  }

  _resolveRunningItemThumb(item, ev) {
    if (!item || !ev || !ev.active) return "";
    if (item.kind !== "tv" && item.kind !== "media_sensor") return "";
    const states = this._hass?.states || {};

    if (item.kind === "media_sensor") {
      const primary = ev.entity || item.entity;
      const thumb = this._extractMediaEntityArtwork(states[primary], "media_sensor");
      if (thumb) return thumb;
      for (const cid of item.fallbackEntities || []) {
        const st = states[cid];
        if (st && st.state === "playing") {
          const fbThumb = this._extractMediaEntityArtwork(st, "media_sensor");
          if (fbThumb) return fbThumb;
        }
      }
      return "";
    }

    const activeEntity = ev.entity || item.entity;
    const activeThumb = this._extractMediaEntityArtwork(states[activeEntity], "tv");
    if (activeThumb) return activeThumb;

    const candidates = [item.entity, ...(item.fallbackEntities || [])];
    for (const cid of candidates) {
      if (!cid || cid === activeEntity || cid.startsWith("switch.")) continue;
      const st = states[cid];
      if (!st) continue;
      if (st.state === "playing" || st.state === "paused" || st.state === "buffering") {
        const candThumb = this._extractMediaEntityArtwork(st, "tv");
        if (candThumb) return candThumb;
      }
    }
    return "";
  }

  _collectTvCoverArtSigParts() {
    const states = this._hass?.states || {};
    const parts = [];
    for (const f of FLOORS) {
      for (const ri of f.runningItems || []) {
        if (ri.kind !== "tv" && ri.kind !== "media_sensor") continue;
        const candidates = [ri.entity, ...(ri.fallbackEntities || [])];
        for (const cid of candidates) {
          if (!cid) continue;
          const st = states[cid];
          if (!st) {
            parts.push("");
            continue;
          }
          parts.push(st.state || "", this._extractMediaEntityArtwork(st, ri.kind));
        }
      }
    }
    return parts;
  }

  _applyRunningItemThumb(el, ri, ev) {
    if (!el || !ri || (ri.kind !== "tv" && ri.kind !== "media_sensor")) return;
    const thumbEl = el.querySelector(`[data-run-thumb="${ri.id}"]`);
    if (!thumbEl) return;
    const thumb = this._resolveRunningItemThumb(ri, ev);
    if (thumb) {
      if (thumbEl.dataset?.failedSrc === thumb) {
        thumbEl.style.display = "none";
        el.classList.remove("has-thumb");
      } else {
        if (thumbEl.getAttribute("src") !== thumb) {
          if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
          thumbEl.setAttribute("src", thumb);
        }
        thumbEl.style.display = "block";
        el.classList.add("has-thumb");
      }
    } else {
      if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
      thumbEl.removeAttribute("src");
      thumbEl.style.display = "none";
      el.classList.remove("has-thumb");
    }
  }
  /* TV_COVER_ART_HELPER_END */"""

TV_COVER_ART_HTML_JS = """/* TV_COVER_ART_HTML_BEGIN */
                  (f.runningItems || [])
                    .map(
                      (item) => `
                  <button class="run-badge ${item.accent || ""}" data-run-id="${item.id}" data-run-kind="${item.kind || ""}" data-run-entity="${item.entity}" style="left:${item.x}%;top:${item.y}%;" title="${item.label}">
                    <span class="run-dot"></span>
                    ${
                      item.kind === "tv" || item.kind === "media_sensor"
                        ? `<img class="run-thumb" data-run-thumb="${item.id}" alt="" decoding="async" draggable="false" style="display:none;" onload="delete this.dataset.failedSrc;this.style.display='block';this.closest('.run-badge')?.classList.add('has-thumb');" onerror="this.dataset.failedSrc=this.getAttribute('src')||'';this.style.display='none';this.closest('.run-badge')?.classList.remove('has-thumb');" />`
                        : ""
                    }
                    <span class="run-icon" data-run-icon="${item.id}">${SVG_ICONS[item.icon] || ""}</span>
                    <span data-run-text="${item.id}">${item.label}</span>
                  </button>
                `
                    )
                  /* TV_COVER_ART_HTML_END */"""

TV_COVER_ART_SIG_JS = """    /* TV_COVER_ART_SIG_BEGIN */
    sigParts.push(...this._collectTvCoverArtSigParts());
    /* TV_COVER_ART_SIG_END */"""

TV_COVER_ART_UPDATE_JS = """      /* TV_COVER_ART_UPDATE_BEGIN */
      for (const ri of (f.runningItems || [])) {
        if (ri.kind !== "tv" && ri.kind !== "media_sensor") continue;
        const el = card?.querySelector(`.run-badge[data-run-id="${ri.id}"]`);
        if (!el) continue;
        const ev = this._evalRunningItem(ri);
        this._applyRunningItemThumb(el, ri, isClimate ? { ...ev, active: false } : ev);
      }
      /* TV_COVER_ART_UPDATE_END */"""


def get_remote_paths() -> dict[str, str]:
    """Resolve Host 1 WWW paths dynamically from nas_config without hardcoded volumes or IPs."""
    www_dir = posixpath.join(nas_config.HA_CONFIG_ROOT, "www", "brock-house-3d")
    return {
        "www_dir": www_dir,
        "card_path": posixpath.join(www_dir, "brock-house-cycles-card.js"),
    }


def resolve_ha_credentials() -> tuple[str, str]:
    """Resolve Home Assistant base URL and access token from env or /secrets."""
    ha_url = (os.environ.get("HA_BASE_URL") or "").strip()
    ha_token = (os.environ.get("HA_ACCESS_TOKEN") or "").strip()

    for secret_path, url_key, tok_key in (
        (Path("/secrets/ha.json"), "url", "token"),
        (Path("/secrets/env.json"), "HA_BASE_URL", "HA_ACCESS_TOKEN"),
    ):
        if ha_url and ha_token:
            break
        if secret_path.is_file():
            try:
                data = json.loads(secret_path.read_text(encoding="utf-8"))
                if not ha_url and data.get(url_key):
                    ha_url = str(data[url_key]).strip()
                if not ha_token and data.get(tok_key):
                    ha_token = str(data[tok_key]).strip()
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] Failed reading {secret_path}: {exc}", file=sys.stderr, flush=True)

    if not ha_url and nas_config.HOST_1_IP and nas_config.HOST_1_IP != "127.0.0.1":
        ha_url = f"http://{nas_config.HOST_1_IP}:8123"
    if not ha_url:
        raise RuntimeError("Could not resolve HA_BASE_URL from env, /secrets/ha.json, or /secrets/env.json")
    return ha_url.rstrip("/"), ha_token


def _replace_or_insert_block(
    src: str,
    begin_marker: str,
    end_marker: str,
    new_block: str,
    anchor_patterns: str | tuple[str, ...],
    *,
    insert_before: bool = False,
    label: str,
) -> str:
    """Idempotently replace a marked block or insert it relative to the first matching anchor regex."""
    marked_re = re.compile(
        r"[ \t]*" + re.escape(begin_marker) + r"[\s\S]*?" + re.escape(end_marker),
    )
    if marked_re.search(src):
        return marked_re.sub(lambda _m: new_block, src, count=1)

    patterns = (anchor_patterns,) if isinstance(anchor_patterns, str) else anchor_patterns
    for pat in patterns:
        m = re.search(pat, src, re.MULTILINE)
        if m:
            if insert_before:
                return src[: m.start()] + new_block + "\n" + src[m.start() :]
            return src[: m.end()] + "\n" + new_block + src[m.end() :]
    raise RuntimeError(f"Could not locate anchor for '{label}'")


def _replace_marked_or_inline_pattern(
    src: str,
    begin_marker: str,
    end_marker: str,
    new_block: str,
    unpatched_pattern: str,
    *,
    label: str,
) -> str:
    """Idempotently replace a marked block or an unpatched inline code span."""
    marked_re = re.compile(re.escape(begin_marker) + r"[\s\S]*?" + re.escape(end_marker))
    if marked_re.search(src):
        return marked_re.sub(lambda _m: new_block, src, count=1)

    m = re.search(unpatched_pattern, src, re.MULTILINE)
    if not m:
        raise RuntimeError(f"Could not locate inline target for '{label}'")
    return src[: m.start()] + new_block + src[m.end() :]


def validate_js_syntax(card_js: str) -> None:
    """Validate patched JavaScript syntax using `node --check`."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(card_js)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["node", "--check", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"node --check failed (rc={proc.returncode}):\n{proc.stderr.strip()}")
    finally:
        tmp_path.unlink(missing_ok=True)


def verify_patched_card_contents(card_js: str) -> None:
    """Assert all TV & media cover art markers, artwork attributes, and handlers are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")
    for token in (
        "_sanitizeArtworkUrl",
        "_extractMediaEntityArtwork",
        "_resolveRunningItemThumb",
        "_collectTvCoverArtSigParts",
        "_applyRunningItemThumb",
        "entity_picture",
        "entity_picture_local",
        "artwork_url",
        "cover_url",
        "data-run-thumb",
        "data-run-icon",
        "data-run-kind",
        "has-thumb",
        "failedSrc",
        "living_tv",
        "office_tv",
        "apolosign_music",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")
    if missing:
        raise RuntimeError("Patched TV cover art verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with TV & media cover-art thumbnails."""
    out = source_js

    out = _replace_or_insert_block(
        out,
        "/* TV_COVER_ART_CSS_BEGIN */",
        "/* TV_COVER_ART_CSS_END */",
        TV_COVER_ART_CSS,
        (
            r"/\*\s*GAMING_BADGES_CSS_END\s*\*/",
            r"^\s*\.run-badge\.danger\s+\.run-dot\s*\{\s*background:\s*#ff5252;\s*\}",
        ),
        insert_before=False,
        label="TV_COVER_ART_CSS",
    )

    out = _replace_or_insert_block(
        out,
        "/* TV_COVER_ART_HELPER_BEGIN */",
        "/* TV_COVER_ART_HELPER_END */",
        TV_COVER_ART_HELPER_JS,
        r"^\s*_evalRunningItem\(item\)\s*\{",
        insert_before=True,
        label="TV_COVER_ART_HELPER",
    )

    out = _replace_marked_or_inline_pattern(
        out,
        "/* TV_COVER_ART_HTML_BEGIN */",
        "/* TV_COVER_ART_HTML_END */",
        TV_COVER_ART_HTML_JS,
        (
            r"\(f\.runningItems\s*\|\|\s*\[\]\)\s*\.map\([\s\S]*?"
            r'<span\s+data-run-text="\$\{item\.id\}">\$\{item\.label\}</span>\s*'
            r"</button>\s*`\s*\)"
        ),
        label="TV_COVER_ART_HTML",
    )

    out = _replace_or_insert_block(
        out,
        "/* TV_COVER_ART_SIG_BEGIN */",
        "/* TV_COVER_ART_SIG_END */",
        TV_COVER_ART_SIG_JS,
        r'^\s*const\s+sig\s*=\s*sigParts\.join\("\|"\);',
        insert_before=True,
        label="TV_COVER_ART_SIG",
    )

    out = _replace_or_insert_block(
        out,
        "/* TV_COVER_ART_UPDATE_BEGIN */",
        "/* TV_COVER_ART_UPDATE_END */",
        TV_COVER_ART_UPDATE_JS,
        (
            r"/\*\s*GAMING_BADGES_UPDATE_END\s*\*/",
            (
                r"for\s*\(const\s+ri\s+of\s+\(f\.runningItems\s*\|\|\s*\[\]\)\)\s*\{[\s\S]*?"
                r"if\s*\(txtEl\)\s*txtEl\.textContent\s*=\s*ev\.text;\s*\n\s*\}\s*\n\s*\}"
            ),
        ),
        insert_before=False,
        label="TV_COVER_ART_UPDATE",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def fetch_live_card_and_backup(backup_path: Path = BACKUP_CARD_PATH) -> str:
    """Fetch live brock-house-cycles-card.js from Host 1 via SSH and back it up locally."""
    paths = get_remote_paths()
    card_path = paths["card_path"]
    print(f"[1/4] Fetching live card from Host 1: {card_path}", flush=True)
    rc, out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"base64 -w 0 {shlex.quote(card_path)}",
        timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"Failed fetching {card_path} from Host 1 (rc={rc}): {err}")

    raw_bytes = base64.b64decode(out.strip())
    card_text = raw_bytes.decode("utf-8")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    already_patched = "TV_COVER_ART_HELPER_BEGIN" in card_text
    if not already_patched or not backup_path.is_file():
        backup_path.write_bytes(raw_bytes)
        print(f"[1/4] Backed up live card ({len(raw_bytes):,} bytes) -> {backup_path}", flush=True)
    else:
        print(
            f"[1/4] Fetched live card ({len(raw_bytes):,} bytes); preserved pre-patch backup at {backup_path}",
            flush=True,
        )
    return card_text


def deploy_card_to_host1(patched_js: str) -> None:
    """Upload updated brock-house-cycles-card.js to Host 1 Volume 2 NVMe www dir."""
    paths = get_remote_paths()
    buf = io.BytesIO()
    now_ts = int(time.time())
    with tarfile.open(fileobj=buf, mode="w") as tf:
        js_bytes = patched_js.encode("utf-8")
        js_info = tarfile.TarInfo(name="brock-house-cycles-card.js")
        js_info.size = len(js_bytes)
        js_info.mode = 0o644
        js_info.mtime = now_ts
        tf.addfile(js_info, io.BytesIO(js_bytes))

    b64_payload = base64.b64encode(buf.getvalue()).decode("ascii")
    remote_cmd = (
        f"mkdir -p {shlex.quote(paths['www_dir'])} && "
        f"base64 -d | tar -xf - -C {shlex.quote(paths['www_dir'])}"
    )
    rc, _out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        remote_cmd,
        timeout=30,
        stdin_data=b64_payload,
    )
    if rc != 0:
        raise RuntimeError(f"Failed deploying card JS to Host 1 (rc={rc}): {err}")
    print(
        f"[3/4] Deployed brock-house-cycles-card.js ({len(patched_js.encode('utf-8')):,} bytes) -> {paths['card_path']}",
        flush=True,
    )


def _with_cache_buster(resource_url: str, cache_buster: str) -> str:
    """Replace or append ?v=<cache_buster> on a Lovelace resource URL."""
    parsed = urllib.parse.urlsplit(resource_url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    new_pairs = [(k, v) for (k, v) in query_pairs if k != "v"]
    new_pairs.append(("v", cache_buster))
    new_query = urllib.parse.urlencode(new_pairs)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


async def _ws_lovelace_resources(ha_url: str, ha_token: str, cache_buster: str | None = None) -> list[dict[str, Any]]:
    """Query Lovelace resources via HA WebSocket and optionally update brock-house-cycles-card.js cache-buster."""
    import aiohttp

    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    client_timeout = aiohttp.ClientTimeout(total=20)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.ws_connect(ws_url, timeout=15) as ws,
    ):
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected HA WS handshake: {hello}")
        await ws.send_json({"type": "auth", "access_token": ha_token})
        auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"HA WS auth failed: {auth_resp}")

        await ws.send_json({"id": 1, "type": "lovelace/resources"})
        res = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if not res.get("success"):
            raise RuntimeError(f"HA WS lovelace/resources failed: {res}")
        resources: list[dict[str, Any]] = res.get("result") or []

        if cache_buster is not None:
            target = next(
                (r for r in resources if "/local/brock-house-3d/brock-house-cycles-card.js" in (r.get("url") or "")),
                None,
            )
            if not target:
                raise RuntimeError("Could not find brock-house-cycles-card.js in HA lovelace/resources")
            new_url = _with_cache_buster(target["url"], cache_buster)
            await ws.send_json(
                {
                    "id": 2,
                    "type": "lovelace/resources/update",
                    "resource_id": target["id"],
                    "url": new_url,
                    "res_type": target.get("type", "module"),
                }
            )
            upd = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if not upd.get("success"):
                raise RuntimeError(f"HA WS lovelace/resources/update failed: {upd}")
            return [upd["result"]]

        return resources


def update_lovelace_resource_cache_buster(ha_url: str, ha_token: str, cache_buster: str) -> str:
    """Update HA Lovelace resource URL for brock-house-cycles-card.js with ?v=<cache_buster>."""
    if not ha_token:
        raise RuntimeError("HA access token required to update Lovelace resource via WebSocket")
    updated = asyncio.run(asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=cache_buster), timeout=25))
    new_url = updated[0]["url"]
    print(f"[4/4] Updated HA Lovelace resource via WebSocket -> {new_url}", flush=True)
    return new_url


def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and TV/media cover art contents on the live JS card on Home Assistant."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None), timeout=25))
            matched = next((r for r in resources if card_rel in (r.get("url") or "")), None)
            if matched:
                card_rel = matched["url"]
                print(f"[verify] Lovelace resource URL: {card_rel}", flush=True)
            else:
                errors.append(f"Lovelace resource for {card_rel} not found via WebSocket")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Lovelace WebSocket query failed: {exc}")

    card_url = f"{ha_url}{card_rel}"
    status, body = _http_get(card_url)
    if status != 200:
        errors.append(f"HTTP {status} for live card: {card_url}")
    else:
        card_text = body.decode("utf-8", errors="replace")
        try:
            verify_patched_card_contents(card_text)
            print(f"OK [200] {card_url} ({len(body):,} bytes)", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Live card content check failed ({card_url}): {exc}")

    return (len(errors) == 0), errors


def run_deploy(*, dry_run: bool = False) -> int:
    """Execute the full fetch -> backup -> patch -> syntax check -> deploy -> WS bust -> HTTP verify pipeline."""
    ha_url, ha_token = resolve_ha_credentials()

    live_js = fetch_live_card_and_backup(BACKUP_CARD_PATH)
    patched_js = patch_card_js(live_js)
    js_hash = hashlib.sha256(patched_js.encode("utf-8")).hexdigest()[:8]
    print(
        f"[2/4] Patched & syntax-checked card JS via node --check "
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, TV & media cover art thumbnails)",
        flush=True,
    )

    if dry_run:
        print("[dry-run] Skipping Host 1 upload, WebSocket cache-buster update, and HTTP check.", flush=True)
        return 0

    deploy_card_to_host1(patched_js)
    cache_buster = f"{js_hash}{int(time.time()) & 0xFFFF:04x}"[:12]
    update_lovelace_resource_cache_buster(ha_url, ha_token, cache_buster=cache_buster)

    ok, errors = verify_live_http(ha_url, ha_token)
    if not ok:
        for err in errors:
            print(f"VERIFY ERROR: {err}", file=sys.stderr, flush=True)
        return 1

    print("SUCCESS: Deployed and verified 3D floorplan TV & media cover art thumbnails on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy 3D floorplan TV & media badge cover-art thumbnails to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and TV/media cover art contents on the live card on Home Assistant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, back up, patch, and syntax-check card JS in memory without modifying Host 1 or HA.",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        ha_url, ha_token = resolve_ha_credentials()
        ok, errors = verify_live_http(ha_url, ha_token)
        if not ok:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr, flush=True)
            return 1
        print("VERIFIED: Live card returned HTTP 200 with TV & media cover art thumbnails wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
