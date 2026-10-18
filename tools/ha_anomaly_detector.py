#!/usr/bin/env python3
"""Home Assistant Statistical Telemetry Anomaly Detector & Self-Healer (Task #58).

Architecture (adapted from Alex/Arcane & Aerial PR #57):
- Primary long-term TSDB: VictoriaMetrics on Host 1 (:8428, 5y retention, /api/prometheus scrape).
- Automatic Day-1 Hybrid Fallback: Queries VictoriaMetrics PromQL range series first and
  transparently supplements from Home Assistant's native /api/history/period + /api/states
  whenever a sensor has <24h of samples in VictoriaMetrics.
- Three deterministic statistical engines (zero LLM tokens on nominal days):
  1. Zombie / Flatlined Cadence Engine: Detects sensors reporting 'available' whose
     readings have flatlined (>24h for ambient climate/CO2 sensors or >overdue threshold).
  2. Rolling MAD Amplitude Engine: Computes rolling Median and Median Absolute Deviation (MAD),
     flagging z_mad >= 4.0 outliers while filtering expected diurnal solar/battery TOU swings.
  3. Safe Self-Healing Engine:
     - Stage 1: Polls anomalous/frozen entities via `homeassistant.update_entity`.
     - Stage 2: For frozen sensors on reload-safe integrations (excluding ZHA/Z-Wave/Matter/Thread
       mesh coordinators to prevent unload deadlocks), reloads the integration config entry with a
       24-hour per-entry cooldown and verifies whether the reading recovered.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.ha_battery_check import get_ha_config
from tools.nas_config import HOST_1_IP

DEFAULT_VM_URL = os.environ.get("VICTORIAMETRICS_URL", f"http://{HOST_1_IP}:8428")
HEAL_STATE_FILE = Path("/workspace/data/ha_anomaly_heal_state.json")
HEAL_COOLDOWN_SECONDS = 24 * 3600

# Device classes monitored by the statistical engines
MONITORED_CLASSES = {"temperature", "humidity", "carbon_dioxide", "power"}

# Minimum denominator floor per device class to prevent division-by-near-zero on ultra-stable sensors
MIN_SIGMA_FLOOR = {
    "temperature": 1.5,      # °F
    "humidity": 3.0,         # %
    "carbon_dioxide": 50.0,  # ppm
    "power": 25.0,           # W
}

# Domains that must NEVER have their entire config entry auto-reloaded for a single stale leaf sensor
# (per scar_zha_unload_deadlock_on_reconnect.md, unloading ZHA/Z-Wave/Matter meshes risks coordinator deadlocks)
NEVER_RELOAD_DOMAINS = {
    "zha",
    "zwave_js",
    "matter",
    "thread",
    "otbr",
    "homeassistant",
    "hassio",
    "mqtt",
}

# Ignore patterns for expected diurnal/TOU swings, static setpoints, and noisy 1-min circuit averages
IGNORE_SUBSTRINGS = (
    "set_temperature",
    "target_temp",
    "target_w_temp",
    "octoprint",
    "openweathermap",
    "forecast",
    "encharge_",
    "envoy_",
    "solar",
    "vue_energy_monitor",
    "power_minute_average",
    "_device_temperature",
    "_chip_temp",
    os.environ.get("NAS_HOST_1_NAME", "nas-host-1"),
    "tesla_wall_connector",
    "collar_",
    "processor",
    "memory_",
    "disk_",
    "cpu_",
    "sun_solar",
    "phone",
    "pixel",
    "fold",
    "watch",
)


def is_monitored_sensor(entity_id: str, attributes: dict[str, Any], state_val: str) -> bool:
    """Return True if entity should be evaluated for amplitude and cadence anomalies."""
    if not entity_id.startswith("sensor."):
        return False
    if state_val in ("unavailable", "unknown", "", None):
        return False
    dc = attributes.get("device_class", "")
    if dc not in MONITORED_CLASSES:
        return False
    eid_low = entity_id.lower()
    name_low = str(attributes.get("friendly_name", "")).lower()
    if any(pat in eid_low or pat in name_low for pat in IGNORE_SUBSTRINGS):
        return False
    try:
        float(state_val)
    except (ValueError, TypeError):
        return False
    return True


def compute_mad_score(values: list[float], current_value: float, device_class: str) -> tuple[float, float, float]:
    """Compute (z_score, median, mad) using Rolling Median Absolute Deviation."""
    if not values:
        return 0.0, current_value, 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(x - med) for x in values])
    min_floor = MIN_SIGMA_FLOOR.get(device_class, 1.5)
    denom = max(1.4826 * mad, min_floor)
    z_score = abs(current_value - med) / denom
    return z_score, med, mad


def compute_overdue_threshold_hours(timestamps_sec: list[float], default_max_hours: float = 24.0) -> float:
    """Compute dynamic cadence overdue threshold in hours from historical state change intervals."""
    if len(timestamps_sec) < 6:
        return default_max_hours
    ordered = sorted(timestamps_sec)
    deltas_h = [
        (ordered[i] - ordered[i - 1]) / 3600.0
        for i in range(1, len(ordered))
        if (ordered[i] - ordered[i - 1]) > 10.0
    ]
    if len(deltas_h) < 4:
        return default_max_hours
    med_dt = statistics.median(deltas_h)
    mad_dt = statistics.median([abs(d - med_dt) for d in deltas_h])
    dynamic_thresh = med_dt + 4.0 * max(1.4826 * mad_dt, 0.1 * med_dt)
    # Clamp between 24h and 72h for ambient household sensors to avoid false alarms overnight
    return max(default_max_hours, min(dynamic_thresh, 72.0))


def compute_flatline_age_hours(
    values: list[float],
    timestamps_sec: list[float],
    fallback_age_hours: float,
    now_ts: float,
) -> float:
    """Compute how many hours the sensor has remained at its current constant value.

    Inspects the historical value series so that reloading an integration (which resets
    HA's state.last_updated without changing the numeric reading) does not mask a frozen sensor.
    """
    if len(values) < 2 or len(values) != len(timestamps_sec):
        return fallback_age_hours
    latest_val = values[-1]
    last_change_ts = timestamps_sec[0]
    for val, ts in zip(reversed(values), reversed(timestamps_sec)):
        if abs(val - latest_val) > 1e-4:
            break
        last_change_ts = ts
    hist_flatline_h = max(0.0, (now_ts - last_change_ts) / 3600.0)
    return max(fallback_age_hours, hist_flatline_h)


def load_heal_state(path: Path = HEAL_STATE_FILE) -> dict[str, Any]:
    """Load persistent self-healing cooldown state."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": {}, "entities": {}}


def save_heal_state(state: dict[str, Any], path: Path = HEAL_STATE_FILE) -> None:
    """Save persistent self-healing cooldown state atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(path)
    except Exception:
        pass


def resolve_entity_config_entry(base_url: str, token: str, entity_id: str) -> tuple[str, str]:
    """Return (domain, config_entry_id) for a Home Assistant entity via template API."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    tpl = (
        "{{ {"
        f'"entries": device_attr(device_id("{entity_id}"), "config_entries") | default([], true) | list'
        "} | to_json }}"
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/template",
            data=json.dumps({"template": tpl}).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            info = json.loads(resp.read().decode())
        entry_ids = info.get("entries") or []
        if not entry_ids:
            return "", ""
        entry_id = entry_ids[0]

        e_req = urllib.request.Request(f"{base_url}/api/config/config_entries/entry", headers=headers)
        with urllib.request.urlopen(e_req, timeout=8) as resp:
            all_entries = json.loads(resp.read().decode())
        for e in all_entries:
            if e.get("entry_id") == entry_id:
                return str(e.get("domain", "")), str(entry_id)
        return "", str(entry_id)
    except Exception:
        return "", ""


def attempt_self_heal(
    base_url: str,
    token: str,
    entity_id: str,
    old_value: float,
    heal_state: dict[str, Any],
    allow_entry_reload: bool = True,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Attempt safe 2-stage self-healing for a frozen or anomalous sensor.

    Stage 1: Trigger `homeassistant.update_entity` to force an active device/cloud poll.
    Stage 2: If allow_entry_reload is True and the integration domain is reload-safe (not in
             NEVER_RELOAD_DOMAINS) and outside the 24h cooldown window, reload the config entry.
    Returns dict with keys: attempted, action, domain, healed, new_value, note.
    """
    now_epoch = now_ts if now_ts is not None else time.time()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    actions_taken: list[str] = []
    domain = ""
    entry_id = ""

    # Stage 1: Force entity poll via homeassistant.update_entity
    try:
        upd_req = urllib.request.Request(
            f"{base_url}/api/services/homeassistant/update_entity",
            data=json.dumps({"entity_id": entity_id}).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(upd_req, timeout=10) as resp:
            if resp.status == 200:
                actions_taken.append("update_entity")
    except Exception:
        pass

    # Stage 2: Scoped config entry reload (with 24h cooldown & mesh exclusion guard)
    if allow_entry_reload:
        domain, entry_id = resolve_entity_config_entry(base_url, token, entity_id)
        if entry_id and domain and domain not in NEVER_RELOAD_DOMAINS:
            entries_map = heal_state.setdefault("entries", {})
            last_reload_ts = float(entries_map.get(entry_id, {}).get("last_reload_ts", 0.0))
            if (now_epoch - last_reload_ts) >= HEAL_COOLDOWN_SECONDS:
                try:
                    rel_req = urllib.request.Request(
                        f"{base_url}/api/config/config_entries/entry/{entry_id}/reload",
                        data=b"{}",
                        headers=headers,
                    )
                    with urllib.request.urlopen(rel_req, timeout=15) as resp:
                        if resp.status == 200:
                            actions_taken.append(f"reload:{domain}")
                            entries_map[entry_id] = {
                                "domain": domain,
                                "entity_id": entity_id,
                                "last_reload_ts": now_epoch,
                            }
                except Exception:
                    pass
            else:
                actions_taken.append(f"cooldown:{domain}")
        elif domain in NEVER_RELOAD_DOMAINS:
            actions_taken.append(f"mesh_protected:{domain}")

    # Re-read state after heal attempt
    time.sleep(1.5)
    new_val = old_value
    try:
        st_req = urllib.request.Request(f"{base_url}/api/states/{entity_id}", headers=headers)
        with urllib.request.urlopen(st_req, timeout=8) as resp:
            st_data = json.loads(resp.read().decode())
            new_val = float(st_data.get("state", old_value))
    except Exception:
        pass

    healed = abs(new_val - old_value) > 1e-4
    action_str = " + ".join(actions_taken) if actions_taken else "none"
    if healed:
        note = f"Auto-healed via `{action_str}` (`{old_value:.1f}` -> `{new_val:.1f}`)"
    elif f"cooldown:{domain}" in actions_taken:
        note = f"Polled `update_entity` (`{domain}` reload in 24h cooldown); still stuck at `{new_val:.1f}`"
    elif f"mesh_protected:{domain}" in actions_taken:
        note = f"Polled `update_entity` (skipped `{domain}` coordinator reload to protect mesh); still `{new_val:.1f}`"
    else:
        note = f"Attempted `{action_str}`, reading unchanged at `{new_val:.1f}` (check device battery or upstream cloud API)"

    return {
        "attempted": bool(actions_taken),
        "action": action_str,
        "domain": domain,
        "healed": healed,
        "new_value": new_val,
        "note": note,
    }


def check_victoriametrics_health(vm_url: str = DEFAULT_VM_URL) -> tuple[bool, int]:
    """Check if VictoriaMetrics is reachable and return (healthy, active_ha_series_count)."""
    try:
        with urllib.request.urlopen(f"{vm_url.rstrip('/')}/health", timeout=5) as resp:
            if resp.status != 200:
                return False, 0
        q_url = f"{vm_url.rstrip('/')}/api/v1/query?query=" + urllib.parse.quote('count({__name__=~"ha_.*"})')
        with urllib.request.urlopen(q_url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            res = data.get("data", {}).get("result", [])
            if res and len(res[0].get("value", [])) >= 2:
                return True, int(float(res[0]["value"][1]))
        return True, 0
    except Exception:
        return False, 0


def query_vm_history(vm_url: str, entity_id: str, days: int = 14) -> tuple[list[float], list[float]]:
    """Query VictoriaMetrics range API for an entity's historical values and timestamps."""
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - (days * 86400)
    promql = f'{{entity="{entity_id}"}}'
    params = urllib.parse.urlencode({
        "query": promql,
        "start": str(start_ts),
        "end": str(end_ts),
        "step": "900s",
    })
    url = f"{vm_url.rstrip('/')}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
        results = payload.get("data", {}).get("result", [])
        vals: list[float] = []
        ts_list: list[float] = []
        for series in results:
            metric_name = series.get("metric", {}).get("__name__", "")
            if metric_name.endswith("_created") or metric_name == "ha_last_updated_time_seconds":
                continue
            for ts, val_str in series.get("values", []):
                try:
                    vals.append(float(val_str))
                    ts_list.append(float(ts))
                except (ValueError, TypeError):
                    pass
            if vals:
                break
        return vals, ts_list
    except Exception:
        return [], []


def analyze_anomalies(
    days: int = 7,
    z_threshold: float = 4.0,
    stale_hours: float = 24.0,
    vm_url: str = DEFAULT_VM_URL,
    auto_heal: bool = True,
) -> dict[str, Any]:
    """Run hybrid VictoriaMetrics + HA History statistical anomaly detection and self-healing."""
    base_url, token = get_ha_config()
    if not token:
        return {"ok": False, "error": "Home Assistant token not found."}

    vm_ok, vm_series_count = check_victoriametrics_health(vm_url)

    req = urllib.request.Request(
        f"{base_url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            states = json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"Failed to query Home Assistant states: {e}"}

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    monitored: dict[str, dict[str, Any]] = {}

    for s in states:
        eid = s.get("entity_id", "")
        attrs = s.get("attributes", {})
        st = s.get("state", "")
        if not is_monitored_sensor(eid, attrs, st):
            continue
        cur_val = float(st)
        dc = attrs.get("device_class", "")
        lu_str = s.get("last_updated") or s.get("last_changed")
        age_hours = 0.0
        if lu_str:
            try:
                dt = datetime.fromisoformat(lu_str.replace("Z", "+00:00"))
                age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
            except Exception:
                pass
        monitored[eid] = {
            "entity_id": eid,
            "name": attrs.get("friendly_name", eid),
            "device_class": dc,
            "current": cur_val,
            "unit": attrs.get("unit_of_measurement", ""),
            "age_hours": age_hours,
        }

    # Fetch HA history for sensors that don't yet have sufficient VictoriaMetrics coverage
    eids_needing_ha_history: list[str] = []
    history_by_eid: dict[str, tuple[list[float], list[float]]] = {}

    for eid in monitored:
        if vm_ok and vm_series_count > 0:
            vals, ts_list = query_vm_history(vm_url, eid, days=days)
            if len(vals) >= 24:
                history_by_eid[eid] = (vals, ts_list)
                continue
        eids_needing_ha_history.append(eid)

    if eids_needing_ha_history:
        start_iso = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = urllib.parse.urlencode({
            "filter_entity_id": ",".join(eids_needing_ha_history),
            "minimal_response": "true",
            "no_attributes": "true",
            "significant_changes_only": "true",
        })
        h_url = f"{base_url}/api/history/period/{start_iso}?{params}"
        h_req = urllib.request.Request(h_url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(h_req, timeout=25) as resp:
                raw_hist = json.loads(resp.read().decode())
            for series in raw_hist:
                if not series:
                    continue
                eid = series[0].get("entity_id", "")
                if eid not in monitored:
                    continue
                vals = []
                ts_list = []
                for pt in series:
                    try:
                        vals.append(float(pt["state"]))
                        lc = pt.get("last_changed") or pt.get("last_updated")
                        if lc:
                            ts_list.append(datetime.fromisoformat(lc.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError, KeyError):
                        pass
                history_by_eid[eid] = (vals, ts_list)
        except Exception as e:
            return {"ok": False, "error": f"Failed to query Home Assistant history: {e}"}

    heal_state = load_heal_state()
    healed_events: list[dict[str, Any]] = []
    zombies: list[dict[str, Any]] = []
    amplitude_anomalies: list[dict[str, Any]] = []

    for eid, info in monitored.items():
        dc = info["device_class"]
        cur_val = info["current"]
        vals, ts_list = history_by_eid.get(eid, ([], []))
        age_h = compute_flatline_age_hours(vals, ts_list, info["age_hours"], now_ts)
        info["age_hours"] = round(age_h, 1)

        # 1. Zombie / Flatlined Sensor Cadence Check
        is_idle_power = (dc == "power" and cur_val <= 5.0)
        if not is_idle_power:
            thresh_h = compute_overdue_threshold_hours(ts_list, default_max_hours=stale_hours)
            if age_h >= thresh_h:
                heal_res = None
                if auto_heal:
                    heal_res = attempt_self_heal(
                        base_url=base_url,
                        token=token,
                        entity_id=eid,
                        old_value=cur_val,
                        heal_state=heal_state,
                        allow_entry_reload=True,
                        now_ts=now_ts,
                    )
                if heal_res and heal_res.get("healed"):
                    healed_events.append({
                        **info,
                        "old_value": cur_val,
                        "new_value": heal_res["new_value"],
                        "heal_note": heal_res["note"],
                    })
                    cur_val = heal_res["new_value"]
                    info["current"] = cur_val
                else:
                    zombies.append({
                        **info,
                        "threshold_hours": round(thresh_h, 1),
                        "heal_note": heal_res["note"] if heal_res else None,
                    })

        # 2. Rolling MAD Amplitude Outlier Check
        if len(vals) >= 15 and not is_idle_power:
            z_score, med, mad = compute_mad_score(vals, cur_val, dc)
            if z_score >= z_threshold:
                # Poll entity once to ensure outlier isn't a stale cached sample
                if auto_heal:
                    poll_res = attempt_self_heal(
                        base_url=base_url,
                        token=token,
                        entity_id=eid,
                        old_value=cur_val,
                        heal_state=heal_state,
                        allow_entry_reload=False,
                        now_ts=now_ts,
                    )
                    if poll_res.get("healed"):
                        cur_val = poll_res["new_value"]
                        info["current"] = cur_val
                        z_score, med, mad = compute_mad_score(vals, cur_val, dc)
                if z_score >= z_threshold:
                    amplitude_anomalies.append({
                        **info,
                        "z_score": round(z_score, 2),
                        "median": round(med, 1),
                        "mad": round(mad, 2),
                    })

    if auto_heal:
        save_heal_state(heal_state)

    zombies.sort(key=lambda x: x["age_hours"], reverse=True)
    amplitude_anomalies.sort(key=lambda x: x["z_score"], reverse=True)

    return {
        "ok": True,
        "vm_healthy": vm_ok,
        "vm_series_count": vm_series_count,
        "monitored_count": len(monitored),
        "healed_events": healed_events,
        "zombies": zombies,
        "amplitude_anomalies": amplitude_anomalies,
        "has_anomalies": bool(zombies or amplitude_anomalies or healed_events or not vm_ok),
    }


def format_report(res: dict[str, Any], quiet: bool = False) -> str:
    """Format human-readable Discord report (follows mobile viewport rules)."""
    if not res.get("ok"):
        return f"⚠️ **HA Telemetry Anomaly Check Failed:** {res.get('error')}"

    healed = res.get("healed_events", [])
    zombies = res.get("zombies", [])
    amp = res.get("amplitude_anomalies", [])
    vm_ok = res.get("vm_healthy", False)
    vm_series = res.get("vm_series_count", 0)
    monitored = res.get("monitored_count", 0)

    if not zombies and not amp and not healed and vm_ok:
        if quiet:
            return ""
        src = f"VictoriaMetrics ({vm_series} series)" if vm_series > 0 else "HA 7d Recorder + VictoriaMetrics (:8428 online)"
        return f"✅ **HA Telemetry Nominal:** {monitored} climate/power sensors within rolling MAD & cadence baselines ({src})."

    lines = ["📡 **Home Assistant Telemetry Anomaly Report**\n"]

    if not vm_ok:
        lines.append("- ⚠️ **VictoriaMetrics TSDB (`:8428`):** Unreachable on Host 1 (falling back to HA 7d recorder).")

    if healed:
        lines.append("### 🛠️ Auto-Healed Sensors")
        for h in healed:
            lines.append(f"- **{h['name']}** (`{h['entity_id']}`): {h['heal_note']}")
        lines.append("")

    if zombies:
        lines.append("### 🧟 Frozen / Zombie Sensors (Marked `available` but Stale)")
        for z in zombies:
            line = f"- **{z['name']}** (`{z['entity_id']}`): stuck at `{z['current']:.1f}{z['unit']}` for **{z['age_hours']:.1f}h** (cadence limit `{z['threshold_hours']:.0f}h`)"
            if z.get("heal_note"):
                line += f"\n  - *Self-Heal:* {z['heal_note']}"
            lines.append(line)
        lines.append("")

    if amp:
        lines.append("### 📈 Statistical Amplitude Outliers (Rolling MAD >= 4.0σ)")
        for a in amp:
            lines.append(
                f"- **{a['name']}** (`{a['entity_id']}`): `{a['current']:.1f}{a['unit']}` vs 7d median `{a['median']:.1f}{a['unit']}` (**`{a['z_score']:.1f}σ`**, MAD `{a['mad']:.2f}`)"
            )
        lines.append("")

    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant Statistical Telemetry Anomaly Detector")
    parser.add_argument("--days", type=int, default=7, help="Rolling baseline window in days (default: 7)")
    parser.add_argument("--z-threshold", type=float, default=4.0, help="MAD Z-score threshold (default: 4.0)")
    parser.add_argument("--stale-hours", type=float, default=24.0, help="Minimum hours before flagging a frozen sensor")
    parser.add_argument("--no-heal", action="store_true", help="Disable active self-healing")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--quiet", action="store_true", help="Suppress output when all sensors are nominal")
    parser.add_argument("--test", action="store_true", help="Run self-test mode")
    args = parser.parse_args()

    res = analyze_anomalies(
        days=args.days,
        z_threshold=args.z_threshold,
        stale_hours=args.stale_hours,
        auto_heal=not args.no_heal,
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    rep = format_report(res, quiet=args.quiet)
    if rep:
        print(rep)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
