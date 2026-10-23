"""Unit tests for Home Assistant Re-Auth & Generalized Matter-over-Thread Self-Healing Watchdog."""

from unittest.mock import MagicMock, patch

from tools.ha_reauth_watchdog import (
    ZHA_RELOAD_TIMEOUT_SECONDS,
    _auto_heal_matter_thread_nodes,
    _auto_heal_zha_entry,
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


def test_auto_heal_zha_entry_closes_socket_resets_radio_and_verifies_loaded():
    events: list[str] = []

    class FakeSocket:
        def __enter__(self):
            events.append("socket_enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("socket_closed")

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            events.append(f"connect_ex:{addr[0]}:{addr[1]}")
            return 0

    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/services/button/press"):
            events.append(f"button_press:timeout={timeout}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/reload"):
            events.append(f"reload:timeout={timeout}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/config/config_entries/entry"):
            events.append("verify_entries")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b'[{"entry_id": "zha_entry_1", "state": "loaded"}]'
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    with (
        patch("tools.ha_reauth_watchdog._get_zha_coordinator_ip", return_value="198.51.100.25"),
        patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket()),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog.time.sleep"),
    ):
        ok = _auto_heal_zha_entry("http://198.51.100.10:8123", "test_token", "zha_entry_1")
        assert ok is True
        assert events == [
            "socket_enter",
            "connect_ex:198.51.100.25:6638",
            "socket_closed",
            "button_press:timeout=10",
            f"reload:timeout={ZHA_RELOAD_TIMEOUT_SECONDS}",
            "verify_entries",
        ]


def test_auto_heal_zha_entry_returns_false_when_still_setup_retry():
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0

    def fake_urlopen(req, timeout=10):
        resp = MagicMock()
        resp.status = 200
        if req.full_url.endswith("/api/config/config_entries/entry"):
            resp.read.return_value = b'[{"entry_id": "zha_entry_1", "state": "setup_retry"}]'
        resp.__enter__.return_value = resp
        return resp

    with (
        patch("tools.ha_reauth_watchdog._get_zha_coordinator_ip", return_value="198.51.100.25"),
        patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket()),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog.time.sleep"),
    ):
        assert _auto_heal_zha_entry("http://198.51.100.10:8123", "test_token", "zha_entry_1") is False
