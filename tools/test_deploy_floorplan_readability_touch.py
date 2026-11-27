"""Unit tests for tools/deploy_floorplan_readability_touch.py."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import deploy_floorplan_readability_touch as mod

CARD_FIXTURE_UNPATCHED = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
class BrockHouseCyclesCard {
  _renderSkeleton() {
    return `
      <style>
        .btn { font-size: 11.5px; padding: 5px 9px; gap: 4px; }
        .lens-btn svg { width: 13px; height: 13px; }
        .title-chip { padding: 4px 10px; }
        .title-main { font-size: 12.5px; }
        .title-sub { font-size: 11.5px; }
        .floor-btn { padding: 5.5px 12px; font-size: 12px; }
        .climate-badge { padding: 4px 8px; }
        .climate-badge-room { font-size: 9px; }
        .climate-badge-temp { font-size: 13px; }
        .climate-badge-hum { font-size: 10px; }
        .run-badge { font-size: 10.5px; padding: 3.5px 8.5px 3.5px 6.5px; }
        .callout { width: 28px; height: 28px; }
        .callout svg { width: 14px; height: 14px; }
        .room-drawer { width: min(340px, calc(100% - 24px)); }
        .rd-close { width: 28px; height: 28px; font-size: 16px; }
        .rd-mini-btn { font-size: 10.5px; padding: 3px 8px; }
        .thermo-modal { width: min(360px, 100%); }
        .tm-step-btn { width: 34px; height: 34px; font-size: 18px; }
        .cook-modal .rd-close { width: 38px; height: 38px; font-size: 22px; border-radius: 11px; }
        .cook-modal .cm-foot .rd-mini-btn { font-size: 13.5px; font-weight: 700; padding: 8px 16px; border-radius: 11px; }
        @media (max-width: 640px) {
          .cook-modal .rd-close { width: 32px; height: 32px; font-size: 19px; }
          .cook-modal .cm-foot .rd-mini-btn { font-size: 12.5px; padding: 7px 12px; }
        }
        @media (min-width: 641px) {
          .cook-modal.kiosk-mode { width: min(94vw, 860px); padding: 24px 26px 26px; }
          .cook-modal.kiosk-mode .cm-title { font-size: 26px; }
          .cook-modal.kiosk-mode .cm-foot .rd-mini-btn { font-size: 16px; padding: 10px 20px; }
        }
        /* ROOM_MUSIC_CSS_BEGIN */
        .stage.exploded .floor-card + .floor-card {
          margin-top: -15.5%;
        }
        .music-group-badge {
          padding: 5px 11px 5px 8px;
        }
        .music-group-badge .mg-label { font-size: 10px; }
        .music-group-badge .mg-track { font-size: 11px; }
        /* ROOM_MUSIC_CSS_END */
      </style>
      <div class="shell">
        <div class="stage" id="stage"></div>
      </div>
    `;
  }
}
"""


def test_patch_card_js_injects_all_surfaces_and_is_strictly_idempotent() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    # Placed immediately after ROOM_MUSIC_CSS_END and before </style>
    room_music_end = patched_once.index("/* ROOM_MUSIC_CSS_END */")
    rt_begin = patched_once.index(mod.CSS_BEGIN_MARKER)
    rt_end = patched_once.index(mod.CSS_END_MARKER)
    style_close = patched_once.index("</style>")
    assert room_music_end < rt_begin < rt_end < style_close

    block = patched_once[rt_begin:rt_end]

    # Top HUD & Floor Dock
    assert ".btn {" in block
    assert "font-size: 14px;" in block
    assert "padding: 9px 14px;" in block
    assert "min-height: 40px;" in block
    assert "gap: 6px;" in block
    assert ".lens-btn svg {" in block
    assert "width: 16px;" in block
    assert ".title-chip {" in block
    assert "padding: 6px 13px;" in block
    assert "font-size: 14.5px;" in block
    assert "font-size: 12.5px;" in block
    assert ".floor-btn {" in block
    assert "padding: 9px 18px;" in block
    assert "min-height: 42px;" in block
    for sel in (
        ".scene-pill",
        ".alert-chip",
        ".climate-summary-btn",
        ".sec-summary-btn",
        ".pres-chip",
        ".cam-bar-chip",
        ".ef-chip",
    ):
        assert sel in block
    assert "min-height: 36px;" in block
    assert ".pres-chip-ava {" in block
    assert "width: 22px;" in block
    assert ".floor-tag {" in block
    assert "font-size: 13px;" in block

    # Floorplan Badges & Pins (single-floor)
    assert "padding: 5.5px 10px;" in block
    assert "font-size: 16px;" in block
    assert ".clim-co2 {" in block
    assert "padding: 2px 6.5px;" in block
    for sel in (".run-badge", ".cam-pill", ".sec-pill", ".energy-pill", ".alert-pin"):
        assert sel in block
    assert "padding: 5.5px 11px 5.5px 8.5px;" in block
    assert "min-height: 28px;" in block
    assert ".pres-pin {" in block
    assert "padding: 5px 12px 5px 6px;" in block
    assert "width: 26px;" in block
    assert ".music-group-badge {" in block
    assert "padding: 8px 14px 8px 9px;" in block
    assert "font-size: 13.5px;" in block
    assert ".callout {" in block
    assert "width: 36px;" in block
    assert "width: 18px;" in block

    # Exploded view & @media (min-width: 820px)
    assert ".stage.exploded .climate-badge {" in block
    assert ".stage.exploded .pres-pin {" in block
    assert ".stage.exploded .callout {" in block
    assert "width: 32px;" in block
    assert "@media (min-width: 820px)" in block
    assert "font-size: 15.5px;" in block
    assert "padding: 5px 10px;" in block

    # Room Drawer
    assert "width: min(410px, calc(100% - 24px));" in block
    assert ".rd-title {" in block
    assert "font-size: 18px;" in block
    assert ".rd-close {" in block
    assert "width: 40px;" in block
    assert "font-size: 22px;" in block
    assert ".rd-toggle," in block
    assert ".rd-pill-btn," in block
    assert ".rd-mini-btn {" in block

    # Modals
    assert "width: min(440px, 100%);" in block
    assert ".tm-title {" in block
    assert "font-size: 20px;" in block
    assert ".tm-cur {" in block
    assert "font-size: 40px;" in block
    assert ".tm-err {" in block
    assert ".tm-mode-btn {" in block
    assert "padding: 12px 6px;" in block
    assert "min-height: 44px;" in block
    assert ".tm-step-btn {" in block
    assert "width: 48px;" in block
    assert ".tc-name {" in block
    assert ".tc-set {" in block
    assert "font-size: 15px;" in block
    assert ".tc-pill {" in block
    assert ".pm-name {" in block
    assert "font-size: 15.5px;" in block
    assert ".pm-ava {" in block
    assert ".pm-ping-btn," in block
    assert ".cp-cam-btn {" in block
    assert "padding: 10px 16px;" in block
    assert ".cp-ring-pill," in block
    assert ".cp-badge-live {" in block

    # Mobile & Portrait
    assert "@media (max-width: 640px)" in block
    assert "padding: 7px 10px;" in block
    assert "padding: 7px 12px;" in block
    assert "min-height: 38px;" in block
    assert "max-height: 55%;" in block
    assert "width: 44px;" in block


def test_cook_monitor_kiosk_rules_preserved_and_not_downgraded() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    rt_begin = patched.index(mod.CSS_BEGIN_MARKER)
    rt_end = patched.index(mod.CSS_END_MARKER)
    rt_block = patched[rt_begin:rt_end]

    # READABILITY_TOUCH_CSS must never target .cook-modal
    assert ".cook-modal" not in rt_block

    # Existing Cook Monitor kiosk & close button rules must remain intact
    assert ".cook-modal.kiosk-mode { width: min(94vw, 860px); padding: 24px 26px 26px; }" in patched
    assert ".cook-modal.kiosk-mode .cm-foot .rd-mini-btn { font-size: 16px; padding: 10px 20px; }" in patched
    assert ".cook-modal .rd-close { width: 38px; height: 38px; font-size: 22px; border-radius: 11px; }" in patched

    # Existing ROOM_MUSIC_CSS stack rules must remain intact
    assert "margin-top: -15.5%;" in patched


def test_patch_replaces_existing_readability_touch_block_cleanly() -> None:
    stale_card = CARD_FIXTURE_UNPATCHED.replace(
        "</style>",
        "        /* READABILITY_TOUCH_CSS_BEGIN */\n        .btn { font-size: 99px; }\n        /* READABILITY_TOUCH_CSS_END */\n      </style>",
    )
    patched = mod.patch_card_js(stale_card)
    assert "font-size: 99px;" not in patched
    assert patched.count(mod.CSS_BEGIN_MARKER) == 1
    assert patched.count(mod.CSS_END_MARKER) == 1
    assert patched == mod.patch_card_js(patched)


def test_verify_patched_card_contents_passes_and_raises_on_unpatched() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    mod.verify_patched_card_contents(patched)

    with pytest.raises(RuntimeError, match="Patched floorplan readability & touch-target verification failed"):
        mod.verify_patched_card_contents(CARD_FIXTURE_UNPATCHED)


def test_patch_card_js_rejects_invalid_js_syntax() -> None:
    broken_js = CARD_FIXTURE_UNPATCHED + "\nclass BrokenSyntax {{{{"
    with pytest.raises(RuntimeError, match="node --check failed"):
        mod.patch_card_js(broken_js)


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
    if ".cook-modal.kiosk-mode" in raw_js:
        assert re.search(r"\.cook-modal\.kiosk-mode\s+\.cm-foot\s+\.rd-mini-btn", patched_once)


def test_dry_run_and_verify_only_cli() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "fetch_live_card_and_backup", return_value=CARD_FIXTURE_UNPATCHED) as mock_fetch,
        mock.patch.object(mod, "deploy_card_to_host1") as mock_deploy,
        mock.patch.object(mod, "update_lovelace_resource_cache_buster") as mock_ws_bust,
    ):
        rc = mod.main(["--dry-run"])
    assert rc == 0
    mock_fetch.assert_called_once_with(mod.BACKUP_CARD_PATH)
    mock_deploy.assert_not_called()
    mock_ws_bust.assert_not_called()

    async def fake_ws(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=123456"}]

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws),
        mock.patch.object(mod, "_http_get", return_value=(200, patched.encode("utf-8"))),
    ):
        rc_verify = mod.main(["--verify-only"])
    assert rc_verify == 0

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws),
        mock.patch.object(mod, "_http_get", return_value=(200, CARD_FIXTURE_UNPATCHED.encode("utf-8"))),
    ):
        rc_verify_fail = mod.main(["--verify-only"])
    assert rc_verify_fail == 1
