"""Deploy 3D floorplan horizontal swipe-to-postMessage bridge for Hearth/ApolloSign to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.hearth-swipe.backup.js.
2. Idempotently patch brock-house-cycles-card.js with a marker-delimited
   (/* HEARTH_SWIPE_BRIDGE_BEGIN */ .. /* HEARTH_SWIPE_BRIDGE_END */) horizontal
   swipe listener block right after /* OVEN_MEATER_LISTENERS_END */ (or inside
   connectedCallback() as a fallback anchor).
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

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.hearth-swipe.backup.js")

BEGIN_MARKER = "/* HEARTH_SWIPE_BRIDGE_BEGIN */"
END_MARKER = "/* HEARTH_SWIPE_BRIDGE_END */"

REQUIRED_MARKERS: tuple[str, ...] = (
    BEGIN_MARKER,
    END_MARKER,
)

HEARTH_SWIPE_BRIDGE_JS = """    /* HEARTH_SWIPE_BRIDGE_BEGIN */
    if (!this._hearthSwipeBound && window.parent && window.parent !== window) {
      this._hearthSwipeBound = true;
      const isSwipeBlocked = (event) => {
        if (!this.shadowRoot) return true;
        if (
          this._cookModalOpen ||
          this._thermoModalOpen ||
          this._presenceModalOpen ||
          this.shadowRoot.querySelector('.cook-modal-overlay.open, .modal-overlay.open, [role="dialog"].open') ||
          this.shadowRoot.querySelector('.cook-backdrop.open, .thermo-backdrop.open, .presence-backdrop.open')
        ) {
          return true;
        }
        const blockedSelector =
          'input, select, textarea, [data-prevent-screen-swipe], .cook-modal-card, [role="dialog"]';
        const extraModalSelector = ".cook-modal, .thermo-modal, .presence-modal";
        const path = typeof event?.composedPath === "function" ? event.composedPath() : [];
        for (const node of path) {
          if (!node) continue;
          if (
            typeof node.matches === "function" &&
            (node.matches(blockedSelector) || node.matches(extraModalSelector))
          ) {
            return true;
          }
          if (
            typeof node.closest === "function" &&
            (node.closest(blockedSelector) || node.closest(extraModalSelector))
          ) {
            return true;
          }
        }
        return false;
      };

      if (this.shadowRoot) {
        let startX = 0;
        let startY = 0;
        let lastX = 0;
        let lastY = 0;
        let startT = 0;
        let trackingTouch = false;
        let cancelled = false;

        this.shadowRoot.addEventListener(
          "touchstart",
          (e) => {
            if (!e.touches || e.touches.length > 1) {
              cancelled = true;
              trackingTouch = false;
              return;
            }
            if (isSwipeBlocked(e)) {
              cancelled = true;
              trackingTouch = false;
              return;
            }
            const t = e.touches[0];
            startX = t.clientX;
            startY = t.clientY;
            lastX = t.clientX;
            lastY = t.clientY;
            startT = performance.now();
            cancelled = false;
            trackingTouch = true;
          },
          { passive: true }
        );

        this.shadowRoot.addEventListener(
          "touchmove",
          (e) => {
            if (!trackingTouch || cancelled) return;
            if (!e.touches || e.touches.length > 1) {
              cancelled = true;
              return;
            }
            const t = e.touches[0];
            lastX = t.clientX;
            lastY = t.clientY;
          },
          { passive: true }
        );

        this.shadowRoot.addEventListener(
          "touchend",
          (e) => {
            if (!trackingTouch) return;
            trackingTouch = false;
            const endTouch = e.changedTouches && e.changedTouches[0];
            if (endTouch && typeof endTouch.clientX === "number" && typeof endTouch.clientY === "number") {
              lastX = endTouch.clientX;
              lastY = endTouch.clientY;
            }
            const dx = lastX - startX;
            const dy = lastY - startY;
            if (
              !cancelled &&
              Math.abs(dx) >= 60 &&
              Math.abs(dx) > Math.abs(dy) * 1.3 &&
              (performance.now() - startT) <= 900
            ) {
              window.parent.postMessage({ type: "hearth-swipe", direction: dx < 0 ? "next" : "prev" }, "*");
            }
            cancelled = true;
          },
          { passive: true }
        );

        this.shadowRoot.addEventListener(
          "touchcancel",
          () => {
            cancelled = true;
            trackingTouch = false;
          },
          { passive: true }
        );

        let pointerActive = false;
        let pointerId = null;
        let pStartX = 0;
        let pStartY = 0;
        let pStartT = 0;

        this.shadowRoot.addEventListener(
          "pointerdown",
          (e) => {
            if (e.pointerType !== "touch") {
              if (typeof e.button === "number" && e.button !== 0) return;
              if (isSwipeBlocked(e)) {
                pointerActive = false;
                pointerId = null;
                return;
              }
              pointerActive = true;
              pointerId = e.pointerId ?? null;
              pStartX = e.clientX;
              pStartY = e.clientY;
              pStartT = performance.now();
            }
          },
          { passive: true }
        );

        this.shadowRoot.addEventListener(
          "pointerup",
          (e) => {
            if (e.pointerType !== "touch") {
              if (!pointerActive) return;
              if (pointerId !== null && e.pointerId !== undefined && e.pointerId !== pointerId) return;
              pointerActive = false;
              pointerId = null;
              const dx = e.clientX - pStartX;
              const dy = e.clientY - pStartY;
              if (
                Math.abs(dx) >= 60 &&
                Math.abs(dx) > Math.abs(dy) * 1.3 &&
                (performance.now() - pStartT) <= 900
              ) {
                window.parent.postMessage({ type: "hearth-swipe", direction: dx < 0 ? "next" : "prev" }, "*");
              }
            }
          },
          { passive: true }
        );

        this.shadowRoot.addEventListener(
          "pointercancel",
          (e) => {
            if (e.pointerType !== "touch") {
              pointerActive = false;
              pointerId = null;
            }
          },
          { passive: true }
        );
      }
    }
    /* HEARTH_SWIPE_BRIDGE_END */"""


def verify_patched_card_contents(card_js: str) -> None:
    """Assert Hearth swipe bridge markers, guards, modal checks, and postMessage handlers are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")
    for token in (
        "if (!this._hearthSwipeBound && window.parent && window.parent !== window)",
        "this._hearthSwipeBound = true",
        "isSwipeBlocked",
        "composedPath",
        '.cook-modal-overlay.open, .modal-overlay.open, [role="dialog"].open',
        'input, select, textarea, [data-prevent-screen-swipe], .cook-modal-card, [role="dialog"]',
        "touchstart",
        "touchmove",
        "touchend",
        "touchcancel",
        "pointerdown",
        "pointerup",
        "pointercancel",
        'e.pointerType !== "touch"',
        "hearth-swipe",
        "window.parent.postMessage",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")
    if missing:
        raise RuntimeError("Patched Hearth swipe bridge verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with the horizontal Hearth swipe-to-postMessage bridge."""
    out = _replace_or_insert_block(
        source_js,
        BEGIN_MARKER,
        END_MARKER,
        HEARTH_SWIPE_BRIDGE_JS,
        (
            r"/\*\s*OVEN_MEATER_LISTENERS_END\s*\*/",
            r"connectedCallback\s*\(\s*\)\s*\{",
        ),
        insert_before=False,
        label="HEARTH_SWIPE_BRIDGE",
    )
    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and Hearth swipe bridge contents on the live JS card on Home Assistant."""
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, Hearth Swipe Bridge)",
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

    print("SUCCESS: Deployed and verified 3D floorplan Hearth swipe bridge on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy 3D floorplan horizontal Hearth swipe-to-postMessage bridge to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and Hearth swipe bridge contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with Hearth swipe bridge wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
