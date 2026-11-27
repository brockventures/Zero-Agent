"""Deploy 3D floorplan readability and touch-target sizing upgrade to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.readability-touch.backup.js.
2. Idempotently patch brock-house-cycles-card.js with marker-delimited CSS
   (/* READABILITY_TOUCH_CSS_BEGIN */ .. /* READABILITY_TOUCH_CSS_END */)
   immediately before </style>, upgrading typography and touch targets across:
   - Top HUD bars & bottom floor dock (.btn, .lens-btn svg, .title-chip, .floor-btn,
     summary/scene/alert/presence/camera/energy chips, .floor-tag)
   - Floorplan badges & pins in single-floor and .stage.exploded views (including
     @media (min-width: 820px) exploded desktop bump)
   - Room drawer (.room-drawer, .rd-*, preserving .cook-modal.kiosk-mode and
     .cook-modal .rd-close higher-specificity rules)
   - Thermostat, Presence, and Camera PiP modals
   - Mobile responsiveness (@media (max-width: 640px) and portrait drawer sheet)
3. Validate patched JS syntax via `node --check`.
4. Deploy updated JS card to Host 1 Volume 2 NVMe, bump the HA Lovelace
   resource cache-buster via WebSocket, and verify HTTP 200 and live content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools.deploy_oven_meater_popup import (
    _http_get,
    _replace_or_insert_block,
    _ws_lovelace_resources,
    deploy_card_to_host1,
    fetch_live_card_and_backup,
    resolve_ha_credentials,
    update_lovelace_resource_cache_buster,
    validate_js_syntax,
)

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.readability-touch.backup.js")

CSS_BEGIN_MARKER = "/* READABILITY_TOUCH_CSS_BEGIN */"
CSS_END_MARKER = "/* READABILITY_TOUCH_CSS_END */"

REQUIRED_MARKERS: tuple[str, ...] = (
    CSS_BEGIN_MARKER,
    CSS_END_MARKER,
)

READABILITY_TOUCH_CSS = """        /* READABILITY_TOUCH_CSS_BEGIN */
        /* Top HUD & Floor Dock */
        .btn {
          font-size: 14px;
          padding: 9px 14px;
          min-height: 40px;
          gap: 6px;
        }
        .lens-btn svg {
          width: 16px;
          height: 16px;
        }
        .title-chip {
          padding: 6px 13px;
        }
        .title-main {
          font-size: 14.5px;
        }
        .title-sub {
          font-size: 12.5px;
        }
        .floor-btn {
          font-size: 14.5px;
          padding: 9px 18px;
          min-height: 42px;
        }
        .scene-pill,
        .alert-chip,
        .climate-summary-btn,
        .sec-summary-btn,
        .pres-chip,
        .cam-bar-chip,
        .ef-chip {
          font-size: 13px;
          padding: 7px 13px;
          min-height: 36px;
        }
        .alert-chip-act,
        .pres-chip-meta,
        .cam-bar-chip .cb-meta,
        .ef-chip .ef-lbl {
          font-size: 11.5px;
        }
        .pres-chip-ava {
          width: 22px;
          height: 22px;
          font-size: 11px;
        }
        .scene-pill svg,
        .alert-chip svg,
        .climate-summary-btn svg,
        .sec-summary-btn svg,
        .pres-chip svg,
        .cam-bar-chip svg,
        .ef-chip svg {
          width: 15px;
          height: 15px;
        }
        .floor-tag {
          font-size: 13px;
          padding: 3px 9px;
        }

        /* Floorplan Badges & Pins (Single-Floor) */
        .climate-badge {
          padding: 5.5px 10px;
        }
        .climate-badge-room {
          font-size: 11px;
        }
        .climate-badge-temp {
          font-size: 16px;
        }
        .climate-badge-hum {
          font-size: 12px;
        }
        .clim-co2 {
          font-size: 11px;
          padding: 2px 6.5px;
        }
        .run-badge,
        .cam-pill,
        .sec-pill,
        .energy-pill,
        .alert-pin {
          font-size: 12.5px;
          padding: 5.5px 11px 5.5px 8.5px;
          min-height: 28px;
        }
        .run-badge svg,
        .cam-pill svg,
        .energy-pill svg {
          width: 14px;
          height: 14px;
        }
        .sec-pill svg,
        .alert-pin svg {
          width: 15px;
          height: 15px;
        }
        .sec-pill .sec-val,
        .cam-pill .cam-tag {
          font-size: 11.5px;
        }
        .pres-pin {
          font-size: 12.5px;
          padding: 5px 12px 5px 6px;
        }
        .pres-pin-ava {
          width: 26px;
          height: 26px;
          font-size: 12px;
        }
        .pres-pin-sub {
          font-size: 11px;
        }
        .music-group-badge {
          padding: 8px 14px 8px 9px;
        }
        .music-group-badge .mg-label {
          font-size: 11.5px;
        }
        .music-group-badge .mg-track {
          font-size: 13.5px;
        }
        .callout {
          width: 36px;
          height: 36px;
        }
        .callout svg {
          width: 18px;
          height: 18px;
        }

        /* Exploded View */
        .stage.exploded .climate-badge {
          padding: 4px 8px;
        }
        .stage.exploded .climate-badge-room {
          font-size: 10px;
        }
        .stage.exploded .climate-badge-temp {
          font-size: 14px;
        }
        .stage.exploded .climate-badge-hum {
          font-size: 11px;
        }
        .stage.exploded .clim-co2 {
          font-size: 10px;
        }
        .stage.exploded .run-badge,
        .stage.exploded .cam-pill,
        .stage.exploded .sec-pill,
        .stage.exploded .alert-pin {
          font-size: 11.5px;
          padding: 4px 9px;
        }
        .stage.exploded .run-badge svg,
        .stage.exploded .cam-pill svg,
        .stage.exploded .sec-pill svg,
        .stage.exploded .alert-pin svg {
          width: 13px;
          height: 13px;
        }
        .stage.exploded .sec-pill .sec-val,
        .stage.exploded .cam-pill .cam-tag {
          font-size: 10.5px;
        }
        .stage.exploded .pres-pin {
          font-size: 11.5px;
        }
        .stage.exploded .callout {
          width: 32px;
          height: 32px;
        }
        .stage.exploded .callout svg {
          width: 16px;
          height: 16px;
        }
        @media (min-width: 820px) {
          .stage.exploded .climate-badge {
            padding: 5px 10px;
          }
          .stage.exploded .climate-badge-room {
            font-size: 11px;
          }
          .stage.exploded .climate-badge-temp {
            font-size: 15.5px;
          }
          .stage.exploded .climate-badge-hum {
            font-size: 12px;
          }
          .stage.exploded .clim-co2 {
            font-size: 11px;
          }
          .stage.exploded .run-badge,
          .stage.exploded .cam-pill,
          .stage.exploded .sec-pill,
          .stage.exploded .alert-pin {
            font-size: 12.5px;
            padding: 5px 10px;
          }
          .stage.exploded .run-badge svg,
          .stage.exploded .cam-pill svg,
          .stage.exploded .sec-pill svg,
          .stage.exploded .alert-pin svg {
            width: 14px;
            height: 14px;
          }
          .stage.exploded .sec-pill .sec-val,
          .stage.exploded .cam-pill .cam-tag {
            font-size: 11.5px;
          }
          .stage.exploded .pres-pin {
            font-size: 12.5px;
          }
          .stage.exploded .callout {
            width: 36px;
            height: 36px;
          }
          .stage.exploded .callout svg {
            width: 18px;
            height: 18px;
          }
        }

        /* Room Drawer */
        .room-drawer {
          width: min(410px, calc(100% - 24px));
        }
        .rd-title {
          font-size: 18px;
        }
        .rd-sub {
          font-size: 13px;
        }
        .rd-sec-title {
          font-size: 12.5px;
        }
        .rd-close {
          width: 40px;
          height: 40px;
          font-size: 22px;
        }
        .rd-row-label {
          font-size: 14.5px;
        }
        .rd-row-state {
          font-size: 13px;
        }
        .rd-toggle,
        .rd-pill-btn,
        .rd-mini-btn {
          font-size: 13.5px;
          padding: 9px 14px;
          min-height: 40px;
        }
        .rd-chip,
        .rd-alert {
          font-size: 12.5px;
          padding: 5px 10px;
        }

        /* Modals */
        .thermo-modal {
          width: min(440px, 100%);
        }
        .tm-title {
          font-size: 20px;
        }
        .tm-sub,
        .tm-status {
          font-size: 13.5px;
        }
        .tm-cur {
          font-size: 40px;
        }
        .tm-err {
          font-size: 13px;
        }
        .tm-mode-btn {
          font-size: 14px;
          padding: 12px 6px;
          min-height: 44px;
        }
        .tm-step-btn {
          width: 48px;
          height: 48px;
          font-size: 22px;
        }
        .tm-step-label {
          font-size: 13px;
        }
        .tc-name {
          font-size: 13px;
        }
        .tc-set {
          font-size: 15px;
        }
        .tc-pill {
          font-size: 13px;
          padding: 6px 12px;
          min-height: 36px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
        }
        .pm-name {
          font-size: 15.5px;
        }
        .pm-sub,
        .pm-loc,
        .pm-meta,
        .cp-sub {
          font-size: 13px;
        }
        .pm-ava {
          width: 40px;
          height: 40px;
          font-size: 15px;
        }
        .pm-ping-btn,
        .cp-cam-btn {
          font-size: 13.5px;
          padding: 10px 16px;
          min-height: 42px;
        }
        .cp-ring-pill,
        .cp-badge-live {
          font-size: 12px;
          padding: 4px 10px;
        }

        /* Portrait & Mobile Responsiveness */
        @media (max-width: 680px), (orientation: portrait) {
          .room-drawer {
            width: auto;
            max-height: 55%;
          }
        }
        @media (max-width: 640px) {
          .btn {
            font-size: 12.5px;
            padding: 7px 10px;
            min-height: 36px;
          }
          .floor-btn {
            font-size: 13px;
            padding: 7px 12px;
            min-height: 38px;
          }
          .room-drawer {
            width: auto;
            max-height: 55%;
          }
          .tm-step-btn {
            width: 44px;
            height: 44px;
          }
        }
        /* READABILITY_TOUCH_CSS_END */"""

REQUIRED_CSS_TOKENS: tuple[str, ...] = (
    ".btn {",
    "min-height: 40px;",
    ".lens-btn svg {",
    ".title-chip {",
    ".title-main {",
    ".title-sub {",
    ".floor-btn {",
    "min-height: 42px;",
    ".scene-pill,",
    ".alert-chip,",
    ".climate-summary-btn,",
    ".sec-summary-btn,",
    ".pres-chip,",
    ".cam-bar-chip,",
    ".ef-chip {",
    ".pres-chip-ava {",
    ".floor-tag {",
    ".climate-badge {",
    ".climate-badge-room {",
    ".climate-badge-temp {",
    ".climate-badge-hum {",
    ".clim-co2 {",
    ".run-badge,",
    ".cam-pill,",
    ".sec-pill,",
    ".energy-pill,",
    ".alert-pin {",
    ".pres-pin {",
    ".pres-pin-ava {",
    ".pres-pin-sub {",
    ".music-group-badge {",
    ".music-group-badge .mg-label {",
    ".music-group-badge .mg-track {",
    ".callout {",
    ".stage.exploded .climate-badge {",
    ".stage.exploded .pres-pin {",
    ".stage.exploded .callout {",
    "@media (min-width: 820px)",
    ".room-drawer {",
    "width: min(410px, calc(100% - 24px));",
    ".rd-title {",
    ".rd-sub {",
    ".rd-sec-title {",
    ".rd-close {",
    ".rd-row-label {",
    ".rd-row-state {",
    ".rd-toggle,",
    ".rd-pill-btn,",
    ".rd-mini-btn {",
    ".rd-chip,",
    ".rd-alert {",
    ".thermo-modal {",
    "width: min(440px, 100%);",
    ".tm-title {",
    ".tm-cur {",
    ".tm-err {",
    ".tm-mode-btn {",
    ".tm-step-btn {",
    ".tm-step-label {",
    ".tc-name {",
    ".tc-set {",
    ".tc-pill {",
    ".pm-name {",
    ".pm-ava {",
    ".pm-ping-btn,",
    ".cp-cam-btn {",
    ".cp-ring-pill,",
    ".cp-badge-live {",
    "@media (max-width: 640px)",
    "max-height: 55%;",
)


def verify_patched_card_contents(card_js: str) -> None:
    """Assert readability & touch-target CSS block and key surface selectors are present and kiosk rules intact."""
    missing: list[str] = []

    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    for token in REQUIRED_CSS_TOKENS:
        if token not in card_js:
            missing.append(f"token:{token}")

    begin_idx = card_js.find(CSS_BEGIN_MARKER)
    end_idx = card_js.find(CSS_END_MARKER)
    style_close_idx = card_js.find("</style>")
    if begin_idx != -1 and end_idx != -1:
        if style_close_idx == -1 or end_idx > style_close_idx:
            missing.append("placement:before_</style>")
        block = card_js[begin_idx:end_idx]
        if ".cook-modal" in block:
            missing.append("forbidden_cook_modal_override_in_block")

    if ".cook-modal.kiosk-mode" in card_js and ".cook-modal.kiosk-mode .cm-foot .rd-mini-btn" not in card_js:
        missing.append("kiosk_mode_foot_btn_missing")

    if missing:
        raise RuntimeError(
            "Patched floorplan readability & touch-target verification failed; issues: " + ", ".join(missing)
        )


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with comprehensive readability & touch-target CSS."""
    out = _replace_or_insert_block(
        source_js,
        CSS_BEGIN_MARKER,
        CSS_END_MARKER,
        READABILITY_TOUCH_CSS,
        (
            r"^\s*</style>",
            r"</style>",
        ),
        insert_before=True,
        label="READABILITY_TOUCH_CSS",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and readability/touch-target CSS block on the live JS card on Home Assistant."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(
                asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None), timeout=25)
            )
            matched = next((r for r in resources if card_rel in (r.get("url") or "")), None)
            if matched:
                card_rel = matched["url"]
                print(f"[verify] Lovelace resource URL: {card_rel}", flush=True)
            else:
                errors.append(f"Lovelace resource for {card_rel} not found via WebSocket")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Lovelace WebSocket query failed: {exc}")

    card_url = f"{ha_url}{card_rel}"
    status, body = _http_get(card_url)
    if status != 200:
        errors.append(f"HTTP {status} for live card: {card_url}")
    else:
        card_text = body.decode("utf-8", errors="replace")
        try:
            verify_patched_card_contents(card_text)
            print(f"OK [200] {card_url} ({len(body):,} bytes)", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Live card content check failed ({card_url}): {exc}")

    return (len(errors) == 0), errors


def run_deploy(*, dry_run: bool = False) -> int:
    """Execute the full fetch -> backup -> patch -> syntax check -> deploy -> WS bust -> HTTP verify pipeline."""
    ha_url, ha_token = resolve_ha_credentials()

    live_js = fetch_live_card_and_backup(BACKUP_CARD_PATH)
    patched_js = patch_card_js(live_js)
    js_hash = hashlib.sha256(patched_js.encode("utf-8")).hexdigest()[:8]
    print(
        f"[2/4] Patched & syntax-checked card JS via node --check "
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, Readability & Touch-Target Sizing)",
        flush=True,
    )

    if dry_run:
        print("[dry-run] Skipping Host 1 upload, WebSocket cache-buster update, and HTTP check.", flush=True)
        return 0

    deploy_card_to_host1(patched_js)
    cache_buster = f"{js_hash}{int(time.time()) & 0xFFFF:04x}"[:12]
    update_lovelace_resource_cache_buster(ha_url, ha_token, cache_buster=cache_buster)

    ok, errors = verify_live_http(ha_url, ha_token)
    if not ok:
        for err in errors:
            print(f"VERIFY ERROR: {err}", file=sys.stderr, flush=True)
        return 1

    print(
        "SUCCESS: Deployed and verified 3D floorplan readability & touch-target sizing on live card (HTTP 200).",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch and deploy 3D floorplan readability & touch-target sizing upgrade to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and readability/touch-target CSS contents on the live card on Home Assistant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, back up, patch, and syntax-check card JS in memory without modifying Host 1 or HA.",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        ha_url, ha_token = resolve_ha_credentials()
        ok, errors = verify_live_http(ha_url, ha_token)
        if not ok:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr, flush=True)
            return 1
        print(
            "VERIFIED: Live card returned HTTP 200 with floorplan readability & touch-target sizing wired.",
            flush=True,
        )
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
