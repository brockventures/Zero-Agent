#!/usr/bin/env python3
"""Deploy 3D floorplan gaming now-playing badges (Steam, Xbox, Switch 2) to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up to
   /workspace/scratch/brock-house-cycles-card.backup.js.
2. Idempotently patch brock-house-cycles-card.js with marker-delimited gaming
   badge config, SVG gamepad icon, dynamic Bermuda/TV/Office placement helpers,
   platform-accent CSS, HTML badges in .running-layer, change-detection
   signature (sigParts), and live DOM update loop.
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

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.backup.js")

STEAM_ENTITY = "sensor.steam_76561197975996046"
XBOX_ENTITY = "media_player.xbox"
SWITCH_2_ENTITY = "input_boolean.switch_2_on"
BERMUDA_PRIMARY_ENTITY = "sensor.pixel_11_pro_xl_area"
BERMUDA_FALLBACK_ENTITY = "sensor.pixel_watch_4_area"
LIVING_ROOM_TV_FALLBACK_ENTITY = "media_player.samsung_s95ba_65_tv_qn65s95bafxza"

GAMING_SIGNATURE_ENTITIES: tuple[str, ...] = (
    STEAM_ENTITY,
    XBOX_ENTITY,
    SWITCH_2_ENTITY,
    BERMUDA_PRIMARY_ENTITY,
    BERMUDA_FALLBACK_ENTITY,
)

ALL_GAMING_ENTITIES: tuple[str, ...] = (
    *GAMING_SIGNATURE_ENTITIES,
    LIVING_ROOM_TV_FALLBACK_ENTITY,
)

REQUIRED_MARKERS: tuple[str, ...] = (
    "/* GAMING_BADGES_CONFIG_BEGIN */",
    "/* GAMING_BADGES_CONFIG_END */",
    "/* GAMING_BADGES_ICONS_BEGIN */",
    "/* GAMING_BADGES_ICONS_END */",
    "/* GAMING_BADGES_HELPER_BEGIN */",
    "/* GAMING_BADGES_HELPER_END */",
    "/* GAMING_BADGES_CSS_BEGIN */",
    "/* GAMING_BADGES_CSS_END */",
    "<!-- GAMING_BADGES_HTML_BEGIN -->",
    "<!-- GAMING_BADGES_HTML_END -->",
    "/* GAMING_BADGES_SIG_BEGIN */",
    "/* GAMING_BADGES_SIG_END */",
    "/* GAMING_BADGES_UPDATE_BEGIN */",
    "/* GAMING_BADGES_UPDATE_END */",
)

GAMING_BADGES_CONFIG_JS = """/* GAMING_BADGES_CONFIG_BEGIN */
const GAMING_BADGES_CONFIG = {
  steamEntity: "sensor.steam_76561197975996046",
  xboxEntity: "media_player.xbox",
  switch2Entity: "input_boolean.switch_2_on",
  bermudaPrimaryEntity: "sensor.pixel_11_pro_xl_area",
  bermudaFallbackEntity: "sensor.pixel_watch_4_area",
  livingRoomTvEntity: "media_player.samsung_s95ba_65_tv_qn65s95bafxza",
  livingRoomPositions: {
    xbox: { x: 27.5, y: 35.5 },
    switch_2: { x: 28.5, y: 41.5 },
    steamFallback: { x: 32.0, y: 46.0 },
  },
  officeFallbackPosition: { x: 24.0, y: 47.0 },
  badges: [
    {
      id: "steam",
      entity: "sensor.steam_76561197975996046",
      label: "Steam",
      icon: "gamepad",
      accent: "steam",
      x: 24.0,
      y: 47.0,
    },
    {
      id: "xbox",
      entity: "media_player.xbox",
      label: "Xbox",
      icon: "gamepad",
      accent: "xbox",
      x: 27.5,
      y: 35.5,
    },
    {
      id: "switch_2",
      entity: "input_boolean.switch_2_on",
      label: "Switch 2",
      icon: "gamepad",
      accent: "switch2",
      x: 28.5,
      y: 41.5,
    },
  ],
};
/* GAMING_BADGES_CONFIG_END */"""

GAMING_BADGES_ICONS_JS = """  /* GAMING_BADGES_ICONS_BEGIN */
  gamepad: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M7.97,16L5,19C4.67,19.3 4.23,19.5 3.75,19.5A1.75,1.75 0 0,1 2,17.75V17.5L3,10.12C3.21,7.81 5.14,6 7.5,6H16.5C18.86,6 20.79,7.81 21,10.12L22,17.5V17.75A1.75,1.75 0 0,1 20.25,19.5C19.77,19.5 19.33,19.3 19,19L16.03,16H7.97M7,8V10H5V11H7V13H8V11H10V10H8V8H7M16.5,8A0.75,0.75 0 0,0 15.75,8.75A0.75,0.75 0 0,0 16.5,9.5A0.75,0.75 0 0,0 17.25,8.75A0.75,0.75 0 0,0 16.5,8M14.75,9.75A0.75,0.75 0 0,0 14,10.5A0.75,0.75 0 0,0 14.75,11.25A0.75,0.75 0 0,0 15.5,10.5A0.75,0.75 0 0,0 14.75,9.75M18.25,9.75A0.75,0.75 0 0,0 17.5,10.5A0.75,0.75 0 0,0 18.25,11.25A0.75,0.75 0 0,0 19,10.5A0.75,0.75 0 0,0 18.25,9.75M16.5,11.5A0.75,0.75 0 0,0 15.75,12.25A0.75,0.75 0 0,0 16.5,13A0.75,0.75 0 0,0 17.25,12.25A0.75,0.75 0 0,0 16.5,11.5Z"/></svg>`,
  /* GAMING_BADGES_ICONS_END */"""

GAMING_BADGES_HELPER_JS = """  /* GAMING_BADGES_HELPER_BEGIN */
  _getLivingRoomFloorId() {
    const tvFloor = FLOORS.find((fl) => (fl.runningItems || []).some((ri) => ri.id === "living_tv"));
    if (tvFloor) return tvFloor.id;
    return BERMUDA_CONFIG?.areaCoords?.living_room?.floorId || BERMUDA_CONFIG?.areaCoords?.living?.floorId || "floor2";
  }

  _getOfficeFloorId() {
    const tvFloor = FLOORS.find((fl) => (fl.runningItems || []).some((ri) => ri.id === "office_tv"));
    if (tvFloor) return tvFloor.id;
    return BERMUDA_CONFIG?.areaCoords?.office?.floorId || "floor1";
  }

  _resolveSteamPlacement() {
    const states = this._hass?.states || {};
    const areaEntities = [
      GAMING_BADGES_CONFIG.bermudaPrimaryEntity,
      GAMING_BADGES_CONFIG.bermudaFallbackEntity,
    ];
    for (const eid of areaEntities) {
      const st = eid ? states[eid] : null;
      if (!st) continue;
      const raw = String(st.state || "").trim();
      if (!raw || ["unknown", "unavailable", "none", "away", "not_home"].includes(raw.toLowerCase())) {
        continue;
      }
      const areaId = st.attributes?.area_id || "";
      const coord =
        (typeof this._resolveAreaCoord === "function" &&
          (this._resolveAreaCoord(areaId) || this._resolveAreaCoord(raw))) ||
        BERMUDA_CONFIG?.areaCoords?.[String(areaId || raw).toLowerCase()] ||
        null;
      if (coord && coord.floorId) {
        return {
          floorId: coord.floorId,
          x: coord.x,
          y: coord.y,
          roomLabel: coord.label || raw,
        };
      }
    }

    const samsungTv = states[GAMING_BADGES_CONFIG.livingRoomTvEntity];
    const tvState = String(samsungTv?.state || "").trim().toLowerCase();
    if (tvState === "on" || tvState === "playing" || tvState === "paused" || tvState === "buffering") {
      const livingCoord = BERMUDA_CONFIG?.areaCoords?.living_room || BERMUDA_CONFIG?.areaCoords?.living;
      const livingFloorId = this._getLivingRoomFloorId();
      return {
        floorId: livingCoord?.floorId || livingFloorId,
        x: livingCoord?.x ?? GAMING_BADGES_CONFIG.livingRoomPositions.steamFallback.x,
        y: livingCoord?.y ?? GAMING_BADGES_CONFIG.livingRoomPositions.steamFallback.y,
        roomLabel: "Living Room",
      };
    }

    const officeCoord = BERMUDA_CONFIG?.areaCoords?.office;
    const officeFloorId = this._getOfficeFloorId();
    return {
      floorId: officeCoord?.floorId || officeFloorId,
      x: officeCoord?.x ?? GAMING_BADGES_CONFIG.officeFallbackPosition.x,
      y: officeCoord?.y ?? GAMING_BADGES_CONFIG.officeFallbackPosition.y,
      roomLabel: "Office",
    };
  }

  _evalGamingBadges() {
    const states = this._hass?.states || {};
    const livingFloorId = this._getLivingRoomFloorId();

    // 1. Steam (sensor.steam_76561197975996046)
    const steamSt = states[GAMING_BADGES_CONFIG.steamEntity];
    const steamRaw = String(steamSt?.state || "").trim().toLowerCase();
    const steamGame = String(steamSt?.attributes?.game || "").trim();
    const steamOnline =
      Boolean(steamSt) &&
      steamRaw !== "" &&
      !["offline", "unavailable", "unknown"].includes(steamRaw);
    const steamActive = steamOnline && steamGame.length > 0;
    const steamThumb = String(
      steamSt?.attributes?.game_image_header ||
        steamSt?.attributes?.game_image_main ||
        steamSt?.attributes?.game_icon ||
        ""
    ).trim();
    const steamText = steamGame.length > 26 ? `${steamGame.slice(0, 25)}…` : steamGame || "Steam";
    const steamLoc = this._resolveSteamPlacement();

    // 2. Xbox (media_player.xbox)
    const xboxSt = states[GAMING_BADGES_CONFIG.xboxEntity];
    const xboxRaw = String(xboxSt?.state || "").trim().toLowerCase();
    const xboxActive = xboxRaw === "playing" || xboxRaw === "on";
    const xboxTitleRaw =
      String(xboxSt?.attributes?.media_title || xboxSt?.attributes?.app_name || "Xbox").trim() || "Xbox";
    const xboxText = xboxTitleRaw.length > 26 ? `${xboxTitleRaw.slice(0, 25)}…` : xboxTitleRaw;
    const xboxThumb = String(xboxSt?.attributes?.entity_picture || "").trim();

    // 3. Nintendo Switch 2 (input_boolean.switch_2_on)
    const swSt = states[GAMING_BADGES_CONFIG.switch2Entity];
    const swActive = swSt?.state === "on";

    return [
      {
        id: "steam",
        entity: GAMING_BADGES_CONFIG.steamEntity,
        active: steamActive,
        text: steamText,
        title: steamGame ? `Steam · ${steamGame} (${steamLoc.roomLabel})` : "Steam",
        thumb: steamThumb,
        floorId: steamLoc.floorId,
        x: steamLoc.x,
        y: steamLoc.y,
      },
      {
        id: "xbox",
        entity: GAMING_BADGES_CONFIG.xboxEntity,
        active: xboxActive,
        text: xboxText,
        title: `Xbox · ${xboxTitleRaw}`,
        thumb: xboxThumb,
        floorId: livingFloorId,
        x: GAMING_BADGES_CONFIG.livingRoomPositions.xbox.x,
        y: GAMING_BADGES_CONFIG.livingRoomPositions.xbox.y,
      },
      {
        id: "switch_2",
        entity: GAMING_BADGES_CONFIG.switch2Entity,
        active: swActive,
        text: "Switch 2",
        title: "Nintendo Switch 2",
        thumb: "",
        floorId: livingFloorId,
        x: GAMING_BADGES_CONFIG.livingRoomPositions.switch_2.x,
        y: GAMING_BADGES_CONFIG.livingRoomPositions.switch_2.y,
      },
    ];
  }
  /* GAMING_BADGES_HELPER_END */"""

GAMING_BADGES_CSS = """        /* GAMING_BADGES_CSS_BEGIN */
        .run-badge.steam {
          border-color: rgba(102, 192, 244, 0.85);
          background: rgba(23, 38, 56, 0.95);
          color: #d8f0ff;
          box-shadow: 0 0 14px rgba(102, 192, 244, 0.28), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.steam .run-dot { background: #66c0f4; }
        .run-badge.xbox {
          border-color: rgba(82, 176, 67, 0.85);
          background: rgba(14, 38, 20, 0.95);
          color: #c8f7c5;
          box-shadow: 0 0 14px rgba(82, 176, 67, 0.28), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.xbox .run-dot { background: #52b043; }
        .run-badge.switch2 {
          border-color: rgba(255, 77, 88, 0.88);
          background: rgba(56, 14, 20, 0.95);
          color: #ffd6d9;
          box-shadow: 0 0 14px rgba(230, 0, 18, 0.30), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.switch2 .run-dot { background: #ff4d58; }
        .run-badge .run-thumb {
          width: 22px;
          height: 14px;
          border-radius: 4px;
          object-fit: cover;
          flex-shrink: 0;
          border: 1px solid rgba(255, 255, 255, 0.24);
          background: rgba(0, 0, 0, 0.35);
        }
        .run-badge .run-icon {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .stage.exploded .run-badge .run-thumb {
          width: 18px;
          height: 12px;
          border-radius: 3px;
        }
        /* GAMING_BADGES_CSS_END */"""

GAMING_BADGES_HTML = """                <!-- GAMING_BADGES_HTML_BEGIN -->
                ${(GAMING_BADGES_CONFIG.badges || [])
                  .map(
                    (gb) => `
                  <button class="run-badge gaming-badge ${gb.accent || ""}" data-gaming-id="${gb.id}" data-run-id="gaming_${gb.id}" data-run-entity="${gb.entity}" style="left:${gb.x}%;top:${gb.y}%;" title="${gb.label}">
                    <span class="run-dot"></span>
                    <img class="run-thumb" data-gaming-thumb="${gb.id}" alt="" decoding="async" draggable="false" style="display:none;" onerror="this.style.display='none'" />
                    <span class="run-icon" data-gaming-icon="${gb.id}">${SVG_ICONS[gb.icon] || ""}</span>
                    <span data-gaming-text="${gb.id}">${gb.label}</span>
                  </button>
                `
                  )
                  .join("")}
                <!-- GAMING_BADGES_HTML_END -->"""

GAMING_BADGES_SIG_JS = """    /* GAMING_BADGES_SIG_BEGIN */
    {
      const stSteam = this._hass?.states?.["sensor.steam_76561197975996046"];
      const stXbox = this._hass?.states?.["media_player.xbox"];
      const stSwitch2 = this._hass?.states?.["input_boolean.switch_2_on"];
      const stAreaPhone = this._hass?.states?.["sensor.pixel_11_pro_xl_area"];
      const stAreaWatch = this._hass?.states?.["sensor.pixel_watch_4_area"];
      const stLivingTv = this._hass?.states?.["media_player.samsung_s95ba_65_tv_qn65s95bafxza"];
      sigParts.push(
        stSteam?.state || "",
        stSteam?.attributes?.game || "",
        stSteam?.attributes?.game_id || "",
        stSteam?.attributes?.game_image_header || stSteam?.attributes?.game_image_main || stSteam?.attributes?.game_icon || "",
        stXbox?.state || "",
        stXbox?.attributes?.media_title || "",
        stXbox?.attributes?.app_name || "",
        stXbox?.attributes?.entity_picture || "",
        stSwitch2?.state || "",
        stAreaPhone?.state || "",
        stAreaPhone?.attributes?.area_id || "",
        stAreaWatch?.state || "",
        stAreaWatch?.attributes?.area_id || "",
        stLivingTv?.state || ""
      );
    }
    /* GAMING_BADGES_SIG_END */"""

GAMING_BADGES_UPDATE_JS = """      /* GAMING_BADGES_UPDATE_BEGIN */
      for (const gb of this._evalGamingBadges()) {
        const el = card?.querySelector(`.gaming-badge[data-gaming-id="${gb.id}"]`);
        if (!el) continue;
        const showOnFloor = gb.active && gb.floorId === f.id && !isClimate;
        el.classList.toggle("active", showOnFloor);
        el.dataset.runEntity = gb.entity;
        if (showOnFloor) {
          el.style.left = `${gb.x}%`;
          el.style.top = `${gb.y}%`;
          el.title = gb.title || gb.text;
          const txtEl = el.querySelector(`[data-gaming-text="${gb.id}"]`);
          if (txtEl) txtEl.textContent = gb.text;
          const thumbEl = el.querySelector(`[data-gaming-thumb="${gb.id}"]`);
          if (thumbEl) {
            if (gb.thumb) {
              if (thumbEl.getAttribute("src") !== gb.thumb) {
                thumbEl.src = gb.thumb;
              }
              thumbEl.style.display = "block";
              el.classList.add("has-thumb");
            } else {
              thumbEl.removeAttribute("src");
              thumbEl.style.display = "none";
              el.classList.remove("has-thumb");
            }
          }
        }
      }
      /* GAMING_BADGES_UPDATE_END */"""


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
                print(f"[warn] Failed reading {secret_path}: {exc}", file=sys.stderr)

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
    """Assert all gaming badge markers, entities, cover art attributes, and handlers are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")
    for eid in ALL_GAMING_ENTITIES:
        if eid not in card_js:
            missing.append(f"entity:{eid}")
    for token in (
        "game_image_header",
        "game_image_main",
        "game_icon",
        "entity_picture",
        "media_title",
        "app_name",
        "Switch 2",
        "_resolveSteamPlacement",
        "_evalGamingBadges",
        "hass-more-info",
        ".run-badge.steam",
        ".run-badge.xbox",
        ".run-badge.switch2",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")
    if missing:
        raise RuntimeError("Patched gaming badges verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with active-only gaming badges."""
    out = source_js

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_CONFIG_BEGIN */",
        "/* GAMING_BADGES_CONFIG_END */",
        GAMING_BADGES_CONFIG_JS,
        r"^\s*const\s+(?:LENSES|FLOORS)\s*=\s*\[",
        insert_before=True,
        label="GAMING_BADGES_CONFIG",
    )

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_ICONS_BEGIN */",
        "/* GAMING_BADGES_ICONS_END */",
        GAMING_BADGES_ICONS_JS,
        r"const\s+SVG_ICONS\s*=\s*\{",
        insert_before=False,
        label="GAMING_BADGES_ICONS",
    )

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_HELPER_BEGIN */",
        "/* GAMING_BADGES_HELPER_END */",
        GAMING_BADGES_HELPER_JS,
        r"^\s*_evalRunningItem\(item\)\s*\{",
        insert_before=True,
        label="GAMING_BADGES_HELPER",
    )

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_CSS_BEGIN */",
        "/* GAMING_BADGES_CSS_END */",
        GAMING_BADGES_CSS,
        r"^\s*\.run-badge\.danger\s+\.run-dot\s*\{\s*background:\s*#ff5252;\s*\}",
        insert_before=False,
        label="GAMING_BADGES_CSS",
    )

    out = _replace_or_insert_block(
        out,
        "<!-- GAMING_BADGES_HTML_BEGIN -->",
        "<!-- GAMING_BADGES_HTML_END -->",
        GAMING_BADGES_HTML,
        r'<div\s+class="running-layer"\s+data-running-layer="\$\{f\.id\}">[\s\S]*?\.join\(""\)\}',
        insert_before=False,
        label="GAMING_BADGES_HTML",
    )

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_SIG_BEGIN */",
        "/* GAMING_BADGES_SIG_END */",
        GAMING_BADGES_SIG_JS,
        r'^\s*const\s+sig\s*=\s*sigParts\.join\("\|"\);',
        insert_before=True,
        label="GAMING_BADGES_SIG",
    )

    out = _replace_or_insert_block(
        out,
        "/* GAMING_BADGES_UPDATE_BEGIN */",
        "/* GAMING_BADGES_UPDATE_END */",
        GAMING_BADGES_UPDATE_JS,
        r"for\s*\(const\s+ri\s+of\s+\(f\.runningItems\s*\|\|\s*\[\]\)\)\s*\{[\s\S]*?if\s*\(txtEl\)\s*txtEl\.textContent\s*=\s*ev\.text;\s*\n\s*\}\s*\n\s*\}",
        insert_before=False,
        label="GAMING_BADGES_UPDATE",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


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
    already_patched = "GAMING_BADGES_CONFIG_BEGIN" in card_text
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
        timeout=60,
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
    """Verify HTTP 200 and gaming badge contents on the live JS card on Home Assistant."""
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

    return (len(errors) == 0), errors


def run_deploy(*, dry_run: bool = False) -> int:
    """Execute the full fetch -> backup -> patch -> syntax check -> deploy -> WS bust -> HTTP verify pipeline."""
    ha_url, ha_token = resolve_ha_credentials()

    live_js = fetch_live_card_and_backup(BACKUP_CARD_PATH)
    patched_js = patch_card_js(live_js)
    js_hash = hashlib.sha256(patched_js.encode("utf-8")).hexdigest()[:8]
    print(
        f"[2/4] Patched & syntax-checked card JS via node --check "
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, 3 gaming badges, 6 entities)",
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

    print("SUCCESS: Deployed and verified 3D floorplan gaming badges on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy 3D floorplan gaming now-playing badges (Steam, Xbox, Switch 2) to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and gaming badge contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with all gaming badges wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
