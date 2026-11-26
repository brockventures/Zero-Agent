"""Unit tests for tools/deploy_climate_co2_and_cross_lens.py."""

from __future__ import annotations

import base64
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

from tools import deploy_climate_co2_and_cross_lens as mod

REALISTIC_CARD_FIXTURE = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const OVEN_MEATER_CONFIG = {
  floorId: "floor3",
  hotspot: { x: 51.5, y: 43.5 },
  ovenBadgePos: { x: 50.5, y: 31.5 },
  meaterBadgePos: { x: 58.5, y: 46.5 },
};

const GAMING_BADGES_CONFIG = {
  badges: [
    { id: "steam", entity: "sensor.steam_76561197975996046", label: "Steam", icon: "gamepad", accent: "steam", x: 18.0, y: 48.0 },
    { id: "xbox", entity: "media_player.xbox", label: "Xbox", icon: "gamepad", accent: "xbox", x: 28.0, y: 35.0 },
    { id: "switch2", entity: "input_boolean.switch_2_on", label: "Switch 2", icon: "gamepad", accent: "switch2", x: 32.0, y: 35.0 },
  ],
};

const SVG_ICONS = {
  coffee: "<svg></svg>",
  music: "<svg></svg>",
  laundry: "<svg></svg>",
  printer: "<svg></svg>",
  bolt: "<svg></svg>",
  tv: "<svg></svg>",
  gamepad: "<svg></svg>",
  oven: "<svg></svg>",
  meater: "<svg></svg>",
};

const FLOORS = [
  {
    id: "floor3",
    runningItems: [
      { id: "coffee", kind: "switch", entity: "switch.coffee_machine", label: "Coffee On", icon: "coffee", accent: "amber", x: 46.5, y: 38.0 },
      { id: "apolosign_music", kind: "media_sensor", entity: "sensor.apolosign_media", label: "Music", icon: "music", accent: "green", x: 29.5, y: 43.0 },
    ],
    climateRooms: [
      {
        id: "rosie",
        name: "Rosie's",
        x: 76.5,
        y: 55.5,
        rx: 24,
        ry: 22,
        tempEntity: "sensor.hub_2_tempsensor_temperature",
        tempFallback: "sensor.rosie_temperature",
        humEntity: "sensor.hub_2_humisensor_humidity",
        humFallback: "sensor.rosie_humidity",
      },
      {
        id: "isaac",
        name: "Isaac's",
        x: 63.5,
        y: 71.0,
        rx: 24,
        ry: 22,
        tempEntity: "sensor.nest_temperature_sensor_isaac_s_room_temperature",
        tempFallback: "sensor.isaac_temperature",
        humEntity: "sensor.isaac_humidity",
      },
    ],
  },
  {
    id: "floor2",
    runningItems: [
      { id: "living_tv", kind: "tv", entity: "media_player.plex_plex_for_android_tv_google_tv_streamer", label: "TV", icon: "tv", accent: "blue", x: 26.5, y: 29.5 },
    ],
    climateRooms: [
      {
        id: "living",
        name: "Living",
        x: 33.0,
        y: 44.5,
        rx: 32,
        ry: 28,
        tempEntity: "sensor.nest_learning_thermostat_4th_gen_temperature",
        tempFallback: "sensor.living_room_thermostat_temperature",
        humEntity: "sensor.nest_learning_thermostat_4th_gen_humidity",
        climateEntity: "climate.nest_learning_thermostat_4th_gen",
      },
      {
        id: "primary",
        name: "Primary",
        x: 60.5,
        y: 70.0,
        rx: 28,
        ry: 24,
        tempEntity: "sensor.h5075_e90f_temperature",
        tempFallback: "sensor.bedroom_temperature",
        humEntity: "sensor.h5075_e90f_humidity",
        humFallback: "sensor.bedroom_humidity",
      },
    ],
  },
  {
    id: "floor1",
    runningItems: [
      { id: "printer3d", kind: "printer", entity: "binary_sensor.octoprint_printing", label: "3D Print", icon: "printer", accent: "amber", x: 82.0, y: 52.5 },
      { id: "washer", kind: "appliance", entity: "input_boolean.washer_running_state", label: "Washer", icon: "laundry", accent: "cyan", x: 65.5, y: 49.0 },
      { id: "dryer", kind: "appliance", entity: "input_boolean.dryer_running_state", label: "Dryer", icon: "laundry", accent: "amber", x: 69.5, y: 52.0 },
      { id: "ev_charger", kind: "ev", entity: "binary_sensor.tesla_wall_connector_contactor_closed", label: "EV Charging", icon: "bolt", accent: "green", x: 73.5, y: 68.0 },
      { id: "office_tv", kind: "tv", entity: "media_player.office_chromecast", label: "Office TV", icon: "tv", accent: "blue", x: 42.0, y: 44.5 },
    ],
    climateRooms: [
      {
        id: "office",
        name: "Office",
        x: 22.5,
        y: 45.0,
        rx: 28,
        ry: 24,
        tempEntity: "sensor.nest_temperature_sensor_office_temperature",
        tempFallback: "sensor.office_co2_temperature",
        humEntity: "sensor.office_co2_humidity",
      },
    ],
  },
];

function thermalBand(tempF) {
  return { stroke: "rgba(102, 198, 134, 0.65)", wash: "rgba(102, 198, 134, 0.22)", text: "#96e6b0" };
}

class BrockHouseCyclesCard {
  constructor() {
    this._hass = { states: {} };
    this._activeLens = "lights";
    this._activeRoom = null;
    this._showControls = true;
    this._cards = {};
    this._thumbsApplied = [];
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

  _getRoomClimate(room) {
    const temp = this._readNumericState(room.tempEntity, room.tempFallback);
    const hum = this._readNumericState(room.humEntity, room.humFallback);
    let hvacAction = null;
    if (room.climateEntity) {
      const cst = this._hass?.states?.[room.climateEntity];
      hvacAction = cst?.attributes?.hvac_action || null;
    }
    return { temp, hum, hvacAction };
  }

  _evalRunningItem(ri) {
    const st = this._hass?.states?.[ri.entity];
    const active = Boolean(st && st.state === "on");
    return { active, entity: ri.entity, text: active ? `${ri.label} Active` : ri.label };
  }

  _evalGamingBadges() {
    return GAMING_BADGES_CONFIG.badges.map((b) => {
      const st = this._hass?.states?.[b.entity];
      const active = Boolean(st && st.state === "on");
      return { ...b, active, floorId: "floor2", text: `${b.label} Playing`, thumb: "" };
    });
  }

  _applyRunningItemThumb(el, ri, ev) {
    this._thumbsApplied.push({ id: ri.id, active: ev.active });
  }

  _evalOvenStatus() {
    const st = this._hass?.states?.["sensor.oven"];
    const active = Boolean(st && st.state === "heating");
    return { active, badgeClass: "oven-active", title: "LG Oven", badgeText: "375°F" };
  }

  _evalMeaterStatus() {
    const st = this._hass?.states?.["sensor.meater"];
    const active = Boolean(st && st.state === "cooking");
    return { active, badgeClass: "meater-cooking", cookStatus: "Cooking", badgeText: "135°/165°F" };
  }

  _renderSkeleton() {
    return `
      <style>
        .climate-badge {
          position: absolute;
        }
        @media (min-width: 820px) {
          .stage.exploded .climate-badge {
            padding: 4px 8px;
          }
          .stage.exploded .climate-badge-room {
            font-size: 9px;
          }
          .stage.exploded .climate-badge-temp {
            font-size: 12.5px;
          }
          .stage.exploded .climate-badge-hum {
            font-size: 10px;
          }
        }
        .running-layer {
          position: absolute;
          inset: 0;
          z-index: 16;
          pointer-events: none;
          opacity: 0;
          visibility: hidden;
          transition: opacity 0.2s ease;
        }
      </style>
      ${FLOORS.map(
        (f) => `
              <div class="climate-layer" data-climate-layer="${f.id}">
                ${(f.climateRooms || [])
                  .map(
                    (r) => `
                  <div class="thermal-wash" data-thermal-room="${r.id}" style="left:${r.x}%;top:${r.y}%;width:${r.rx}%;height:${r.ry}%;"></div>
                  <button class="climate-badge" data-climate-room="${r.id}" data-info-entity="${r.climateEntity || r.tempEntity}" style="left:${r.x}%;top:${r.y}%;" title="${r.name} Climate">
                    <span class="climate-badge-room">
                      ${r.climateEntity ? `<span class="hvac-dot" data-room-hvac="${r.id}"></span>` : ""}
                      <span>${r.name}</span>
                    </span>
                    <span class="climate-badge-vals">
                      <span class="climate-badge-temp" data-room-temp="${r.id}">--°</span>
                      <span class="climate-badge-hum" data-room-hum="${r.id}"></span>
                    </span>
                  </button>
                `
                  )
                  .join("")}
              </div>
        `
      ).join("")}
    `;
  }

  _update() {
    const isClimate = this._activeLens === "climate";
    const isSecurity = this._activeLens === "security";
    const isEnergy = this._activeLens === "energy";
    const isCameras = this._activeLens === "cameras";
    const isPresence = this._activeLens === "presence";
    const sigParts = [this._activeLens];
    const allIndoorTemps = [];

    for (const f of FLOORS) {
      for (const r of (f.climateRooms || [])) {
        const c = this._getRoomClimate(r);
        sigParts.push(
          c.temp !== null ? c.temp.toFixed(1) : "",
          c.hum !== null ? Math.round(c.hum) : "",
          c.hvacAction || ""
        );
      }
    }

    for (const f of FLOORS) {
      const card = this._cards[f.id];
      if (card) {
        const runLayer = card.querySelector(".running-layer");
        if (runLayer) runLayer.classList.toggle("visible", !isSecurity && !isEnergy && !isCameras && !isPresence && !this._activeRoom);
      }

      const floorTemps = [];
      for (const r of (f.climateRooms || [])) {
        const c = this._getRoomClimate(r);
        if (c.temp !== null) {
          floorTemps.push(c.temp);
          allIndoorTemps.push(c.temp);
        }
        if (isClimate && card) {
          const band = thermalBand(c.temp);
          const wash = card.querySelector(`.thermal-wash[data-thermal-room="${r.id}"]`);
          if (wash) {
            wash.style.background = `radial-gradient(ellipse at center, ${band.wash} 0%, transparent 72%)`;
          }
          const badgeEl = card.querySelector(`.climate-badge[data-climate-room="${r.id}"]`);
          if (badgeEl) {
            badgeEl.style.borderColor = band.stroke;
          }
          const tempEl = card.querySelector(`[data-room-temp="${r.id}"]`);
          if (tempEl) {
            tempEl.textContent = c.temp !== null ? `${c.temp.toFixed(1)}°` : "--°";
            tempEl.style.color = band.text;
          }
          const humEl = card.querySelector(`[data-room-hum="${r.id}"]`);
          if (humEl) {
            humEl.textContent = c.hum !== null ? `${Math.round(c.hum)}%` : "";
          }
          const hvacEl = card.querySelector(`[data-room-hvac="${r.id}"]`);
          if (hvacEl) {
            hvacEl.classList.toggle("heating", c.hvacAction === "heating");
            hvacEl.classList.toggle("cooling", c.hvacAction === "cooling");
          }
        }
      }

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
      for (const gb of this._evalGamingBadges()) {
        const el = card?.querySelector(`.gaming-badge[data-gaming-id="${gb.id}"]`);
        if (!el) continue;
        const showOnFloor = gb.active && gb.floorId === f.id && !isClimate;
        el.classList.toggle("active", showOnFloor);
      }
      for (const ri of (f.runningItems || [])) {
        if (ri.kind !== "tv" && ri.kind !== "media_sensor") continue;
        const el = card?.querySelector(`.run-badge[data-run-id="${ri.id}"]`);
        if (!el) continue;
        const ev = this._evalRunningItem(ri);
        this._applyRunningItemThumb(el, ri, isClimate ? { ...ev, active: false } : ev);
      }
      if (f.id === OVEN_MEATER_CONFIG.floorId) {
        const ovenEv = this._evalOvenStatus();
        const ovenEl = card?.querySelector('[data-oven-meater-badge="lg_oven"]');
        if (ovenEl) {
          ovenEl.classList.remove("oven-active", "oven-cooling", "oven-done");
          ovenEl.classList.add(ovenEv.badgeClass);
          ovenEl.classList.toggle("active", ovenEv.active && !isClimate);
        }
        const meaterEv = this._evalMeaterStatus();
        const meaterEl = card?.querySelector('[data-oven-meater-badge="meater"]');
        if (meaterEl) {
          meaterEl.classList.remove("meater-standby", "meater-cooking", "meater-almost", "meater-pull", "meater-done");
          meaterEl.classList.add(meaterEv.badgeClass);
          meaterEl.classList.toggle("active", meaterEv.active && !isClimate);
        }
      }
    }
    return sigParts.join("|");
  }
}
"""


def test_patch_card_js_replacements_and_idempotency() -> None:
    patched_once = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert patched_once.count(marker) == 1

    # Verify CO2 entities added to all 5 climate rooms
    assert 'co2Entity: "sensor.rosie_co2"' in patched_once
    assert 'co2Entity: "sensor.isaac_co2"' in patched_once
    assert 'co2Entity: "sensor.aranet4_29564_carbon_dioxide"' in patched_once
    assert 'co2Entity: "sensor.bedroom_co2"' in patched_once
    assert 'co2Entity: "sensor.office_co2"' in patched_once

    # Verify bedroom alias is also supported if a fixture uses id: "bedroom"
    bedroom_alias_fixture = REALISTIC_CARD_FIXTURE.replace('id: "primary"', 'id: "bedroom"')
    patched_alias = mod.patch_card_js(bedroom_alias_fixture)
    assert mod.patch_card_js(patched_alias) == patched_alias
    assert 'co2Entity: "sensor.bedroom_co2"' in patched_alias


def test_patch_card_js_runtime_co2_tiers_and_cross_lens_active_badges_in_node() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    mod.validate_js_syntax(patched)

    harness = (
        patched
        + """
function makeMockEl() {
  const classes = new Set();
  const children = {};
  return {
    style: {},
    dataset: {},
    textContent: "",
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : Boolean(force);
        if (on) classes.add(c);
        else classes.delete(c);
        return on;
      },
      has: (c) => classes.has(c),
      toArray: () => Array.from(classes),
    },
    querySelector: (sel) => {
      if (!children[sel]) children[sel] = makeMockEl();
      return children[sel];
    },
  };
}

function makeCardMock() {
  const els = {};
  return {
    els,
    querySelector: (sel) => {
      if (!els[sel]) els[sel] = makeMockEl();
      return els[sel];
    },
  };
}

const card = new BrockHouseCyclesCard();
card._cards = {
  floor3: makeCardMock(),
  floor2: makeCardMock(),
  floor1: makeCardMock(),
};
card._hass = {
  states: {
    "sensor.rosie_co2": { state: "642" },
    "sensor.isaac_co2": { state: "1215" },
    "sensor.aranet4_29564_carbon_dioxide": { state: "1680" },
    "sensor.bedroom_co2": { state: "unavailable" },
    "sensor.office_co2": { state: "815.4" },
    "switch.coffee_machine": { state: "on" },
    "sensor.apolosign_media": { state: "on" },
    "media_player.plex_plex_for_android_tv_google_tv_streamer": { state: "on" },
    "binary_sensor.octoprint_printing": { state: "off" },
    "input_boolean.washer_running_state": { state: "on" },
    "sensor.steam_76561197975996046": { state: "on" },
    "sensor.oven": { state: "heating" },
    "sensor.meater": { state: "cooking" },
  },
};

const html = card._renderSkeleton();

const lenses = ["lights", "climate", "security", "energy", "cameras", "presence"];
const lensResults = {};
for (const lens of lenses) {
  card._activeLens = lens;
  card._thumbsApplied = [];
  card._update();
  lensResults[lens] = {
    f3RunLayerVisible: card._cards.floor3.els[".running-layer"].classList.has("visible"),
    f2RunLayerVisible: card._cards.floor2.els[".running-layer"].classList.has("visible"),
    f1RunLayerVisible: card._cards.floor1.els[".running-layer"].classList.has("visible"),
    coffeeActive: card._cards.floor3.els['.run-badge[data-run-id="coffee"]'].classList.has("active"),
    printerInactive: !card._cards.floor1.els['.run-badge[data-run-id="printer3d"]'].classList.has("active"),
    washerActive: card._cards.floor1.els['.run-badge[data-run-id="washer"]'].classList.has("active"),
    steamActiveOnF2: card._cards.floor2.els['.gaming-badge[data-gaming-id="steam"]'].classList.has("active"),
    xboxInactiveOnF2: !card._cards.floor2.els['.gaming-badge[data-gaming-id="xbox"]'].classList.has("active"),
    ovenActive: card._cards.floor3.els['[data-oven-meater-badge="lg_oven"]'].classList.has("active"),
    meaterActive: card._cards.floor3.els['[data-oven-meater-badge="meater"]'].classList.has("active"),
    thumbsActiveInLens: card._thumbsApplied.filter((t) => t.active).map((t) => t.id),
  };
}

// Verify CO2 tiers populated during climate lens update
const co2Results = {
  rosie: {
    text: card._cards.floor3.els['[data-room-co2="rosie"]'].textContent,
    classes: card._cards.floor3.els['[data-room-co2="rosie"]'].classList.toArray(),
    display: card._cards.floor3.els['[data-room-co2="rosie"]'].style.display,
  },
  isaac: {
    text: card._cards.floor3.els['[data-room-co2="isaac"]'].textContent,
    classes: card._cards.floor3.els['[data-room-co2="isaac"]'].classList.toArray(),
    display: card._cards.floor3.els['[data-room-co2="isaac"]'].style.display,
  },
  living: {
    text: card._cards.floor2.els['[data-room-co2="living"]'].textContent,
    classes: card._cards.floor2.els['[data-room-co2="living"]'].classList.toArray(),
    display: card._cards.floor2.els['[data-room-co2="living"]'].style.display,
  },
  primary: {
    text: card._cards.floor2.els['[data-room-co2="primary"]'].textContent,
    classes: card._cards.floor2.els['[data-room-co2="primary"]'].classList.toArray(),
    display: card._cards.floor2.els['[data-room-co2="primary"]'].style.display,
  },
  office: {
    text: card._cards.floor1.els['[data-room-co2="office"]'].textContent,
    classes: card._cards.floor1.els['[data-room-co2="office"]'].classList.toArray(),
    display: card._cards.floor1.els['[data-room-co2="office"]'].style.display,
  },
};

// Verify sig changes when CO2 state changes
const sig1 = card._update();
card._hass.states["sensor.office_co2"].state = "1120";
const sig2 = card._update();

// Verify .running-layer hides when _activeRoom is set
card._activeRoom = "office";
card._update();
const hiddenWhenZoomed = !card._cards.floor1.els[".running-layer"].classList.has("visible");

console.log(JSON.stringify({
  hasCo2SpansInHtml: ["rosie", "isaac", "living", "primary", "office"].every(
    (id) => html.includes(`<span class="clim-co2" data-room-co2="${id}"></span>`)
  ),
  lensResults,
  co2Results,
  sigChangedOnCo2Update: sig1 !== sig2,
  hiddenWhenZoomed,
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

    assert res["hasCo2SpansInHtml"] is True
    assert res["sigChangedOnCo2Update"] is True
    assert res["hiddenWhenZoomed"] is True

    # Check CO2 tiers: <1000 co2-good, 1000-1499 co2-warn, >=1500 co2-high, null hidden/empty
    assert res["co2Results"]["rosie"] == {"text": "642 ppm", "classes": ["co2-good"], "display": "inline-flex"}
    assert res["co2Results"]["isaac"] == {"text": "1215 ppm", "classes": ["co2-warn"], "display": "inline-flex"}
    assert res["co2Results"]["living"] == {"text": "1680 ppm", "classes": ["co2-high"], "display": "inline-flex"}
    assert res["co2Results"]["primary"] == {"text": "", "classes": [], "display": "none"}
    assert res["co2Results"]["office"] == {"text": "815 ppm", "classes": ["co2-good"], "display": "inline-flex"}

    # Check active contextual badges across ALL lenses
    for lens_name, lres in res["lensResults"].items():
        assert lres["f3RunLayerVisible"] is True, lens_name
        assert lres["f2RunLayerVisible"] is True, lens_name
        assert lres["f1RunLayerVisible"] is True, lens_name
        assert lres["coffeeActive"] is True, lens_name
        assert lres["printerInactive"] is True, lens_name
        assert lres["washerActive"] is True, lens_name
        assert lres["steamActiveOnF2"] is True, lens_name
        assert lres["xboxInactiveOnF2"] is True, lens_name
        assert lres["ovenActive"] is True, lens_name
        assert lres["meaterActive"] is True, lens_name
        assert "apolosign_music" in lres["thumbsActiveInLens"], lens_name
        assert "living_tv" in lres["thumbsActiveInLens"], lens_name


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


def test_dry_run_and_verify_only_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HA_BASE_URL", "http://198.51.100.42:8123")
    monkeypatch.setenv("HA_ACCESS_TOKEN", "test-token")
    backup_file = tmp_path / "backup.js"
    monkeypatch.setattr(mod, "BACKUP_CARD_PATH", backup_file)

    b64_fixture = base64.b64encode(REALISTIC_CARD_FIXTURE.encode("utf-8")).decode("ascii")
    with mock.patch.object(mod.nas_config, "run_ssh", return_value=(0, b64_fixture, "")) as ssh_mock:
        rc = mod.main(["--dry-run"])
    assert rc == 0
    assert backup_file.is_file()
    assert ssh_mock.call_count == 1

    with mock.patch.object(mod, "verify_live_http", return_value=(True, [])):
        assert mod.main(["--verify-only"]) == 0

    with mock.patch.object(mod, "verify_live_http", return_value=(False, ["mismatch"])):
        assert mod.main(["--verify-only"]) == 1
