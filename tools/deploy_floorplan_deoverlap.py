"""Deploy 3D floorplan same-lens coordinate separation & cross-lens auto de-overlap engine to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.deoverlap.backup.js.
2. Idempotently patch brock-house-cycles-card.js with:
   - Static same-lens coordinate separation for 3F Security (rosie_shades, rosie_door),
     1F Laundry (washer, dryer, washer_pwr, dryer_pwr), and 3F Oven/MEATER+
     (ovenBadgePos, meaterBadgePos).
   - Marker-delimited CSS (/* DEOVERLAP_CSS_BEGIN */ .. /* DEOVERLAP_CSS_END */)
     applying nudges via --deoverlap-x / --deoverlap-y on margin-left / margin-top
     and hiding .cook-hotspot when active cook badges are displayed.
   - Marker-delimited runtime De-Overlap Engine
     (/* DEOVERLAP_ENGINE_BEGIN */ .. /* DEOVERLAP_ENGINE_END */) hooked after
     /* OVEN_MEATER_LISTENERS_END */ (with connectedCallback() fallback).
3. Validate patched JS syntax via `node --check`.
4. Deploy updated JS card to Host 1 Volume 2 NVMe, bump the HA Lovelace
   resource cache-buster via WebSocket, and verify HTTP 200 and live content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
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

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.deoverlap.backup.js")

CSS_BEGIN_MARKER = "/* DEOVERLAP_CSS_BEGIN */"
CSS_END_MARKER = "/* DEOVERLAP_CSS_END */"
ENGINE_BEGIN_MARKER = "/* DEOVERLAP_ENGINE_BEGIN */"
ENGINE_END_MARKER = "/* DEOVERLAP_ENGINE_END */"

REQUIRED_MARKERS: tuple[str, ...] = (
    CSS_BEGIN_MARKER,
    CSS_END_MARKER,
    ENGINE_BEGIN_MARKER,
    ENGINE_END_MARKER,
)

# Static same-lens coordinate updates: (item_id, x_str, y_str)
STATIC_ITEM_COORDS: tuple[tuple[str, str, str], ...] = (
    # 3F Security
    ("rosie_shades", "80.5", "44.5"),
    ("rosie_door", "81.5", "53.5"),
    # 1F Laundry (runningItems + energyItems)
    ("washer", "63.5", "46.0"),
    ("dryer", "70.5", "54.0"),
    ("washer_pwr", "63.5", "46.0"),
    ("dryer_pwr", "70.5", "54.0"),
)

# 3F Oven / MEATER+ config coordinate updates: (prop_name, x_str, y_str)
STATIC_CONFIG_COORDS: tuple[tuple[str, str, str], ...] = (
    ("ovenBadgePos", "56.5", "40.5"),
    ("meaterBadgePos", "59.5", "48.0"),
)

DEOVERLAP_CSS = """        /* DEOVERLAP_CSS_BEGIN */
        .run-badge {
          margin-left: var(--deoverlap-x, 0px);
          margin-top: var(--deoverlap-y, 0px);
          transition: margin-left 140ms ease, margin-top 140ms ease;
        }
        .cook-hotspot.cook-hotspot-hidden {
          opacity: 0 !important;
          pointer-events: none !important;
        }
        /* DEOVERLAP_CSS_END */"""

DEOVERLAP_ENGINE_JS = """    /* DEOVERLAP_ENGINE_BEGIN */
    if (!this._deoverlapBound) {
      this._deoverlapBound = true;

      this._deoverlapActiveBadges = () => {
        const sr = this.shadowRoot;
        if (!sr || typeof sr.querySelectorAll !== "function") return;

        let floorCards = Array.from(sr.querySelectorAll(".floor-card.active"));
        if (!floorCards.length) {
          floorCards = Array.from(sr.querySelectorAll(".floor-card:not(.hidden-floor)"));
        }

        const getCS =
          (typeof window !== "undefined" && typeof window.getComputedStyle === "function"
            ? window.getComputedStyle.bind(window)
            : null) ||
          (typeof getComputedStyle === "function" ? getComputedStyle : null);

        const isVisibleEl = (el, rect) => {
          if (!el || !rect) return false;
          if (!(rect.width > 4 && rect.height > 4)) return false;
          if (getCS) {
            try {
              const cs = getCS(el);
              if (cs) {
                if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") {
                  return false;
                }
                if (cs.opacity !== undefined && cs.opacity !== "" && Number(cs.opacity) <= 0.01) {
                  return false;
                }
              }
            } catch (_e) {
              // ignore non-DOM synthetic element in unit tests
            }
          }
          return true;
        };

        const toBox = (r) => ({
          left: r.left,
          top: r.top,
          right: r.right !== undefined ? r.right : r.left + r.width,
          bottom: r.bottom !== undefined ? r.bottom : r.top + r.height,
          width: r.width,
          height: r.height,
        });

        const computeOverlapArea = (rectA, rectB, pad = 5) => {
          const ox = Math.max(
            0,
            Math.min(rectA.right + pad, rectB.right) - Math.max(rectA.left - pad, rectB.left)
          );
          const oy = Math.max(
            0,
            Math.min(rectA.bottom + pad, rectB.bottom) - Math.max(rectA.top - pad, rectB.top)
          );
          return ox * oy;
        };

        const lensObstacleSelectors = {
          energy: ".energy-pill",
          security: ".sec-pill, .cam-pill",
          cameras: ".sec-pill, .cam-pill",
          climate: ".climate-badge",
          presence: "[data-presence-pins] > *",
          lights: ".callout",
        };
        const activeLens = this._activeLens || "lights";
        const obstacleSelector =
          activeLens === "lights" && this._showControls === false
            ? ""
            : lensObstacleSelectors[activeLens] || "";

        for (const card of floorCards) {
          if (!card || typeof card.getBoundingClientRect !== "function") continue;
          const rawCardRect = card.getBoundingClientRect();
          if (!rawCardRect || !(rawCardRect.width >= 100 && rawCardRect.height >= 100)) continue;
          const cardRect = toBox(rawCardRect);

          // 1. Toggle .cook-hotspot-hidden whenever .oven-badge.active or .meater-badge.active exists
          const hasActiveCookBadge = Boolean(
            (typeof card.querySelector === "function" &&
              (card.querySelector(".oven-badge.active, .meater-badge.active") ||
                card.querySelector(".oven-badge.active") ||
                card.querySelector(".meater-badge.active"))) ||
              false
          );
          if (typeof card.querySelectorAll === "function") {
            Array.from(card.querySelectorAll(".cook-hotspot")).forEach((hs) => {
              if (hs && hs.classList && typeof hs.classList.toggle === "function") {
                hs.classList.toggle("cook-hotspot-hidden", hasActiveCookBadge);
              }
            });
          }

          // 2. Reset --deoverlap-x and --deoverlap-y to 0px on all .running-layer .run-badge
          const allRunBadges =
            typeof card.querySelectorAll === "function"
              ? Array.from(card.querySelectorAll(".running-layer .run-badge"))
              : [];
          for (const badge of allRunBadges) {
            if (badge && badge.style) {
              badge.style.transition = "none";
              if (typeof badge.style.setProperty === "function") {
                badge.style.setProperty("--deoverlap-x", "0px");
                badge.style.setProperty("--deoverlap-y", "0px");
              }
            }
          }

          // 3. Gather visible obstacle rects for the active lens
          const occupied = [];
          if (obstacleSelector && typeof card.querySelectorAll === "function") {
            const obsEls = Array.from(card.querySelectorAll(obstacleSelector));
            for (const obsEl of obsEls) {
              const r =
                obsEl && typeof obsEl.getBoundingClientRect === "function"
                  ? obsEl.getBoundingClientRect()
                  : null;
              if (isVisibleEl(obsEl, r)) {
                occupied.push(toBox(r));
              }
            }
          }

          // 4. Gather visible .running-layer .run-badge.active sorted deterministically
          let rawActive =
            typeof card.querySelectorAll === "function"
              ? Array.from(card.querySelectorAll(".running-layer .run-badge.active"))
              : [];
          if (!rawActive.length && allRunBadges.length) {
            rawActive = allRunBadges.filter(
              (b) => b && b.classList && typeof b.classList.contains === "function" && b.classList.contains("active")
            );
          }

          const activeBadges = rawActive
            .map((el) => {
              const r = el && typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
              return { el, rect: r ? toBox(r) : null };
            })
            .filter((item) => isVisibleEl(item.el, item.rect))
            .sort((a, b) => {
              const idA = String(a.el.dataset?.runId || a.el.dataset?.ovenMeaterBadge || "");
              const idB = String(b.el.dataset?.runId || b.el.dataset?.ovenMeaterBadge || "");
              return idA.localeCompare(idB);
            });

          // 5. Test candidate (dx, dy) offsets and apply nudges
          const padBounds = { left: 20, right: 20, top: 14, bottom: 14 };
          const ringScales = [1.0, 1.4, 1.8, 2.2];

          for (const { el, rect: baseRect } of activeBadges) {
            const bw = baseRect.width;
            const bh = baseRect.height;
            const baseUnitY = Math.max(22, bh + 6);
            const baseUnitX = Math.max(32, bw * 0.55);

            const candidates = [[0, 0]];
            for (const scale of ringScales) {
              const sy = Math.round(baseUnitY * scale);
              const sxHalf = Math.round(baseUnitX * 0.65 * scale);
              const sx = Math.round(baseUnitX * scale);
              const sxFull = Math.round(Math.max(bw + 6, baseUnitX) * scale);
              candidates.push(
                [0, -sy],
                [0, sy],
                [-sxHalf, -sy],
                [sxHalf, -sy],
                [-sxHalf, sy],
                [sxHalf, sy],
                [-sx, 0],
                [sx, 0],
                [-sx, -sy],
                [sx, -sy],
                [-sx, sy],
                [sx, sy],
                [-sxFull, 0],
                [sxFull, 0]
              );
            }

            const minLeft = cardRect.left + padBounds.left;
            const maxLeft = Math.max(minLeft, cardRect.right - padBounds.right - bw);
            const minTop = cardRect.top + padBounds.top;
            const maxTop = Math.max(minTop, cardRect.bottom - padBounds.bottom - bh);

            let best = null;
            for (const [rawDx, rawDy] of candidates) {
              const clampedLeft = Math.min(maxLeft, Math.max(minLeft, baseRect.left + rawDx));
              const clampedTop = Math.min(maxTop, Math.max(minTop, baseRect.top + rawDy));
              const dx = Math.round(clampedLeft - baseRect.left);
              const dy = Math.round(clampedTop - baseRect.top);
              const candRect = {
                left: baseRect.left + dx,
                top: baseRect.top + dy,
                right: baseRect.left + dx + bw,
                bottom: baseRect.top + dy + bh,
                width: bw,
                height: bh,
              };

              let overlap = 0;
              for (const obs of occupied) {
                overlap += computeOverlapArea(candRect, obs, 5);
              }
              const dist = Math.hypot(dx, dy);

              if (overlap === 0) {
                best = { dx, dy, rect: candRect, overlap: 0, dist };
                break;
              }
              if (
                !best ||
                overlap < best.overlap ||
                (overlap === best.overlap && dist < best.dist)
              ) {
                best = { dx, dy, rect: candRect, overlap, dist };
              }
            }

            const finalChoice = best || { dx: 0, dy: 0, rect: baseRect };
            if (el && el.style) {
              el.style.transition = "";
              if (typeof el.style.setProperty === "function") {
                el.style.setProperty("--deoverlap-x", `${finalChoice.dx}px`);
                el.style.setProperty("--deoverlap-y", `${finalChoice.dy}px`);
              }
            }
            if (el && el.dataset) {
              el.dataset.deoverlapX = String(finalChoice.dx);
              el.dataset.deoverlapY = String(finalChoice.dy);
            }
            occupied.push(finalChoice.rect);
          }

          for (const badge of allRunBadges) {
            if (badge && badge.style && badge.style.transition === "none") {
              badge.style.transition = "";
            }
          }
        }
      };

      this._scheduleDeoverlap = () => {
        if (this._deoverlapRaf) return;
        const raf =
          (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function"
            ? window.requestAnimationFrame.bind(window)
            : null) ||
          (typeof requestAnimationFrame === "function" ? requestAnimationFrame : null);
        if (raf) {
          this._deoverlapRaf = raf(() => {
            this._deoverlapRaf = 0;
            this._deoverlapActiveBadges();
          });
        } else {
          this._deoverlapActiveBadges();
        }
      };

      if (typeof this._updateState === "function" && !this._deoverlapUpdateWrapped) {
        this._deoverlapUpdateWrapped = true;
        const origUpdateState = this._updateState.bind(this);
        this._updateState = (...args) => {
          const res = origUpdateState(...args);
          this._deoverlapActiveBadges();
          this._scheduleDeoverlap();
          return res;
        };
      }

      const RO =
        (typeof window !== "undefined" && window.ResizeObserver) ||
        (typeof ResizeObserver !== "undefined" ? ResizeObserver : null);
      if (RO && !this._deoverlapRo) {
        this._deoverlapRo = new RO(() => {
          this._scheduleDeoverlap();
        });
        if (this.shadowRoot && typeof this.shadowRoot.querySelectorAll === "function") {
          const targets = Array.from(this.shadowRoot.querySelectorAll(".stage, .floor-card"));
          for (const t of targets) {
            try {
              this._deoverlapRo.observe(t);
            } catch (_e) {
              // ignore non-Element mock nodes
            }
          }
        }
      }
    }
    /* DEOVERLAP_ENGINE_END */"""


def _apply_static_coordinate_updates(source_js: str) -> str:
    """Idempotently apply static same-lens coordinate separations across 3F Security, 1F Laundry, and 3F Oven/MEATER+."""
    out = source_js
    for item_id, new_x, new_y in STATIC_ITEM_COORDS:
        item_re = re.compile(
            rf'(\{{\s*id:\s*"{re.escape(item_id)}"[^{{}}\n]*?\bx:\s*)-?\d+(?:\.\d+)?(,\s*y:\s*)-?\d+(?:\.\d+)?'
        )
        out = item_re.sub(rf"\g<1>{new_x}\g<2>{new_y}", out, count=1)

    for prop_name, new_x, new_y in STATIC_CONFIG_COORDS:
        cfg_re = re.compile(
            rf"(\b{re.escape(prop_name)}:\s*\{{\s*x:\s*)-?\d+(?:\.\d+)?(,\s*y:\s*)-?\d+(?:\.\d+)?(\s*\}})"
        )
        out = cfg_re.sub(rf"\g<1>{new_x}\g<2>{new_y}\g<3>", out, count=1)

    return out


def verify_patched_card_contents(card_js: str) -> None:
    """Assert de-overlap markers, CSS custom properties, engine logic, and static coordinate updates are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    for token in (
        "margin-left: var(--deoverlap-x, 0px)",
        "margin-top: var(--deoverlap-y, 0px)",
        "transition: margin-left 140ms ease, margin-top 140ms ease",
        ".cook-hotspot.cook-hotspot-hidden",
        "if (!this._deoverlapBound)",
        "this._deoverlapBound = true",
        "this._deoverlapActiveBadges",
        "this._scheduleDeoverlap",
        "ResizeObserver",
        "requestAnimationFrame",
        "cook-hotspot-hidden",
        "--deoverlap-x",
        "--deoverlap-y",
        "dataset?.runId || a.el.dataset?.ovenMeaterBadge",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")

    for item_id, exp_x, exp_y in STATIC_ITEM_COORDS:
        if f'id: "{item_id}"' in card_js:
            pat = re.compile(
                rf'\{{[^{{}}\n]*\bid:\s*"{re.escape(item_id)}"[^{{}}\n]*\bx:\s*{re.escape(exp_x)},\s*y:\s*{re.escape(exp_y)}\b'
            )
            if not pat.search(card_js):
                missing.append(f"coord:{item_id}=({exp_x},{exp_y})")

    for prop_name, exp_x, exp_y in STATIC_CONFIG_COORDS:
        if prop_name in card_js:
            pat = re.compile(
                rf"\b{re.escape(prop_name)}:\s*\{{\s*x:\s*{re.escape(exp_x)},\s*y:\s*{re.escape(exp_y)}\s*\}}"
            )
            if not pat.search(card_js):
                missing.append(f"coord:{prop_name}=({exp_x},{exp_y})")

    if missing:
        raise RuntimeError("Patched floorplan de-overlap verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with static coordinate separation, CSS, and de-overlap engine."""
    out = _apply_static_coordinate_updates(source_js)

    out = _replace_or_insert_block(
        out,
        CSS_BEGIN_MARKER,
        CSS_END_MARKER,
        DEOVERLAP_CSS,
        (
            r"^\s*</style>",
            r"</style>",
        ),
        insert_before=True,
        label="DEOVERLAP_CSS",
    )

    out = _replace_or_insert_block(
        out,
        ENGINE_BEGIN_MARKER,
        ENGINE_END_MARKER,
        DEOVERLAP_ENGINE_JS,
        (
            r"/\*\s*OVEN_MEATER_LISTENERS_END\s*\*/",
            r"connectedCallback\s*\(\s*\)\s*\{",
        ),
        insert_before=False,
        label="DEOVERLAP_ENGINE",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and de-overlap engine contents on the live JS card on Home Assistant."""
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, De-Overlap Engine)",
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

    print("SUCCESS: Deployed and verified 3D floorplan de-overlap engine on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy 3D floorplan same-lens separation & cross-lens auto de-overlap engine to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and de-overlap engine contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with floorplan de-overlap engine wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
