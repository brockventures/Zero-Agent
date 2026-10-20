#!/usr/bin/env python3
"""Home Assistant & UniFi Statistical Telemetry Anomaly Detector & Self-Healer (Task #58).

Architecture (adapted from Alex/Arcane & Aerial PR #57):
- Primary long-term TSDB: VictoriaMetrics on Host 1 (:8428, 5y retention, /api/prometheus scrape).
- Automatic Day-1 Hybrid Fallback: Queries VictoriaMetrics PromQL range series first and
  transparently supplements from Home Assistant's native /api/history/period + /api/states
  whenever a sensor has <24h of samples in VictoriaMetrics.
- Four deterministic statistical engines (zero LLM tokens on nominal days):
  1. Zombie / Flatlined Cadence Engine: Detects sensors reporting 'available' whose
     readings have flatlined (>24h for ambient climate/CO2 sensors or >overdue threshold).
  2. Rolling MAD Amplitude Engine: Computes rolling Median and Median Absolute Deviation (MAD),
     flagging z_mad >= 4.0 outliers while filtering expected diurnal solar/battery TOU swings.
  3. Safe Self-Healing Engine:
     - Stage 1: Polls anomalous/frozen entities via `homeassistant.update_entity`.
     - Stage 2: For frozen sensors on reload-safe integrations (excluding ZHA/Z-Wave/Matter/Thread
       mesh coordinators to prevent unload deadlocks), reloads the integration config entry with a
       24-hour per-entry cooldown and verifies whether the reading recovered.
  4. UniFi Per-Device Network Traffic Engine:
     - Queries UCG-Max 30-day daily (`daily.user`) and 24-hour hourly (`hourly.user`) traffic reports.
     - Exports compact daily/3h per-client byte summaries to VictoriaMetrics (`/api/v1/import/prometheus`).
     - Detects runaway firmware download/upload loops (e.g., Google Home speaker 100+ GB/day bug)
       using a drift-resistant P25/median baseline, category-aware noise floors, and a 3-hour
       liveness gate that distinguishes active loops from already-subsided spikes.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
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
HEAL_COOLDOWN_SECONDS = 23 * 3600
ENV_FILE = Path("/secrets/env.json")

# Device classes monitored by the statistical engines
MONITORED_CLASSES = {"temperature", "humidity", "carbon_dioxide", "power", "voltage"}

# Minimum denominator floor per device class to prevent division-by-near-zero on ultra-stable sensors
MIN_SIGMA_FLOOR = {
    "temperature": 1.5,      # °F
    "humidity": 3.0,         # %
    "carbon_dioxide": 50.0,  # ppm
    "power": 25.0,           # W
    "voltage": 4.0,          # V (120V/240V mains & UPS voltage stability)
    "energy_daily": 2.5,     # kWh/day for Emporia Vue daily circuit totals
    "net_iot": 2.5,          # GB/day for IoT / smart speakers
    "net_camera": 5.0,       # GB/day for cameras
    "net_general": 25.0,     # GB/day for general clients (phones, tablets, TVs)
}

# Hard daily ceilings (GB/day) for IoT devices to prevent multi-week firmware loops from normalizing into the median
IOT_HARD_CEILING_GB = 20.0
CAMERA_HARD_CEILING_GB = 50.0

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
    "inverter_",
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

# High-throughput NAS / desktop / server host substrings excluded from client network spike alerts
UNIFI_INFRA_SUBSTRINGS = (
    os.environ.get("NAS_HOST_1_NAME", "nas-host-1").lower(),
    os.environ.get("NAS_HOST_2_NAME", "nas-host-2").lower(),
    "synology",
    "nas-",
    "macbook",
)

IOT_SPEAKER_SUBSTRINGS = (
    "google-home",
    "google-nest",
    "nest-mini",
    "home-mini",
    "fuchsia",
    "hub",
    "speaker",
    "echo",
    "sonos",
    "homepod",
    "thermostat",
    "nest-connect",
    "switchbot",
    "shelly",
    "esphome",
    "tuya",
    "roborock",
    "roomba",
)

CAMERA_SUBSTRINGS = (
    "nest-cam",
    "doorbell",
    "camera",
    "reolink",
    "wyze",
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
        val = float(state_val)
    except (ValueError, TypeError):
        return False
    # For voltage sensors, only monitor 120V/240V mains & UPS rails (>= 50V), skipping 3V coin cells or inactive 0V phases
    if dc == "voltage" and val < 50.0:
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


def classify_unifi_client(name: str, oui: str = "", is_wired: bool = False) -> str:
    """Classify a UniFi client into ('infra', 'iot_speaker', 'camera', 'general')."""
    low = f"{name} {oui}".lower()
    if any(k in low for k in UNIFI_INFRA_SUBSTRINGS):
        return "infra"
    if any(k in low for k in CAMERA_SUBSTRINGS):
        return "camera"
    if any(k in low for k in IOT_SPEAKER_SUBSTRINGS) and not is_wired:
        return "iot_speaker"
    return "general"


def evaluate_client_traffic_series(
    daily_gb_series: list[float],
    last_24h_gb: float,
    last_3h_gb: float,
    category: str,
    z_threshold: float = 4.0,
) -> dict[str, Any]:
    """Evaluate a client's 24h and 3h traffic (in GB) against its 30-day daily history.

    Uses the lower-half (uncontaminated) distribution for `iot_speaker` devices so that
    multi-week firmware download loops (e.g. 13 straight days at 150 GB/day) cannot shift
    the rolling median upward and mask themselves.
    """
    if category == "infra" or len(daily_gb_series) < 5:
        return {"anomalous_24h": False, "active_3h": False, "z_score": 0.0, "baseline_gb": 0.0, "mad_gb": 0.0}

    clean_series = sorted(daily_gb_series)
    if category == "iot_speaker":
        # Filter out days above the IoT runaway ceiling when building the baseline
        below_cap = [x for x in clean_series if x < IOT_HARD_CEILING_GB]
        ref_series = below_cap if len(below_cap) >= 5 else clean_series[: max(5, len(clean_series) // 2)]
        dc_key = "net_iot"
        hard_cap = IOT_HARD_CEILING_GB
    elif category == "camera":
        dc_key = "net_camera"
        ref_series = clean_series
        hard_cap = CAMERA_HARD_CEILING_GB
    else:
        dc_key = "net_general"
        ref_series = clean_series
        hard_cap = None

    med = statistics.median(ref_series)
    if category == "general" and med >= 20.0:
        return {"anomalous_24h": False, "active_3h": False, "z_score": 0.0, "baseline_gb": round(med, 2), "mad_gb": 0.0}
    mad = statistics.median([abs(x - med) for x in ref_series])
    min_floor = MIN_SIGMA_FLOOR[dc_key]
    denom = max(1.4826 * mad, min_floor)
    z_score = max(0.0, (last_24h_gb - med) / denom)

    breached_mad = z_score >= z_threshold and last_24h_gb >= (med + min_floor * 2.0)
    breached_cap = bool(hard_cap is not None and last_24h_gb >= hard_cap)
    anomalous_24h = breached_mad or breached_cap

    # Active 3-hour liveness check: is the device still pulling elevated traffic right now?
    expected_3h_gb = max(0.25, med * (3.0 / 24.0))
    active_floor_gb = 0.75 if category == "iot_speaker" else 2.0
    active_3h = anomalous_24h and (last_3h_gb >= max(active_floor_gb, expected_3h_gb * 3.0))

    return {
        "anomalous_24h": anomalous_24h,
        "active_3h": active_3h,
        "z_score": round(z_score, 2),
        "baseline_gb": round(med, 2),
        "mad_gb": round(mad, 2),
    }


def _get_unifi_credentials() -> tuple[str, str, str]:
    """Load (base_url, username, password) from environment or /secrets/env.json."""
    base = os.environ.get("UNIFI_BASE_URL", "")
    user = os.environ.get("UNIFI_USER", "")
    pw = os.environ.get("UNIFI_PASSWORD", "")
    if (not base or not user or not pw) and ENV_FILE.exists():
        try:
            env_data = json.loads(ENV_FILE.read_text(encoding="utf-8"))
            base = base or env_data.get("UNIFI_BASE_URL", "")
            user = user or env_data.get("UNIFI_USER", "")
            pw = pw or env_data.get("UNIFI_PASSWORD", "")
        except Exception:
            pass
    return base.rstrip("/"), user, pw


def _resolve_cast_eureka_name(ip: str, ctx: ssl.SSLContext) -> str:
    """Query local Google Cast / Home device eureka_info for friendly room name (fast 1s timeout)."""
    if not ip:
        return ""
    for url in (f"http://{ip}:8008/setup/eureka_info", f"https://{ip}:8443/setup/eureka_info"):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ctx, timeout=1.2) as resp:
                data = json.loads(resp.read().decode())
                name = str(data.get("name") or "").strip()
                if name:
                    return name
        except Exception:
            pass
    return ""


def export_unifi_to_victoriametrics(vm_url: str, client_summaries: list[dict[str, Any]]) -> bool:
    """Push compact per-client 24h/3h traffic gauges to VictoriaMetrics via Prometheus import API."""
    if not vm_url or not client_summaries:
        return False
    lines: list[str] = []
    now_ms = int(time.time() * 1000)
    for c in client_summaries:
        mac = str(c.get("mac", "")).replace('"', "")
        name = str(c.get("name", "")).replace('"', "")
        cat = str(c.get("category", "general")).replace('"', "")
        labels = f'mac="{mac}",name="{name}",category="{cat}"'
        lines.append(f"unifi_client_download_gb_24h{{{labels}}} {c.get('down_24h_gb', 0.0):.4f} {now_ms}")
        lines.append(f"unifi_client_upload_gb_24h{{{labels}}} {c.get('up_24h_gb', 0.0):.4f} {now_ms}")
        lines.append(f"unifi_client_download_gb_3h{{{labels}}} {c.get('down_3h_gb', 0.0):.4f} {now_ms}")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{vm_url.rstrip('/')}/api/v1/import/prometheus",
            data=payload,
            headers={"Content-Type": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def analyze_unifi_traffic_anomalies(
    days: int = 30,
    z_threshold: float = 4.0,
    vm_url: str = DEFAULT_VM_URL,
) -> dict[str, Any]:
    """Query UCG-Max per-device traffic, export to VictoriaMetrics, and detect runaway bandwidth loops."""
    unifi_base, unifi_user, unifi_pw = _get_unifi_credentials()
    if not unifi_base or not unifi_user or not unifi_pw:
        return {"ok": False, "monitored_clients": 0, "active_anomalies": [], "subsided_spikes": []}

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(cj),
    )

    try:
        login_payload = json.dumps({"username": unifi_user, "password": unifi_pw}).encode("utf-8")
        login_req = urllib.request.Request(
            f"{unifi_base}/api/auth/login",
            data=login_payload,
            headers={"Content-Type": "application/json"},
        )
        with opener.open(login_req, timeout=8) as resp:
            csrf_token = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token") or ""

        with opener.open(urllib.request.Request(f"{unifi_base}/proxy/network/api/s/default/stat/sta"), timeout=8) as resp:
            sta_data = json.loads(resp.read().decode()).get("data", [])

        client_meta: dict[str, dict[str, Any]] = {}
        for cl in sta_data:
            mac = cl.get("mac")
            if not mac:
                continue
            raw_name = cl.get("name") or cl.get("hostname") or mac
            oui = cl.get("oui", "")
            is_wired = bool(cl.get("is_wired", False))
            cat = classify_unifi_client(raw_name, oui=oui, is_wired=is_wired)
            client_meta[mac] = {
                "mac": mac,
                "name": raw_name,
                "ip": cl.get("ip", ""),
                "oui": oui,
                "is_wired": is_wired,
                "category": cat,
            }

        if not client_meta:
            return {"ok": True, "monitored_clients": 0, "active_anomalies": [], "subsided_spikes": []}

        now_ms = int(time.time() * 1000)
        headers = {"Content-Type": "application/json"}
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token

        rep_daily = json.dumps({
            "attrs": ["rx_bytes", "tx_bytes", "time"],
            "start": now_ms - (days * 86400 * 1000),
            "end": now_ms,
            "macs": list(client_meta.keys()),
        }).encode("utf-8")
        with opener.open(
            urllib.request.Request(
                f"{unifi_base}/proxy/network/api/s/default/stat/report/daily.user",
                data=rep_daily,
                headers=headers,
            ),
            timeout=10,
        ) as resp:
            daily_rows = json.loads(resp.read().decode()).get("data", [])

        rep_hourly = json.dumps({
            "attrs": ["rx_bytes", "tx_bytes", "time"],
            "start": now_ms - (24 * 3600 * 1000),
            "end": now_ms,
            "macs": list(client_meta.keys()),
        }).encode("utf-8")
        with opener.open(
            urllib.request.Request(
                f"{unifi_base}/proxy/network/api/s/default/stat/report/hourly.user",
                data=rep_hourly,
                headers=headers,
            ),
            timeout=10,
        ) as resp:
            hourly_rows = json.loads(resp.read().decode()).get("data", [])
    except Exception as e:
        return {"ok": False, "error": str(e), "monitored_clients": 0, "active_anomalies": [], "subsided_spikes": []}

    by_mac_daily: dict[str, list[dict[str, Any]]] = {}
    for row in daily_rows:
        m = row.get("user") or row.get("mac")
        if m:
            by_mac_daily.setdefault(m, []).append(row)

    by_mac_hourly: dict[str, list[dict[str, Any]]] = {}
    for row in hourly_rows:
        m = row.get("user") or row.get("mac")
        if m:
            by_mac_hourly.setdefault(m, []).append(row)

    active_anomalies: list[dict[str, Any]] = []
    subsided_spikes: list[dict[str, Any]] = []
    vm_summaries: list[dict[str, Any]] = []
    monitored_count = 0

    for mac, meta in client_meta.items():
        cat = meta["category"]
        if cat == "infra":
            continue
        d_rows = sorted(by_mac_daily.get(mac, []), key=lambda x: x.get("time", 0))
        h_rows = sorted(by_mac_hourly.get(mac, []), key=lambda x: x.get("time", 0))
        if len(d_rows) < 5:
            continue
        monitored_count += 1

        # In UniFi wireless stat/report, tx_bytes is AP->Client (Client Download) and rx_bytes is Client->AP (Client Upload).
        # For wired switch ports, rx_bytes/tx_bytes are port-relative.
        is_wired = meta["is_wired"]
        down_key = "rx_bytes" if is_wired else "tx_bytes"
        up_key = "tx_bytes" if is_wired else "rx_bytes"

        # Exclude current partial day when computing historical daily reference series
        hist_down_gb = [x.get(down_key, 0) / (1024**3) for x in d_rows[:-1]]
        hist_up_gb = [x.get(up_key, 0) / (1024**3) for x in d_rows[:-1]]

        down_24h_gb = sum(x.get(down_key, 0) for x in h_rows) / (1024**3)
        up_24h_gb = sum(x.get(up_key, 0) for x in h_rows) / (1024**3)
        down_3h_gb = sum(x.get(down_key, 0) for x in h_rows[-3:]) / (1024**3)
        up_3h_gb = sum(x.get(up_key, 0) for x in h_rows[-3:]) / (1024**3)

        vm_summaries.append({
            "mac": mac,
            "name": meta["name"],
            "category": cat,
            "down_24h_gb": down_24h_gb,
            "up_24h_gb": up_24h_gb,
            "down_3h_gb": down_3h_gb,
        })

        for direction, hist_series, cur_24h, cur_3h in (
            ("Download", hist_down_gb, down_24h_gb, down_3h_gb),
            ("Upload", hist_up_gb, up_24h_gb, up_3h_gb),
        ):
            eval_res = evaluate_client_traffic_series(
                daily_gb_series=hist_series,
                last_24h_gb=cur_24h,
                last_3h_gb=cur_3h,
                category=cat,
                z_threshold=z_threshold,
            )
            if eval_res["anomalous_24h"]:
                friendly = _resolve_cast_eureka_name(meta["ip"], ctx) if cat == "iot_speaker" else ""
                display_name = f"{friendly} ({meta['name']})" if friendly and friendly.lower() != meta["name"].lower() else meta["name"]
                entry = {
                    "mac": mac,
                    "name": display_name,
                    "ip": meta["ip"],
                    "category": cat,
                    "direction": direction,
                    "current_24h_gb": round(cur_24h, 2),
                    "last_3h_gb": round(cur_3h, 2),
                    "baseline_gb": eval_res["baseline_gb"],
                    "mad_gb": eval_res["mad_gb"],
                    "z_score": eval_res["z_score"],
                }
                if eval_res["active_3h"]:
                    active_anomalies.append(entry)
                else:
                    subsided_spikes.append(entry)

    export_unifi_to_victoriametrics(vm_url, vm_summaries)
    active_anomalies.sort(key=lambda x: x["current_24h_gb"], reverse=True)
    subsided_spikes.sort(key=lambda x: x["current_24h_gb"], reverse=True)

    return {
        "ok": True,
        "monitored_clients": monitored_count,
        "active_anomalies": active_anomalies,
        "subsided_spikes": subsided_spikes,
    }


def evaluate_solar_inverter_shares(
    panel_daily_wh: dict[str, list[tuple[int, float]]],
    min_array_wh: float = 3000.0,
) -> dict[str, Any]:
    """Evaluate per-microinverter daily Wh and weather-normalized array share (%) across days.

    Because clouds reduce all panels proportionally, each inverter's percentage share of the
    total 22-panel daily output is invariant to weather. Flags any inverter that:
      1. Drops to < 15 Wh on a sunny/producing day (array >= 3.0 kWh), OR
      2. Drops below 50% of its historical median array share AND >= 1.0 percentage point below median.
    """
    if not panel_daily_wh:
        return {"monitored_inverters": 0, "solar_anomalies": [], "total_array_kwh": 0.0}

    by_ts_total: dict[int, float] = {}
    for pts in panel_daily_wh.values():
        for ts, wh in pts:
            by_ts_total[ts] = by_ts_total.get(ts, 0.0) + max(0.0, wh)

    valid_ts = sorted(ts for ts, tot in by_ts_total.items() if tot >= min_array_wh)
    if len(valid_ts) < 3:
        latest_tot = by_ts_total[max(by_ts_total.keys())] / 1000.0 if by_ts_total else 0.0
        return {"monitored_inverters": len(panel_daily_wh), "solar_anomalies": [], "total_array_kwh": round(latest_tot, 2)}

    latest_ts = valid_ts[-1]
    latest_array_wh = by_ts_total[latest_ts]
    hist_ts = valid_ts[:-1]
    solar_anomalies: list[dict[str, Any]] = []

    for eid, pts in sorted(panel_daily_wh.items()):
        pt_map = {ts: max(0.0, wh) for ts, wh in pts}
        if latest_ts not in pt_map:
            continue
        cur_wh = pt_map[latest_ts]
        cur_share = (cur_wh / latest_array_wh) * 100.0
        hist_shares = [
            (pt_map[t] / by_ts_total[t]) * 100.0
            for t in hist_ts
            if t in pt_map and by_ts_total[t] >= min_array_wh
        ]
        if len(hist_shares) < 2:
            continue
        med_share = statistics.median(hist_shares)
        mad_share = statistics.median([abs(s - med_share) for s in hist_shares])
        denom = max(1.4826 * mad_share, 0.25)
        z_drop = max(0.0, (med_share - cur_share) / denom)

        is_dead = (cur_wh < 15.0 and med_share >= 0.3)
        is_degraded = (cur_share < (med_share * 0.5) and (med_share - cur_share) >= 1.0 and z_drop >= 4.0)
        if is_dead or is_degraded:
            solar_anomalies.append({
                "entity_id": eid,
                "current_wh": round(cur_wh, 1),
                "current_share_pct": round(cur_share, 2),
                "median_share_pct": round(med_share, 2),
                "z_score": round(z_drop, 1),
                "reason": "dead_zero_output" if is_dead else "share_drop",
            })

    return {
        "monitored_inverters": len(panel_daily_wh),
        "solar_anomalies": solar_anomalies,
        "total_array_kwh": round(latest_array_wh / 1000.0, 2),
    }


def analyze_vm_extended_telemetry(vm_url: str = DEFAULT_VM_URL, days: int = 14) -> dict[str, Any]:
    """Query VictoriaMetrics for Solar Microinverters (22), Emporia Daily Circuits (17), and Battery Drain Velocity (30+)."""
    end_ts = int(time.time())
    start_ts = end_ts - (days * 86400)
    out: dict[str, Any] = {
        "solar_inverters_count": 0,
        "solar_array_kwh": 0.0,
        "solar_anomalies": [],
        "circuit_energy_count": 0,
        "circuit_anomalies": [],
        "battery_checked_count": 0,
        "battery_drain_anomalies": [],
    }

    # 1. 22 Solar Microinverters (24h integrated Wh & weather-normalized array share %)
    try:
        promql_solar = 'avg_over_time(ha_sensor_power_w{entity=~"sensor.inverter_.*"}[24h]) * 24'
        params = urllib.parse.urlencode({"query": promql_solar, "start": str(start_ts), "end": str(end_ts), "step": "86400"})
        with urllib.request.urlopen(f"{vm_url.rstrip('/')}/api/v1/query_range?{params}", timeout=8) as resp:
            s_res = json.loads(resp.read().decode()).get("data", {}).get("result", [])
        panel_daily_wh = {
            s["metric"]["entity"]: [(int(ts), float(v)) for ts, v in s.get("values", [])]
            for s in s_res
            if s.get("metric", {}).get("entity")
        }
        s_eval = evaluate_solar_inverter_shares(panel_daily_wh)
        out["solar_inverters_count"] = s_eval["monitored_inverters"]
        out["solar_array_kwh"] = s_eval["total_array_kwh"]
        out["solar_anomalies"] = s_eval["solar_anomalies"]
    except Exception:
        pass

    # 2. Emporia Vue Daily Circuit Energy (*_energy_today compared at same time of day over 14d)
    try:
        promql_vue = 'ha_sensor_energy_kwh{entity=~"sensor.*_energy_today.*"}'
        params_vue = urllib.parse.urlencode({"query": promql_vue, "start": str(start_ts), "end": str(end_ts), "step": "86400"})
        with urllib.request.urlopen(f"{vm_url.rstrip('/')}/api/v1/query_range?{params_vue}", timeout=8) as resp:
            v_res = json.loads(resp.read().decode()).get("data", {}).get("result", [])
        for s in v_res:
            eid = s.get("metric", {}).get("entity", "")
            fname = s.get("metric", {}).get("friendly_name", eid)
            low_comb = f"{eid} {fname}".lower()
            if (
                not eid
                or any(k in low_comb for k in ("production", "forecast", "car charger", "water_heater_energy_today", "total home energy", "balance_energy"))
                or eid == "sensor.vue_energy_monitor_energy_today"
            ):
                continue
            vals = [float(v) for _, v in s.get("values", [])]
            if len(vals) < 4:
                continue
            out["circuit_energy_count"] += 1
            cur_kwh = vals[-1]
            hist_kwh = vals[:-1]
            med = statistics.median(hist_kwh)
            mad = statistics.median([abs(x - med) for x in hist_kwh])
            denom = max(1.4826 * mad, MIN_SIGMA_FLOOR["energy_daily"])
            z_score = max(0.0, (cur_kwh - med) / denom)
            if z_score >= 4.0 and cur_kwh >= (med + 5.0):
                out["circuit_anomalies"].append({
                    "entity_id": eid,
                    "name": s.get("metric", {}).get("friendly_name", eid),
                    "current_kwh": round(cur_kwh, 2),
                    "median_kwh": round(med, 2),
                    "z_score": round(z_score, 1),
                })
    except Exception:
        pass

    # 3. Household Sensor / Lock / Blind 24h Battery Drain Velocity (>= 15% drop in 24h)
    try:
        q_now = urllib.parse.quote("ha_sensor_battery_percent")
        q_max = urllib.parse.quote("max_over_time(ha_sensor_battery_percent[24h])")
        with urllib.request.urlopen(f"{vm_url.rstrip('/')}/api/v1/query?query={q_now}", timeout=5) as r1, \
             urllib.request.urlopen(f"{vm_url.rstrip('/')}/api/v1/query?query={q_max}", timeout=5) as r2:
            b_now = {
                x["metric"]["entity"]: (float(x["value"][1]), x["metric"].get("friendly_name", x["metric"]["entity"]))
                for x in json.loads(r1.read().decode()).get("data", {}).get("result", [])
            }
            b_max = {
                x["metric"]["entity"]: float(x["value"][1])
                for x in json.loads(r2.read().decode()).get("data", {}).get("result", [])
            }
        skip_bat = ("phone", "pixel", "fold", "watch", "ev9", "encharge", "envoy", "ipad", "tablet")
        for eid, (cur_pct, fname) in b_now.items():
            if any(k in eid.lower() or k in fname.lower() for k in skip_bat):
                continue
            out["battery_checked_count"] += 1
            max_pct = b_max.get(eid, cur_pct)
            drop_pct = max_pct - cur_pct
            if drop_pct >= 15.0:
                out["battery_drain_anomalies"].append({
                    "entity_id": eid,
                    "name": fname,
                    "current_pct": round(cur_pct, 1),
                    "max_24h_pct": round(max_pct, 1),
                    "drop_24h_pct": round(drop_pct, 1),
                })
    except Exception:
        pass

    return out


def compute_overdue_threshold_hours(
    timestamps_sec: list[float],
    default_max_hours: float = 24.0,
    values: list[float] | None = None,
) -> float:
    """Compute dynamic cadence overdue threshold in hours from historical state change intervals."""
    if values is not None and len(values) == len(timestamps_sec) and len(values) >= 2:
        paired = sorted(zip(timestamps_sec, values), key=lambda x: x[0])
        change_ts = [paired[0][0]]
        last_val = paired[0][1]
        for ts, val in paired[1:]:
            if abs(val - last_val) > 1e-4:
                change_ts.append(ts)
                last_val = val
    else:
        change_ts = sorted(timestamps_sec)
    if len(change_ts) < 5:
        return default_max_hours
    deltas_h = [
        (change_ts[i] - change_ts[i - 1]) / 3600.0
        for i in range(1, len(change_ts))
        if (change_ts[i] - change_ts[i - 1]) > 10.0
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


def query_vm_history(vm_url: str, entity_id: str, days: int = 14, unit: str = "") -> tuple[list[float], list[float]]:
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
        ignored_exact = {
            "ha_entity_available",
            "ha_entity_info",
            "ha_last_updated_time_seconds",
            "ha_state_change_total",
            "ha_sensor_timestamp_seconds",
        }
        for series in results:
            metric_name = series.get("metric", {}).get("__name__", "")
            if (
                metric_name in ignored_exact
                or metric_name.endswith(("_created", "_total", "_info"))
            ):
                continue
            is_celsius_to_f = metric_name.endswith("_celsius") and unit == "°F"
            is_kw_to_w = metric_name.endswith("_kw") and unit == "W"
            for ts, val_str in series.get("values", []):
                try:
                    val = float(val_str)
                    if is_celsius_to_f:
                        val = val * 9.0 / 5.0 + 32.0
                    elif is_kw_to_w:
                        val = val * 1000.0
                    vals.append(val)
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
    include_unifi: bool = True,
) -> dict[str, Any]:
    """Run hybrid VictoriaMetrics + HA History + UniFi statistical anomaly detection and self-healing."""
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
            vals, ts_list = query_vm_history(vm_url, eid, days=days, unit=monitored[eid].get("unit", ""))
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
            thresh_h = compute_overdue_threshold_hours(ts_list, default_max_hours=stale_hours, values=vals)
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

    unifi_res: dict[str, Any] = {"monitored_clients": 0, "active_anomalies": [], "subsided_spikes": []}
    if include_unifi:
        unifi_res = analyze_unifi_traffic_anomalies(days=30, z_threshold=z_threshold, vm_url=vm_url)

    network_anomalies = unifi_res.get("active_anomalies", [])
    subsided_network_spikes = unifi_res.get("subsided_spikes", [])
    unifi_clients_count = unifi_res.get("monitored_clients", 0)

    ext_vm = analyze_vm_extended_telemetry(vm_url=vm_url, days=14) if vm_ok else {}
    solar_anomalies = ext_vm.get("solar_anomalies", [])
    circuit_anomalies = ext_vm.get("circuit_anomalies", [])
    battery_drain_anomalies = ext_vm.get("battery_drain_anomalies", [])
    solar_inverters_count = ext_vm.get("solar_inverters_count", 0)
    circuit_energy_count = ext_vm.get("circuit_energy_count", 0)
    battery_checked_count = ext_vm.get("battery_checked_count", 0)

    return {
        "ok": True,
        "vm_healthy": vm_ok,
        "vm_series_count": vm_series_count,
        "monitored_count": len(monitored),
        "unifi_clients_count": unifi_clients_count,
        "solar_inverters_count": solar_inverters_count,
        "circuit_energy_count": circuit_energy_count,
        "battery_checked_count": battery_checked_count,
        "healed_events": healed_events,
        "zombies": zombies,
        "amplitude_anomalies": amplitude_anomalies,
        "network_anomalies": network_anomalies,
        "subsided_network_spikes": subsided_network_spikes,
        "solar_anomalies": solar_anomalies,
        "circuit_anomalies": circuit_anomalies,
        "battery_drain_anomalies": battery_drain_anomalies,
        "has_anomalies": bool(
            zombies
            or amplitude_anomalies
            or healed_events
            or network_anomalies
            or solar_anomalies
            or circuit_anomalies
            or battery_drain_anomalies
            or not vm_ok
        ),
    }


def format_report(res: dict[str, Any], quiet: bool = False) -> str:
    """Format human-readable Discord report (follows mobile viewport rules)."""
    if not res.get("ok"):
        return f"⚠️ **HA Telemetry Anomaly Check Failed:** {res.get('error')}"

    healed = res.get("healed_events", [])
    zombies = res.get("zombies", [])
    amp = res.get("amplitude_anomalies", [])
    net_active = res.get("network_anomalies", [])
    net_subsided = res.get("subsided_network_spikes", [])
    solar_anom = res.get("solar_anomalies", [])
    circuit_anom = res.get("circuit_anomalies", [])
    bat_anom = res.get("battery_drain_anomalies", [])
    vm_ok = res.get("vm_healthy", False)
    vm_series = res.get("vm_series_count", 0)
    monitored = res.get("monitored_count", 0)
    unifi_cnt = res.get("unifi_clients_count", 0)
    solar_cnt = res.get("solar_inverters_count", 0)
    circuit_cnt = res.get("circuit_energy_count", 0)
    bat_cnt = res.get("battery_checked_count", 0)

    if not zombies and not amp and not healed and not net_active and not solar_anom and not circuit_anom and not bat_anom and vm_ok:
        if quiet:
            return ""
        src = f"VictoriaMetrics ({vm_series} series)" if vm_series > 0 else "HA 7d Recorder + VictoriaMetrics (:8428 online)"
        extras = []
        if solar_cnt:
            extras.append(f"{solar_cnt} solar microinverters")
        if circuit_cnt:
            extras.append(f"{circuit_cnt} energy circuits")
        if bat_cnt:
            extras.append(f"{bat_cnt} battery curves")
        if unifi_cnt:
            extras.append(f"{unifi_cnt} UniFi clients")
        extra_str = (", " + ", ".join(extras)) if extras else ""
        base_msg = f"✅ **HA & Network Telemetry Nominal:** {monitored} climate/power/voltage sensors{extra_str} within rolling MAD & cadence baselines ({src})."
        if net_subsided:
            sub_items = ", ".join(
                f"**{n['name']}** ({n['current_24h_gb']:.1f} GB 24h {n['direction'].lower()}, subsided to {n['last_3h_gb']:.2f} GB in last 3h)"
                for n in net_subsided
            )
            base_msg += f"\n- ℹ️ *Subsided 24h Network Spike:* {sub_items}"
        return base_msg

    lines = ["📡 **Home Assistant & Network Telemetry Anomaly Report**\n"]

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

    if net_active:
        lines.append("### 🌐 Runaway Network Traffic Anomalies (UniFi 30d Rolling MAD)")
        for n in net_active:
            lines.append(
                f"- **{n['name']}** (`{n['ip']}`): **{n['current_24h_gb']:.1f} GB** {n['direction'].lower()} in 24h "
                f"(**{n['last_3h_gb']:.2f} GB** in last 3h) vs 30d baseline **{n['baseline_gb']:.1f} GB/day** (**{n['z_score']:.1f}σ**)"
            )
        lines.append("")

    if solar_anom:
        lines.append("### ☀️ Solar Microinverter Output Anomalies (Weather-Normalized Array Share)")
        for s in solar_anom:
            lines.append(
                f"- **`{s['entity_id']}`**: **{s['current_wh']:.0f} Wh** today (**{s['current_share_pct']:.2f}%** of array vs "
                f"14d median **{s['median_share_pct']:.2f}%**, **{s['z_score']:.1f}σ**)"
            )
        lines.append("")

    if circuit_anom:
        lines.append("### ⚡ Emporia Circuit Daily Energy Outliers (14d Rolling MAD)")
        for c in circuit_anom:
            lines.append(
                f"- **{c['name']}** (`{c['entity_id']}`): **{c['current_kwh']:.2f} kWh** today vs 14d median "
                f"**{c['median_kwh']:.2f} kWh** (**{c['z_score']:.1f}σ**)"
            )
        lines.append("")

    if bat_anom:
        lines.append("### 🔋 Rapid 24h Battery Drain Anomalies (>= 15%/day Drop)")
        for b in bat_anom:
            lines.append(
                f"- **{b['name']}** (`{b['entity_id']}`): dropped **{b['drop_24h_pct']:.1f}%** in 24h "
                f"(**{b['max_24h_pct']:.0f}%** -> **{b['current_pct']:.0f}%**)"
            )
        lines.append("")

    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant & UniFi Statistical Telemetry Anomaly Detector")
    parser.add_argument("--days", type=int, default=7, help="Rolling baseline window in days (default: 7)")
    parser.add_argument("--z-threshold", type=float, default=4.0, help="MAD Z-score threshold (default: 4.0)")
    parser.add_argument("--stale-hours", type=float, default=24.0, help="Minimum hours before flagging a frozen sensor")
    parser.add_argument("--no-heal", action="store_true", help="Disable active self-healing")
    parser.add_argument("--no-unifi", action="store_true", help="Disable UniFi per-client network traffic check")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--quiet", action="store_true", help="Suppress output when all sensors are nominal")
    parser.add_argument("--test", action="store_true", help="Run self-test mode")
    args = parser.parse_args()

    res = analyze_anomalies(
        days=args.days,
        z_threshold=args.z_threshold,
        stale_hours=args.stale_hours,
        auto_heal=not args.no_heal,
        include_unifi=not args.no_unifi,
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
