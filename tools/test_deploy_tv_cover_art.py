#!/usr/bin/env python3
"""Unit tests for tools/deploy_tv_cover_art.py."""

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

from tools import deploy_tv_cover_art as mod

REALISTIC_CARD_FIXTURE = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const ASSET_VERSION = "4a92eae1";

/* GAMING_BADGES_CONFIG_BEGIN */
const GAMING_BADGES_CONFIG = {
  badges: [
    { id: "steam", entity: "sensor.steam_76561197975996046", label: "Steam", icon: "gamepad", accent: "steam", x: 24.0, y: 47.0 },
  ],
};
/* GAMING_BADGES_CONFIG_END */

const BERMUDA_CONFIG = {
  areaCoords: {
    kitchen: { floorId: "floor3", x: 39.0, y: 47.0, label: "Kitchen" },
    living_room: { floorId: "floor2", x: 32.0, y: 46.0, label: "Living Room" },
    office: { floorId: "floor1", x: 24.0, y: 47.0, label: "Office" },
  },
};

const FLOORS = [
  {
    id: "floor3",
    /* OPENING_PATCHES_CONFIG_BEGIN:floor3 */
    openingPatches: [],
    /* OPENING_PATCHES_CONFIG_END:floor3 */
    runningItems: [
      { id: "coffee", kind: "switch", entity: "switch.coffee_machine", label: "Coffee On", icon: "coffee", accent: "amber", x: 46.5, y: 38.0 },
      { id: "apolosign_music", kind: "media_sensor", entity: "sensor.apolosign_media", label: "Music", icon: "music", accent: "green", x: 29.5, y: 43.0 },
    ],
    securityItems: [],
    lights: [],
  },
  {
    id: "floor2",
    runningItems: [
      {
        id: "living_tv",
        kind: "tv",
        entity: "media_player.plex_plex_for_android_tv_google_tv_streamer",
        fallbackEntities: [
          "media_player.living_room_tv_2",
          "media_player.living_room_tv",
          "media_player.living_room_living_room_tv",
          "switch.living_room_tv",
        ],
        label: "TV",
        icon: "tv",
        accent: "blue",
        x: 26.5,
        y: 29.5,
      },
    ],
    securityItems: [],
    lights: [],
  },
  {
    id: "floor1",
    runningItems: [
      {
        id: "office_tv",
        kind: "tv",
        entity: "media_player.office_chromecast",
        fallbackEntities: ["media_player.office_chromecast_2"],
        label: "Office TV",
        icon: "tv",
        accent: "blue",
        x: 42.0,
        y: 44.5,
      },
    ],
    securityItems: [],
    lights: [],
  },
];

const SVG_ICONS = {
  coffee: `<svg viewBox="0 0 24 24"></svg>`,
  music: `<svg viewBox="0 0 24 24"></svg>`,
  tv: `<svg viewBox="0 0 24 24"></svg>`,
  gamepad: `<svg viewBox="0 0 24 24"></svg>`,
};

class BrockHouseCyclesCard {
  constructor() {
    this._hass = { states: {} };
    this._activeFloor = "all";
    this._activeLens = "lights";
    this._lastStateSig = "";
    this._mockCards = {};
  }

  /* GAMING_BADGES_HELPER_BEGIN */
  _evalGamingBadges() {
    return [];
  }
  /* GAMING_BADGES_HELPER_END */

  _evalRunningItem(item) {
    const states = this._hass?.states || {};
    if (item.kind === "media_sensor") {
      const st = states[item.entity];
      if (st && st.state === "playing") {
        const title = String(st.attributes?.title || "").trim();
        const artist = String(st.attributes?.artist || "").trim();
        let label = "Music Playing";
        if (title && artist) label = `${title} · ${artist}`;
        else if (title) label = title;
        if (label.length > 28) label = label.slice(0, 27) + "…";
        return { active: true, text: label, entity: item.entity };
      }
      return { active: false, text: item.label, entity: item.entity };
    }
    if (item.kind === "switch") {
      const st = states[item.entity];
      if (st && st.state === "on") return { active: true, text: item.label, entity: item.entity };
      return { active: false, text: item.label, entity: item.entity };
    }
    if (item.kind === "tv") {
      const candidates = [item.entity, ...(item.fallbackEntities || [])];
      for (const cid of candidates) {
        const st = states[cid];
        if (!st) continue;
        if (cid.startsWith("switch.") && st.state === "on") {
          return { active: true, text: `${item.label} On`, entity: cid };
        }
        if (st.state === "playing" || st.state === "paused" || st.state === "buffering") {
          const title = st.attributes?.media_title || st.attributes?.app_name;
          const shortTitle = title ? String(title).slice(0, 14) : st.state === "paused" ? "Paused" : "Playing";
          return { active: true, text: `${item.label} · ${shortTitle}`, entity: cid };
        }
      }
      return { active: false, text: item.label, entity: item.entity };
    }
    return { active: false, text: item.label, entity: item.entity };
  }

  _renderSkeleton() {
    const html = `
      <style>
        .run-badge.danger .run-dot { background: #ff5252; }
        /* GAMING_BADGES_CSS_BEGIN */
        .run-badge .run-thumb {
          width: 22px;
          height: 14px;
          border-radius: 4px;
          object-fit: cover;
          flex-shrink: 0;
          border: 1px solid rgba(255, 255, 255, 0.24);
          background: rgba(0, 0, 0, 0.35);
        }
        /* GAMING_BADGES_CSS_END */
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
            <!-- GAMING_BADGES_HTML_BEGIN -->
            <button class="run-badge gaming-badge steam" data-gaming-id="steam"></button>
            <!-- GAMING_BADGES_HTML_END -->
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
    /* GAMING_BADGES_SIG_BEGIN */
    sigParts.push("gaming-sig");
    /* GAMING_BADGES_SIG_END */
    const sig = sigParts.join("|");
    if (!force && sig === this._lastStateSig) return false;
    this._lastStateSig = sig;

    for (const f of FLOORS) {
      const card = this._mockCards[f.id] || null;
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
      /* GAMING_BADGES_UPDATE_BEGIN */
      for (const _gb of this._evalGamingBadges()) {
        // existing gaming badge update preserved
      }
      /* GAMING_BADGES_UPDATE_END */
    }
    return true;
  }
}
"""


def test_patch_card_js_is_idempotent_and_preserves_existing_features() -> None:
    patched_once = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    # Verify existing markers and features remain completely intact
    for preserved in (
        "/* GAMING_BADGES_CONFIG_BEGIN */",
        "/* GAMING_BADGES_CSS_BEGIN */",
        "<!-- GAMING_BADGES_HTML_BEGIN -->",
        "/* GAMING_BADGES_SIG_BEGIN */",
        "/* GAMING_BADGES_UPDATE_BEGIN */",
        "/* OPENING_PATCHES_CONFIG_BEGIN:floor3 */",
        "BERMUDA_CONFIG",
    ):
        assert preserved in patched_once


def test_patched_card_evaluates_artwork_fallbacks_onerror_and_sig_updates() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    mod.validate_js_syntax(patched)

    harness = (
        patched
        + """
function makeMockEl() {
  const classes = new Set();
  const attrs = {};
  const children = {};
  return {
    style: { display: "none" },
    dataset: {},
    textContent: "",
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, force) => (force ? classes.add(c) : classes.delete(c)),
    },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute: (k) => { delete attrs[k]; },
    _children: children,
    querySelector: (sel) => children[sel] || null,
  };
}

function makeFloorMock(ids) {
  const badges = {};
  for (const id of ids) {
    const badge = makeMockEl();
    const thumb = makeMockEl();
    const txt = makeMockEl();
    badge._children[`[data-run-thumb="${id}"]`] = thumb;
    badge._children[`[data-run-text="${id}"]`] = txt;
    badges[`.run-badge[data-run-id="${id}"]`] = badge;
  }
  return {
    badges,
    querySelector: (sel) => badges[sel] || null,
  };
}

const card = new BrockHouseCyclesCard();
card._mockCards = {
  floor3: makeFloorMock(["coffee", "apolosign_music"]),
  floor2: makeFloorMock(["living_tv"]),
  floor1: makeFloorMock(["office_tv"]),
};

const skeletonHtml = card._renderSkeleton();

// Scenario 1: Primary Plex entity playing + Office TV fallback entity + Apolosign music artwork_url
card._hass = {
  states: {
    "media_player.plex_plex_for_android_tv_google_tv_streamer": {
      state: "playing",
      attributes: {
        media_title: "Dune: Part Two",
        entity_picture: "/api/media_player_proxy/plex_streamer?token=abc&cache=1",
      },
    },
    "media_player.office_chromecast": { state: "off", attributes: {} },
    "media_player.office_chromecast_2": {
      state: "playing",
      attributes: {
        media_title: "Severance",
        entity_picture_local: "/api/media_player_proxy/office_cast2?token=def",
      },
    },
    "sensor.apolosign_media": {
      state: "playing",
      attributes: {
        title: "Midnight City",
        artist: "M83",
        artwork_url: "https://images.example.com/m83.jpg",
      },
    },
  },
};
const updated1 = card._updateState();
const s1Living = card._mockCards.floor2.badges['.run-badge[data-run-id="living_tv"]'];
const s1Office = card._mockCards.floor1.badges['.run-badge[data-run-id="office_tv"]'];
const s1Music = card._mockCards.floor3.badges['.run-badge[data-run-id="apolosign_music"]'];

const snap1 = {
  updated: updated1,
  livingSrc: s1Living._children['[data-run-thumb="living_tv"]'].getAttribute("src"),
  livingHasThumb: s1Living.classList.contains("has-thumb"),
  officeSrc: s1Office._children['[data-run-thumb="office_tv"]'].getAttribute("src"),
  officeHasThumb: s1Office.classList.contains("has-thumb"),
  musicSrc: s1Music._children['[data-run-thumb="apolosign_music"]'].getAttribute("src"),
  musicHasThumb: s1Music.classList.contains("has-thumb"),
};

// Scenario 2: Only entity_picture changes (next episode with same truncated title) -> sig MUST change
card._hass.states["media_player.plex_plex_for_android_tv_google_tv_streamer"].attributes.entity_picture =
  "/api/media_player_proxy/plex_streamer?token=abc&cache=2";
const updated2 = card._updateState();
const snap2 = {
  updated: updated2,
  livingSrc: s1Living._children['[data-run-thumb="living_tv"]'].getAttribute("src"),
};

// Scenario 3: Simulate broken proxy image (onerror sets failedSrc, hides img, removes has-thumb)
// Subsequent unrelated state tick must NOT re-expose broken image; new URL DOES recover.
const livingThumbEl = s1Living._children['[data-run-thumb="living_tv"]'];
livingThumbEl.dataset.failedSrc = livingThumbEl.getAttribute("src");
livingThumbEl.style.display = "none";
s1Living.classList.remove("has-thumb");
card._hass.states["sensor.apolosign_media"].attributes.cover_url = "https://images.example.com/other.jpg";
card._updateState(true);
const snap3FailedStaysHidden = {
  display: livingThumbEl.style.display,
  hasThumb: s1Living.classList.contains("has-thumb"),
};
card._hass.states["media_player.plex_plex_for_android_tv_google_tv_streamer"].attributes.entity_picture =
  "/api/media_player_proxy/plex_streamer?token=abc&cache=3";
const updated3Recover = card._updateState();
const snap3Recovered = {
  updated: updated3Recover,
  src: livingThumbEl.getAttribute("src"),
  display: livingThumbEl.style.display,
  hasThumb: s1Living.classList.contains("has-thumb"),
};

// Scenario 4: Living TV primary unavailable -> fallback media_player.living_room_tv active;
// then fallback switch.living_room_tv only (no artwork) -> icon fallback;
// and Apolosign sentinel artwork "unknown" -> icon fallback.
card._hass = {
  states: {
    "media_player.plex_plex_for_android_tv_google_tv_streamer": { state: "unavailable", attributes: {} },
    "media_player.living_room_tv": {
      state: "paused",
      attributes: {
        media_title: "Andor",
        entity_picture: "/api/media_player_proxy/living_tv_fb?token=99",
      },
    },
    "sensor.apolosign_media": {
      state: "playing",
      attributes: { title: "Radio", artwork_url: "unknown" },
    },
  },
};
card._updateState();
const snap4Fallback = {
  livingSrc: livingThumbEl.getAttribute("src"),
  livingHasThumb: s1Living.classList.contains("has-thumb"),
  musicSrc: s1Music._children['[data-run-thumb="apolosign_music"]'].getAttribute("src"),
  musicHasThumb: s1Music.classList.contains("has-thumb"),
};

card._hass = {
  states: {
    "media_player.plex_plex_for_android_tv_google_tv_streamer": { state: "unavailable", attributes: {} },
    "media_player.living_room_tv": { state: "off", attributes: { entity_picture: "/stale.jpg" } },
    "switch.living_room_tv": { state: "on", attributes: {} },
  },
};
card._updateState();
const snap5SwitchOnly = {
  livingSrc: livingThumbEl.getAttribute("src"),
  livingHasThumb: s1Living.classList.contains("has-thumb"),
  livingDisplay: livingThumbEl.style.display,
};

console.log(JSON.stringify({
  hasLivingImg: skeletonHtml.includes('data-run-thumb="living_tv"'),
  hasOfficeImg: skeletonHtml.includes('data-run-thumb="office_tv"'),
  hasMusicImg: skeletonHtml.includes('data-run-thumb="apolosign_music"'),
  hasCoffeeImg: skeletonHtml.includes('data-run-thumb="coffee"'),
  snap1,
  snap2,
  snap3FailedStaysHidden,
  snap3Recovered,
  snap4Fallback,
  snap5SwitchOnly,
}));
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
        res = json.loads(proc.stdout.strip())
    finally:
        tmp_path.unlink(missing_ok=True)

    assert res["hasLivingImg"] is True
    assert res["hasOfficeImg"] is True
    assert res["hasMusicImg"] is True
    assert res["hasCoffeeImg"] is False

    assert res["snap1"] == {
        "updated": True,
        "livingSrc": "/api/media_player_proxy/plex_streamer?token=abc&cache=1",
        "livingHasThumb": True,
        "officeSrc": "/api/media_player_proxy/office_cast2?token=def",
        "officeHasThumb": True,
        "musicSrc": "https://images.example.com/m83.jpg",
        "musicHasThumb": True,
    }

    assert res["snap2"]["updated"] is True
    assert res["snap2"]["livingSrc"] == "/api/media_player_proxy/plex_streamer?token=abc&cache=2"

    assert res["snap3FailedStaysHidden"] == {"display": "none", "hasThumb": False}
    assert res["snap3Recovered"] == {
        "updated": True,
        "src": "/api/media_player_proxy/plex_streamer?token=abc&cache=3",
        "display": "block",
        "hasThumb": True,
    }

    assert res["snap4Fallback"] == {
        "livingSrc": "/api/media_player_proxy/living_tv_fb?token=99",
        "livingHasThumb": True,
        "musicSrc": None,
        "musicHasThumb": False,
    }

    assert res["snap5SwitchOnly"] == {
        "livingSrc": None,
        "livingHasThumb": False,
        "livingDisplay": "none",
    }


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
