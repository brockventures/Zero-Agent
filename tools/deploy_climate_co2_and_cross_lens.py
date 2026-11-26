"""Deploy 3D floorplan Climate lens CO2 readouts and cross-lens contextual active badges to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.climate-co2-cross-lens.backup.js.
2. Idempotently patch brock-house-cycles-card.js to:
   - Add `co2Entity` to every room in `f.climateRooms` (rosie -> sensor.rosie_co2,
     isaac -> sensor.isaac_co2, living -> sensor.aranet4_29564_carbon_dioxide,
     primary/bedroom -> sensor.bedroom_co2, office -> sensor.office_co2).
   - Update `_getRoomClimate(r)` to read `co2: r.co2Entity ? this._readNumericState(r.co2Entity) : null`
     and include `co2Entity` state in `sigParts` so CO2 changes trigger re-renders.
   - Update `.climate-badge` template with `<span class="clim-co2" data-room-co2="${r.id}"></span>`,
     style `.clim-co2` color tiers (`.co2-good` <1000 ppm soft green, `.co2-warn` 1000-1499 ppm amber,
     `.co2-high` >=1500 ppm red), and populate it in the `isClimate` update loop.
   - Update `.running-layer` visibility toggle to `!this._activeRoom` (and z-index to clear
     thermal washes and lens overlays without blocking interactions) and remove `&& !isClimate`
     gates from `runningItems`, gaming badges, TV/media cover art thumbnails, LG Oven, and MEATER+.
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

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.climate-co2-cross-lens.backup.js")

ROOM_CO2_MAP: tuple[tuple[str, str], ...] = (
    ("rosie", "sensor.rosie_co2"),
    ("isaac", "sensor.isaac_co2"),
    ("living", "sensor.aranet4_29564_carbon_dioxide"),
    ("primary", "sensor.bedroom_co2"),
    ("bedroom", "sensor.bedroom_co2"),
    ("office", "sensor.office_co2"),
)

REQUIRED_MARKERS: tuple[str, ...] = (
    "/* CLIMATE_CO2_CROSS_LENS_CSS_BEGIN */",
    "/* CLIMATE_CO2_CROSS_LENS_CSS_END */",
    "/* CLIMATE_CO2_DOM_UPDATE_BEGIN */",
    "/* CLIMATE_CO2_DOM_UPDATE_END */",
)

CLIMATE_CO2_CROSS_LENS_CSS = """        /* CLIMATE_CO2_CROSS_LENS_CSS_BEGIN */
        .clim-co2 {
          display: none;
          align-items: center;
          justify-content: center;
          font-size: 9px;
          font-weight: 700;
          line-height: 1.1;
          padding: 1.5px 5.5px;
          margin-top: 1px;
          border-radius: 999px;
          letter-spacing: 0.01em;
          border: 1px solid transparent;
        }
        .clim-co2:not(:empty) {
          display: inline-flex;
        }
        .clim-co2.co2-good,
        .co2-good {
          color: #86efac;
          background: rgba(74, 222, 128, 0.14);
          border-color: rgba(74, 222, 128, 0.36);
        }
        .clim-co2.co2-warn,
        .co2-warn {
          color: #ffd180;
          background: rgba(255, 183, 77, 0.18);
          border-color: rgba(255, 183, 77, 0.45);
        }
        .clim-co2.co2-high,
        .co2-high {
          color: #ff9e80;
          background: rgba(255, 82, 82, 0.22);
          border-color: rgba(255, 82, 82, 0.55);
        }
        .stage.exploded .clim-co2 {
          font-size: 8px;
          padding: 1px 4.5px;
        }
        @media (min-width: 820px) {
          .stage.exploded .clim-co2 {
            font-size: 9px;
            padding: 1.5px 5.5px;
          }
        }
        /* CLIMATE_CO2_CROSS_LENS_CSS_END */"""

GET_ROOM_CLIMATE_JS = """  _getRoomClimate(r) {
    const temp = this._readNumericState(r.tempEntity, r.tempFallback);
    const hum = this._readNumericState(r.humEntity, r.humFallback);
    let hvacAction = null;
    if (r.climateEntity) {
      const cst = this._hass?.states?.[r.climateEntity];
      hvacAction = cst?.attributes?.hvac_action || null;
    }
    return {
      temp,
      hum,
      co2: r.co2Entity ? this._readNumericState(r.co2Entity) : null,
      hvacAction,
    };
  }"""

CLIMATE_SIG_LOOP_JS = """      for (const r of (f.climateRooms || [])) {
        const c = this._getRoomClimate(r);
        sigParts.push(
          c.temp !== null ? c.temp.toFixed(1) : "",
          c.hum !== null ? Math.round(c.hum) : "",
          c.co2 !== null && c.co2 !== undefined ? Math.round(c.co2) : "",
          r.co2Entity ? (this._hass?.states?.[r.co2Entity]?.state || "") : "",
          c.hvacAction || ""
        );
      }"""

CLIMATE_CO2_DOM_UPDATE_JS = """          /* CLIMATE_CO2_DOM_UPDATE_BEGIN */
          const co2El = card.querySelector(`[data-room-co2="${r.id}"]`);
          if (co2El) {
            co2El.classList.remove("co2-good", "co2-warn", "co2-high");
            if (c.co2 !== null && c.co2 !== undefined) {
              const ppm = Math.round(c.co2);
              const co2Tier = ppm >= 1500 ? "co2-high" : ppm >= 1000 ? "co2-warn" : "co2-good";
              co2El.textContent = `${ppm} ppm`;
              co2El.classList.add(co2Tier);
              co2El.style.display = "inline-flex";
            } else {
              co2El.textContent = "";
              co2El.style.display = "none";
            }
          }
          /* CLIMATE_CO2_DOM_UPDATE_END */"""


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


def _patch_climate_room_co2(text: str, room_id: str, co2_entity: str, *, required: bool = True) -> tuple[str, bool]:
    """Idempotently add or update `co2Entity` on a climateRooms entry identified by `room_id`."""
    pattern = re.compile(
        rf'(\{{\s*id:\s*"{re.escape(room_id)}"[^{{}}]*?tempEntity:\s*"[^"]+"[^{{}}]*?)(\n([ \t]*)\}})',
    )
    match = pattern.search(text)
    if not match:
        if required:
            raise RuntimeError(f"Could not locate climateRooms entry for room '{room_id}'")
        return text, False

    body = match.group(1)
    closing = match.group(2)
    closing_indent = match.group(3)
    prop_indent = closing_indent + "  "

    if re.search(r'\bco2Entity\s*:\s*"[^"]*"', body):
        new_body = re.sub(
            r'\bco2Entity\s*:\s*"[^"]*"',
            f'co2Entity: "{co2_entity}"',
            body,
            count=1,
        )
    else:
        stripped = body.rstrip()
        if not stripped.endswith(","):
            stripped += ","
        new_body = f'{stripped}\n{prop_indent}co2Entity: "{co2_entity}",'

    patched = text[: match.start()] + new_body + closing + text[match.end() :]
    return patched, True


def verify_patched_card_contents(card_js: str) -> None:
    """Assert CO2 entities, climate badge readout, color tiers, and cross-lens active badge logic are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    required_tokens: tuple[str, ...] = (
        'co2Entity: "sensor.rosie_co2"',
        'co2Entity: "sensor.isaac_co2"',
        'co2Entity: "sensor.aranet4_29564_carbon_dioxide"',
        'co2Entity: "sensor.bedroom_co2"',
        'co2Entity: "sensor.office_co2"',
        "co2: r.co2Entity ? this._readNumericState(r.co2Entity) : null",
        '<span class="clim-co2" data-room-co2="${r.id}"></span>',
        ".clim-co2",
        ".co2-good",
        ".co2-warn",
        ".co2-high",
        'if (runLayer) runLayer.classList.toggle("visible", !this._activeRoom);',
        'el.classList.toggle("active", ev.active);',
        "const showOnFloor = gb.active && gb.floorId === f.id;",
        "this._applyRunningItemThumb(el, ri, ev);",
        'ovenEl.classList.toggle("active", ovenEv.active);',
        'meaterEl.classList.toggle("active", meaterEv.active);',
    )
    for token in required_tokens:
        if token not in card_js:
            missing.append(f"token:{token}")

    forbidden_tokens: tuple[str, ...] = (
        'runLayer.classList.toggle("visible", !isSecurity && !isEnergy && !isCameras && !isPresence && !this._activeRoom)',
        'el.classList.toggle("active", ev.active && !isClimate)',
        "const showOnFloor = gb.active && gb.floorId === f.id && !isClimate;",
        "this._applyRunningItemThumb(el, ri, isClimate ? { ...ev, active: false } : ev);",
        'ovenEl.classList.toggle("active", ovenEv.active && !isClimate)',
        'meaterEl.classList.toggle("active", meaterEv.active && !isClimate)',
    )
    for bad in forbidden_tokens:
        if bad in card_js:
            missing.append(f"forbidden_still_present:{bad}")

    if missing:
        raise RuntimeError("Patched climate CO2 & cross-lens verification failed: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js for Climate CO2 readouts and cross-lens active badges."""
    out = source_js

    # 1. Add co2Entity to every room in f.climateRooms
    out, _ = _patch_climate_room_co2(out, "rosie", "sensor.rosie_co2", required=True)
    out, _ = _patch_climate_room_co2(out, "isaac", "sensor.isaac_co2", required=True)
    out, _ = _patch_climate_room_co2(out, "living", "sensor.aranet4_29564_carbon_dioxide", required=True)
    out, patched_primary = _patch_climate_room_co2(out, "primary", "sensor.bedroom_co2", required=False)
    out, patched_bedroom = _patch_climate_room_co2(out, "bedroom", "sensor.bedroom_co2", required=False)
    if not (patched_primary or patched_bedroom):
        raise RuntimeError("Could not locate climateRooms entry for room 'primary' or 'bedroom'")
    out, _ = _patch_climate_room_co2(out, "office", "sensor.office_co2", required=True)

    # 2. Update _getRoomClimate(r) to include co2: r.co2Entity ? this._readNumericState(r.co2Entity) : null
    out = _sub_required(
        r"[ \t]*_getRoomClimate\s*\(\s*(?:room|r)\s*\)\s*\{[\s\S]*?return\s*\{[\s\S]*?hvacAction[\s\S]*?\};\s*\}",
        GET_ROOM_CLIMATE_JS,
        out,
        label="_getRoomClimate(r)",
    )

    # 3. Include co2Entity state in sigParts inside climateRooms loop
    out = _sub_required(
        r"[ \t]*for\s*\(\s*const\s+r\s+of\s+\(f\.climateRooms\s*\|\|\s*\[\]\)\s*\)\s*\{\s*const\s+c\s*=\s*this\._getRoomClimate\(r\);\s*sigParts\.push\([\s\S]*?\);\s*\}",
        CLIMATE_SIG_LOOP_JS,
        out,
        label="sigParts climateRooms loop",
    )

    # 4. Inject .clim-co2 and color tier CSS (.co2-good, .co2-warn, .co2-high) + update .running-layer z-index
    marked_css_re = re.compile(
        r"[ \t]*/\*\s*CLIMATE_CO2_CROSS_LENS_CSS_BEGIN\s*\*/[\s\S]*?/\*\s*CLIMATE_CO2_CROSS_LENS_CSS_END\s*\*/",
    )
    if marked_css_re.search(out):
        out = marked_css_re.sub(lambda _m: CLIMATE_CO2_CROSS_LENS_CSS, out, count=1)
    else:
        out = _sub_required(
            r"(\.stage\.exploded\s+\.climate-badge-hum\s*\{\s*font-size:\s*10px;\s*\}\s*\})",
            r"\1\n" + CLIMATE_CO2_CROSS_LENS_CSS,
            out,
            label="climate-badge CSS block",
        )

    out = _sub_required(
        r"(\.running-layer\s*\{\s*position:\s*absolute;\s*inset:\s*0;\s*z-index:\s*)\d+(;)",
        r"\g<1>18\2",
        out,
        label=".running-layer z-index",
    )

    # 5. Add <span class="clim-co2" data-room-co2="${r.id}"></span> to .climate-badge template
    if '<span class="clim-co2" data-room-co2="${r.id}"></span>' not in out:
        out = _sub_required(
            r'(<span class="climate-badge-vals">\s*<span class="climate-badge-temp" data-room-temp="\$\{r\.id\}">--°</span>\s*<span class="climate-badge-hum" data-room-hum="\$\{r\.id\}"></span>\s*</span>)',
            r'\1\n                    <span class="clim-co2" data-room-co2="${r.id}"></span>',
            out,
            label=".climate-badge template clim-co2 span",
        )

    # 6. Populate [data-room-co2="${r.id}"] in the isClimate update loop
    marked_dom_re = re.compile(
        r"[ \t]*/\*\s*CLIMATE_CO2_DOM_UPDATE_BEGIN\s*\*/[\s\S]*?/\*\s*CLIMATE_CO2_DOM_UPDATE_END\s*\*/",
    )
    if marked_dom_re.search(out):
        out = marked_dom_re.sub(lambda _m: CLIMATE_CO2_DOM_UPDATE_JS, out, count=1)
    else:
        out = _sub_required(
            r'(const\s+humEl\s*=\s*card\.querySelector\(`\[data-room-hum="\$\{r\.id\}"\]`\);\s*if\s*\(humEl\)\s*\{\s*humEl\.textContent\s*=\s*c\.hum\s*!==\s*null\s*\?\s*`\$\{Math\.round\(c\.hum\)\}%`\s*:\s*"";\s*\})',
            r"\1\n" + CLIMATE_CO2_DOM_UPDATE_JS,
            out,
            label="isClimate update loop CO2 population",
        )

    # 7. Cross-lens contextual active badges: .running-layer visible across all lenses when !this._activeRoom
    out = _sub_required(
        r'if\s*\(runLayer\)\s*runLayer\.classList\.toggle\("visible",\s*(?:!isSecurity\s*&&\s*!isEnergy\s*&&\s*!isCameras\s*&&\s*!isPresence\s*&&\s*)?!this\._activeRoom\);',
        'if (runLayer) runLayer.classList.toggle("visible", !this._activeRoom);',
        out,
        label=".running-layer visibility toggle",
    )

    # 8. Remove && !isClimate gate from active contextual badges in the update loop
    out = _sub_required(
        r'el\.classList\.toggle\("active",\s*ev\.active(?:\s*&&\s*!isClimate)?\);',
        'el.classList.toggle("active", ev.active);',
        out,
        label="runningItems active toggle",
    )
    out = _sub_required(
        r"const\s+showOnFloor\s*=\s*gb\.active\s*&&\s*gb\.floorId\s*===\s*f\.id(?:\s*&&\s*!isClimate)?;?",
        "const showOnFloor = gb.active && gb.floorId === f.id;",
        out,
        label="gamingBadges showOnFloor",
    )
    out = _sub_required(
        r"this\._applyRunningItemThumb\(el,\s*ri,\s*(?:isClimate\s*\?\s*\{\s*\.\.\.ev,\s*active:\s*false\s*\}\s*:\s*)?ev\);",
        "this._applyRunningItemThumb(el, ri, ev);",
        out,
        label="TV/media cover art _applyRunningItemThumb",
    )
    out = _sub_required(
        r'ovenEl\.classList\.toggle\("active",\s*ovenEv\.active(?:\s*&&\s*!isClimate)?\);',
        'ovenEl.classList.toggle("active", ovenEv.active);',
        out,
        label="LG Oven active toggle",
    )
    out = _sub_required(
        r'meaterEl\.classList\.toggle\("active",\s*meaterEv\.active(?:\s*&&\s*!isClimate)?\);',
        'meaterEl.classList.toggle("active", meaterEv.active);',
        out,
        label="MEATER+ active toggle",
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
    already_patched = "CLIMATE_CO2_CROSS_LENS_CSS_BEGIN" in card_text
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
    updated = asyncio.run(
        asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=cache_buster), timeout=25)
    )
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
    """Verify HTTP 200 and Climate CO2 + cross-lens active badge contents on the live JS card on Home Assistant."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(
                asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None), timeout=25)
            )
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, Climate CO2 & cross-lens active badges)",
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

    print(
        "SUCCESS: Deployed and verified Climate lens CO2 readouts and cross-lens contextual active badges (HTTP 200).",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch and deploy 3D floorplan Climate CO2 readouts and cross-lens active badges to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and Climate CO2 / cross-lens active badge contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with Climate CO2 & cross-lens active badges applied.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
