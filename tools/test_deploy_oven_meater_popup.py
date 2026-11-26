#!/usr/bin/env python3
"""Unit tests for tools/deploy_oven_meater_popup.py."""

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

from tools import deploy_oven_meater_popup as mod

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
    runningItems: [],
    securityItems: [],
    lights: [],
  },
];

const SVG_ICONS = {
  /* GAMING_BADGES_ICONS_BEGIN */
  gamepad: `<svg viewBox="0 0 24 24"></svg>`,
  /* GAMING_BADGES_ICONS_END */
  coffee: `<svg viewBox="0 0 24 24"></svg>`,
  music: `<svg viewBox="0 0 24 24"></svg>`,
};

class BrockHouseCyclesCard {
  constructor() {
    this._hass = { states: {} };
    this._activeFloor = "all";
    this._activeLens = "lights";
    this._thermoModalOpen = false;
    this._presenceModalOpen = false;
    this._lastStateSig = "";
    this._mockCards = {};
    this._serviceCalls = [];
    this._moreInfoCalls = [];
    this.shadowRoot = null;
  }

  _readNumericState(entityId, fallbackId = null) {
    for (const id of [entityId, fallbackId]) {
      if (!id) continue;
      const st = this._hass?.states?.[id];
      if (!st || st.state === "unavailable" || st.state === "unknown") continue;
      const val = Number(st.state);
      if (!Number.isNaN(val)) return val;
    }
    return null;
  }

  _openMoreInfo(entityId) {
    this._moreInfoCalls.push(entityId);
  }

  /* GAMING_BADGES_HELPER_BEGIN */
  _evalGamingBadges() {
    return [];
  }
  /* GAMING_BADGES_HELPER_END */

  _evalRunningItem(item) {
    return { active: false, text: item.label, entity: item.entity };
  }

  _renderSkeleton() {
    const html = `
      <style>
        .run-badge.danger .run-dot { background: #ff5252; }
        /* GAMING_BADGES_CSS_BEGIN */
        .run-badge .run-thumb { width: 22px; }
        /* GAMING_BADGES_CSS_END */
        /* TV_COVER_ART_CSS_BEGIN */
        .run-badge.has-thumb .run-icon[data-run-icon] { display: none; }
        /* TV_COVER_ART_CSS_END */
      </style>
      ${FLOORS.map(
        (f) => `
        <div class="floor-card" data-floor-card="${f.id}">
          <div class="running-layer" data-running-layer="${f.id}">
            ${/* TV_COVER_ART_HTML_BEGIN */
              (f.runningItems || [])
                .map(
                  (item) => `
              <button class="run-badge ${item.accent || ""}" data-run-id="${item.id}" data-run-kind="${item.kind || ""}" data-run-entity="${item.entity}" style="left:${item.x}%;top:${item.y}%;" title="${item.label}">
                <span class="run-dot"></span>
                <span data-run-text="${item.id}">${item.label}</span>
              </button>
            `
                )
              /* TV_COVER_ART_HTML_END */
              .join("")}
            <!-- GAMING_BADGES_HTML_BEGIN -->
            <button class="run-badge gaming-badge steam" data-gaming-id="steam"></button>
            <!-- GAMING_BADGES_HTML_END -->
          </div>
        </div>
      `
      ).join("")}
      <div class="thermo-backdrop" id="thermoBackdrop" aria-hidden="true">
        <div class="thermo-modal" id="thermoModal"></div>
      </div>
      <div class="presence-backdrop" id="presenceBackdrop" aria-hidden="true">
        <div class="presence-modal" id="presenceModal"></div>
      </div>
    `;
    if (this.shadowRoot && this.shadowRoot._bindMock) {
      this.shadowRoot._bindMock(html);
    }
    return html;
  }

  _bindMockListeners(sr) {
    sr.getElementById("thermoModal")?.addEventListener("click", (e) => {
      void e;
    });
    sr.getElementById("presenceModal")?.addEventListener("click", (e) => {
      void e;
    });
  }

  _renderThermostatModal() {}
  _renderPresenceModal() {}

  _updateState(force = false) {
    const isClimate = this._activeLens === "climate";
    const sigParts = [this._activeFloor, this._activeLens];
    /* GAMING_BADGES_SIG_BEGIN */
    sigParts.push("gaming-sig");
    /* GAMING_BADGES_SIG_END */
    const sig = sigParts.join("|");
    if (!force && sig === this._lastStateSig) return false;
    this._lastStateSig = sig;

    for (const f of FLOORS) {
      const card = this._mockCards[f.id] || null;
      /* GAMING_BADGES_UPDATE_BEGIN */
      for (const _gb of this._evalGamingBadges()) {
        void _gb;
      }
      /* GAMING_BADGES_UPDATE_END */
      /* TV_COVER_ART_UPDATE_BEGIN */
      void card;
      /* TV_COVER_ART_UPDATE_END */
    }
    if (this._thermoModalOpen) this._renderThermostatModal();
    if (this._presenceModalOpen) this._renderPresenceModal();
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

    for preserved in (
        "/* GAMING_BADGES_CONFIG_BEGIN */",
        "/* GAMING_BADGES_ICONS_BEGIN */",
        "/* GAMING_BADGES_CSS_BEGIN */",
        "/* TV_COVER_ART_CSS_BEGIN */",
        "<!-- GAMING_BADGES_HTML_BEGIN -->",
        "/* GAMING_BADGES_SIG_BEGIN */",
        "/* GAMING_BADGES_UPDATE_BEGIN */",
        "/* TV_COVER_ART_UPDATE_BEGIN */",
        "/* OPENING_PATCHES_CONFIG_BEGIN:floor3 */",
        "BERMUDA_CONFIG",
    ):
        assert preserved in patched_once


def test_patch_card_js_badges_modal_and_controls_behavior() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    mod.validate_js_syntax(patched)

    harness = (
        patched
        + """
function makeMockEl(id = "") {
  const classes = new Set();
  const attrs = {};
  const listeners = {};
  return {
    id,
    style: {},
    dataset: {},
    textContent: "",
    innerHTML: "",
    title: "",
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      contains: (c) => classes.has(c),
      toggle: (c, force) => (force ? classes.add(c) : classes.delete(c)),
    },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch: (ev, evtObj) => { (listeners[ev] || []).forEach((fn) => fn(evtObj)); },
    querySelector: () => null,
  };
}

function makeFloorMock() {
  const mkBadge = (key) => {
    const badge = makeMockEl();
    const txt = makeMockEl();
    badge._txt = txt;
    badge.querySelector = (sel) => (sel === `[data-om-text="${key}"]` ? txt : null);
    return badge;
  };
  const lgOven = mkBadge("lg_oven");
  const meater = mkBadge("meater");
  return {
    lgOven,
    meater,
    querySelector: (sel) => {
      if (sel === '[data-oven-meater-badge="lg_oven"]') return lgOven;
      if (sel === '[data-oven-meater-badge="meater"]') return meater;
      return null;
    },
  };
}

const card = new BrockHouseCyclesCard();
const cookBackdrop = makeMockEl("cookBackdrop");
const cookModal = makeMockEl("cookModal");
const thermoModal = makeMockEl("thermoModal");
const presenceModal = makeMockEl("presenceModal");
const hotspotEl = makeMockEl("hotspot");
const sr = {
  getElementById: (id) => {
    if (id === "cookBackdrop") return cookBackdrop;
    if (id === "cookModal") return cookModal;
    if (id === "thermoModal") return thermoModal;
    if (id === "presenceModal") return presenceModal;
    return null;
  },
  querySelectorAll: (sel) => (sel === "[data-cook-open]" ? [hotspotEl] : []),
};
card.shadowRoot = sr;

// Inject listener wiring via method extracted from _bindMockListeners + OVEN_MEATER_LISTENERS
const skeletonHtml = card._renderSkeleton();
// Run OVEN_MEATER_LISTENERS manually against mock sr by evaluating it in card context
card._mockCards = { floor3: makeFloorMock(), floor2: makeFloorMock() };

const serviceCalls = [];
card._hass = {
  callService: (domain, service, data) => serviceCalls.push({ domain, service, data }),
  states: {
    "sensor.oven_upper_current_status": { state: "initial", attributes: {} },
    "sensor.oven_lower_current_status": { state: "initial", attributes: {} },
    "number.oven_upper_temperature": { state: "0", attributes: {} },
    "number.oven_lower_temperature": { state: "0", attributes: {} },
    "select.oven_upper_cook_mode": { state: "unknown", attributes: { options: ["bake"] } },
    "select.oven_lower_cook_mode": { state: "unknown", attributes: { options: ["bake", "convection_bake", "convection_roast"] } },
    "sensor.meater_meater_cook_status": { state: "Standby", attributes: { target_temp_f: 165.0, cook_preset: "Custom", rssi: -94 } },
    "binary_sensor.meater_meater_connected": { state: "off", attributes: {} },
  },
};

// 1. Standby/Initial -> both badges inactive, hotspot present in Floor 3 HTML
card._updateState();
const idleSnap = {
  hasHotspot: skeletonHtml.includes('class="cook-hotspot"'),
  hasOvenIcon: Boolean(SVG_ICONS.oven),
  hasMeaterIcon: Boolean(SVG_ICONS.meater),
  hasCookBackdrop: skeletonHtml.includes('id="cookBackdrop"'),
  ovenActive: card._mockCards.floor3.lgOven.classList.contains("active"),
  meaterActive: card._mockCards.floor3.meater.classList.contains("active"),
};

// 2. Active LG Lower Oven + Active MEATER+ Cooking
card._hass.states = {
  "sensor.oven_upper_current_status": { state: "initial", attributes: {} },
  "sensor.oven_lower_current_status": { state: "cooking_in_progress", attributes: {} },
  "number.oven_upper_temperature": { state: "0", attributes: {} },
  "number.oven_lower_temperature": { state: "350", attributes: {} },
  "select.oven_upper_cook_mode": { state: "unknown", attributes: {} },
  "select.oven_lower_cook_mode": { state: "convection_roast", attributes: {} },
  "select.oven_upper_operation": { state: "unknown", attributes: {} },
  "select.oven_lower_operation": { state: "start", attributes: {} },
  "sensor.meater_meater_internal_temperature": { state: "121.2", attributes: {} },
  "sensor.meater_meater_ambient_temperature": { state: "313.7", attributes: {} },
  "sensor.meater_meater_battery": { state: "98", attributes: {} },
  "sensor.meater_meater_eta": { state: "60", attributes: { rate_f_per_min: 0.65 } },
  "sensor.meater_meater_cook_status": {
    state: "Cooking",
    attributes: { target_temp_f: 165.0, cook_preset: "Turkey Breast (165°F)", rssi: -94 },
  },
  "binary_sensor.meater_meater_connected": { state: "on", attributes: {} },
  "number.meater_meater_target_temperature": { state: "165.0", attributes: { min: 100, max: 225, step: 1 } },
  "select.meater_meater_cook_preset": {
    state: "Turkey Breast (165°F)",
    attributes: { options: ["Custom", "Turkey Breast (165°F)", "Brisket (203°F)"] },
  },
};
const activeUpdated = card._updateState();
const activeSnap = {
  activeUpdated,
  ovenActive: card._mockCards.floor3.lgOven.classList.contains("active"),
  ovenText: card._mockCards.floor3.lgOven._txt.textContent,
  meaterActive: card._mockCards.floor3.meater.classList.contains("active"),
  meaterText: card._mockCards.floor3.meater._txt.textContent,
  meaterCookingCls: card._mockCards.floor3.meater.classList.contains("meater-cooking"),
};

// 3. Only MEATER internal temp changes -> sig MUST change
card._hass.states["sensor.meater_meater_internal_temperature"].state = "122.4";
const tempChangeUpdated = card._updateState();

// 4. MEATER status color states: Pull Now -> meater-pull; Target Reached -> meater-done
card._hass.states["sensor.meater_meater_cook_status"].state = "Pull Now";
card._updateState();
const pullSnap = {
  hasPullCls: card._mockCards.floor3.meater.classList.contains("meater-pull"),
  text: card._mockCards.floor3.meater._txt.textContent,
};

card._hass.states["sensor.meater_meater_cook_status"].state = "Target Reached";
card._updateState();
const doneSnap = {
  hasDoneCls: card._mockCards.floor3.meater.classList.contains("meater-done"),
  text: card._mockCards.floor3.meater._txt.textContent,
};

// 5. Climate lens hides running badges
card._activeLens = "climate";
card._updateState();
const climateHidden = {
  ovenActive: card._mockCards.floor3.lgOven.classList.contains("active"),
  meaterActive: card._mockCards.floor3.meater.classList.contains("active"),
};
card._activeLens = "lights";
card._updateState();

// 6. Open Cook Monitor modal & verify live HTML, stepper clamping, presets, and close
card._openCookModal();
const modalOpenHtml = cookModal.innerHTML;
const backdropOpen = cookBackdrop.classList.contains("open");
card._stepMeaterTargetTemp(-5);
card._stepMeaterTargetTemp(500); // clamped to maxTarget (225)
card._stepMeaterTargetTemp(-500); // clamped to minTarget (100)
card._selectMeaterPreset("Brisket (203°F)");
card._closeCookModal();
const backdropAfterClose = cookBackdrop.classList.contains("open");

console.log(JSON.stringify({
  idleSnap,
  activeSnap,
  tempChangeUpdated,
  pullSnap,
  doneSnap,
  climateHidden,
  backdropOpen,
  backdropAfterClose,
  modalChecks: {
    hasInternal: modalOpenHtml.includes("122.4°"),
    hasTarget: modalOpenHtml.includes("/ 165°F"),
    hasPullMarker: modalOpenHtml.includes("Pull 160°F"),
    hasAmbient: modalOpenHtml.includes("314°F"),
    hasEta: modalOpenHtml.includes("1h (+0.65°/m)"),
    hasBattery: modalOpenHtml.includes("98%"),
    hasRssi: modalOpenHtml.includes("-94 dBm"),
    hasUpperOven: modalOpenHtml.includes("Upper Oven"),
    hasLowerOven: modalOpenHtml.includes("Lower Oven") && modalOpenHtml.includes("350°F") && modalOpenHtml.includes("Convection Roast"),
    hasPreset: modalOpenHtml.includes("Brisket (203°F)"),
    hasGaugeSvg: modalOpenHtml.includes("cm-gauge-svg"),
  },
  serviceCalls,
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

    assert res["idleSnap"] == {
        "hasHotspot": True,
        "hasOvenIcon": True,
        "hasMeaterIcon": True,
        "hasCookBackdrop": True,
        "ovenActive": False,
        "meaterActive": False,
    }
    assert res["activeSnap"] == {
        "activeUpdated": True,
        "ovenActive": True,
        "ovenText": "Oven 350°F",
        "meaterActive": True,
        "meaterText": "121°/165°F",
        "meaterCookingCls": True,
    }
    assert res["tempChangeUpdated"] is True
    assert res["pullSnap"] == {"hasPullCls": True, "text": "122°F · Pull!"}
    assert res["doneSnap"] == {"hasDoneCls": True, "text": "122°F · Done"}
    assert res["climateHidden"] == {"ovenActive": False, "meaterActive": False}
    assert res["backdropOpen"] is True
    assert res["backdropAfterClose"] is False
    assert all(res["modalChecks"].values()), res["modalChecks"]
    assert res["serviceCalls"] == [
        {
            "domain": "number",
            "service": "set_value",
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 160},
        },
        {
            "domain": "number",
            "service": "set_value",
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 225},
        },
        {
            "domain": "number",
            "service": "set_value",
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 100},
        },
        {
            "domain": "select",
            "service": "select_option",
            "data": {"entity_id": "select.meater_meater_cook_preset", "option": "Brisket (203°F)"},
        },
    ]


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
