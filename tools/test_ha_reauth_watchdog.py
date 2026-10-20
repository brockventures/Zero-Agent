"""Unit tests for Home Assistant Re-Auth & Matter-over-Thread Self-Healing Watchdog."""

from unittest.mock import patch

from tools.ha_reauth_watchdog import (
    _auto_heal_matter_thread_nodes,
    _parse_active_srp_matter_services,
)

SAMPLE_SRP_OUTPUT = """\
B83BF2A10F621A7B-1B3AA1798AB779D5._matter._tcp.default.service.arpa.
    deleted: false
    subtypes: _IB83BF2A10F621A7B
    port: 5540
2F335EDB579AD87D-000000000000001F._matter._tcp.default.service.arpa.
    deleted: false
    subtypes: _I2F335EDB579AD87D
    port: 5540
DEADCAFE00000000-0000000000000099._matter._tcp.default.service.arpa.
    deleted: true
Done
"""


def test_parse_active_srp_matter_services():
    services = _parse_active_srp_matter_services(SAMPLE_SRP_OUTPUT)
    assert services == [
        "B83BF2A10F621A7B-1B3AA1798AB779D5._matter._tcp.local.",
        "2F335EDB579AD87D-000000000000001F._matter._tcp.local.",
    ]


def test_auto_heal_matter_thread_nodes_skips_when_online():
    states = [
        {
            "entity_id": "binary_sensor.entryway_entryway_motion_sensor_occupancy",
            "state": "off",
        }
    ]
    with patch("tools.nas_config.run_ssh") as mock_ssh:
        healed = _auto_heal_matter_thread_nodes(states)
        assert healed == []
        mock_ssh.assert_not_called()


def test_auto_heal_matter_thread_nodes_pokes_mdns_when_unavailable():
    states = [
        {
            "entity_id": "binary_sensor.entryway_entryway_motion_sensor_occupancy",
            "state": "unavailable",
        }
    ]
    with patch("tools.nas_config.run_ssh") as mock_ssh:
        mock_ssh.side_effect = [
            (0, SAMPLE_SRP_OUTPUT, ""),
            (0, "POKED_SERVICES=2F335EDB579AD87D-000000000000001F._matter._tcp.local.\n", ""),
        ]
        healed = _auto_heal_matter_thread_nodes(states)
        assert healed == ["binary_sensor.entryway_entryway_motion_sensor_occupancy"]
        assert mock_ssh.call_count == 2
