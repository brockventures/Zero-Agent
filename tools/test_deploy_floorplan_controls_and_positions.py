"""Unit tests for tools/deploy_floorplan_controls_and_positions.py."""

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

from tools import deploy_floorplan_controls_and_positions as mod

CARD_FIXTURE_UNPATCHED = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const CLIMATE_CONFIG = {
  thermostat: "climate.nest_learning_thermostat_4th_gen",
  thermostatFallback: "climate.living_room_thermostat",
  outdoorTemp: "sensor.outdoor_temperature",
};

const CONTEXT_ALERTS_CONFIG = {
  afterDarkItems: [
    { id: "night_garage", floorId: "floor1", kind: "opening", entity: "binary_sensor.garage_door_sensor_2_opening", label: "Garage Open", x: 61.0, y: 78.5 },
  ],
};

const ROOMS = {
  isaac: {
    lights: [{ entity: "light.isaacs_stars", name: "Star Lights" }],
    covers: [{ entity: "cover.curtain_3_64", name: "Isaac's Curtains" }],
    fans: [{ entity: "fan.isaacs_fan", name: "Isaac's Fan" }],
    media: [{ entity: "media_player.isaac_s_speaker", name: "Isaac's Speaker" }],
  },
  living: {
    media: [
      { entity: "media_player.living_room_tv_2", name: "Living Room TV" },
      { entity: "media_player.plex_plex_for_android_tv_google_tv_streamer", name: "Plex Streamer" },
    ],
  },
  garage: {
    alerts: [
      { entity: "binary_sensor.lumi_lumi_sensor_magnet_aq2_opening", label: "Entryway Door Open", severity: "warn" },
      { entity: "binary_sensor.front_door_sensor_opening", label: "Front Door Open", severity: "warn" },
    ],
    lights: [
      { entity: "light.garage_lights", name: "Garage Ceiling" },
      { entity: "light.garage_1", name: "Garage Lamp 1" },
    ],
  },
};

const FLOORS = [
  {
    id: "floor3",
    runningItems: [
      { id: "coffee", kind: "switch", entity: "switch.coffee_machine", label: "Coffee On", icon: "coffee", accent: "amber", x: 46.5, y: 38.0 },
    ],
    securityItems: [
      { id: "rosie_door", entity: "binary_sensor.rosie_door_sensor_opening", name: "Rosie Door", kind: "door", x: 81.5, y: 53.5 },
    ],
  },
  {
    id: "floor2",
    runningItems: [
      { id: "living_tv", kind: "tv", entity: "media_player.plex_plex_for_android_tv_google_tv_streamer", fallbackEntities: ["media_player.living_room_tv_2", "media_player.living_room_tv", "media_player.living_room_living_room_tv", "switch.living_room_tv"], label: "TV", icon: "tv", accent: "blue", x: 26.5, y: 29.5 },
    ],
    energyItems: [
      { id: "living_pwr", name: "Living Room", entity: "sensor.living_room_power_minute_average", unit: "W", x: 30.0, y: 42.5 },
      { id: "hvac_pwr", name: "HVAC", entity: "sensor.ac_power_minute_average", extraEntity: "sensor.furnace_hvac_fan_power_minute_average", unit: "W", x: 48.5, y: 49.0 },
      { id: "primary_pwr", name: "Primary", entity: "sensor.vue_energy_monitor_power_minute_average_3", unit: "W", x: 61.5, y: 68.5 },
    ],
  },
  {
    id: "floor1",
    runningItems: [
      { id: "ev_charger", kind: "ev", entity: "binary_sensor.tesla_wall_connector_contactor_closed", powerEntity: "sensor.tesla_wall_connector_total_power", label: "EV Charging", icon: "bolt", accent: "green", x: 73.5, y: 68.0 },
      { id: "office_tv", kind: "tv", entity: "media_player.office_chromecast", fallbackEntities: ["media_player.office_chromecast_2"], label: "Office TV", icon: "tv", accent: "blue", x: 42.0, y: 44.5 },
    ],
    energyItems: [
      { id: "office_pwr", name: "Office", entity: "sensor.vue_energy_monitor_power_minute_average_2", unit: "W", x: 19.0, y: 52.5 },
      { id: "ev_pwr", name: "EV Charger", kind: "ev", entity: "sensor.tesla_wall_connector_total_power", socEntity: "sensor.my_ev9_ev_battery_level", unit: "kW", x: 73.5, y: 68.0 },
    ],
    securityItems: [
      { id: "garage_door", entity: "binary_sensor.garage_door_sensor_2_opening", name: "Garage Door", kind: "door", x: 61.0, y: 78.5 },
    ],
    lights: [
      { entity: "light.garage_lights", name: "Garage Lights", slug: "floor1_light_garage_lights", bx: 75.0, by: 34.0, tx: 66.5, ty: 63.0, icon: "ceiling" },
    ],
  },
];

class BrockHouseCyclesCard {
  constructor() {
    this._hass = null;
    this._thermoModalOpen = false;
    this.shadowRoot = null;
  }

  _readNumericState(entityId) {
    const st = this._hass?.states?.[entityId];
    if (!st) return null;
    const val = Number(st.state);
    return Number.isNaN(val) ? null : val;
  }

  _getThermostatInfo() {
    const primary = this._hass?.states?.[CLIMATE_CONFIG.thermostat];
    const fallback = this._hass?.states?.[CLIMATE_CONFIG.thermostatFallback];
    const st = primary || fallback;
    const entityId = primary ? CLIMATE_CONFIG.thermostat : CLIMATE_CONFIG.thermostatFallback;
    const hum = this._readNumericState("sensor.nest_learning_thermostat_4th_gen_humidity");
    if (!st) {
      return {
        entityId,
        mode: "off",
        action: "idle",
        current: null,
        target: null,
        low: null,
        high: null,
        hum,
        modes: ["off", "heat", "cool", "heat_cool"],
        minTemp: 50,
        maxTemp: 90,
      };
    }
    const attrs = st.attributes || {};
    return {
      entityId,
      mode: st.state || "off",
      action: attrs.hvac_action || (st.state === "off" ? "off" : "idle"),
      current: typeof attrs.current_temperature === "number" ? attrs.current_temperature : null,
      target: typeof attrs.temperature === "number" ? attrs.temperature : null,
      low: typeof attrs.target_temp_low === "number" ? attrs.target_temp_low : null,
      high: typeof attrs.target_temp_high === "number" ? attrs.target_temp_high : null,
      hum,
      modes: Array.isArray(attrs.hvac_modes) && attrs.hvac_modes.length ? attrs.hvac_modes : ["off", "heat", "cool", "heat_cool"],
      minTemp: typeof attrs.min_temp === "number" ? attrs.min_temp : 50,
      maxTemp: typeof attrs.max_temp === "number" ? attrs.max_temp : 90,
    };
  }

  _formatHvacMode(mode) {
    const map = { heat_cool: "Heat/Cool", heat: "Heat", cool: "Cool", off: "Off", auto: "Auto" };
    return map[mode] || mode;
  }

  _adjustThermostat(field, delta) {
    if (!this._hass) return;
    const th = this._getThermostatInfo();
    if (!th.entityId) return;
    const clamp = (v) => Math.max(th.minTemp, Math.min(th.maxTemp, Math.round(v)));
    if (th.mode === "heat_cool") {
      let low = th.low !== null ? th.low : 68;
      let high = th.high !== null ? th.high : 75;
      if (field === "low") {
        low = clamp(low + delta);
        if (high - low < 2) high = clamp(low + 2);
      } else if (field === "high") {
        high = clamp(high + delta);
        if (high - low < 2) low = clamp(high - 2);
      }
      this._hass.callService("climate", "set_temperature", {
        entity_id: th.entityId,
        target_temp_low: low,
        target_temp_high: high,
      });
    } else if (th.mode === "heat" || th.mode === "cool") {
      const base = th.target !== null ? th.target : th.current !== null ? Math.round(th.current) : 70;
      const next = clamp(base + delta);
      this._hass.callService("climate", "set_temperature", {
        entity_id: th.entityId,
        temperature: next,
      });
    }
  }

  _renderSkeleton() {
    return `
      <style>
        .tm-foot {
          display: flex;
          justify-content: space-between;
        }
      </style>
    `;
  }

  _bindListeners(sr) {
    sr.getElementById("thermoModal")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tm-action]");
      if (!btn) return;
      e.stopPropagation();
      const act = btn.dataset.tmAction;
      if (act === "close") {
        this._thermoModalOpen = false;
        this._renderThermostatModal();
      } else if (act === "mode") {
        const mode = btn.dataset.tmMode;
        const th = this._getThermostatInfo();
        if (mode && th.entityId && this._hass) {
          this._hass.callService("climate", "set_hvac_mode", { entity_id: th.entityId, hvac_mode: mode });
        }
      } else if (act === "step") {
        const field = btn.dataset.tmField;
        const delta = Number(btn.dataset.tmDelta);
        if (!Number.isNaN(delta)) this._adjustThermostat(field, delta);
      }
    });

    sr.getElementById("presenceModal")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-pm-action]");
      if (!btn) return;
      e.stopPropagation();
      const act = btn.dataset.pmAction;
      if (act === "close") {
        this._closePresenceModal();
      } else if (act === "ring") {
          this._ringTag(pmBtn.dataset.presId, pmBtn.dataset.btnEntity);
          return;
        }
    });
  }

  _ringTag(presId, buttonEntity) {
    this._lastRing = { presId, buttonEntity };
  }

  _renderThermostatModal() {
    this._modalRenderCount = (this._modalRenderCount || 0) + 1;
    const sr = this.shadowRoot;
    const modal = sr?.getElementById?.("thermoModal");
    if (!modal) return;
    const th = this._getThermostatInfo();
    const steppersHtml = `<div class="tm-steppers" data-low="${th.low}" data-high="${th.high}" data-target="${th.target}"></div>`;
    modal.innerHTML = `
      <div class="tm-modes" data-mode="${th.mode}" data-action="${th.action}"></div>
      ${steppersHtml}
      <div class="tm-foot">
        <button class="rd-mini-btn" data-tm-action="close">Done</button>
      </div>
    `;
  }
}
"""


def test_patch_card_js_applies_all_fixes_and_is_strictly_idempotent() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    # B) Broken buttons & dead entities
    assert "this._ringTag(btn.dataset.presId, btn.dataset.btnEntity);" in patched_once
    assert "pmBtn" not in patched_once
    assert '"light.garage_lights"' not in patched_once
    assert patched_once.count('"switch.garage_lights"') == 2
    assert '"media_player.living_room_tv_2"' not in patched_once
    assert '"media_player.living_room_tv"' in patched_once
    assert (
        'fallbackEntities: ["media_player.living_room_tv", "media_player.living_room_living_room_tv", "switch.living_room_tv"]'
        in patched_once
    )
    assert '"binary_sensor.front_door_sensor_opening"' not in patched_once
    assert '"binary_sensor.lumi_lumi_sensor_magnet_aq2_opening"' in patched_once
    assert '"fan.isaacs_fan"' not in patched_once
    assert "fans: []," in patched_once

    # C) Badge positions & hvac_pwr relocation from floor2 to floor1
    assert 'id: "coffee", kind: "switch", entity: "switch.coffee_machine", label: "Coffee On", icon: "coffee", accent: "amber", x: 44.5, y: 31.5' in patched_once
    assert 'id: "hvac_pwr", name: "HVAC", entity: "sensor.ac_power_minute_average", extraEntity: "sensor.furnace_hvac_fan_power_minute_average", unit: "W", x: 68.5, y: 36.5' in patched_once
    assert 'id: "ev_charger", kind: "ev", entity: "binary_sensor.tesla_wall_connector_contactor_closed", powerEntity: "sensor.tesla_wall_connector_total_power", label: "EV Charging", icon: "bolt", accent: "green", x: 70.0, y: 76.0' in patched_once
    assert 'id: "ev_pwr", name: "EV Charger", kind: "ev", entity: "sensor.tesla_wall_connector_total_power", socEntity: "sensor.my_ev9_ev_battery_level", unit: "kW", x: 70.0, y: 76.0' in patched_once
    assert 'id: "garage_door", entity: "binary_sensor.garage_door_sensor_2_opening", name: "Garage Door", kind: "door", x: 59.0, y: 79.5' in patched_once
    assert 'id: "night_garage", floorId: "floor1", kind: "opening", entity: "binary_sensor.garage_door_sensor_2_opening", label: "Garage Open", x: 59.0, y: 79.5' in patched_once

    # Verify rosie_door and office_tv remain unchanged
    assert 'id: "rosie_door", entity: "binary_sensor.rosie_door_sensor_opening", name: "Rosie Door", kind: "door", x: 81.5, y: 53.5' in patched_once
    assert 'id: "office_tv", kind: "tv", entity: "media_player.office_chromecast", fallbackEntities: ["media_player.office_chromecast_2"], label: "Office TV", icon: "tv", accent: "blue", x: 42.0, y: 44.5' in patched_once


def test_patch_card_js_on_live_or_cached_scratch_card_if_present() -> None:
    candidates = [
        Path("/workspace/scratch/live_brock_house_cycles_card.js"),
        Path("/workspace/scratch/brock-house-cycles-card.climate-co2-cross-lens.backup.js"),
        Path("/workspace/scratch/brock-house-cycles-card.backup.js"),
    ]
    target_path = next((p for p in candidates if p.is_file()), None)
    if target_path is None:
        pytest.skip("No cached brock-house-cycles-card.js found in /workspace/scratch")
    raw_js = target_path.read_text(encoding="utf-8")
    patched_once = mod.patch_card_js(raw_js)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice
    mod.verify_patched_card_contents(patched_once)


def test_verify_patched_card_contents_passes_and_raises_on_unpatched() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    mod.verify_patched_card_contents(patched)

    with pytest.raises(RuntimeError, match="Patched floorplan controls & positions verification failed"):
        mod.verify_patched_card_contents(CARD_FIXTURE_UNPATCHED)


def test_thermostat_double_tap_3f_deadband_hvac_action_fallback_and_error_handling() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    harness = (
        patched
        + """
const serviceCalls = [];
let shouldRejectNextCall = false;

const mockModal = { innerHTML: "" };
const mockShadowRoot = {
  getElementById: (id) => (id === "thermoModal" ? mockModal : null),
};

const card = new BrockHouseCyclesCard();
card.shadowRoot = mockShadowRoot;
card._hass = {
  states: {
    // Primary Matter entity lacks hvac_action; starts at 70..73 in heat_cool
    "climate.nest_learning_thermostat_4th_gen": {
      state: "heat_cool",
      attributes: {
        current_temperature: 69,
        target_temp_low: 70,
        target_temp_high: 73,
        hvac_modes: ["off", "heat", "cool", "heat_cool"],
        min_temp: 50,
        max_temp: 90,
      },
    },
    // Fallback SDM entity provides hvac_action: "heating"
    "climate.living_room_thermostat": {
      state: "heat_cool",
      attributes: {
        hvac_action: "heating",
      },
    },
  },
  callService: (domain, service, data) => {
    serviceCalls.push({ domain, service, data });
    if (shouldRejectNextCall) {
      shouldRejectNextCall = false;
      return Promise.reject(new Error("Nest rate limit exceeded"));
    }
    return Promise.resolve();
  },
};

// 1. Verify fallback hvac_action is read when primary lacks hvac_action
const info0 = card._getThermostatInfo();

// 2. Rapid double-tap +1 on low in heat_cool mode (without HA state mutating in between)
// Start: low=70, high=73 -> tap 1: low=71, high=74 -> tap 2: low=72, high=75 (3F deadband)
card._adjustThermostat("low", 1);
const infoAfterTap1 = card._getThermostatInfo();
card._adjustThermostat("low", 1);
const infoAfterTap2 = card._getThermostatInfo();

// 3. Test service rejection clears optimistic state and renders .tm-err
shouldRejectNextCall = true;
card._adjustThermostat("low", 1);

setTimeout(() => {
  const infoAfterReject = card._getThermostatInfo();
  const hasErrBanner = mockModal.innerHTML.includes("tm-err") && mockModal.innerHTML.includes("Nest rate limit exceeded");

  //Verify floor1 vs floor2 hvac_pwr placement at runtime
  const f2 = FLOORS.find((f) => f.id === "floor2");
  const f1 = FLOORS.find((f) => f.id === "floor1");
  const f2HasHvac = (f2.energyItems || []).some((i) => i.id === "hvac_pwr");
  const f1Hvac = (f1.energyItems || []).find((i) => i.id === "hvac_pwr");

  console.log(
    JSON.stringify({
      initialAction: info0.action,
      tap1Low: infoAfterTap1.low,
      tap1High: infoAfterTap1.high,
      tap2Low: infoAfterTap2.low,
      tap2High: infoAfterTap2.high,
      serviceCalls,
      revertLowAfterReject: infoAfterReject.low,
      revertHighAfterReject: infoAfterReject.high,
      hasErrBanner,
      f2HasHvac,
      f1Hvac,
    })
  );
}, 20);
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

    assert res["initialAction"] == "heating"
    assert res["tap1Low"] == 71
    assert res["tap1High"] == 74
    assert res["tap2Low"] == 72
    assert res["tap2High"] == 75
    assert res["serviceCalls"][0]["data"] == {
        "entity_id": "climate.nest_learning_thermostat_4th_gen",
        "target_temp_low": 71,
        "target_temp_high": 74,
    }
    assert res["serviceCalls"][1]["data"] == {
        "entity_id": "climate.nest_learning_thermostat_4th_gen",
        "target_temp_low": 72,
        "target_temp_high": 75,
    }
    # After rejection of 3rd tap, optimistic overlay is cleared and state reverts to HA entity state (70/73)
    assert res["revertLowAfterReject"] == 70
    assert res["revertHighAfterReject"] == 73
    assert res["hasErrBanner"] is True
    assert res["f2HasHvac"] is False
    assert res["f1Hvac"]["x"] == 68.5
    assert res["f1Hvac"]["y"] == 36.5


def test_dry_run_and_verify_only_cli() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "fetch_live_card_and_backup", return_value=CARD_FIXTURE_UNPATCHED) as mock_fetch,
        mock.patch.object(mod, "deploy_card_to_host1") as mock_deploy,
    ):
        rc = mod.main(["--dry-run"])
    assert rc == 0
    mock_fetch.assert_called_once_with(mod.BACKUP_CARD_PATH)
    mock_deploy.assert_not_called()

    async def fake_ws(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=123456"}]

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws),
        mock.patch.object(mod, "_http_get", return_value=(200, patched.encode("utf-8"))),
    ):
        rc_verify = mod.main(["--verify-only"])
    assert rc_verify == 0
