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
cookBackdrop.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1600, height: 960, right: 1600, bottom: 960 });
const cookTetherSvg = makeMockEl("cookTetherSvg");
const cookModal = makeMockEl("cookModal");
cookModal.getBoundingClientRect = () => {
  const l = cookModal.style.left ? parseFloat(cookModal.style.left) : 410;
  const t = cookModal.style.top ? parseFloat(cookModal.style.top) : 180;
  const w = cookModal.style.maxWidth ? Math.min(780, parseFloat(cookModal.style.maxWidth)) : 780;
  const h = 620;
  return { left: l, top: t, width: w, height: h, right: l + w, bottom: t + h };
};
const thermoModal = makeMockEl("thermoModal");
const presenceModal = makeMockEl("presenceModal");
const hotspotEl = makeMockEl("hotspot");
hotspotEl.getBoundingClientRect = () => ({ left: 898, top: 410, width: 44, height: 44, right: 942, bottom: 454 });
const sr = {
  getElementById: (id) => {
    if (id === "cookBackdrop") return cookBackdrop;
    if (id === "cookTetherSvg") return cookTetherSvg;
    if (id === "cookModal") return cookModal;
    if (id === "thermoModal") return thermoModal;
    if (id === "presenceModal") return presenceModal;
    return null;
  },
  querySelector: (sel) => (sel === ".cook-hotspot" ? hotspotEl : null),
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

// 5. Cross-lens active badges stay active on climate lens
card._activeLens = "climate";
card._updateState();
const climatePreserved = {
  ovenActive: card._mockCards.floor3.lgOven.classList.contains("active"),
  meaterActive: card._mockCards.floor3.meater.classList.contains("active"),
};
card._activeLens = "lights";
card._updateState();

// 6. Open Cook Monitor modal & verify live HTML, tether cone, optimistic stepper clamping, two-level protein picker, presets, and close
card._openCookModal();
const modalOpenHtml = cookModal.innerHTML;
const initialProteinCat = card._cookProteinCat;
const tetherOpenHtml = cookTetherSvg.innerHTML;
const modalLeftWhenOpen = cookModal.style.left;
const modalTopWhenOpen = cookModal.style.top;
const modalPosWhenOpen = cookModal.style.position;
const backdropOpen = cookBackdrop.classList.contains("open");
card._stepMeaterTargetTemp(-5);
const afterStepMinus5Html = cookModal.innerHTML;
card._stepMeaterTargetTemp(-1); // rapid tap stacks on optimistic 160 -> 159
const afterStackedMinus1Html = cookModal.innerHTML;
card._stepMeaterTargetTemp(500); // clamped to maxTarget (225)
card._stepMeaterTargetTemp(-500); // clamped to minTarget (100)
card._selectMeaterPreset("Brisket (203°F)");

// Test two-level visual protein & cut/doneness picker:
// Switch to Beef tab -> renders Prime Rib, Steak Rare, Steak Med-Rare, Steak Medium, Brisket
card._selectCookProteinCat("beef");
const beefTabHtml = cookModal.innerHTML;
// Select Brisket cut (beef idx 4, which exists in presetOptions -> selects "Brisket (203°F)" + sets optimistic target 203)
card._selectMeaterCut("beef", 4);
const afterBeefBrisketHtml = cookModal.innerHTML;
// Switch to Lamb & Fish tab and select Salmon (lamb_fish idx 1, not in presetOptions -> selects "Custom" + sets target 125)
card._selectCookProteinCat("lamb_fish");
const lambFishTabHtml = cookModal.innerHTML;
card._selectMeaterCut("lamb_fish", 1);
// Switch to Custom tab -> shows manual target note pointing to -5°/-1°/+1°/+5° stepper
card._selectCookProteinCat("custom");
const customTabHtml = cookModal.innerHTML;

card._closeCookModal();
const backdropAfterClose = cookBackdrop.classList.contains("open");
const tetherAfterClose = cookTetherSvg.innerHTML;

console.log(JSON.stringify({
  idleSnap,
  activeSnap,
  tempChangeUpdated,
  pullSnap,
  doneSnap,
  climatePreserved,
  backdropOpen,
  backdropAfterClose,
  modalChecks: {
    hasInternal: modalOpenHtml.includes("122.4°"),
    hasTarget: modalOpenHtml.includes("/ 165°F"),
    hasPullMarker: modalOpenHtml.includes("Pull 160°F"),
    hasAmbient: modalOpenHtml.includes("314°F"),
    hasProminentEta: modalOpenHtml.includes("cm-stat-eta") && modalOpenHtml.includes("cm-stat-eta-val") && modalOpenHtml.includes("1h") && modalOpenHtml.includes("Pull 160°F"),
    hasSubtleRate: modalOpenHtml.includes("cm-stat-rate-sub") && modalOpenHtml.includes("+0.65°F/min"),
    hasBattery: modalOpenHtml.includes("98%"),
    noBleSignal: !modalOpenHtml.includes("BLE Signal") && !modalOpenHtml.includes("-94 dBm"),
    hasMobileMediaCss: skeletonHtml.includes("@media (max-width: 640px)") && skeletonHtml.includes("@media (max-width: 400px)"),
    hasImmediateStepRender: afterStepMinus5Html.includes("/ 160°F") && afterStepMinus5Html.includes(">160°F<") && afterStepMinus5Html.includes("Pull 155°F"),
    hasStackedTapRender: afterStackedMinus1Html.includes("/ 159°F") && afterStackedMinus1Html.includes(">159°F<"),
    hasImmediateCutRender: afterBeefBrisketHtml.includes("/ 203°F") && afterBeefBrisketHtml.includes(">203°F<"),
    hasUpperOven: modalOpenHtml.includes("Upper Oven"),
    hasLowerOven: modalOpenHtml.includes("Lower Oven") && modalOpenHtml.includes("350°F") && modalOpenHtml.includes("Convection Roast"),
    autoSelectedPoultryCat: initialProteinCat === "poultry",
    hasProteinTabs: modalOpenHtml.includes("cm-protein-tabs") && modalOpenHtml.includes('data-cm-action="protein-cat"'),
    hasAllFiveProteinIcons: Boolean(SVG_ICONS.poultry && SVG_ICONS.beef && SVG_ICONS.pork && SVG_ICONS.lamb_fish && SVG_ICONS.custom),
    hasCutGrid: modalOpenHtml.includes("cm-cut-grid") && modalOpenHtml.includes('data-cm-action="protein-cut"'),
    hasPoultryCuts: modalOpenHtml.includes("Turkey Breast") && modalOpenHtml.includes("Turkey Thigh") && modalOpenHtml.includes("Whole Chicken") && modalOpenHtml.includes("Chicken Breast") && modalOpenHtml.includes("Duck Breast") && modalOpenHtml.includes("20m rest"),
    hasBeefCuts: beefTabHtml.includes("Prime Rib") && beefTabHtml.includes("Steak Rare") && beefTabHtml.includes("Steak Med-Rare") && beefTabHtml.includes("Steak Medium") && beefTabHtml.includes("Brisket") && beefTabHtml.includes("30m rest"),
    hasLambFishCuts: lambFishTabHtml.includes("Rack of Lamb") && lambFishTabHtml.includes("Salmon") && lambFishTabHtml.includes("White Fish") && lambFishTabHtml.includes("3m rest"),
    hasCustomNote: customTabHtml.includes("cm-custom-note") && customTabHtml.includes("-5° / -1° / +1° / +5°"),
    hasGaugeSvg: modalOpenHtml.includes("cm-gauge-svg") && modalOpenHtml.includes('viewBox="0 0 172 156"'),
    hasChartHeader: modalOpenHtml.includes("Temperature History &amp; Projected Trend"),
    hasChartRatePill: modalOpenHtml.includes("+0.65°F/min"),
    hasChartSvg: modalOpenHtml.includes("cm-chart-svg") && modalOpenHtml.includes('viewBox="0 0 680 210"'),
    hasChartFutureZone: modalOpenHtml.includes("cm-chart-future-zone"),
    hasChartTargetLine: modalOpenHtml.includes("cm-chart-target-line") && modalOpenHtml.includes("Target 165°F"),
    hasChartPullLine: modalOpenHtml.includes("cm-chart-pull-line") && modalOpenHtml.includes("Pull 160°F"),
    hasChartHistoryLine: modalOpenHtml.includes("cm-chart-history-line"),
    hasChartTrendLine: modalOpenHtml.includes("cm-chart-trend-line"),
    hasChartNowDot: modalOpenHtml.includes("cm-chart-now-dot"),
    hasLargeModalCss: skeletonHtml.includes("min(92vw, 780px)") && skeletonHtml.includes("backdrop-filter: blur(12px)"),
    hasLighterBackdropCss: skeletonHtml.includes("rgba(4, 8, 15, 0.44)"),
    hasTetherSvgInSkeleton: skeletonHtml.includes('id="cookTetherSvg"') && skeletonHtml.includes('class="cook-tether-svg"'),
    hasModalPositionedLeftOfOven: modalPosWhenOpen === "absolute" && parseFloat(modalLeftWhenOpen) === 106 && parseFloat(modalTopWhenOpen) === 122,
    hasTetherConePolygon: tetherOpenHtml.includes("cook-tether-cone") && tetherOpenHtml.includes("920.0,432.0 886.0,146.0 886.0,718.0"),
    hasTetherGradient: tetherOpenHtml.includes("rgba(255, 148, 72, 0.34)") && tetherOpenHtml.includes("rgba(255, 148, 72, 0.08)"),
    hasTetherRays: tetherOpenHtml.includes("cook-tether-ray") && tetherOpenHtml.includes("rgba(255, 178, 106, 0.58)"),
    hasTetherRungs: tetherOpenHtml.includes("cook-tether-rung") && tetherOpenHtml.includes("rgba(255, 184, 118, 0.24)"),
    hasTetherReticle: tetherOpenHtml.includes("cook-tether-halo") && tetherOpenHtml.includes("cook-tether-dot"),
    tetherClearedOnClose: tetherAfterClose === "",
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
    assert res["climatePreserved"] == {"ovenActive": True, "meaterActive": True}
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
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 159},
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
        {
            "domain": "select",
            "service": "select_option",
            "data": {"entity_id": "select.meater_meater_cook_preset", "option": "Brisket (203°F)"},
        },
        {
            "domain": "number",
            "service": "set_value",
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 203},
        },
        {
            "domain": "select",
            "service": "select_option",
            "data": {"entity_id": "select.meater_meater_cook_preset", "option": "Custom"},
        },
        {
            "domain": "number",
            "service": "set_value",
            "data": {"entity_id": "number.meater_meater_target_temperature", "value": 125},
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


def test_cook_history_fetch_caching_downsampling_and_trend_fallbacks() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    harness = (
        patched
        + """
async function run() {
  const card = new BrockHouseCyclesCard();
  const cookBackdrop = {
    classList: { add: () => {}, remove: () => {} },
    setAttribute: () => {},
  };
  const cookModal = { innerHTML: "" };
  card.shadowRoot = {
    getElementById: (id) => (id === "cookBackdrop" ? cookBackdrop : id === "cookModal" ? cookModal : null),
  };

  const nowSec = Math.floor(Date.now() / 1000);
  // Build 200 history items mixing minimal_response ({s, lu}) and full ({state, last_updated})
  const rawHistory = [];
  for (let i = 0; i < 200; i++) {
    const tSec = nowSec - (200 - i) * 30;
    const temp = (95 + i * 0.15).toFixed(2);
    if (i % 2 === 0) {
      rawHistory.push({ s: temp, lu: tSec });
    } else {
      rawHistory.push({ state: temp, last_updated: new Date(tSec * 1000).toISOString() });
    }
  }
  rawHistory.push({ s: "unavailable", lu: nowSec - 5 });

  let apiCalls = 0;
  let lastApiPath = "";
  card._hass = {
    callApi: async (method, path) => {
      apiCalls += 1;
      lastApiPath = `${method}:${path}`;
      return [rawHistory];
    },
    states: {
      "sensor.meater_meater_internal_temperature": { state: "128.5", attributes: {} },
      "sensor.meater_meater_ambient_temperature": { state: "325.0", attributes: {} },
      "sensor.meater_meater_battery": { state: "92", attributes: {} },
      "sensor.meater_meater_eta": { state: "50", attributes: { rate_f_per_min: 0.72 } },
      "sensor.meater_meater_cook_status": { state: "Cooking", attributes: { target_temp_f: 165 } },
      "binary_sensor.meater_meater_connected": { state: "on", attributes: {} },
      "number.meater_meater_target_temperature": { state: "165", attributes: { min: 100, max: 225 } },
    },
  };

  card._openCookModal();
  const pts1 = await card._fetchCookHistory(false);
  const callsAfterFirst = apiCalls;
  // Second call within 30s TTL uses cache
  const pts2 = await card._fetchCookHistory(false);
  const callsAfterCached = apiCalls;
  // Forced call bypasses TTL
  const pts3 = await card._fetchCookHistory(true);
  const callsAfterForced = apiCalls;

  // Test fallback 1: (target - internal) / etaMin when rate_f_per_min is null
  card._hass.states["sensor.meater_meater_eta"].attributes = {};
  const meaterEtaFallback = card._evalMeaterStatus();
  const projEtaFallback = card._computeCookProjection(meaterEtaFallback, pts1);

  // Test fallback 2: 20-30m linear regression when both rate_f_per_min and etaMin are null
  card._hass.states["sensor.meater_meater_eta"].state = "unknown";
  const meaterRegFallback = card._evalMeaterStatus();
  const projRegFallback = card._computeCookProjection(meaterRegFallback, pts1);

  // Test 180m cap on very slow climb rate
  card._hass.states["sensor.meater_meater_eta"].attributes = { rate_f_per_min: 0.05 };
  const meaterSlow = card._evalMeaterStatus();
  const projSlow = card._computeCookProjection(meaterSlow, pts1);

  // Test empty/idle fallback when no probe data exists
  card._cookHistoryPoints = [];
  card._hass.states["sensor.meater_meater_internal_temperature"].state = "unavailable";
  const meaterEmpty = card._evalMeaterStatus();
  const emptySvg = card._buildCookChartSvg(meaterEmpty, []);

  console.log(JSON.stringify({
    callsAfterFirst,
    callsAfterCached,
    callsAfterForced,
    hasHistoryPath: lastApiPath.startsWith("GET:history/period/") && lastApiPath.includes("sensor.meater_meater_internal_temperature"),
    ptsCount: pts1.length,
    sameCachedRef: pts1 === pts2,
    forcedCount: pts3.length,
    lastPtIsLive: Math.abs(pts1[pts1.length - 1].temp - 128.5) < 0.01,
    etaFallbackRate: Number(projEtaFallback.effectiveRate.toFixed(3)),
    regFallbackHasProjection: projRegFallback.hasProjection && projRegFallback.effectiveRate > 0.1,
    slowCappedAt180: projSlow.projMins === 180,
    modalHasEtaLabel: cookModal.innerHTML.includes("cm-chart-eta-label"),
    emptySvgHasFallback: emptySvg.includes("Waiting for MEATER+ temperature telemetry"),
  }));
}
run();
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

    assert res["callsAfterFirst"] == 1
    assert res["callsAfterCached"] == 1
    assert res["callsAfterForced"] == 2
    assert res["hasHistoryPath"] is True
    assert res["ptsCount"] == 160
    assert res["sameCachedRef"] is True
    assert res["forcedCount"] == 160
    assert res["lastPtIsLive"] is True
    # (165 - 128.5) / 50 = 0.73
    assert res["etaFallbackRate"] == 0.73
    assert res["regFallbackHasProjection"] is True
    assert res["slowCappedAt180"] is True
    assert res["modalHasEtaLabel"] is True
    assert res["emptySvgHasFallback"] is True


def test_position_cook_modal_and_cone_all_sides_and_narrow_fallback() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    harness = (
        patched
        + """
function runScenario(bw, bh, hx, hy, mw = 700, mh = 520) {
  const card = new BrockHouseCyclesCard();
  card._cookModalOpen = true;
  const backdrop = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: bw, height: bh }),
  };
  const modal = {
    style: {},
    getBoundingClientRect: () => {
      const l = modal.style.left ? parseFloat(modal.style.left) : (bw - mw) / 2;
      const t = modal.style.top ? parseFloat(modal.style.top) : (bh - mh) / 2;
      const w = modal.style.maxWidth ? Math.min(mw, parseFloat(modal.style.maxWidth)) : mw;
      return { left: l, top: t, width: w, height: mh };
    },
  };
  const tetherSvg = {
    innerHTML: "",
    viewBox: "",
    setAttribute: (k, v) => {
      if (k === "viewBox") tetherSvg.viewBox = v;
    },
  };
  const hotspot = {
    getBoundingClientRect: () => ({ left: hx - 20, top: hy - 20, width: 40, height: 40 }),
  };
  card.shadowRoot = {
    getElementById: (id) => (id === "cookBackdrop" ? backdrop : id === "cookModal" ? modal : id === "cookTetherSvg" ? tetherSvg : null),
    querySelector: (sel) => (sel === ".cook-hotspot" ? hotspot : null),
  };
  card._positionCookModalAndCone();
  return {
    pos: modal.style.position,
    left: modal.style.left ? parseFloat(modal.style.left) : null,
    top: modal.style.top ? parseFloat(modal.style.top) : null,
    viewBox: tetherSvg.viewBox,
    svg: tetherSvg.innerHTML,
  };
}

const leftRes = runScenario(1600, 960, 1100, 480);
const rightRes = runScenario(1600, 960, 260, 480);
const belowRes = runScenario(900, 1200, 450, 200);
const aboveRes = runScenario(900, 1200, 450, 980);
const narrowRes = runScenario(520, 800, 300, 360);

console.log(JSON.stringify({ leftRes, rightRes, belowRes, aboveRes, narrowRes }));
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

    # Left of oven (hx=1100, mw=700, gap=34 -> left = 1100 - 34 - 700 = 366, mr = 1066)
    assert res["leftRes"]["pos"] == "absolute"
    assert res["leftRes"]["left"] == 366
    assert "1100.0,480.0 1066.0," in res["leftRes"]["svg"]

    # Right of oven (hx=260, gap=34 -> left = 294)
    assert res["rightRes"]["pos"] == "absolute"
    assert res["rightRes"]["left"] == 294
    assert "260.0,480.0 294.0," in res["rightRes"]["svg"]

    # Below oven (hy=200, gap=34 -> top = 234)
    assert res["belowRes"]["pos"] == "absolute"
    assert res["belowRes"]["top"] == 234
    assert ",234.0" in res["belowRes"]["svg"]

    # Above oven (hy=980, mh=520, gap=34 -> top = 980 - 34 - 520 = 426, mb = 946)
    assert res["aboveRes"]["pos"] == "absolute"
    assert res["aboveRes"]["top"] == 426
    assert ",946.0" in res["aboveRes"]["svg"]

    # Narrow screen fallback -> relative centered
    assert res["narrowRes"]["pos"] == "relative"
    assert res["narrowRes"]["left"] is None
    assert "cook-tether-cone" in res["narrowRes"]["svg"]


def test_migrates_stray_modal_refresh_out_of_ping_device_into_update_state() -> None:
    stray_fixture = REALISTIC_CARD_FIXTURE.replace(
        "  _renderThermostatModal() {}\n  _renderPresenceModal() {}",
        """  _renderThermostatModal() {}
  _renderPresenceModal() {}

  _pingDevice(presId, notifySvc, devName) {
    setTimeout(() => {
      if (this._presenceModalOpen) this._renderPresenceModal();
    /* OVEN_MEATER_MODAL_REFRESH_BEGIN */
    if (this._cookModalOpen) {
      this._fetchCookHistory(false);
      this._renderCookModal();
    }
    /* OVEN_MEATER_MODAL_REFRESH_END */
    }, 3500);
  }""",
    )
    patched = mod.patch_card_js(stray_fixture)
    patched_twice = mod.patch_card_js(patched)
    assert patched == patched_twice
    assert patched.count("/* OVEN_MEATER_MODAL_REFRESH_BEGIN */") == 1

    ping_start = patched.index("_pingDevice(")
    update_start = patched.index("_updateState(")
    refresh_pos = patched.index("/* OVEN_MEATER_MODAL_REFRESH_BEGIN */")
    assert "OVEN_MEATER_MODAL_REFRESH_BEGIN" not in patched[ping_start:update_start]
    assert refresh_pos > update_start
    assert (
        "if (this._presenceModalOpen) this._renderPresenceModal();\n    /* OVEN_MEATER_MODAL_REFRESH_BEGIN */"
        in patched[update_start:]
    )


def test_optimistic_target_temp_clears_on_live_match_and_ttl() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    harness = (
        patched
        + """
const card = new BrockHouseCyclesCard();
card.shadowRoot = {
  getElementById: () => null,
  querySelector: () => null,
};
card._hass = {
  callService: () => {},
  states: {
    "sensor.meater_meater_cook_status": { state: "Cooking", attributes: { target_temp_f: 165 } },
    "binary_sensor.meater_meater_connected": { state: "on", attributes: {} },
    "number.meater_meater_target_temperature": { state: "165", attributes: { min: 100, max: 225 } },
  },
};

card._stepMeaterTargetTemp(-5);
const optTarget = card._evalMeaterStatus().targetF;
const optSet = card._optimisticTargetF;

// Live HA state catches up to 160 -> optimistic state clears cleanly
card._hass.states["number.meater_meater_target_temperature"].state = "160";
const matchedTarget = card._evalMeaterStatus().targetF;
const optAfterMatch = card._optimisticTargetF;

// Stale optimistic state older than 6s TTL expires back to live HA value
card._optimisticTargetF = 175;
card._optimisticTargetAt = Date.now() - 7000;
const expiredTarget = card._evalMeaterStatus().targetF;
const optAfterExpiry = card._optimisticTargetF;

console.log(JSON.stringify({
  optTarget,
  optSet,
  matchedTarget,
  optAfterMatch,
  expiredTarget,
  optAfterExpiry,
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

    assert res == {
        "optTarget": 160,
        "optSet": 160,
        "matchedTarget": 160,
        "optAfterMatch": None,
        "expiredTarget": 160,
        "optAfterExpiry": None,
    }



