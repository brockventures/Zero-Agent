"""Unit tests for tools/deploy_lamp_glow_tune.py."""

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

from tools import deploy_lamp_glow_tune as mod

REALISTIC_CARD_FIXTURE = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const BASE_PATH = "/local/brock-house-3d";
const ASSET_VERSION = "4a92eae1";

const FLOORS = [
  {
    id: "floor2",
    shortLabel: "2F",
    label: "Second Floor",
    lights: [
      { entity: "light.living_room_lights", name: "Living Room", slug: "floor2_light_living_room_lights", bx: 16.5, by: 23.0, tx: 32.0, ty: 41.5, icon: "ceiling" },
      { entity: "light.stairs_2", name: "Stairs", slug: "floor2_light_stairs_lights", bx: 57.0, by: 23.5, tx: 53.9, ty: 40.7, icon: "ceiling", visibleFixture: true, glowBoost: "strong", glowRx: 15.5, glowRy: 21.0 },
      { entity: "light.living_room_lantern", name: "Lantern", slug: "floor2_light_living_room_lantern", bx: 28.0, by: 67.5, tx: 38.1, ty: 56.7, icon: "lamp", glowBoost: "strong", glowRx: 14.5, glowRy: 19.5 },
      { entity: "light.ryan_s_lamp", name: "Ryan's Lamp", slug: "floor2_light_ryan_lamp", bx: 43.0, by: 76.5, tx: 50.3, ty: 68.1, icon: "lamp" },
    ],
  },
  {
    id: "floor1",
    shortLabel: "1F",
    label: "First Floor",
    extraOverlays: [
      { entity: "light.stairs_2", slug: "floor1_light_stairs_lights", tx: 53.5, ty: 44.0, glowBoost: "strong", glowRx: 13.5, glowRy: 18.5 },
      { entity: "light.custom_overlay", slug: "floor1_light_custom", tx: 40.0, ty: 50.0 },
    ],
    lights: [
      { entity: "light.office_light", name: "Office Lamp", slug: "floor1_light_office_lamp", bx: 5.8, by: 37.5, tx: 12.7, ty: 46.2, icon: "lamp" },
    ],
  },
];

class BrockHouseCyclesCard {
  constructor() {
    this._hass = { states: {} };
    this._isDayTheme = false;
    this._cards = {};
  }

  _entityOnAndBrightness(entityId) {
    const st = this._hass?.states?.[entityId];
    if (!st || st.state !== "on") return { on: false, factor: 0, pct: 0 };
    const bri = st.attributes?.brightness ?? 255;
    const factor = Math.max(0.1, Math.min(1, bri / 255));
    return { on: true, factor, pct: Math.round(factor * 100) };
  }

  _renderSkeleton() {
    return `
      <style>
        .img-overlay {
          mix-blend-mode: screen;
          opacity: 0;
          display: none;
          transition: opacity 0.32s ease;
          filter: saturate(1.32) brightness(1.16);
        }
        .img-overlay.glow-strong {
          filter: saturate(1.68) brightness(1.38);
        }
        .lamp-glow {
          position: absolute;
          transform: translate(-50%, -50%);
          border-radius: 50%;
          pointer-events: none;
          display: none;
          opacity: 0;
          mix-blend-mode: screen;
          background: radial-gradient(
            circle at center,
            rgba(255, 246, 206, 0.96) 0%,
            rgba(255, 204, 88, 0.72) 22%,
            rgba(255, 166, 38, 0.34) 52%,
            rgba(255, 146, 24, 0.0) 78%
          );
          transition: opacity 0.32s ease;
        }
        .lamp-glow.glow-strong {
          background: radial-gradient(
            circle at center,
            rgba(255, 250, 224, 1.0) 0%,
            rgba(255, 212, 92, 0.90) 24%,
            rgba(255, 168, 38, 0.52) 54%,
            rgba(255, 140, 20, 0.0) 82%
          );
        }
        .shell.day-theme .lamp-glow {
          mix-blend-mode: normal;
          background: radial-gradient(
            circle at center,
            rgba(255, 244, 186, 0.95) 0%,
            rgba(255, 194, 62, 0.68) 26%,
            rgba(245, 158, 11, 0.30) 56%,
            rgba(245, 158, 11, 0.0) 80%
          );
        }
        .shell.day-theme .lamp-glow.glow-strong {
          background: radial-gradient(
            circle at center,
            rgba(255, 248, 204, 1.0) 0%,
            rgba(255, 196, 54, 0.88) 28%,
            rgba(245, 152, 12, 0.48) 58%,
            rgba(245, 152, 12, 0.0) 84%
          );
        }
      </style>
      ${FLOORS.map(
        (f) => `
        <div class="floor-card" data-floor-card="${f.id}">
          <div class="media">
            ${f.lights
              .map(
                (l) => {
                  const isStrong = l.glowBoost === "strong";
                  const rx = l.glowRx || (l.icon === "lamp" ? 10.5 : 9.0);
                  const ry = l.glowRy || (l.icon === "lamp" ? 14.0 : 12.0);
                  return `
              <img class="img-overlay ${isStrong ? "glow-strong" : ""}" data-entity="${l.entity}" data-src="${BASE_PATH}/${l.slug}.webp?v=${ASSET_VERSION}" alt="" decoding="async" draggable="false" />
              <div class="lamp-glow ${isStrong ? "glow-strong" : ""}" data-glow-entity="${l.entity}" data-glow-kind="${isStrong ? "strong" : l.icon}" style="left:${l.tx}%;top:${l.ty}%;width:${rx}%;height:${ry}%;"></div>
            `;
                }
              )
              .join("")}
            ${(f.extraOverlays || [])
              .map(
                (ov) => {
                  const isStrong = ov.glowBoost === "strong";
                  const glowDiv = ov.tx != null && ov.ty != null
                    ? `<div class="lamp-glow ${isStrong ? "glow-strong" : ""}" data-extra-glow="${ov.slug}" data-glow-kind="${isStrong ? "strong" : "lamp"}" style="left:${ov.tx}%;top:${ov.ty}%;width:${ov.glowRx || 13}%;height:${ov.glowRy || 18}%;"></div>`
                    : "";
                  return `
              <img class="img-overlay ${isStrong ? "glow-strong" : ""}" data-extra-entity="${ov.entity}" data-extra-slug="${ov.slug}" data-src="${BASE_PATH}/${ov.slug}.webp?v=${ASSET_VERSION}" alt="" decoding="async" draggable="false" />
              ${glowDiv}
            `;
                }
              )
              .join("")}
          </div>
        </div>
      `
      ).join("")}
    `;
  }

  _updateLights() {
    const isDayTheme = this._isDayTheme;
    const overlayScale = isDayTheme ? 0.92 : 0.85;
    for (const f of FLOORS) {
      const card = this._cards[f.id];
      for (const l of f.lights) {
        const info = this._entityOnAndBrightness(l.entity);
        const isStrongGlow = l.glowBoost === "strong";
        const glowEl = card?.querySelector(`.lamp-glow[data-glow-entity="${l.entity}"]`);
        if (glowEl) {
          if (info.on) {
            glowEl.style.display = "block";
            const baseGain = isStrongGlow
              ? (isDayTheme ? 0.94 : 0.88)
              : l.icon === "lamp"
              ? (isDayTheme ? 0.72 : 0.62)
              : (isDayTheme ? 0.42 : 0.28);
            glowEl.style.opacity = String(Math.min(1, info.factor * baseGain).toFixed(3));
          } else {
            glowEl.style.opacity = "0";
            glowEl.style.display = "none";
          }
        }
      }

      for (const eo of (f.extraOverlays || [])) {
        const info = this._entityOnAndBrightness(eo.entity);
        const isStrongEo = eo.glowBoost === "strong";
        const eoGlow = card?.querySelector(`.lamp-glow[data-extra-glow="${eo.slug}"]`);
        if (eoGlow) {
          if (info.on) {
            eoGlow.style.display = "block";
            const baseGain = isStrongEo ? (isDayTheme ? 0.92 : 0.86) : 0.55;
            eoGlow.style.opacity = String(Math.min(1, info.factor * baseGain).toFixed(3));
          } else {
            eoGlow.style.opacity = "0";
            eoGlow.style.display = "none";
          }
        }
      }
    }
    return overlayScale;
  }
}
"""


def test_patch_card_js_replacements_and_idempotency() -> None:
    patched_once = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert patched_once.count(marker) == 1

    # Old aggressive radii and opacities must be gone
    for old_val in (
        "glowRx: 15.5, glowRy: 21.0",
        "glowRx: 14.5, glowRy: 19.5",
        "glowRx: 13.5, glowRy: 18.5",
        'l.icon === "lamp" ? 10.5 : 9.0',
        'l.icon === "lamp" ? 14.0 : 12.0',
        "ov.glowRx || 13",
        "ov.glowRy || 18",
        "filter: saturate(1.68) brightness(1.38);",
        "rgba(255, 250, 224, 1.0) 0%",
        "rgba(255, 248, 204, 1.0) 0%",
        "? (isDayTheme ? 0.94 : 0.88)",
        "isStrongEo ? (isDayTheme ? 0.92 : 0.86) : 0.55",
    ):
        assert old_val not in patched_once


def test_patch_card_js_runtime_geometry_and_opacities_in_node() -> None:
    patched = mod.patch_card_js(REALISTIC_CARD_FIXTURE)
    mod.validate_js_syntax(patched)

    harness = (
        patched
        + """
function makeGlowMock() {
  const els = {};
  return {
    els,
    querySelector: (sel) => {
      if (!els[sel]) els[sel] = { style: {} };
      return els[sel];
    },
  };
}

const card = new BrockHouseCyclesCard();
card._cards = {
  floor2: makeGlowMock(),
  floor1: makeGlowMock(),
};
card._hass = {
  states: {
    "light.living_room_lights": { state: "on", attributes: { brightness: 255 } },
    "light.stairs_2": { state: "on", attributes: { brightness: 255 } },
    "light.living_room_lantern": { state: "on", attributes: { brightness: 255 } },
    "light.ryan_s_lamp": { state: "on", attributes: { brightness: 255 } },
    "light.custom_overlay": { state: "on", attributes: { brightness: 255 } },
  },
};

const html = card._renderSkeleton();

card._isDayTheme = false;
card._updateLights();
const nightOpacities = {
  ceiling: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.living_room_lights"]'].style.opacity,
  strongStairs2: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.stairs_2"]'].style.opacity,
  lamp: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.ryan_s_lamp"]'].style.opacity,
  extraStrongStairs1: card._cards.floor1.els['.lamp-glow[data-extra-glow="floor1_light_stairs_lights"]'].style.opacity,
  extraNormal: card._cards.floor1.els['.lamp-glow[data-extra-glow="floor1_light_custom"]'].style.opacity,
};

card._isDayTheme = true;
card._updateLights();
const dayOpacities = {
  ceiling: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.living_room_lights"]'].style.opacity,
  strongStairs2: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.stairs_2"]'].style.opacity,
  lamp: card._cards.floor2.els['.lamp-glow[data-glow-entity="light.ryan_s_lamp"]'].style.opacity,
  extraStrongStairs1: card._cards.floor1.els['.lamp-glow[data-extra-glow="floor1_light_stairs_lights"]'].style.opacity,
  extraNormal: card._cards.floor1.els['.lamp-glow[data-extra-glow="floor1_light_custom"]'].style.opacity,
};

console.log(JSON.stringify({
  htmlChecks: {
    f2Stairs: html.includes('data-glow-entity="light.stairs_2" data-glow-kind="strong" style="left:53.9%;top:40.7%;width:8%;height:10.5%;"'),
    f2Lantern: html.includes('data-glow-entity="light.living_room_lantern" data-glow-kind="strong" style="left:38.1%;top:56.7%;width:7.5%;height:10%;"'),
    f2CeilingDefault: html.includes('data-glow-entity="light.living_room_lights" data-glow-kind="ceiling" style="left:32%;top:41.5%;width:6%;height:8%;"'),
    f2LampDefault: html.includes('data-glow-entity="light.ryan_s_lamp" data-glow-kind="lamp" style="left:50.3%;top:68.1%;width:7%;height:9.5%;"'),
    f1ExtraStairs: html.includes('data-extra-glow="floor1_light_stairs_lights" data-glow-kind="strong" style="left:53.5%;top:44%;width:7.5%;height:9.5%;"'),
    f1ExtraFallback: html.includes('data-extra-glow="floor1_light_custom" data-glow-kind="lamp" style="left:40%;top:50%;width:7.5%;height:9.5%;"'),
  },
  nightOpacities,
  dayOpacities,
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

    assert all(res["htmlChecks"].values()), res["htmlChecks"]
    assert res["nightOpacities"] == {
        "ceiling": "0.200",
        "strongStairs2": "0.540",
        "lamp": "0.440",
        "extraStrongStairs1": "0.520",
        "extraNormal": "0.340",
    }
    assert res["dayOpacities"] == {
        "ceiling": "0.260",
        "strongStairs2": "0.480",
        "lamp": "0.420",
        "extraStrongStairs1": "0.460",
        "extraNormal": "0.340",
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
