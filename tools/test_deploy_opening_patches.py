#!/usr/bin/env python3
"""Unit tests for tools/deploy_opening_patches.py."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import deploy_opening_patches as mod

MINIMAL_CARD_FIXTURE = """/* Brock House Card */
const ASSET_VERSION = "00000000";
const BASE_PATH = "/local/brock-house-3d/cycles";

const FLOORS = [
  {
    id: "floor3",
    night: `${BASE_PATH}/floor3_cycles_evening_base.webp?v=${ASSET_VERSION}`,
    securityItems: [
      { id: "rosie_shades", entity: "cover.curtain_3_31", name: "Rosie Shades", kind: "cover", x: 80.5, y: 49.5 },
      { id: "isaac_shades", entity: "cover.curtain_3_64", name: "Isaac Shades", kind: "cover", x: 73.0, y: 73.0 },
      { id: "kitchen_door", entity: "binary_sensor.kitchen_door_sensor_opening", name: "Kitchen Door", kind: "door", x: 37.0, y: 27.5 },
      { id: "kitchen_win", entity: "binary_sensor.kitchen_window_sensor_1_opening", name: "Kitchen Win", kind: "window", x: 52.0, y: 31.5 },
      { id: "rosie_door", entity: "binary_sensor.rosie_door_sensor_opening", name: "Rosie Door", kind: "door", x: 69.0, y: 58.5 },
    ],
  },
  {
    id: "floor2",
    night: `${BASE_PATH}/floor2_cycles_evening_base.webp?v=${ASSET_VERSION}`,
    securityItems: [
      { id: "living_blinds", entity: "cover.living_room_blinds", name: "Living Blinds", kind: "cover", x: 13.0, y: 41.5 },
    ],
  },
  {
    id: "floor1",
    night: `${BASE_PATH}/floor1_cycles_evening_base.webp?v=${ASSET_VERSION}`,
    securityItems: [
      { id: "front_door", entity: "binary_sensor.lumi_lumi_sensor_magnet_aq2_opening", name: "Front Door", kind: "door", x: 42.5, y: 68.5 },
      { id: "garage_door", entity: "binary_sensor.garage_door_sensor_2_opening", name: "Garage Door", kind: "door", x: 61.0, y: 78.5 },
    ],
  },
];

class BrockHouseCyclesCard extends HTMLElement {
  _computeNightBlend() {
    return 0.5;
  }
  _entityOnAndBrightness(entityId) {
    return { on: false, factor: 0, pct: 0 };
  }
  _renderSkeleton() {
    this.shadowRoot.innerHTML = `
      <style>
        .img-night {
          transition: opacity 0.6s ease;
        }
      </style>
      <div class="media-frame">
        <img class="img-base img-day" src="${f.day}" />
        <img class="img-base img-night" data-src="${f.night}" />
        ${f.lights.map(() => `<img class="img-overlay" />`).join("")}
      </div>
    `;
  }
  _updateState() {
    const nightBlend = this._computeNightBlend();
    for (const f of FLOORS) {
      for (const eo of (f.extraOverlays || [])) {
        const info = this._entityOnAndBrightness(eo.entity);
        sigParts.push(info.on ? info.pct : -1);
      }
      const card = null;
      if (card) {
        const nightImg = card.querySelector(".img-night");
        if (nightImg) {
          if (nightBlend > 0.005) {
            nightImg.style.display = "block";
          } else {
            nightImg.style.display = "none";
          }
        }
      }
    }
  }
}
"""


def test_patch_card_js_is_idempotent_and_wires_all_requirements() -> None:
    patched_once = mod.patch_card_js(MINIMAL_CARD_FIXTURE, "abcd1234")
    patched_twice = mod.patch_card_js(patched_once, "abcd1234")
    assert patched_once == patched_twice
    assert 'const ASSET_VERSION = "abcd1234";' in patched_once

    for fname in mod.ALL_18_PATCHES:
        assert fname in patched_once
    for eid in mod.LIVE_ENTITIES:
        assert eid in patched_once
    for pin_id, (px, py) in mod.PIN_COORDINATE_UPDATES.items():
        assert re.search(rf'id:\s*"{pin_id}"[^\}}]*?\bx:\s*{px:.1f},\s*\by:\s*{py:.1f}\b', patched_once)

    # Layer ordering inside .media-frame: img-night -> .opening-patches -> img-overlay
    idx_night = patched_once.index('<img class="img-base img-night"')
    idx_patches = patched_once.index('<div class="opening-patches"')
    idx_overlay = patched_once.index('<img class="img-overlay"')
    assert idx_night < idx_patches < idx_overlay

    # Rosie's curtains ordered above Rosie's exterior door
    assert patched_once.index('id: "rosie_door_open"') < patched_once.index('id: "rosie_curtains_closed"')
    assert 'z: 1' in patched_once
    assert 'z: 2' in patched_once

    # Cover position < 50 threshold and display:none optimization
    assert "numPos < 50" in patched_once
    assert 'dayPatch.style.display = "none"' in patched_once
    assert 'nightPatch.style.display = "none"' in patched_once


def test_with_cache_buster() -> None:
    assert mod._with_cache_buster("/local/brock-house-3d/brock-house-cycles-card.js?v=old", "new123") == (
        "/local/brock-house-3d/brock-house-cycles-card.js?v=new123"
    )
    assert mod._with_cache_buster("/local/brock-house-3d/brock-house-cycles-card.js", "new123") == (
        "/local/brock-house-3d/brock-house-cycles-card.js?v=new123"
    )


def test_verify_live_http_checks_card_and_all_18_webps() -> None:
    patched = mod.patch_card_js(MINIMAL_CARD_FIXTURE, "abcd1234")
    webp_bytes = b"RIFF\x24\x00\x00\x00WEBP" + (b"\x00" * 2048)

    def fake_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
        if url.endswith(".js") or ".js?v=" in url:
            return 200, patched.encode("utf-8")
        if url.endswith(".webp"):
            return 200, webp_bytes
        return 404, b""

    with (
        mock.patch.object(mod, "_http_get", side_effect=fake_get),
        mock.patch.object(
            mod,
            "_ws_lovelace_resources",
            return_value=[{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=abcd1234"}],
        ),
    ):
        # Need asyncio.run to return the mock value when _ws_lovelace_resources is patched as non-async
        async def fake_ws(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
            return [{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=abcd1234"}]

        with mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws):
            ok, errors = mod.verify_live_http("http://198.51.100.10:8123", "tok")
    assert ok is True
    assert errors == []


def test_verify_live_http_fails_on_non_200() -> None:
    patched = mod.patch_card_js(MINIMAL_CARD_FIXTURE, "abcd1234")
    webp_bytes = b"RIFF\x24\x00\x00\x00WEBP" + (b"\x00" * 2048)

    def fake_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
        if "brock-house-cycles-card.js" in url:
            return 200, patched.encode("utf-8")
        if "floor3_rosie_curtains_closed_night.webp" in url:
            return 404, b""
        return 200, webp_bytes

    with mock.patch.object(mod, "_http_get", side_effect=fake_get):
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
