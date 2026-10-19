#!/usr/bin/env python3
"""Unit tests for Home Assistant Statistical Telemetry Anomaly Detector & Self-Healer (Task #58)."""

import sys

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.ha_anomaly_detector import (
    NEVER_RELOAD_DOMAINS,
    compute_flatline_age_hours,
    compute_mad_score,
    compute_overdue_threshold_hours,
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
        "healed_events": [],
        "zombies": [],
        "amplitude_anomalies": [],
        "has_anomalies": False,
    }
    assert format_report(nominal, quiet=True) == ""
    assert "HA Telemetry Nominal" in format_report(nominal, quiet=False)

    alert = {
        "ok": True,
        "vm_healthy": True,
        "vm_series_count": 0,
        "monitored_count": 88,
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
        "has_anomalies": True,
    }
    out = format_report(alert, quiet=True)
    assert "Auto-Healed Sensors" in out
    assert "Frozen / Zombie Sensors" in out
    assert "64.2h" in out
    assert "reload:nest_protect" in out


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


