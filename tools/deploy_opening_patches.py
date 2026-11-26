#!/usr/bin/env python3
"""Deploy 3D floorplan opening & curtain patches and update the live HA Cycles card.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up to
   /workspace/scratch/brock-house-cycles-card.backup.js.
2. Wire a dedicated .opening-patches layer per floor between the base/evening
   images and additive light overlays, cross-fading day/night patch pairs via
   _computeNightBlend(), setting display:none when inactive, and ordering
   Rosie's curtains above Rosie's exterior door.
3. Connect all 7 live HA entities across Floor 1 and Floor 3.
4. Update Security pin coordinates for living_blinds, kitchen_win, rosie_door,
   and isaac_shades to match real walls.
5. Deploy updated JS card and all 18 WebP patches to Host 1 Volume 2 NVMe,
   update the HA Lovelace resource cache-buster via WebSocket, and verify
   HTTP 200 on the JS card and every patch asset.
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
import sys
import tarfile
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

LOCAL_PATCHES_DIR = Path("/workspace/data/renders/patches")
BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.backup.js")

ALL_18_PATCHES: tuple[str, ...] = (
    "floor1_front_door_closed_day.webp",
    "floor1_front_door_closed_night.webp",
    "floor1_front_door_open_day.webp",
    "floor1_front_door_open_night.webp",
    "floor1_garage_door_closed_day.webp",
    "floor1_garage_door_closed_night.webp",
    "floor3_kitchen_window_open_day.webp",
    "floor3_kitchen_window_open_night.webp",
    "floor3_kitchen_door_open_day.webp",
    "floor3_kitchen_door_open_night.webp",
    "floor3_rosie_door_open_day.webp",
    "floor3_rosie_door_open_night.webp",
    "floor3_rosie_curtains_closed_day.webp",
    "floor3_rosie_curtains_closed_night.webp",
    "floor3_isaac_curtains_closed_day.webp",
    "floor3_isaac_curtains_closed_night.webp",
    "floor3_isaac_curtains_open_day.webp",
    "floor3_isaac_curtains_open_night.webp",
)

LIVE_ENTITIES: tuple[str, ...] = (
    "binary_sensor.lumi_lumi_sensor_magnet_aq2_opening",
    "binary_sensor.garage_door_sensor_2_opening",
    "binary_sensor.kitchen_window_sensor_1_opening",
    "binary_sensor.kitchen_door_sensor_opening",
    "binary_sensor.rosie_door_sensor_opening",
    "cover.curtain_3_31",
    "cover.curtain_3_64",
)

PIN_COORDINATE_UPDATES: dict[str, tuple[float, float]] = {
    "living_blinds": (28.0, 60.0),
    "kitchen_win": (18.0, 35.0),
    "rosie_door": (82.0, 51.0),
    "isaac_shades": (77.0, 71.0),
}

FLOOR3_OPENING_PATCHES_JS = """    /* OPENING_PATCHES_CONFIG_BEGIN:floor3 */
    openingPatches: [
      {
        id: "kitchen_window_open",
        entity: "binary_sensor.kitchen_window_sensor_1_opening",
        kind: "opening",
        when: "on",
        day: "floor3_kitchen_window_open_day.webp",
        night: "floor3_kitchen_window_open_night.webp",
        z: 1,
      },
      {
        id: "kitchen_door_open",
        entity: "binary_sensor.kitchen_door_sensor_opening",
        kind: "opening",
        when: "on",
        day: "floor3_kitchen_door_open_day.webp",
        night: "floor3_kitchen_door_open_night.webp",
        z: 1,
      },
      {
        id: "rosie_door_open",
        entity: "binary_sensor.rosie_door_sensor_opening",
        kind: "opening",
        when: "on",
        day: "floor3_rosie_door_open_day.webp",
        night: "floor3_rosie_door_open_night.webp",
        z: 1,
      },
      {
        id: "rosie_curtains_closed",
        entity: "cover.curtain_3_31",
        kind: "cover",
        when: "closed",
        day: "floor3_rosie_curtains_closed_day.webp",
        night: "floor3_rosie_curtains_closed_night.webp",
        z: 2,
      },
      {
        id: "isaac_curtains_closed",
        entity: "cover.curtain_3_64",
        kind: "cover",
        when: "closed",
        day: "floor3_isaac_curtains_closed_day.webp",
        night: "floor3_isaac_curtains_closed_night.webp",
        z: 2,
      },
      {
        id: "isaac_curtains_open",
        entity: "cover.curtain_3_64",
        kind: "cover",
        when: "open",
        day: "floor3_isaac_curtains_open_day.webp",
        night: "floor3_isaac_curtains_open_night.webp",
        z: 2,
      },
    ],
    /* OPENING_PATCHES_CONFIG_END:floor3 */"""

FLOOR2_OPENING_PATCHES_JS = """    /* OPENING_PATCHES_CONFIG_BEGIN:floor2 */
    openingPatches: [],
    /* OPENING_PATCHES_CONFIG_END:floor2 */"""

FLOOR1_OPENING_PATCHES_JS = """    /* OPENING_PATCHES_CONFIG_BEGIN:floor1 */
    openingPatches: [
      {
        id: "front_door_closed",
        entity: "binary_sensor.lumi_lumi_sensor_magnet_aq2_opening",
        kind: "opening",
        when: "off",
        day: "floor1_front_door_closed_day.webp",
        night: "floor1_front_door_closed_night.webp",
        z: 1,
      },
      {
        id: "front_door_open",
        entity: "binary_sensor.lumi_lumi_sensor_magnet_aq2_opening",
        kind: "opening",
        when: "on",
        day: "floor1_front_door_open_day.webp",
        night: "floor1_front_door_open_night.webp",
        z: 1,
      },
      {
        id: "garage_door_closed",
        entity: "binary_sensor.garage_door_sensor_2_opening",
        kind: "opening",
        when: "off",
        day: "floor1_garage_door_closed_day.webp",
        night: "floor1_garage_door_closed_night.webp",
        z: 1,
      },
    ],
    /* OPENING_PATCHES_CONFIG_END:floor1 */"""

OPENING_PATCH_HELPER_JS = """  /* OPENING_PATCHES_HELPER_BEGIN */
  _isOpeningPatchActive(patch) {
    const st = this._hass?.states?.[patch.entity];
    const raw = st ? st.state : "unknown";
    if (patch.kind === "cover") {
      const pos = st?.attributes?.current_position;
      const numPos =
        typeof pos === "number"
          ? pos
          : pos !== undefined && pos !== null && pos !== ""
          ? Number(pos)
          : NaN;
      const isClosed = raw === "closed" || (!Number.isNaN(numPos) && numPos < 50);
      return patch.when === "closed" ? isClosed : !isClosed;
    }
    const isOpen = raw === "on";
    return patch.when === "on" ? isOpen : !isOpen;
  }
  /* OPENING_PATCHES_HELPER_END */"""

OPENING_PATCHES_CSS = """        /* OPENING_PATCHES_CSS_BEGIN */
        .opening-patches {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          z-index: 1;
          pointer-events: none;
        }
        .img-patch {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: contain;
          display: none;
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.6s ease;
        }
        .img-overlay, .lamp-glow {
          z-index: 2;
        }
        /* OPENING_PATCHES_CSS_END */"""

OPENING_PATCHES_HTML = """                <!-- OPENING_PATCHES_HTML_BEGIN -->
                <div class="opening-patches" data-opening-patches="${f.id}">
                  ${(f.openingPatches || [])
                    .map(
                      (op) => `
                  <img class="img-patch img-patch-day" data-patch-id="${op.id}" data-patch-phase="day" data-src="${BASE_PATH}/patches/${op.day}?v=${ASSET_VERSION}" alt="" decoding="async" style="display:none;opacity:0;z-index:${op.z || 1};" draggable="false" />
                  <img class="img-patch img-patch-night" data-patch-id="${op.id}" data-patch-phase="night" data-src="${BASE_PATH}/patches/${op.night}?v=${ASSET_VERSION}" alt="" decoding="async" style="display:none;opacity:0;z-index:${op.z || 1};" draggable="false" />
                `
                    )
                    .join("")}
                </div>
                <!-- OPENING_PATCHES_HTML_END -->"""

OPENING_PATCHES_SIG_JS = """      /* OPENING_PATCHES_SIG_BEGIN */
      for (const op of (f.openingPatches || [])) {
        sigParts.push(this._isOpeningPatchActive(op) ? 1 : 0);
      }
      /* OPENING_PATCHES_SIG_END */"""

OPENING_PATCHES_UPDATE_JS = """        /* OPENING_PATCHES_UPDATE_BEGIN */
        for (const op of (f.openingPatches || [])) {
          const active = this._isOpeningPatchActive(op);
          const dayPatch = card.querySelector(`.img-patch[data-patch-id="${op.id}"][data-patch-phase="day"]`);
          const nightPatch = card.querySelector(`.img-patch[data-patch-id="${op.id}"][data-patch-phase="night"]`);
          const dayOp = active ? Math.max(0, 1 - nightBlend) : 0;
          const nightOp = active ? nightBlend : 0;
          if (dayPatch) {
            if (dayOp > 0.005) {
              if (!dayPatch.getAttribute("src")) dayPatch.src = dayPatch.dataset.src;
              dayPatch.style.display = "block";
              dayPatch.style.opacity = dayOp.toFixed(3);
            } else {
              dayPatch.style.opacity = "0";
              dayPatch.style.display = "none";
            }
          }
          if (nightPatch) {
            if (nightOp > 0.005) {
              if (!nightPatch.getAttribute("src")) nightPatch.src = nightPatch.dataset.src;
              nightPatch.style.display = "block";
              nightPatch.style.opacity = nightOp.toFixed(3);
            } else {
              nightPatch.style.opacity = "0";
              nightPatch.style.display = "none";
            }
          }
        }
        /* OPENING_PATCHES_UPDATE_END */"""


def get_remote_paths() -> dict[str, str]:
    """Resolve Host 1 WWW paths dynamically from nas_config without hardcoded volumes or IPs."""
    www_dir = posixpath.join(nas_config.HA_CONFIG_ROOT, "www", "brock-house-3d")
    cycles_dir = posixpath.join(www_dir, "cycles")
    return {
        "www_dir": www_dir,
        "card_path": posixpath.join(www_dir, "brock-house-cycles-card.js"),
        "cycles_dir": cycles_dir,
        "patches_dir": posixpath.join(cycles_dir, "patches"),
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
                print(f"[warn] Failed reading {secret_path}: {exc}", file=sys.stderr)

    if not ha_url and nas_config.HOST_1_IP and nas_config.HOST_1_IP != "127.0.0.1":
        ha_url = f"http://{nas_config.HOST_1_IP}:8123"
    if not ha_url:
        raise RuntimeError("Could not resolve HA_BASE_URL from env, /secrets/ha.json, or /secrets/env.json")
    return ha_url.rstrip("/"), ha_token


def compute_asset_version(patches_dir: Path = LOCAL_PATCHES_DIR) -> str:
    """Compute an 8-char deterministic content hash across all 18 WebP patch files."""
    h = hashlib.sha256()
    for fname in ALL_18_PATCHES:
        fpath = patches_dir / fname
        if not fpath.is_file():
            raise FileNotFoundError(f"Missing required patch asset: {fpath}")
        h.update(fname.encode("utf-8"))
        h.update(fpath.read_bytes())
    return h.hexdigest()[:8]


def validate_local_patches(patches_dir: Path = LOCAL_PATCHES_DIR) -> None:
    """Verify all 18 WebP files exist locally, are >1 KB, and start with RIFF...WEBP."""
    errors: list[str] = []
    for fname in ALL_18_PATCHES:
        fpath = patches_dir / fname
        if not fpath.is_file():
            errors.append(f"Missing patch file: {fpath}")
            continue
        raw = fpath.read_bytes()
        if len(raw) <= 1024:
            errors.append(f"Patch too small ({len(raw)} bytes): {fpath}")
            continue
        if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
            errors.append(f"Invalid WEBP header in {fpath}")
    if errors:
        raise RuntimeError("Local patch validation failed:\n  " + "\n  ".join(errors))


def _replace_or_insert_block(
    src: str,
    begin_marker: str,
    end_marker: str,
    new_block: str,
    anchor_pattern: str,
    *,
    insert_before: bool = False,
    label: str,
) -> str:
    """Idempotently replace a marked block or insert it relative to an anchor regex."""
    marked_re = re.compile(
        r"[ \t]*" + re.escape(begin_marker) + r"[\s\S]*?" + re.escape(end_marker),
    )
    if marked_re.search(src):
        return marked_re.sub(lambda _m: new_block, src, count=1)

    m = re.search(anchor_pattern, src, re.MULTILINE)
    if not m:
        raise RuntimeError(f"Could not locate anchor for '{label}'")
    if insert_before:
        return src[: m.start()] + new_block + "\n" + src[m.start() :]
    return src[: m.end()] + "\n" + new_block + src[m.end() :]


def _update_security_pin_coords(src: str) -> str:
    """Update the 4 Security pin coordinates to match real walls."""
    updated = src
    for pin_id, (new_x, new_y) in PIN_COORDINATE_UPDATES.items():
        pin_re = re.compile(
            rf'(\{{\s*id:\s*"{re.escape(pin_id)}"[^\}}]*?\bx:\s*)[-+]?\d+(?:\.\d+)?(,\s*\by:\s*)[-+]?\d+(?:\.\d+)?([^\}}]*?\}})'
        )
        updated, count = pin_re.subn(rf"\g<1>{new_x:.1f}\g<2>{new_y:.1f}\g<3>", updated, count=1)
        if count != 1:
            raise RuntimeError(f"Could not update Security pin coordinates for '{pin_id}' (matched {count})")
    return updated


def patch_card_js(source_js: str, asset_version: str) -> str:
    """Patch brock-house-cycles-card.js idempotently with .opening-patches and pin fixes."""
    out = source_js

    out, ver_count = re.subn(
        r'(const\s+ASSET_VERSION\s*=\s*")[^"]*(";)',
        rf"\g<1>{asset_version}\g<2>",
        out,
        count=1,
    )
    if ver_count != 1:
        raise RuntimeError("Could not locate ASSET_VERSION declaration in card JS")

    out = _update_security_pin_coords(out)

    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_CONFIG_BEGIN:floor3 */",
        "/* OPENING_PATCHES_CONFIG_END:floor3 */",
        FLOOR3_OPENING_PATCHES_JS,
        r'^\s*night:\s*`\$\{BASE_PATH\}/floor3_cycles_evening_base\.webp\?v=\$\{ASSET_VERSION\}`,',
        label="floor3 openingPatches",
    )
    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_CONFIG_BEGIN:floor2 */",
        "/* OPENING_PATCHES_CONFIG_END:floor2 */",
        FLOOR2_OPENING_PATCHES_JS,
        r'^\s*night:\s*`\$\{BASE_PATH\}/floor2_cycles_evening_base\.webp\?v=\$\{ASSET_VERSION\}`,',
        label="floor2 openingPatches",
    )
    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_CONFIG_BEGIN:floor1 */",
        "/* OPENING_PATCHES_CONFIG_END:floor1 */",
        FLOOR1_OPENING_PATCHES_JS,
        r'^\s*night:\s*`\$\{BASE_PATH\}/floor1_cycles_evening_base\.webp\?v=\$\{ASSET_VERSION\}`,',
        label="floor1 openingPatches",
    )

    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_HELPER_BEGIN */",
        "/* OPENING_PATCHES_HELPER_END */",
        OPENING_PATCH_HELPER_JS,
        r"^\s*_entityOnAndBrightness\(entityId\)\s*\{",
        insert_before=True,
        label="_isOpeningPatchActive helper",
    )

    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_CSS_BEGIN */",
        "/* OPENING_PATCHES_CSS_END */",
        OPENING_PATCHES_CSS,
        r"^\s*\.img-night\s*\{\s*\n\s*transition:\s*opacity\s+0\.6s\s+ease;\s*\n\s*\}",
        label="opening-patches CSS",
    )

    out = _replace_or_insert_block(
        out,
        "<!-- OPENING_PATCHES_HTML_BEGIN -->",
        "<!-- OPENING_PATCHES_HTML_END -->",
        OPENING_PATCHES_HTML,
        r'^\s*<img\s+class="img-base img-night"[^\n]*/>\s*$',
        label="opening-patches HTML",
    )

    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_SIG_BEGIN */",
        "/* OPENING_PATCHES_SIG_END */",
        OPENING_PATCHES_SIG_JS,
        r"for\s*\(const\s+eo\s+of\s+\(f\.extraOverlays\s*\|\|\s*\[\]\)\)\s*\{[\s\S]*?sigParts\.push\(info\.on\s*\?\s*info\.pct\s*:\s*-1\);\s*\n\s*\}",
        label="opening-patches signature",
    )

    out = _replace_or_insert_block(
        out,
        "/* OPENING_PATCHES_UPDATE_BEGIN */",
        "/* OPENING_PATCHES_UPDATE_END */",
        OPENING_PATCHES_UPDATE_JS,
        r"const\s+nightImg\s*=\s*card\.querySelector\(\"\.img-night\"\);[\s\S]*?nightImg\.style\.display\s*=\s*\"none\";\s*\n\s*\}\s*\n\s*\}",
        label="opening-patches update loop",
    )

    verify_patched_card_contents(out)
    return out


def verify_patched_card_contents(card_js: str) -> None:
    """Assert all required patches, entities, layers, and pin coordinates are present."""
    missing: list[str] = []
    if ".opening-patches" not in card_js or 'data-opening-patches="${f.id}"' not in card_js:
        missing.append(".opening-patches layer")
    if "_computeNightBlend()" not in card_js:
        missing.append("_computeNightBlend()")
    for fname in ALL_18_PATCHES:
        if fname not in card_js:
            missing.append(f"patch:{fname}")
    for eid in LIVE_ENTITIES:
        if eid not in card_js:
            missing.append(f"entity:{eid}")
    for pin_id, (px, py) in PIN_COORDINATE_UPDATES.items():
        pat = rf'id:\s*"{re.escape(pin_id)}"[^\}}]*?\bx:\s*{px:.1f},\s*\by:\s*{py:.1f}\b'
        if not re.search(pat, card_js):
            missing.append(f"pin:{pin_id}=({px:.1f},{py:.1f})")
    rosie_door_idx = card_js.find('id: "rosie_door_open"')
    rosie_curtains_idx = card_js.find('id: "rosie_curtains_closed"')
    if rosie_door_idx == -1 or rosie_curtains_idx == -1 or rosie_curtains_idx < rosie_door_idx:
        missing.append("rosie_curtains_above_rosie_door_ordering")
    if missing:
        raise RuntimeError("Patched card verification failed; missing: " + ", ".join(missing))


def fetch_live_card_and_backup(backup_path: Path = BACKUP_CARD_PATH) -> str:
    """Fetch live brock-house-cycles-card.js from Host 1 via SSH and back it up locally."""
    paths = get_remote_paths()
    card_path = paths["card_path"]
    rc, out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"base64 -w 0 {shlex.quote(card_path)}",
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f"Failed fetching {card_path} from Host 1 (rc={rc}): {err}")

    raw_bytes = base64.b64decode(out.strip())
    card_text = raw_bytes.decode("utf-8")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve original pre-patch backup if live card is already patched from a prior run
    already_patched = "OPENING_PATCHES_CONFIG_BEGIN" in card_text
    if not already_patched or not backup_path.is_file():
        backup_path.write_bytes(raw_bytes)
        print(f"[1/5] Backed up live card ({len(raw_bytes):,} bytes) -> {backup_path}", flush=True)
    else:
        print(
            f"[1/5] Fetched live card ({len(raw_bytes):,} bytes); preserved pristine backup at {backup_path}",
            flush=True,
        )
    return card_text


def deploy_assets_to_host1(patched_js: str, patches_dir: Path = LOCAL_PATCHES_DIR) -> None:
    """Upload updated JS card and all 18 WebP patches into Host 1 Volume 2 NVMe www dir."""
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

        for fname in ALL_18_PATCHES:
            raw = (patches_dir / fname).read_bytes()
            p_info = tarfile.TarInfo(name=f"cycles/patches/{fname}")
            p_info.size = len(raw)
            p_info.mode = 0o644
            p_info.mtime = now_ts
            tf.addfile(p_info, io.BytesIO(raw))

    b64_payload = base64.b64encode(buf.getvalue()).decode("ascii")
    remote_cmd = (
        f"mkdir -p {shlex.quote(paths['patches_dir'])} && "
        f"base64 -d | tar -xf - -C {shlex.quote(paths['www_dir'])}"
    )
    rc, _out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        remote_cmd,
        timeout=120,
        stdin_data=b64_payload,
    )
    if rc != 0:
        raise RuntimeError(f"Failed deploying assets to Host 1 (rc={rc}): {err}")
    print(
        f"[5/5] Deployed brock-house-cycles-card.js ({len(patched_js.encode('utf-8')):,} bytes) "
        f"and {len(ALL_18_PATCHES)} WebP patches to {paths['www_dir']}",
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
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(ws_url, timeout=15) as ws,
    ):
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected HA WS handshake: {hello}")
            await ws.send_json({"type": "auth", "access_token": ha_token})
            auth_resp = await ws.receive_json()
            if auth_resp.get("type") != "auth_ok":
                raise RuntimeError(f"HA WS auth failed: {auth_resp}")

            await ws.send_json({"id": 1, "type": "lovelace/resources"})
            res = await ws.receive_json()
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
                upd = await ws.receive_json()
                if not upd.get("success"):
                    raise RuntimeError(f"HA WS lovelace/resources/update failed: {upd}")
                return [upd["result"]]

            return resources


def update_lovelace_resource_cache_buster(ha_url: str, ha_token: str, cache_buster: str) -> str:
    """Update HA Lovelace resource URL for brock-house-cycles-card.js with ?v=<cache_buster>."""
    if not ha_token:
        raise RuntimeError("HA access token required to update Lovelace resource via WebSocket")
    updated = asyncio.run(_ws_lovelace_resources(ha_url, ha_token, cache_buster=cache_buster))
    new_url = updated[0]["url"]
    print(f"[5/5] Updated HA Lovelace resource via WebSocket -> {new_url}", flush=True)
    return new_url


def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 on the live JS card (including Lovelace resource URL) and all 18 patch URLs."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None))
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

    for fname in ALL_18_PATCHES:
        patch_url = f"{ha_url}/local/brock-house-3d/cycles/patches/{fname}"
        p_status, p_body = _http_get(patch_url)
        if p_status != 200:
            errors.append(f"HTTP {p_status} for patch: {patch_url}")
            continue
        if len(p_body) <= 1024 or p_body[:4] != b"RIFF" or p_body[8:12] != b"WEBP":
            errors.append(f"Invalid WebP response ({len(p_body)} bytes) for: {patch_url}")
            continue
        print(f"OK [200] {patch_url} ({len(p_body):,} bytes)", flush=True)

    return (len(errors) == 0), errors


def run_deploy(*, dry_run: bool = False) -> int:
    """Execute the full fetch -> backup -> patch -> deploy -> WS bust -> HTTP verify pipeline."""
    validate_local_patches(LOCAL_PATCHES_DIR)
    asset_version = compute_asset_version(LOCAL_PATCHES_DIR)
    ha_url, ha_token = resolve_ha_credentials()

    live_js = fetch_live_card_and_backup(BACKUP_CARD_PATH)
    patched_js = patch_card_js(live_js, asset_version=asset_version)
    print(
        f"[2-4/5] Patched card JS (asset_version={asset_version}, "
        f"{len(patched_js.encode('utf-8')):,} bytes, 18 patches, 7 entities, 4 pin fixes)",
        flush=True,
    )

    if dry_run:
        print("[dry-run] Skipping Host 1 upload, WebSocket cache-buster update, and HTTP check.", flush=True)
        return 0

    deploy_assets_to_host1(patched_js, LOCAL_PATCHES_DIR)
    cache_buster = f"{asset_version}{int(time.time()) & 0xFFFF:04x}"[:12]
    update_lovelace_resource_cache_buster(ha_url, ha_token, cache_buster=cache_buster)

    ok, errors = verify_live_http(ha_url, ha_token)
    if not ok:
        for err in errors:
            print(f"VERIFY ERROR: {err}", file=sys.stderr, flush=True)
        return 1

    print(f"SUCCESS: Deployed and verified live card + all {len(ALL_18_PATCHES)} patch URLs (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy all 18 3D floorplan opening/curtain patches to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 on the live card and all 18 patch URLs on Home Assistant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, back up, and patch card JS in memory without modifying Host 1 or HA.",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        ha_url, ha_token = resolve_ha_credentials()
        ok, errors = verify_live_http(ha_url, ha_token)
        if not ok:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr, flush=True)
            return 1
        print(f"VERIFIED: Live card and all {len(ALL_18_PATCHES)} patch URLs returned HTTP 200.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
