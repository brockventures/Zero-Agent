"""Deploy 3D floorplan lamp and stairs glow dial-back tuning to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.lamp-glow-tune.backup.js.
2. Idempotently patch brock-house-cycles-card.js to soften overly aggressive
   .lamp-glow and .glow-strong halos (shrinking Floor 1/2 stairs and lantern
   radii, default lamp/ceiling glow radii, radial-gradient alpha stops in night
   and day themes, and runtime baseGain multipliers).
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

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.lamp-glow-tune.backup.js")

REQUIRED_MARKERS: tuple[str, ...] = (
    "/* LAMP_GLOW_TUNE_CSS_BEGIN */",
    "/* LAMP_GLOW_TUNE_CSS_END */",
)

LAMP_GLOW_TUNE_CSS = """        /* LAMP_GLOW_TUNE_CSS_BEGIN */
        .img-overlay.glow-strong {
          filter: saturate(1.36) brightness(1.16);
        }
        .lamp-glow {
          position: absolute;
          transform: translate(-50%, -50%);
          border-radius: 50%;
          pointer-events: none;
          display: none;
          opacity: 0;
          mix-blend-mode: screen;
          background: radial-gradient(
            circle at center,
            rgba(255, 242, 196, 0.50) 0%,
            rgba(255, 198, 86, 0.25) 26%,
            rgba(255, 162, 42, 0.08) 54%,
            rgba(255, 146, 24, 0.0) 76%
          );
          transition: opacity 0.32s ease;
        }
        .lamp-glow.glow-strong {
          background: radial-gradient(
            circle at center,
            rgba(255, 246, 210, 0.62) 0%,
            rgba(255, 206, 90, 0.32) 26%,
            rgba(255, 166, 40, 0.11) 54%,
            rgba(255, 140, 20, 0.0) 78%
          );
        }
        .shell.day-theme .lamp-glow {
          mix-blend-mode: normal;
          background: radial-gradient(
            circle at center,
            rgba(255, 238, 178, 0.42) 0%,
            rgba(255, 192, 68, 0.20) 26%,
            rgba(245, 158, 11, 0.06) 54%,
            rgba(245, 158, 11, 0.0) 76%
          );
        }
        .shell.day-theme .lamp-glow.glow-strong {
          background: radial-gradient(
            circle at center,
            rgba(255, 242, 192, 0.52) 0%,
            rgba(255, 196, 62, 0.26) 26%,
            rgba(245, 152, 12, 0.09) 54%,
            rgba(245, 152, 12, 0.0) 78%
          );
        }
        /* LAMP_GLOW_TUNE_CSS_END */"""

MAIN_BASE_GAIN_JS = """const baseGain = isStrongGlow
              ? (isDayTheme ? 0.48 : 0.54)
              : l.icon === "lamp"
              ? (isDayTheme ? 0.42 : 0.44)
              : (isDayTheme ? 0.26 : 0.20);"""

EXTRA_OVERLAY_BASE_GAIN_JS = "const baseGain = isStrongEo ? (isDayTheme ? 0.46 : 0.52) : 0.34;"


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


def _sub_required(pattern: str, repl: str, text: str, *, label: str, flags: int = 0) -> str:
    """Apply a regex replacement and raise RuntimeError if the pattern did not match."""
    new_text, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count == 0:
        raise RuntimeError(f"Could not locate pattern for '{label}'")
    return new_text


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
    """Assert all dial-back glow radii, CSS stops, markers, and runtime baseGain multipliers are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    required_tokens: tuple[str, ...] = (
        "glowRx: 8.0, glowRy: 10.5",
        "glowRx: 7.5, glowRy: 10.0",
        "glowRx: 7.5, glowRy: 9.5",
        'const rx = l.glowRx || (l.icon === "lamp" ? 7.0 : 6.0);',
        'const ry = l.glowRy || (l.icon === "lamp" ? 9.5 : 8.0);',
        "width:${ov.glowRx || 7.5}%;height:${ov.glowRy || 9.5}%;",
        "filter: saturate(1.36) brightness(1.16);",
        "rgba(255, 242, 196, 0.50) 0%",
        "rgba(255, 198, 86, 0.25) 26%",
        "rgba(255, 162, 42, 0.08) 54%",
        "rgba(255, 146, 24, 0.0) 76%",
        "rgba(255, 246, 210, 0.62) 0%",
        "rgba(255, 206, 90, 0.32) 26%",
        "rgba(255, 166, 40, 0.11) 54%",
        "rgba(255, 140, 20, 0.0) 78%",
        "rgba(255, 238, 178, 0.42) 0%",
        "rgba(255, 192, 68, 0.20) 26%",
        "rgba(245, 158, 11, 0.06) 54%",
        "rgba(245, 158, 11, 0.0) 76%",
        "rgba(255, 242, 192, 0.52) 0%",
        "rgba(255, 196, 62, 0.26) 26%",
        "rgba(245, 152, 12, 0.09) 54%",
        "rgba(245, 152, 12, 0.0) 78%",
        "? (isDayTheme ? 0.48 : 0.54)",
        "? (isDayTheme ? 0.42 : 0.44)",
        ": (isDayTheme ? 0.26 : 0.20);",
        "const baseGain = isStrongEo ? (isDayTheme ? 0.46 : 0.52) : 0.34;",
    )
    for token in required_tokens:
        if token not in card_js:
            missing.append(f"token:{token}")

    if missing:
        raise RuntimeError("Patched lamp glow tune verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js to dial back lamp and stairs glow halos."""
    out = source_js

    # 1. Shrink stairs & lantern glow radii in FLOORS config
    out = _sub_required(
        r'(\{\s*entity:\s*"light\.stairs_2"\s*,\s*name:\s*"Stairs"[^}]*?glowRx:\s*)\d+(?:\.\d+)?(\s*,\s*glowRy:\s*)\d+(?:\.\d+)?',
        r"\g<1>8.0\g<2>10.5",
        out,
        label="Floor 2 light.stairs_2 glowRx/glowRy",
    )
    out = _sub_required(
        r'(\{\s*entity:\s*"light\.living_room_lantern"[^}]*?glowRx:\s*)\d+(?:\.\d+)?(\s*,\s*glowRy:\s*)\d+(?:\.\d+)?',
        r"\g<1>7.5\g<2>10.0",
        out,
        label="Floor 2 light.living_room_lantern glowRx/glowRy",
    )
    out = _sub_required(
        r'(\{\s*entity:\s*"light\.stairs_2"\s*,\s*slug:\s*"floor1_light_stairs_lights"[^}]*?glowRx:\s*)\d+(?:\.\d+)?(\s*,\s*glowRy:\s*)\d+(?:\.\d+)?',
        r"\g<1>7.5\g<2>9.5",
        out,
        label="Floor 1 extraOverlays light.stairs_2 glowRx/glowRy",
    )

    # 2. Shrink default lamp/ceiling glow radii in template
    out = _sub_required(
        r'const\s+rx\s*=\s*l\.glowRx\s*\|\|\s*\(l\.icon\s*===\s*"lamp"\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\);',
        'const rx = l.glowRx || (l.icon === "lamp" ? 7.0 : 6.0);',
        out,
        label="template default rx",
    )
    out = _sub_required(
        r'const\s+ry\s*=\s*l\.glowRy\s*\|\|\s*\(l\.icon\s*===\s*"lamp"\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\);',
        'const ry = l.glowRy || (l.icon === "lamp" ? 9.5 : 8.0);',
        out,
        label="template default ry",
    )
    out = _sub_required(
        r"(\$\{ov\.glowRx\s*\|\|\s*)\d+(?:\.\d+)?(\}%;height:\$\{ov\.glowRy\s*\|\|\s*)\d+(?:\.\d+)?(\}%)",
        r"\g<1>7.5\g<2>9.5\g<3>",
        out,
        label="template extraOverlay fallback ov.glowRx/ov.glowRy",
    )

    # 3. Soften CSS radial gradients & overlay filter
    marked_css_re = re.compile(
        r"[ \t]*/\*\s*LAMP_GLOW_TUNE_CSS_BEGIN\s*\*/[\s\S]*?/\*\s*LAMP_GLOW_TUNE_CSS_END\s*\*/",
    )
    if marked_css_re.search(out):
        out = marked_css_re.sub(lambda _m: LAMP_GLOW_TUNE_CSS, out, count=1)
    else:
        out = _sub_required(
            r"[ \t]*\.img-overlay\.glow-strong\s*\{[\s\S]*?\.shell\.day-theme\s+\.lamp-glow\.glow-strong\s*\{[\s\S]*?\}",
            LAMP_GLOW_TUNE_CSS,
            out,
            label="CSS .img-overlay.glow-strong through .shell.day-theme .lamp-glow.glow-strong",
        )

    # 4. Lower runtime baseGain opacity multipliers
    out = _sub_required(
        r'const\s+baseGain\s*=\s*isStrongGlow\s*\?\s*\(isDayTheme\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\)\s*:\s*l\.icon\s*===\s*"lamp"\s*\?\s*\(isDayTheme\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\)\s*:\s*\(isDayTheme\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\);',
        MAIN_BASE_GAIN_JS,
        out,
        label="main lights loop baseGain",
    )
    out = _sub_required(
        r"const\s+baseGain\s*=\s*isStrongEo\s*\?\s*\(isDayTheme\s*\?\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\)\s*:\s*\d+(?:\.\d+)?;",
        EXTRA_OVERLAY_BASE_GAIN_JS,
        out,
        label="extraOverlays loop baseGain",
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
    already_patched = "LAMP_GLOW_TUNE_CSS_BEGIN" in card_text
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
    """Verify HTTP 200 and lamp/stairs glow dial-back contents on the live JS card on Home Assistant."""
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, 3D floorplan lamp & stairs glow dial-back)",
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

    print("SUCCESS: Deployed and verified 3D floorplan lamp & stairs glow dial-back on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch and deploy 3D floorplan lamp and stairs glow dial-back tuning to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and lamp/stairs glow tuning contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with lamp & stairs glow dial-back applied.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
