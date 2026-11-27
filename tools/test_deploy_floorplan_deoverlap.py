"""Unit tests for tools/deploy_floorplan_deoverlap.py."""

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

from tools import deploy_floorplan_deoverlap as mod

CARD_FIXTURE_UNPATCHED = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const OVEN_MEATER_CONFIG = {
  floorId: "floor3",
  hotspot: { x: 57.5, y: 45.0 },
  ovenBadgePos: { x: 57.0, y: 42.0 },
  meaterBadgePos: { x: 58.5, y: 46.5 },
};

const FLOORS = [
  {
    id: "floor3",
    securityItems: [
      { id: "rosie_shades", entity: "cover.curtain_3_31", name: "Rosie Shades", kind: "cover", x: 80.5, y: 49.5 },
      { id: "rosie_door", entity: "binary_sensor.rosie_door_sensor_opening", name: "Rosie Door", kind: "door", x: 82.0, y: 51.0 },
    ],
  },
  {
    id: "floor1",
    runningItems: [
      { id: "washer", kind: "appliance", entity: "input_boolean.washer_running_state", powerEntity: "sensor.washer_net_power", label: "Washer", icon: "laundry", accent: "cyan", x: 65.5, y: 49.0 },
      { id: "dryer", kind: "appliance", entity: "input_boolean.dryer_running_state", powerEntity: "sensor.dryer_power_minute_average", label: "Dryer", icon: "laundry", accent: "amber", x: 69.5, y: 52.0 },
    ],
    energyItems: [
      { id: "washer_pwr", name: "Washer", entity: "sensor.washer_net_power", unit: "W", x: 65.0, y: 49.0 },
      { id: "dryer_pwr", name: "Dryer", entity: "sensor.dryer_power_minute_average", unit: "W", x: 69.5, y: 53.5 },
    ],
  },
];

class BrockHouseCyclesCard {
  constructor() {
    this._activeLens = "energy";
    this._showControls = true;
    this.shadowRoot = null;
  }

  connectedCallback() {
    this._syncKioskMode();
  }

  _syncKioskMode() {}

  _renderSkeleton() {
    const tpl = `
      <style>
        .run-badge {
          position: absolute;
          transform: translate(-50%, -50%);
        }
      </style>
      <div class="stage"></div>
    `;
    return tpl;
  }

  _bindListeners(sr) {
    /* OVEN_MEATER_LISTENERS_BEGIN */
    sr.getElementById?.("cookBackdrop")?.addEventListener?.("click", () => {});
    /* OVEN_MEATER_LISTENERS_END */
  }

  _updateState() {
    this._stateUpdatedCount = (this._stateUpdatedCount || 0) + 1;
  }
}
"""

CARD_FIXTURE_FALLBACK_ANCHOR = """/* Brock House 4K Photorealistic Cycles Floorplan Card (Fallback Anchor) */
class BrockHouseCyclesCard {
  constructor() {
    this._activeLens = "lights";
    this.shadowRoot = null;
  }

  connectedCallback() {
    this._syncKioskMode();
  }

  _syncKioskMode() {}

  _renderSkeleton() {
    return `<style>.run-badge { position: absolute; }</style>`;
  }

  _updateState() {}
}
"""


def test_patch_card_js_updates_all_static_coords_injects_markers_and_is_idempotent() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    # Verify all 3 static coordinate groups updated cleanly:
    # 1. 3F Security
    assert '{ id: "rosie_shades", entity: "cover.curtain_3_31", name: "Rosie Shades", kind: "cover", x: 80.5, y: 44.5 }' in patched_once
    assert '{ id: "rosie_door", entity: "binary_sensor.rosie_door_sensor_opening", name: "Rosie Door", kind: "door", x: 81.5, y: 53.5 }' in patched_once
    # 2. 1F Laundry (runningItems + energyItems)
    assert 'id: "washer", kind: "appliance", entity: "input_boolean.washer_running_state", powerEntity: "sensor.washer_net_power", label: "Washer", icon: "laundry", accent: "cyan", x: 63.5, y: 46.0' in patched_once
    assert 'id: "dryer", kind: "appliance", entity: "input_boolean.dryer_running_state", powerEntity: "sensor.dryer_power_minute_average", label: "Dryer", icon: "laundry", accent: "amber", x: 70.5, y: 54.0' in patched_once
    assert '{ id: "washer_pwr", name: "Washer", entity: "sensor.washer_net_power", unit: "W", x: 63.5, y: 46.0 }' in patched_once
    assert '{ id: "dryer_pwr", name: "Dryer", entity: "sensor.dryer_power_minute_average", unit: "W", x: 70.5, y: 54.0 }' in patched_once
    # 3. 3F Oven / MEATER+
    assert "ovenBadgePos: { x: 56.5, y: 40.5 }" in patched_once
    assert "meaterBadgePos: { x: 59.5, y: 48.0 }" in patched_once

    # Anchor placement checks
    css_end_idx = patched_once.index(mod.CSS_END_MARKER)
    style_close_idx = patched_once.index("</style>")
    assert css_end_idx < style_close_idx

    oven_end_idx = patched_once.index("/* OVEN_MEATER_LISTENERS_END */")
    engine_begin_idx = patched_once.index(mod.ENGINE_BEGIN_MARKER)
    assert engine_begin_idx > oven_end_idx


def test_patch_card_js_fallback_anchor_in_connected_callback() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_FALLBACK_ANCHOR)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    cc_idx = patched_once.index("connectedCallback() {")
    engine_idx = patched_once.index(mod.ENGINE_BEGIN_MARKER)
    sync_idx = patched_once.index("this._syncKioskMode();")
    assert cc_idx < engine_idx < sync_idx


def test_patch_card_js_on_hearth_swipe_backup_if_present() -> None:
    backup_path = Path("/workspace/scratch/brock-house-cycles-card.hearth-swipe.backup.js")
    if not backup_path.is_file():
        pytest.skip(f"{backup_path} not present on this machine")
    raw_js = backup_path.read_text(encoding="utf-8")
    patched_once = mod.patch_card_js(raw_js)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice
    mod.verify_patched_card_contents(patched_once)


def test_verify_patched_card_contents_passes_and_raises_on_unpatched() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    mod.verify_patched_card_contents(patched)

    with pytest.raises(RuntimeError, match="Patched floorplan de-overlap verification failed"):
        mod.verify_patched_card_contents(CARD_FIXTURE_UNPATCHED)


def test_deoverlap_engine_runtime_washer_nudge_and_cook_hotspot_hide() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    harness = (
        patched
        + """
function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      const shouldAdd = force === undefined ? !set.has(c) : Boolean(force);
      if (shouldAdd) set.add(c);
      else set.delete(c);
      return shouldAdd;
    },
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  };
}

function makeStyle() {
  const props = {};
  return {
    transition: "",
    setProperty: (k, v) => {
      props[k] = String(v);
    },
    getPropertyValue: (k) => props[k] || "",
    _props: props,
  };
}

function makeRect(left, top, width, height) {
  return {
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
  };
}

// Floor 1 card: Washer Energy pill at (600, 400, 110x28) & overlapping Washer active run-badge at (600, 400, 110x28)
const washerEnergyPill = {
  dataset: { energyId: "washer_pwr" },
  classList: makeClassList(["energy-pill"]),
  style: makeStyle(),
  getBoundingClientRect: () => makeRect(600, 400, 110, 28),
};

const washerRunBadge = {
  dataset: { runId: "washer" },
  classList: makeClassList(["run-badge", "active"]),
  style: makeStyle(),
  getBoundingClientRect: () => {
    const dx = parseFloat(washerRunBadge.style.getPropertyValue("--deoverlap-x") || "0") || 0;
    const dy = parseFloat(washerRunBadge.style.getPropertyValue("--deoverlap-y") || "0") || 0;
    return makeRect(600 + dx, 400 + dy, 110, 28);
  },
};

const floor1Card = {
  classList: makeClassList(["floor-card", "active"]),
  getBoundingClientRect: () => makeRect(0, 0, 1200, 800),
  querySelector: (sel) => {
    if (sel.includes(".oven-badge.active") || sel.includes(".meater-badge.active")) return null;
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel === ".cook-hotspot") return [];
    if (sel === ".running-layer .run-badge") return [washerRunBadge];
    if (sel === ".running-layer .run-badge.active") return [washerRunBadge];
    if (sel === ".energy-pill") return [washerEnergyPill];
    return [];
  },
};

// Floor 3 card: Cook hotspot + LG Oven badge (toggled active -> idle)
const cookHotspot = {
  classList: makeClassList(["cook-hotspot"]),
  style: makeStyle(),
  getBoundingClientRect: () => makeRect(550, 350, 44, 44),
};

const lgOvenBadge = {
  dataset: { ovenMeaterBadge: "lg_oven" },
  classList: makeClassList(["run-badge", "oven-badge", "active"]),
  style: makeStyle(),
  getBoundingClientRect: () => {
    const dx = parseFloat(lgOvenBadge.style.getPropertyValue("--deoverlap-x") || "0") || 0;
    const dy = parseFloat(lgOvenBadge.style.getPropertyValue("--deoverlap-y") || "0") || 0;
    return makeRect(540 + dx, 330 + dy, 120, 28);
  },
};

const floor3Card = {
  classList: makeClassList(["floor-card", "active"]),
  getBoundingClientRect: () => makeRect(0, 0, 1200, 800),
  querySelector: (sel) => {
    if (
      (sel.includes(".oven-badge.active") || sel.includes(".meater-badge.active")) &&
      lgOvenBadge.classList.contains("active")
    ) {
      return lgOvenBadge;
    }
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel === ".cook-hotspot") return [cookHotspot];
    if (sel === ".running-layer .run-badge") return [lgOvenBadge];
    if (sel === ".running-layer .run-badge.active") {
      return lgOvenBadge.classList.contains("active") ? [lgOvenBadge] : [];
    }
    return [];
  },
};

let resizeCallback = null;
globalThis.ResizeObserver = class {
  constructor(cb) {
    resizeCallback = cb;
  }
  observe() {}
  disconnect() {}
};
globalThis.requestAnimationFrame = (fn) => {
  fn();
  return 1;
};

const mockShadowRoot = {
  querySelectorAll: (sel) => {
    if (sel === ".floor-card.active" || sel === ".stage, .floor-card") {
      return [floor1Card, floor3Card];
    }
    return [];
  },
};

const card = new BrockHouseCyclesCard();
card._activeLens = "energy";
card.shadowRoot = mockShadowRoot;
card._bindListeners(mockShadowRoot);
card._bindListeners(mockShadowRoot); // idempotent guard check

// Trigger updateState -> runs _deoverlapActiveBadges
card._updateState();

const washerDx = parseFloat(washerRunBadge.style.getPropertyValue("--deoverlap-x") || "0");
const washerDy = parseFloat(washerRunBadge.style.getPropertyValue("--deoverlap-y") || "0");
const washerFinalRect = washerRunBadge.getBoundingClientRect();
const pillRect = washerEnergyPill.getBoundingClientRect();

const overlapX = Math.max(
  0,
  Math.min(washerFinalRect.right, pillRect.right) - Math.max(washerFinalRect.left, pillRect.left)
);
const overlapY = Math.max(
  0,
  Math.min(washerFinalRect.bottom, pillRect.bottom) - Math.max(washerFinalRect.top, pillRect.top)
);
const overlapAreaAfterNudge = overlapX * overlapY;
const hotspotHiddenWhenOvenActive = cookHotspot.classList.contains("cook-hotspot-hidden");

// Now mark LG Oven inactive and re-run -> cook-hotspot-hidden should be removed
lgOvenBadge.classList.remove("active");
card._updateState();
const hotspotHiddenWhenOvenIdle = cookHotspot.classList.contains("cook-hotspot-hidden");

console.log(
  JSON.stringify({
    deoverlapBound: card._deoverlapBound,
    hasResizeObserver: Boolean(resizeCallback),
    stateUpdatedCount: card._stateUpdatedCount,
    washerDx,
    washerDy,
    overlapAreaAfterNudge,
    hotspotHiddenWhenOvenActive,
    hotspotHiddenWhenOvenIdle,
  })
);
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

    assert res["deoverlapBound"] is True
    assert res["hasResizeObserver"] is True
    assert res["stateUpdatedCount"] == 2
    assert (res["washerDx"] != 0) or (res["washerDy"] != 0)
    assert res["overlapAreaAfterNudge"] == 0
    assert res["hotspotHiddenWhenOvenActive"] is True
    assert res["hotspotHiddenWhenOvenIdle"] is False


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
