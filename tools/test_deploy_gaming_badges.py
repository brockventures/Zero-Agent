#!/usr/bin/env python3
"""Unit tests for tools/deploy_gaming_badges.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import deploy_gaming_badges as mod

REALISTIC_CARD_FIXTURE = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const ASSET_VERSION = "4a92eae1";
const BASE_PATH = "/local/brock-house-3d/cycles";

const BERMUDA_CONFIG = {
  areaCoords: {
    kitchen: { floorId: "floor3", x: 39.0, y: 47.0, label: "Kitchen" },
    living_room: { floorId: "floor2", x: 32.0, y: 46.0, label: "Living Room" },
    living: { floorId: "floor2", x: 32.0, y: 46.0, label: "Living Room" },
    office: { floorId: "floor1", x: 24.0, y: 47.0, label: "Office" },
  },
};

const LENSES = [
  { id: "lights", label: "Lights", icon: "ceiling" },
  { id: "climate", label: "Climate", icon: "thermo" },
];

const FLOORS = [
  {
    id: "floor3",
    runningItems: [
      { id: "apolosign_music", kind: "media_sensor", entity: "sensor.apolosign_media", label: "Music", icon: "music", accent: "green", x: 29.5, y: 43.0 },
    ],
    securityItems: [],
    lights: [],
  },
  {
    id: "floor2",
    runningItems: [
      { id: "living_tv", kind: "tv", entity: "media_player.plex_plex_for_android_tv_google_tv_streamer", label: "TV", icon: "tv", accent: "blue", x: 26.5, y: 29.5 },
    ],
    securityItems: [],
    lights: [],
  },
  {
    id: "floor1",
    runningItems: [
      { id: "office_tv", kind: "tv", entity: "media_player.office_chromecast", label: "Office TV", icon: "tv", accent: "blue", x: 42.0, y: 44.5 },
    ],
    securityItems: [],
    lights: [],
  },
];

const SVG_ICONS = {
  music: `<svg viewBox="0 0 24 24"></svg>`,
  tv: `<svg viewBox="0 0 24 24"></svg>`,
};

class BrockHouseCyclesCard {
  constructor() {
    this._hass = { states: {} };
    this._activeFloor = "all";
    this._activeLens = "lights";
    this._lastStateSig = "";
  }

  _openMoreInfo(entityId) {
    this.lastMoreInfo = entityId;
    if (typeof this.dispatchEvent === "function") {
      this.dispatchEvent({ type: "hass-more-info", detail: { entityId } });
    }
  }

  _normalizeAreaKey(raw) {
    if (!raw) return "";
    return String(raw)
      .toLowerCase()
      .replace(/['’]/g, "")
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  _resolveAreaCoord(areaIdOrName) {
    const key = this._normalizeAreaKey(areaIdOrName);
    if (!key) return null;
    if (BERMUDA_CONFIG.areaCoords[key]) return BERMUDA_CONFIG.areaCoords[key];
    for (const [k, v] of Object.entries(BERMUDA_CONFIG.areaCoords)) {
      if (key.includes(k) || k.includes(key)) return v;
    }
    return null;
  }

  _evalRunningItem(item) {
    return { active: false, text: item.label, entity: item.entity };
  }

  _renderSkeleton() {
    const html = `
      <style>
        .run-badge.danger .run-dot { background: #ff5252; }
      </style>
      ${FLOORS.map(
        (f) => `
        <div class="floor-card" data-floor-card="${f.id}">
          <div class="running-layer" data-running-layer="${f.id}">
            ${(f.runningItems || [])
              .map(
                (item) => `
              <button class="run-badge ${item.accent || ""}" data-run-id="${item.id}" data-run-entity="${item.entity}" style="left:${item.x}%;top:${item.y}%;" title="${item.label}">
                <span class="run-dot"></span>
                ${SVG_ICONS[item.icon] || ""}
                <span data-run-text="${item.id}">${item.label}</span>
              </button>
            `
              )
              .join("")}
          </div>
        </div>
      `
      ).join("")}
    `;
    return html;
  }

  _updateState(force = false) {
    const isClimate = this._activeLens === "climate";
    const sigParts = [this._activeFloor, this._activeLens];
    for (const f of FLOORS) {
      for (const ri of (f.runningItems || [])) {
        const ev = this._evalRunningItem(ri);
        sigParts.push(ev.active ? ev.text : "");
      }
    }
    const sig = sigParts.join("|");
    if (!force && sig === this._lastStateSig) return;
    this._lastStateSig = sig;

    const card = null;
    for (const f of FLOORS) {
      for (const ri of (f.runningItems || [])) {
        const ev = this._evalRunningItem(ri);
        const el = card?.querySelector(`.run-badge[data-run-id="${ri.id}"]`);
        if (el) {
          el.classList.toggle("active", ev.active && !isClimate);
          el.dataset.runEntity = ev.entity;
          const txtEl = el.querySelector(`[data-run-text="${ri.id}"]`);
          if (txtEl) txtEl.textContent = ev.text;
        }
      }
    }
  }
}
"""


def test_patch_card_js_is_idempotent_and_wires_all_markers_and_entities() -> None:
    patched_once = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    for eid in mod.ALL_GAMING_ENTITIES:
        assert eid in patched_once


def test_patched_card_passes_node_check_and_evaluates_gaming_logic() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    mod.validate_js_syntax(patched)

    harness = (
        patched
        + """
const card = new BrockHouseCyclesCard();

// 1. Default inactive state
card._hass = {
  states: {
    "sensor.steam_76561197975996046": { state: "offline", attributes: { game: "Hades II" } },
    "media_player.xbox": { state: "standby", attributes: { media_title: "Halo" } },
    "input_boolean.switch_2_on": { state: "off", attributes: {} },
  },
};
const inactive = card._evalGamingBadges();

// 2. Steam active in Kitchen via primary Bermuda sensor
card._hass = {
  states: {
    "sensor.steam_76561197975996046": {
      state: "online",
      attributes: {
        game: "Baldur's Gate 3",
        game_image_header: "https://cdn.cloudflare.steamstatic.com/steam/apps/1086940/header.jpg",
      },
    },
    "sensor.pixel_11_pro_xl_area": { state: "Kitchen", attributes: { area_id: "kitchen" } },
    "media_player.xbox": {
      state: "playing",
      attributes: { media_title: "Forza Horizon 5", entity_picture: "/api/media_player_proxy/xbox" },
    },
    "input_boolean.switch_2_on": { state: "on", attributes: {} },
  },
};
const activeKitchen = card._evalGamingBadges();

// 3. Steam fallback to watch sensor -> Office; then unmapped + Samsung TV on -> Living Room; then TV off -> Office
card._hass = {
  states: {
    "sensor.steam_76561197975996046": {
      state: "online",
      attributes: { game: "Elden Ring", game_icon: "https://example.com/icon.jpg" },
    },
    "sensor.pixel_11_pro_xl_area": { state: "unknown", attributes: {} },
    "sensor.pixel_watch_4_area": { state: " away ", attributes: {} },
    "media_player.samsung_s95ba_65_tv_qn65s95bafxza": { state: "on", attributes: {} },
  },
};
const tvFallback = card._evalGamingBadges();

card._hass.states["media_player.samsung_s95ba_65_tv_qn65s95bafxza"] = { state: "off", attributes: {} };
const officeFallback = card._evalGamingBadges();

console.log(JSON.stringify({ inactive, activeKitchen, tvFallback, officeFallback }));
"""
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8", delete=False) as tmp:
        tmp.write(harness)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["node", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        result = json.loads(proc.stdout.strip())
    finally:
        tmp_path.unlink(missing_ok=True)

    assert all(not b["active"] for b in result["inactive"])

    ak = {b["id"]: b for b in result["activeKitchen"]}
    assert ak["steam"]["active"] is True
    assert ak["steam"]["floorId"] == "floor3"
    assert ak["steam"]["x"] == 39.0
    assert ak["steam"]["y"] == 47.0
    assert ak["steam"]["text"] == "Baldur's Gate 3"
    assert "1086940/header.jpg" in ak["steam"]["thumb"]

    assert ak["xbox"]["active"] is True
    assert ak["xbox"]["floorId"] == "floor2"
    assert ak["xbox"]["text"] == "Forza Horizon 5"
    assert ak["xbox"]["thumb"] == "/api/media_player_proxy/xbox"

    assert ak["switch_2"]["active"] is True
    assert ak["switch_2"]["floorId"] == "floor2"
    assert ak["switch_2"]["text"] == "Switch 2"

    # Non-colliding coordinates in Living Room (living_tv is at 26.5, 29.5)
    assert (ak["xbox"]["x"], ak["xbox"]["y"]) != (26.5, 29.5)
    assert (ak["switch_2"]["x"], ak["switch_2"]["y"]) != (26.5, 29.5)
    assert (ak["xbox"]["x"], ak["xbox"]["y"]) != (ak["switch_2"]["x"], ak["switch_2"]["y"])

    tv_steam = next(b for b in result["tvFallback"] if b["id"] == "steam")
    assert tv_steam["active"] is True
    assert tv_steam["floorId"] == "floor2"
    assert tv_steam["thumb"] == "https://example.com/icon.jpg"

    off_steam = next(b for b in result["officeFallback"] if b["id"] == "steam")
    assert off_steam["active"] is True
    assert off_steam["floorId"] == "floor1"
    assert (off_steam["x"], off_steam["y"]) != (42.0, 44.5)


def test_with_cache_buster() -> None:
    assert mod._with_cache_buster("/local/brock-house-3d/brock-house-cycles-card.js?v=old", "new123") == (
        "/local/brock-house-3d/brock-house-cycles-card.js?v=new123"
    )
    assert mod._with_cache_buster("/local/brock-house-3d/brock-house-cycles-card.js", "new123") == (
        "/local/brock-house-3d/brock-house-cycles-card.js?v=new123"
    )


def test_verify_live_http_checks_card_and_contents() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)

    def fake_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
        if "brock-house-cycles-card.js" in url:
            return 200, patched.encode("utf-8")
        return 404, b""

    async def fake_ws(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=abcd1234"}]

    with (
        mock.patch.object(mod, "_http_get", side_effect=fake_get),
        mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws),
    ):
        ok, errors = mod.verify_live_http("http://198.51.100.10:8123", "tok")
    assert ok is True
    assert errors == []


def test_verify_live_http_fails_on_non_200() -> None:
    with mock.patch.object(mod, "_http_get", return_value=(404, b"")):
        ok, errors = mod.verify_live_http("http://198.51.100.10:8123", "")
    assert ok is False
    assert len(errors) == 1
    assert "404" in errors[0]


def test_resolve_ha_credentials_uses_env_without_hardcoded_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_BASE_URL", "http://198.51.100.42:8123/")
    monkeypatch.setenv("HA_ACCESS_TOKEN", "test-token")
    url, tok = mod.resolve_ha_credentials()
    assert url == "http://198.51.100.42:8123"
    assert tok == "test-token"
