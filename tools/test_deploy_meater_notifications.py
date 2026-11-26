"""Unit tests for tools/deploy_meater_notifications.py."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import deploy_meater_notifications as mod

MOCK_HA_URL = "http://198.51.100.10:8123"
MOCK_HA_TOKEN = "test-token-rfc5737"
MOCK_PHONE_A = "notify.mobile_app_phone_alpha"
MOCK_PHONE_B = "notify.mobile_app_phone_beta"

SAMPLE_EXISTING_AUTOMATIONS_YAML = """- id: '1000001'
  alias: Dryer started
  description: ''
  triggers:
  - trigger: state
    entity_id: sensor.dryer_state
    to: running
  conditions: []
  actions:
  - action: input_boolean.turn_on
    target:
      entity_id: input_boolean.dryer_running
  mode: single
- id: '1000002'
  alias: Washer finished
  description: ''
  triggers:
  - trigger: state
    entity_id: sensor.washer_state
    to: finished
  conditions: []
  actions:
  - action: input_boolean.turn_off
    target:
      entity_id: input_boolean.washer_running
  mode: single
"""


def test_discover_phone_notify_services_maps_ryan_and_emily_and_filters_non_phones() -> None:
    services_payload = [
        {
            "domain": "notify",
            "services": {
                "send_message": {},
                "persistent_notification": {},
                "mobile_app_watch_ultra": {},
                "mobile_app_tablet_pad": {},
                "mobile_app_alpha_handset": {},
                "mobile_app_beta_handset_ext": {},
            },
        }
    ]
    states_payload = [
        {
            "entity_id": "person.ryan_user",
            "state": "home",
            "attributes": {
                "id": "ryan_user",
                "friendly_name": "Ryan",
                "device_trackers": [
                    "device_tracker.watch_ultra",
                    "device_tracker.alpha_handset",
                ],
                "source": "device_tracker.alpha_handset",
            },
        },
        {
            "entity_id": "person.emily_user",
            "state": "home",
            "attributes": {
                "id": "emily_user",
                "friendly_name": "Emily",
                "device_trackers": [
                    "device_tracker.beta_handset",
                ],
                "source": "device_tracker.beta_handset",
            },
        },
    ]

    discovered = mod.discover_phone_notify_services(
        MOCK_HA_URL,
        MOCK_HA_TOKEN,
        services_payload=services_payload,
        states_payload=states_payload,
    )
    assert discovered == [
        "notify.mobile_app_alpha_handset",
        "notify.mobile_app_beta_handset_ext",
    ]


def test_discover_phone_notify_services_supports_cli_override() -> None:
    discovered = mod.discover_phone_notify_services(
        MOCK_HA_URL,
        MOCK_HA_TOKEN,
        notify_services_override="mobile_app_phone_alpha, notify.mobile_app_phone_beta",
    )
    assert discovered == [MOCK_PHONE_A, MOCK_PHONE_B]


def test_build_meater_automations_definitions_and_triggers() -> None:
    automations = mod.build_meater_automations([MOCK_PHONE_A, MOCK_PHONE_B])
    assert len(automations) == 3
    by_id = {a["id"]: a for a in automations}

    # 1. meater_cook_5min_warning
    a_5min = by_id["meater_cook_5min_warning"]
    assert a_5min["alias"] == "MEATER+: 5 Minutes Before Cook Finishes"
    trig_5m = a_5min["triggers"][0]
    assert trig_5m["trigger"] == "numeric_state"
    assert trig_5m["entity_id"] == "sensor.meater_meater_eta"
    assert trig_5m["below"] == 5.5
    assert trig_5m["for"] == "00:00:30"

    conds_5m = a_5min["conditions"]
    assert any(
        c.get("entity_id") == "binary_sensor.meater_meater_connected" and c.get("state") == "on"
        for c in conds_5m
    )
    assert any(
        c.get("entity_id") == "sensor.meater_meater_cook_status"
        and c.get("state") == ["Cooking", "Almost Ready"]
        for c in conds_5m
    )
    assert any("1800" in str(c.get("value_template", "")) for c in conds_5m)

    actions_5m = a_5min["actions"]
    assert [a["action"] for a in actions_5m] == [MOCK_PHONE_A, MOCK_PHONE_B]
    for act in actions_5m:
        assert act["data"]["title"] == "MEATER+: ~5 Minutes Left"
        assert act["data"]["data"]["tag"] == "meater_cook_5min"
        assert act["data"]["data"]["priority"] == "high"
        assert "sensor.meater_meater_internal_temperature" in act["data"]["message"]
        assert "number.meater_meater_target_temperature" in act["data"]["message"]
        assert "sensor.meater_meater_ambient_temperature" in act["data"]["message"]

    # 2. meater_cook_finished_pull
    a_pull = by_id["meater_cook_finished_pull"]
    assert a_pull["alias"] == "MEATER+: Cook Finished — Pull Now"
    trig_pull = a_pull["triggers"][0]
    assert trig_pull["trigger"] == "state"
    assert trig_pull["entity_id"] == "sensor.meater_meater_cook_status"
    assert trig_pull["to"] == ["Pull Now", "Target Reached"]
    assert any("1200" in str(c.get("value_template", "")) for c in a_pull["conditions"])

    actions_pull = a_pull["actions"]
    assert [a["action"] for a in actions_pull] == [MOCK_PHONE_A, MOCK_PHONE_B]
    for act in actions_pull:
        assert act["data"]["title"] == "MEATER+: Time to Pull!"
        assert act["data"]["data"]["tag"] == "meater_cook_done"
        assert "25" in act["data"]["message"]
        assert "20" in act["data"]["message"]
        assert "10" in act["data"]["message"]

    # 3. meater_cook_rest_complete
    a_rest = by_id["meater_cook_rest_complete"]
    assert a_rest["alias"] == "MEATER+: Resting Complete — Ready to Carve"
    assert a_rest["mode"] == "single"
    trig_rest = a_rest["triggers"][0]
    assert trig_rest["trigger"] == "state"
    assert trig_rest["entity_id"] == "sensor.meater_meater_cook_status"
    assert trig_rest["to"] == ["Pull Now", "Target Reached"]

    actions_rest = a_rest["actions"]
    assert "delay" in actions_rest[0]
    delay_jinja = actions_rest[0]["delay"]["minutes"]
    assert "select.meater_meater_cook_preset" in delay_jinja
    assert "Prime Rib" in delay_jinja and "Brisket" in delay_jinja and "Pork Shoulder" in delay_jinja
    assert "Turkey" in delay_jinja

    notify_rest = actions_rest[1:]
    assert [a["action"] for a in notify_rest] == [MOCK_PHONE_A, MOCK_PHONE_B]
    for act in notify_rest:
        assert act["data"]["title"] == "MEATER+: Resting Complete — Ready to Carve!"
        assert act["data"]["data"]["tag"] == "meater_rest_done"
        assert "sensor.meater_meater_internal_temperature" in act["data"]["message"]


def test_patch_automations_yaml_is_idempotent_and_preserves_existing() -> None:
    patched_once = mod.patch_automations_yaml(
        SAMPLE_EXISTING_AUTOMATIONS_YAML,
        [MOCK_PHONE_A, MOCK_PHONE_B],
    )
    patched_twice = mod.patch_automations_yaml(
        patched_once,
        [MOCK_PHONE_A, MOCK_PHONE_B],
    )
    assert patched_once == patched_twice

    parsed = yaml.safe_load(patched_once)
    assert isinstance(parsed, list)
    assert len(parsed) == 5
    ids = [item["id"] for item in parsed]
    assert ids == [
        "1000001",
        "1000002",
        "meater_cook_5min_warning",
        "meater_cook_finished_pull",
        "meater_cook_rest_complete",
    ]


def test_run_deploy_dry_run_and_full_flow(tmp_path: Path) -> None:
    backup_file = tmp_path / "ha_automations.meater.backup.yaml"
    b64_yaml = base64.b64encode(SAMPLE_EXISTING_AUTOMATIONS_YAML.encode("utf-8")).decode("ascii")

    mock_states = [
        {
            "entity_id": "automation.meater_5_minutes_before_cook_finishes",
            "state": "on",
            "attributes": {"id": "meater_cook_5min_warning"},
        },
        {
            "entity_id": "automation.meater_cook_finished_pull_now",
            "state": "on",
            "attributes": {"id": "meater_cook_finished_pull"},
        },
        {
            "entity_id": "automation.meater_resting_complete_ready_to_carve",
            "state": "on",
            "attributes": {"id": "meater_cook_rest_complete"},
        },
    ]

    with (
        mock.patch.object(mod, "BACKUP_AUTOMATIONS_PATH", backup_file),
        mock.patch.object(mod, "resolve_ha_credentials", return_value=(MOCK_HA_URL, MOCK_HA_TOKEN)),
        mock.patch.object(mod.nas_config, "HOST_1_IP", "198.51.100.10"),
        mock.patch.object(mod.nas_config, "run_ssh", return_value=(0, b64_yaml, "")) as mock_ssh,
        mock.patch.object(mod, "_ha_api_request") as mock_api,
    ):
        rc_dry = mod.run_deploy(
            dry_run=True,
            notify_services_override=[MOCK_PHONE_A, MOCK_PHONE_B],
        )
        assert rc_dry == 0
        assert backup_file.is_file()
        assert mock_ssh.call_count == 1
        assert mock_api.call_count == 0

        def _fake_api(_url: str, _tok: str, path: str, **kwargs: object) -> object:
            if path == "/api/services/automation/reload":
                assert kwargs.get("method") == "POST"
                return []
            if path == "/api/states":
                return mock_states
            raise AssertionError(f"Unexpected API path: {path}")

        mock_api.side_effect = _fake_api
        rc_live = mod.run_deploy(
            dry_run=False,
            notify_services_override=[MOCK_PHONE_A, MOCK_PHONE_B],
        )
        assert rc_live == 0
        assert mock_ssh.call_count == 3


def test_reload_and_verify_ha_automations_raises_when_missing() -> None:
    with (
        mock.patch.object(mod, "_ha_api_request", return_value=[]),
        pytest.raises(RuntimeError, match="missing from GET /api/states"),
    ):
        mod.reload_and_verify_ha_automations(
            MOCK_HA_URL,
            MOCK_HA_TOKEN,
            retries=1,
            retry_delay_sec=0,
        )
