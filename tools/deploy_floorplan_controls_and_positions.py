"""Deploy 3D floorplan thermostat control fixes, broken button/entity fixes, and badge repositioning to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.controls-pos.backup.js.
2. Idempotently patch brock-house-cycles-card.js with:
   A) Thermostat control fixes (/* THERMO_FIX_BEGIN */ .. /* THERMO_FIX_END */):
      - Fallback to fallback?.attributes?.hvac_action when primary Matter entity
        lacks hvac_action.
      - Optimistic state overlay (this._thermoOptimistic with 8s expiry) on
        mode, low, high, target so rapid taps accumulate immediately.
      - Nest 4th Gen 3°F minimum deadband (high - low < 3) in heat_cool mode.
      - Immediate modal re-render and Promise.resolve(callService(...)).catch(...)
        error rollback with inline .tm-err message for temperature & mode changes.
   B) Broken button & dead entity fixes:
      - Presence modal Ring Tag button ReferenceError (pmBtn -> btn).
      - light.garage_lights -> switch.garage_lights (ROOMS.garage.lights & Floor 1 lights).
      - media_player.living_room_tv_2 -> media_player.living_room_tv (with clean fallbackEntities).
      - binary_sensor.front_door_sensor_opening -> binary_sensor.lumi_lumi_sensor_magnet_aq2_opening.
      - Remove nonexistent fan.isaacs_fan from ROOMS.isaac.fans (fans: []).
   C) Badge proximity repositioning:
      - 3F coffee -> (44.5, 31.5).
      - Move hvac_pwr from Floor 2 energyItems to Floor 1 energyItems at (68.5, 36.5).
      - 1F ev_charger & ev_pwr -> (70.0, 76.0).
      - 1F garage_door & night_garage -> (59.0, 79.5).
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
    _ws_lovelace_resources,
    deploy_card_to_host1,
    fetch_live_card_and_backup,
    resolve_ha_credentials,
    update_lovelace_resource_cache_buster,
    validate_js_syntax,
)

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.controls-pos.backup.js")

THERMO_FIX_BEGIN_MARKER = "/* THERMO_FIX_BEGIN */"
THERMO_FIX_END_MARKER = "/* THERMO_FIX_END */"

REQUIRED_MARKERS: tuple[str, ...] = (
    THERMO_FIX_BEGIN_MARKER,
    THERMO_FIX_END_MARKER,
)

# Static badge coordinate updates: (item_id, x_str, y_str)
STATIC_BADGE_COORDS: tuple[tuple[str, str, str], ...] = (
    # 3F Kitchen Coffee Maker (north counter above island)
    ("coffee", "44.5", "31.5"),
    # 1F Garage North Wall HVAC power badge (moved from Floor 2)
    ("hvac_pwr", "68.5", "36.5"),
    # 1F Garage Bottom-Right EV Charger badges
    ("ev_charger", "70.0", "76.0"),
    ("ev_pwr", "70.0", "76.0"),
    # 1F Garage Door badges (shifted left away from EV charger)
    ("garage_door", "59.0", "79.5"),
    ("night_garage", "59.0", "79.5"),
)

DEFAULT_HVAC_PWR_ITEM = (
    '{ id: "hvac_pwr", name: "HVAC", entity: "sensor.ac_power_minute_average", '
    'extraEntity: "sensor.furnace_hvac_fan_power_minute_average", unit: "W", x: 68.5, y: 36.5 }'
)

THERMO_FIX_METHODS_JS = """  /* THERMO_FIX_BEGIN */
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
    const fallbackAttrs = fallback?.attributes || {};
    let mode = st.state || "off";
    let target = typeof attrs.temperature === "number" ? attrs.temperature : null;
    let low = typeof attrs.target_temp_low === "number" ? attrs.target_temp_low : null;
    let high = typeof attrs.target_temp_high === "number" ? attrs.target_temp_high : null;

    const opt = this._thermoOptimistic;
    if (opt && typeof opt.expiresAt === "number" && Date.now() < opt.expiresAt) {
      if (typeof opt.mode === "string" && opt.mode) mode = opt.mode;
      if (typeof opt.low === "number") low = opt.low;
      if (typeof opt.high === "number") high = opt.high;
      if (typeof opt.target === "number") target = opt.target;
    }

    const rawAction = attrs.hvac_action || fallback?.attributes?.hvac_action || fallbackAttrs.hvac_action;
    const action = rawAction || (mode === "off" ? "off" : "idle");

    return {
      entityId,
      mode,
      action,
      current: typeof attrs.current_temperature === "number" ? attrs.current_temperature : null,
      target,
      low,
      high,
      hum,
      modes:
        Array.isArray(attrs.hvac_modes) && attrs.hvac_modes.length
          ? attrs.hvac_modes
          : ["off", "heat", "cool", "heat_cool"],
      minTemp: typeof attrs.min_temp === "number" ? attrs.min_temp : 50,
      maxTemp: typeof attrs.max_temp === "number" ? attrs.max_temp : 90,
    };
  }

  _formatHvacMode(mode) {
    const map = { heat_cool: "Heat/Cool", heat: "Heat", cool: "Cool", off: "Off", auto: "Auto" };
    return map[mode] || mode;
  }

  _handleThermoServiceError(err) {
    this._thermoOptimistic = null;
    const msg =
      err && (err.message || err.body?.message)
        ? String(err.message || err.body.message)
        : "Thermostat update failed";
    this._thermoError = msg;
    this._renderThermostatModal();
    const modal = this.shadowRoot?.getElementById?.("thermoModal");
    if (modal && typeof modal.innerHTML === "string" && !modal.innerHTML.includes("tm-err")) {
      modal.innerHTML += `<div class="tm-err">${msg}</div>`;
    }
  }

  _adjustThermostat(field, delta) {
    if (!this._hass) return;
    const th = this._getThermostatInfo();
    if (!th.entityId) return;
    const clamp = (v) => Math.max(th.minTemp, Math.min(th.maxTemp, Math.round(v)));
    const prev =
      this._thermoOptimistic && typeof this._thermoOptimistic.expiresAt === "number" && Date.now() < this._thermoOptimistic.expiresAt
        ? this._thermoOptimistic
        : {};
    if (th.mode === "heat_cool") {
      let low = th.low !== null ? th.low : 68;
      let high = th.high !== null ? th.high : 75;
      if (field === "low") {
        low = clamp(low + delta);
        if (high - low < 3) high = clamp(low + 3);
        if (high - low < 3) low = clamp(high - 3);
      } else if (field === "high") {
        high = clamp(high + delta);
        if (high - low < 3) low = clamp(high - 3);
        if (high - low < 3) high = clamp(low + 3);
      }
      this._thermoError = null;
      this._thermoOptimistic = {
        ...prev,
        mode: th.mode,
        low,
        high,
        expiresAt: Date.now() + 8000,
      };
      this._renderThermostatModal();
      Promise.resolve(
        this._hass.callService("climate", "set_temperature", {
          entity_id: th.entityId,
          target_temp_low: low,
          target_temp_high: high,
        })
      ).catch((err) => {
        this._handleThermoServiceError(err);
      });
    } else if (th.mode === "heat" || th.mode === "cool") {
      const base = th.target !== null ? th.target : th.current !== null ? Math.round(th.current) : 70;
      const next = clamp(base + delta);
      this._thermoError = null;
      this._thermoOptimistic = {
        ...prev,
        mode: th.mode,
        target: next,
        expiresAt: Date.now() + 8000,
      };
      this._renderThermostatModal();
      Promise.resolve(
        this._hass.callService("climate", "set_temperature", {
          entity_id: th.entityId,
          temperature: next,
        })
      ).catch((err) => {
        this._handleThermoServiceError(err);
      });
    }
  }
  /* THERMO_FIX_END */"""

THERMO_MODE_CLICK_REPLACEMENT = """      } else if (act === "mode") {
        const mode = btn.dataset.tmMode;
        const th = this._getThermostatInfo();
        if (mode && th.entityId && this._hass) {
          const prev =
            this._thermoOptimistic && typeof this._thermoOptimistic.expiresAt === "number" && Date.now() < this._thermoOptimistic.expiresAt
              ? this._thermoOptimistic
              : {};
          this._thermoError = null;
          this._thermoOptimistic = { ...prev, mode, expiresAt: Date.now() + 8000 };
          this._renderThermostatModal();
          Promise.resolve(
            this._hass.callService("climate", "set_hvac_mode", { entity_id: th.entityId, hvac_mode: mode })
          ).catch((err) => {
            this._handleThermoServiceError(err);
          });
        }
      }"""

THERMO_ERR_CSS = """        .tm-err {
          font-size: 11.5px;
          font-weight: 600;
          color: #ff8a80;
          background: rgba(255, 82, 82, 0.14);
          border: 1px solid rgba(255, 82, 82, 0.35);
          border-radius: 10px;
          padding: 6px 10px;
          text-align: center;
        }"""


def _apply_thermostat_fixes(source_js: str) -> str:
    """Idempotently replace _getThermostatInfo.._adjustThermostat and wire optimistic mode clicks + .tm-err feedback."""
    out = source_js

    # 1. Replace or inject /* THERMO_FIX_BEGIN */ .. /* THERMO_FIX_END */
    if THERMO_FIX_BEGIN_MARKER in out and THERMO_FIX_END_MARKER in out:
        begin_idx = out.index(THERMO_FIX_BEGIN_MARKER)
        end_idx = out.index(THERMO_FIX_END_MARKER) + len(THERMO_FIX_END_MARKER)
        line_start = out.rfind("\n", 0, begin_idx)
        if line_start != -1 and out[line_start + 1 : begin_idx].strip() == "":
            begin_idx = line_start + 1
        out = out[:begin_idx] + THERMO_FIX_METHODS_JS + out[end_idx:]
    else:
        thermo_methods_re = re.compile(
            r"^[ \t]*_getThermostatInfo\s*\(\s*\)\s*\{[\s\S]*?_adjustThermostat\s*\(\s*field\s*,\s*delta\s*\)\s*\{[\s\S]*?\n  \}",
            re.MULTILINE,
        )
        m = thermo_methods_re.search(out)
        if not m:
            raise RuntimeError("Could not locate _getThermostatInfo .. _adjustThermostat block in card JS")
        out = out[: m.start()] + THERMO_FIX_METHODS_JS + out[m.end() :]

    # 2. Wire optimistic update & error catch on data-tm-action="mode" click
    mode_click_re = re.compile(
        r'^[ \t]*\}\s*else\s+if\s*\(\s*act\s*===\s*"mode"\s*\)\s*\{[\s\S]*?set_hvac_mode[\s\S]*?\n[ \t]*\}(?=\s*else\s+if\s*\(\s*act\s*===\s*"step"\s*\))',
        re.MULTILINE,
    )
    if mode_click_re.search(out):
        out = mode_click_re.sub(THERMO_MODE_CLICK_REPLACEMENT, out, count=1)

    # 3. Render .tm-err inside _renderThermostatModal() template if not already present
    if "tm-err" not in out or '${this._thermoError ? `<div class="tm-err">' not in out:
        out = re.sub(
            r"(\$\{steppersHtml\}\n)([ \t]*<div class=\"tm-foot\">)",
            r'\g<1>      ${this._thermoError ? `<div class="tm-err">${this._thermoError}</div>` : ""}\n\g<2>',
            out,
            count=1,
        )

    # 4. Inject .tm-err CSS rule after .tm-foot { ... } if not already present
    if ".tm-err {" not in out:
        out = re.sub(
            r"(\.tm-foot\s*\{[^{}]*\})",
            rf"\g<1>\n{THERMO_ERR_CSS}",
            out,
            count=1,
        )

    return out


def _apply_button_and_entity_fixes(source_js: str) -> str:
    """Idempotently fix the Presence Ring button ReferenceError and dead entity IDs."""
    out = source_js

    # 1. Fix Presence modal Ring button ReferenceError (pmBtn -> btn)
    out = out.replace(
        "this._ringTag(pmBtn.dataset.presId, pmBtn.dataset.btnEntity);",
        "this._ringTag(btn.dataset.presId, btn.dataset.btnEntity);",
    )

    # 2. Replace dead light.garage_lights with switch.garage_lights (ROOMS.garage.lights & Floor 1 lights)
    out = out.replace('"light.garage_lights"', '"switch.garage_lights"')

    # 3. Replace dead media_player.living_room_tv_2 with media_player.living_room_tv (deduplicating fallbackEntities)
    out = re.sub(
        r'"media_player\.living_room_tv_2"\s*,\s*"media_player\.living_room_tv"',
        '"media_player.living_room_tv"',
        out,
    )
    out = out.replace('"media_player.living_room_tv_2"', '"media_player.living_room_tv"')

    # 4. Replace dead binary_sensor.front_door_sensor_opening with binary_sensor.lumi_lumi_sensor_magnet_aq2_opening
    out = out.replace(
        '"binary_sensor.front_door_sensor_opening"',
        '"binary_sensor.lumi_lumi_sensor_magnet_aq2_opening"',
    )

    # 5. Remove nonexistent fan.isaacs_fan from ROOMS.isaac.fans
    out = re.sub(
        r'fans:\s*\[\s*\{\s*entity:\s*"fan\.isaacs_fan"\s*,\s*name:\s*"Isaac\'s Fan"\s*\}\s*\]',
        "fans: []",
        out,
    )

    return out


def _move_hvac_pwr_to_floor1(source_js: str) -> str:
    """Idempotently move hvac_pwr out of Floor 2 energyItems and insert it into Floor 1 energyItems."""
    out = source_js
    extracted_item = DEFAULT_HVAC_PWR_ITEM

    floor2_energy_re = re.compile(
        r'(\bid:\s*"floor2"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\benergyItems:\s*\[\n?)([\s\S]*?)(\n?[ \t]*\])'
    )
    m_f2 = floor2_energy_re.search(out)
    if m_f2:
        f2_body = m_f2.group(2)
        hvac_line_re = re.compile(
            r'^[ \t]*(\{[^{}\n]*\bid:\s*"hvac_pwr"[^{}\n]*\}),?[ \t]*\n?',
            re.MULTILINE,
        )
        m_hvac = hvac_line_re.search(f2_body)
        if m_hvac:
            extracted_item = m_hvac.group(1).strip()
            new_f2_body = hvac_line_re.sub("", f2_body).rstrip("\n")
            out = out[: m_f2.start(2)] + new_f2_body + out[m_f2.end(2) :]

    floor1_energy_re = re.compile(
        r'(\bid:\s*"floor1"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\benergyItems:\s*\[\n?)([\s\S]*?)(\n?[ \t]*\])'
    )
    m_f1 = floor1_energy_re.search(out)
    if m_f1:
        f1_body = m_f1.group(2)
        if '"hvac_pwr"' not in f1_body:
            indent_match = re.search(r"^([ \t]+)\{", f1_body, re.MULTILINE)
            indent = indent_match.group(1) if indent_match else "      "
            stripped_body = f1_body.rstrip()
            if stripped_body:
                if not stripped_body.endswith(","):
                    stripped_body += ","
                new_f1_body = f"{stripped_body}\n{indent}{extracted_item},"
            else:
                new_f1_body = f"{indent}{extracted_item},"
            out = out[: m_f1.start(2)] + new_f1_body + out[m_f1.end(2) :]

    return out


def _apply_badge_coordinate_updates(source_js: str) -> str:
    """Idempotently relocate hvac_pwr to Floor 1 and update badge coordinates."""
    out = _move_hvac_pwr_to_floor1(source_js)

    for item_id, new_x, new_y in STATIC_BADGE_COORDS:
        item_re = re.compile(
            rf'(\{{\s*id:\s*"{re.escape(item_id)}"[^{{}}\n]*?\bx:\s*)-?\d+(?:\.\d+)?(,\s*y:\s*)-?\d+(?:\.\d+)?'
        )
        out = item_re.sub(rf"\g<1>{new_x}\g<2>{new_y}", out, count=1)

    return out


def verify_patched_card_contents(card_js: str) -> None:
    """Assert thermostat fixes, button/entity fixes, and badge coordinates are present in patched card JS."""
    missing: list[str] = []

    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    for token in (
        "this._thermoOptimistic",
        "expiresAt: Date.now() + 8000",
        "fallback?.attributes?.hvac_action",
        "if (high - low < 3)",
        "Promise.resolve(",
        "tm-err",
        "this._ringTag(btn.dataset.presId, btn.dataset.btnEntity);",
        '"switch.garage_lights"',
        '"media_player.living_room_tv"',
        '"binary_sensor.lumi_lumi_sensor_magnet_aq2_opening"',
    ):
        if token not in card_js:
            missing.append(f"token:{token}")

    for forbidden in (
        "this._ringTag(pmBtn.dataset.presId",
        '"light.garage_lights"',
        '"media_player.living_room_tv_2"',
        '"binary_sensor.front_door_sensor_opening"',
        '"fan.isaacs_fan"',
    ):
        if forbidden in card_js:
            missing.append(f"forbidden_still_present:{forbidden}")

    # Verify hvac_pwr is in floor1 energyItems and not in floor2 energyItems
    floor2_energy_re = re.compile(
        r'\bid:\s*"floor2"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\benergyItems:\s*\[([\s\S]*?)\]'
    )
    m_f2 = floor2_energy_re.search(card_js)
    if not m_f2 or '"hvac_pwr"' in m_f2.group(1):
        missing.append("hvac_pwr_still_in_floor2")

    floor1_energy_re = re.compile(
        r'\bid:\s*"floor1"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\benergyItems:\s*\[([\s\S]*?)\]'
    )
    m_f1 = floor1_energy_re.search(card_js)
    if not m_f1 or '"hvac_pwr"' not in m_f1.group(1):
        missing.append("hvac_pwr_missing_from_floor1")

    for item_id, exp_x, exp_y in STATIC_BADGE_COORDS:
        if f'id: "{item_id}"' in card_js:
            pat = re.compile(
                rf'\{{[^{{}}\n]*\bid:\s*"{re.escape(item_id)}"[^{{}}\n]*\bx:\s*{re.escape(exp_x)},\s*y:\s*{re.escape(exp_y)}\b'
            )
            if not pat.search(card_js):
                missing.append(f"coord:{item_id}=({exp_x},{exp_y})")
        else:
            missing.append(f"badge_missing:{item_id}")

    if missing:
        raise RuntimeError(
            "Patched floorplan controls & positions verification failed; issues: " + ", ".join(missing)
        )


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with thermostat fixes, entity fixes, and badge positions."""
    out = _apply_thermostat_fixes(source_js)
    out = _apply_button_and_entity_fixes(out)
    out = _apply_badge_coordinate_updates(out)

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and patched controls & badge coordinates on the live JS card on Home Assistant."""
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, Controls & Badge Positions)",
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

    print("SUCCESS: Deployed and verified 3D floorplan controls & badge positions on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch and deploy 3D floorplan thermostat fixes, broken buttons, and badge positions to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and patched controls/positions contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with floorplan controls & badge positions patched.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
