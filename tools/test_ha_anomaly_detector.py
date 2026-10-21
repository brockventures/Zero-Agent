#!/usr/bin/env python3
"""Unit tests for Home Assistant & UniFi Statistical Telemetry Anomaly Detector & Self-Healer (Task #58)."""

import sys

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.ha_anomaly_detector import (
    NEVER_RELOAD_DOMAINS,
    classify_unifi_client,
    compute_flatline_age_hours,
    compute_mad_score,
    compute_overdue_threshold_hours,
    evaluate_client_traffic_series,
    format_report,
    is_monitored_sensor,
)


def test_is_monitored_sensor_filters_properly():
    # Valid ambient temperature sensor
    assert is_monitored_sensor(
        "sensor.living_room_temperature",
        {"device_class": "temperature", "friendly_name": "Living Room Temperature"},
        "71.2",
    )
    # Valid power sensor
    assert is_monitored_sensor(
        "sensor.fridge_power",
        {"device_class": "power", "friendly_name": "Fridge Power"},
        "125.0",
    )
    # Excluded set_temperature / solar / encharge / unavailable
    assert not is_monitored_sensor(
        "sensor.my_ev9_set_temperature",
        {"device_class": "temperature", "friendly_name": "EV9 Set Temperature"},
        "72.0",
    )
    assert not is_monitored_sensor(
        "sensor.encharge_492531003077_power",
        {"device_class": "power", "friendly_name": "Encharge Power"},
        "1100.0",
    )
    assert not is_monitored_sensor(
        "sensor.isaac_temperature",
        {"device_class": "temperature", "friendly_name": "Isaac Temperature"},
        "unavailable",
    )


def test_compute_mad_score_detects_outliers_and_respects_floor():
    # Stable room temperature around 70°F with tiny jitter -> min floor 1.5°F prevents false positive at 71°F
    vals = [70.0, 70.1, 69.9, 70.0, 70.2, 69.8, 70.0] * 4
    z_small, med, mad = compute_mad_score(vals, 71.0, "temperature")
    assert med == 70.0
    assert z_small < 1.0

    # Spike to 82.3°F against 73.9°F median -> strong outlier (z > 4.0)
    office_vals = [73.8, 73.9, 74.0, 73.9, 73.7, 74.1, 73.9] * 4
    z_spike, med_o, _ = compute_mad_score(office_vals, 82.3, "temperature")
    assert z_spike >= 5.0

    # Bedroom CO2 rising from daytime open-door baseline (~576 ppm) to normal bedtime closed-door level (992 ppm)
    co2_daytime = [550.0, 576.5, 600.0, 520.0, 620.0, 576.5] * 4
    z_bedtime, _, _ = compute_mad_score(co2_daytime, 992.0, "carbon_dioxide")
    assert z_bedtime < 4.0
    # Genuine high CO2 spike (1850 ppm) still triggers z >= 4.0
    z_co2_high, _, _ = compute_mad_score(co2_daytime, 1850.0, "carbon_dioxide")
    assert z_co2_high >= 4.0


def test_compute_overdue_threshold_hours_clamps_sensibly():
    # Hourly updates over 20 hours -> threshold clamps to default floor (24.0h)
    hourly_ts = [i * 3600.0 for i in range(20)]
    assert compute_overdue_threshold_hours(hourly_ts, default_max_hours=24.0) == 24.0

    # Sparse updates every 8 hours with variance -> dynamic threshold above 24h
    sparse_ts = [0, 8 * 3600, 18 * 3600, 28 * 3600, 40 * 3600, 50 * 3600, 62 * 3600]
    thresh = compute_overdue_threshold_hours(sparse_ts, default_max_hours=24.0)
    assert 24.0 <= thresh <= 72.0


def test_compute_flatline_age_hours_ignores_reload_timestamp_reset():
    now_ts = 100 * 3600.0
    # Sensor changed at t=36h to 66.2, then was reloaded at t=99.9h (still 66.2)
    vals = [67.0, 66.5, 66.2, 66.2]
    ts = [20 * 3600.0, 30 * 3600.0, 36 * 3600.0, 99.9 * 3600.0]
    age_h = compute_flatline_age_hours(vals, ts, fallback_age_hours=0.1, now_ts=now_ts)
    assert abs(age_h - 64.0) < 0.01


def test_mesh_coordinators_excluded_from_auto_reload():
    for protected in ("zha", "zwave_js", "matter", "thread", "otbr"):
        assert protected in NEVER_RELOAD_DOMAINS


def test_format_report_outputs_clean_markdown():
    nominal = {
        "ok": True,
        "vm_healthy": True,
        "vm_series_count": 412,
        "monitored_count": 88,
        "unifi_clients_count": 68,
        "healed_events": [],
        "zombies": [],
        "amplitude_anomalies": [],
        "network_anomalies": [],
        "subsided_network_spikes": [],
        "has_anomalies": False,
    }
    assert format_report(nominal, quiet=True) == ""
    assert "HA & Network Telemetry Nominal" in format_report(nominal, quiet=False)
    assert "68 UniFi clients" in format_report(nominal, quiet=False)

    alert = {
        "ok": True,
        "vm_healthy": True,
        "vm_series_count": 0,
        "monitored_count": 88,
        "unifi_clients_count": 68,
        "healed_events": [
            {
                "entity_id": "sensor.patio_temp",
                "name": "Patio Temp",
                "heal_note": "Auto-healed via `update_entity + reload:switchbot` (`65.0` -> `70.2`)",
            }
        ],
        "zombies": [
            {
                "entity_id": "sensor.nest_temperature_sensor_office_temperature",
                "name": "Nest Temperature Sensor (Office) Temperature",
                "current": 66.2,
                "unit": "°F",
                "age_hours": 64.2,
                "threshold_hours": 24.0,
                "heal_note": "Attempted `update_entity + reload:nest_protect`, reading unchanged at `66.2`",
            }
        ],
        "amplitude_anomalies": [],
        "network_anomalies": [
            {
                "mac": "00:11:22:33:44:55",
                "name": "Kitchen speaker (Google-Home-Speaker)",
                "ip": "198.51.100.214",
                "category": "iot_speaker",
                "direction": "Download",
                "current_24h_gb": 136.3,
                "last_3h_gb": 28.4,
                "baseline_gb": 6.2,
                "mad_gb": 1.1,
                "z_score": 52.0,
            }
        ],
        "subsided_network_spikes": [],
        "has_anomalies": True,
    }
    out = format_report(alert, quiet=True)
    assert "Auto-Healed Sensors" in out
    assert "Frozen / Zombie Sensors" in out
    assert "64.2h" in out
    assert "reload:nest_protect" in out
    assert "Runaway Network Traffic Anomalies" in out
    assert "Kitchen speaker" in out


def test_query_vm_history_filters_metadata_and_converts_celsius(monkeypatch):
    import io
    import json
    import urllib.request
    from tools.ha_anomaly_detector import query_vm_history

    fake_payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "ha_entity_available", "entity": "sensor.bedroom_temperature"},
                    "values": [[1700000000, "1"], [1700000900, "1"]],
                },
                {
                    "metric": {"__name__": "ha_entity_info", "entity": "sensor.bedroom_temperature"},
                    "values": [[1700000000, "1"], [1700000900, "1"]],
                },
                {
                    "metric": {"__name__": "ha_sensor_temperature_celsius", "entity": "sensor.bedroom_temperature"},
                    "values": [[1700000000, "20.0"], [1700000900, "25.0"]],
                },
            ],
        },
    }

    class DummyResp:
        def __enter__(self):
            return io.BytesIO(json.dumps(fake_payload).encode())

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: DummyResp())
    vals, ts = query_vm_history("http://198.51.100.1:8428", "sensor.bedroom_temperature", days=7, unit="°F")
    assert vals == [68.0, 77.0]
    assert ts == [1700000000.0, 1700000900.0]


def test_compute_overdue_threshold_hours_filters_uniform_vm_grid():
    from tools.ha_anomaly_detector import compute_overdue_threshold_hours

    # Simulate a 7-day uniform 15-min (900s) VM grid where a quantized sensor only shifts every ~12-30h
    change_hours = [0.0, 1.0, 12.5, 20.0, 43.75, 75.25]
    ts_list = [1700000000.0 + i * 900.0 for i in range(400)]
    vals = []
    for t in ts_list:
        elapsed_h = (t - 1700000000.0) / 3600.0
        idx = sum(1 for ch in change_hours if elapsed_h >= ch)
        vals.append(66.0 + idx * 0.5)

    thresh = compute_overdue_threshold_hours(ts_list, default_max_hours=24.0, values=vals)
    assert thresh > 48.0


def test_unifi_traffic_anomaly_detection_and_baseline_drift_guard():
    assert classify_unifi_client("Google-Home-Speaker", "Google, Inc.", is_wired=False) == "iot_speaker"
    assert classify_unifi_client("Nest-Cam-indoor", "Google, Inc.", is_wired=False) == "camera"
    assert classify_unifi_client("Living-Room-TV", "Google, Inc.", is_wired=True) == "general"

    # Simulate 17 normal days (~6 GB/day) + 12 days of runaway firmware loop (~150 GB/day)
    speaker_hist = [5.8, 6.2, 6.0, 5.5, 6.4, 7.1, 5.9, 6.3, 6.0, 5.7, 6.1, 6.5, 5.9, 6.2, 6.0, 5.8, 6.1] + [150.0] * 12

    # Case 1: Active runaway loop (136 GB in 24h, 25 GB in last 3h) -> flagged as active
    res_active = evaluate_client_traffic_series(
        daily_gb_series=speaker_hist,
        last_24h_gb=136.3,
        last_3h_gb=25.0,
        category="iot_speaker",
    )
    assert res_active["anomalous_24h"] is True
    assert res_active["active_3h"] is True
    assert res_active["baseline_gb"] < 8.0
    assert res_active["z_score"] >= 10.0

    # Case 2: Subsided runaway loop (136 GB in 24h, but patched 5h ago so last 3h is 0.05 GB) -> anomalous_24h True, active_3h False
    res_subsided = evaluate_client_traffic_series(
        daily_gb_series=speaker_hist,
        last_24h_gb=136.3,
        last_3h_gb=0.05,
        category="iot_speaker",
    )
    assert res_subsided["anomalous_24h"] is True
    assert res_subsided["active_3h"] is False

    # Case 3: Wired Living Room TV watching 4K video (11.8 GB in 24h vs 0.1 GB median) -> ignored by 25 GB general noise floor
    tv_hist = [0.05, 0.08, 0.1, 0.04, 0.02, 0.12, 0.09] * 4
    res_tv = evaluate_client_traffic_series(
        daily_gb_series=tv_hist,
        last_24h_gb=11.8,
        last_3h_gb=11.7,
        category="general",
    )
    assert res_tv["anomalous_24h"] is False


def test_solar_inverter_weather_normalized_share_detection():
    from tools.ha_anomaly_detector import evaluate_solar_inverter_shares

    # 5 days of 10-panel array where each panel normally produces ~10% of the array (even on a cloudy day at day 3)
    days_ts = [1700000000 + i * 86400 for i in range(5)]
    panel_daily_wh = {}
    for p in range(10):
        eid = f"sensor.inverter_test_{p}"
        pts = []
        for d_idx, ts in enumerate(days_ts):
            base_wh = 400.0 if d_idx == 2 else 1000.0  # Day 2 is cloudy (4 kWh total vs 10 kWh total)
            if p == 9 and d_idx == 4:
                # Panel 9 dies on the final day (5 Wh instead of 1000 Wh)
                pts.append((ts, 5.0))
            else:
                pts.append((ts, base_wh))
        panel_daily_wh[eid] = pts

    res = evaluate_solar_inverter_shares(panel_daily_wh, min_array_wh=3000.0)
    assert res["monitored_inverters"] == 10
    assert len(res["solar_anomalies"]) == 1
    assert res["solar_anomalies"][0]["entity_id"] == "sensor.inverter_test_9"
    assert res["solar_anomalies"][0]["reason"] == "dead_zero_output"


def test_attempt_cast_speaker_reboot_respects_cooldown(monkeypatch):
    import urllib.request
    from tools.ha_anomaly_detector import attempt_cast_speaker_reboot

    class DummyResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: DummyResp())
    heal_state = {}
    res1 = attempt_cast_speaker_reboot("198.51.100.214", "00:11:22:33:44:55", heal_state, now_ts=100000.0)
    assert res1["rebooted"] is True
    assert "cast_reboot" in res1["note"]

    # Calling again within 24h should hit cooldown
    res2 = attempt_cast_speaker_reboot("198.51.100.214", "00:11:22:33:44:55", heal_state, now_ts=100500.0)
    assert res2["attempted"] is False
    assert "cooldown" in res2["note"]


