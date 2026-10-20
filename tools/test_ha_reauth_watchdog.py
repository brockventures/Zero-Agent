"""Unit tests for Home Assistant Re-Auth & Generalized Matter-over-Thread Self-Healing Watchdog."""

from unittest.mock import patch

from tools.ha_reauth_watchdog import (
    _auto_heal_matter_thread_nodes,
    _find_stale_matter_thread_services,
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
2F335EDB579AD87D-0000000000000020._matter._tcp.default.service.arpa.
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
        "2F335EDB579AD87D-0000000000000020._matter._tcp.local.",
    ]


def test_find_stale_matter_thread_services_generalized():
    active_srp = _parse_active_srp_matter_services(SAMPLE_SRP_OUTPUT)
    compressed_fabric_id = 0x2F335EDB579AD87D
    matter_nodes = [
        {"node_id": 1, "available": False},   # Unplugged Wi-Fi node (not in OTBR SRP) -> ignored
        {"node_id": 2, "available": True},    # Online Wi-Fi node -> ignored
        {"node_id": 31, "available": True},   # Online Thread node -> ignored
        {"node_id": 32, "available": False},  # Thread node 0x20 alive on OTBR SRP but offline in matterserver -> healed!
    ]
    stale = _find_stale_matter_thread_services(active_srp, compressed_fabric_id, matter_nodes)
    assert stale == ["2F335EDB579AD87D-0000000000000020._matter._tcp.local."]


def test_auto_heal_matter_thread_nodes_skips_when_all_thread_nodes_online():
    with patch("tools.nas_config.run_ssh") as mock_ssh:
        mock_ssh.side_effect = [
            (0, SAMPLE_SRP_OUTPUT, ""),
            (0, "", ""),
        ]
        healed = _auto_heal_matter_thread_nodes()
        assert healed == []
        assert mock_ssh.call_count == 2


def test_auto_heal_matter_thread_nodes_pokes_any_offline_thread_node():
    with patch("tools.nas_config.run_ssh") as mock_ssh:
        mock_ssh.side_effect = [
            (0, SAMPLE_SRP_OUTPUT, ""),
            (0, "POKED_SERVICES=2F335EDB579AD87D-000000000000001F._matter._tcp.local.\n", ""),
        ]
        healed = _auto_heal_matter_thread_nodes()
        assert healed == ["2F335EDB579AD87D-000000000000001F._matter._tcp.local."]
        assert mock_ssh.call_count == 2
