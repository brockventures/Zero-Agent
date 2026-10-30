"""Unit tests for Home Assistant Re-Auth & Generalized Matter-over-Thread Self-Healing Watchdog."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from tools.ha_reauth_watchdog import (
    PT,
    ZHA_RELOAD_TIMEOUT_SECONDS,
    ZWAVE_CONTAINER_STARTUP_SETTLE_SECONDS,
    ZWAVE_JS_DEFAULT_ENTRY_ID,
    ZWAVE_RADIO_RESET_SETTLE_SECONDS,
    ZWAVE_RELOAD_RETRY_SETTLE_SECONDS,
    ZWAVE_RELOAD_TIMEOUT_SECONDS,
    _auto_heal_matter_thread_nodes,
    _auto_heal_zha_entry,
    _auto_heal_zwave_js,
    _find_stale_matter_thread_services,
    _parse_active_srp_matter_services,
    _zwave_logs_indicate_wedged_driver,
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


def test_zwave_logs_indicate_wedged_driver():
    unrecovered = (
        "2026-10-26 10:00:00 INFO Z-Wave: Controller status: Ready\n"
        "2026-10-26 18:10:00 ERROR Z-Wave: The driver is not ready or has been destroyed (ZW0103)\n"
    )
    assert _zwave_logs_indicate_wedged_driver(unrecovered) is True

    recovered = (
        "2026-10-26 18:10:00 ERROR Z-Wave: Serial port closed unexpectedly\n"
        "2026-10-26 18:11:00 INFO Z-Wave: Z-Wave JS initialized\n"
    )
    assert _zwave_logs_indicate_wedged_driver(recovered) is False


def test_auto_heal_zwave_js_resets_unreachable_radio_restarts_container_and_closes_blinds():
    events: list[str] = []
    sleeps: list[float] = []

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
            return 111  # Unreachable -> triggers button.tubeszb_2026_zw_restart_the_device

    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/services/button/press"):
            body = req.data.decode()
            events.append(f"button_press:{body}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        if url.endswith(f"/api/config/config_entries/entry/{ZWAVE_JS_DEFAULT_ENTRY_ID}/reload"):
            events.append(f"reload:timeout={timeout}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/config/config_entries/entry"):
            events.append("verify_entries")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = (
                f'[{{"entry_id": "{ZWAVE_JS_DEFAULT_ENTRY_ID}", "state": "loaded"}}]'
            ).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/services/cover/close_cover"):
            body = req.data.decode()
            events.append(f"close_cover:{body}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    sample_states = [
        {"entity_id": "sensor.tubeszb_2026_zw_esp_ip_address", "state": "198.51.100.50"},
        {"entity_id": "cover.living_room_blinds_1", "state": "open"},
        {"entity_id": "cover.living_room_blinds_2", "state": "open"},
    ]

    evening_pt = datetime(2026, 10, 26, 19, 30, tzinfo=PT)
    with (
        patch("tools.nas_config.run_ssh", return_value=(0, "Restarted", "")) as mock_ssh,
        patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket()),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog.time.sleep", side_effect=lambda s: sleeps.append(s)),
        patch("tools.ha_reauth_watchdog.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = evening_pt
        ok = _auto_heal_zwave_js(
            "http://198.51.100.10:8123",
            "test_token",
            ZWAVE_JS_DEFAULT_ENTRY_ID,
            states=sample_states,
        )
        assert ok is True
        assert mock_ssh.call_count == 1
        assert "docker compose restart zwave-js-ui" in mock_ssh.call_args[0][1]
        assert ZWAVE_RADIO_RESET_SETTLE_SECONDS in sleeps
        assert ZWAVE_CONTAINER_STARTUP_SETTLE_SECONDS in sleeps
        assert events == [
            "socket_enter",
            "connect_ex:198.51.100.50:6053",
            "socket_closed",
            'button_press:{"entity_id": "button.tubeszb_2026_zw_restart_the_device"}',
            f"reload:timeout={ZWAVE_RELOAD_TIMEOUT_SECONDS}",
            "verify_entries",
            'close_cover:{"entity_id": ["cover.living_room_blinds_1", "cover.living_room_blinds_2"]}',
        ]


def test_auto_heal_zwave_js_fetches_states_when_none_and_retries_reload_on_cold_start():
    events: list[str] = []
    sleeps: list[float] = []
    reload_attempts = 0

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            events.append(f"connect_ex:{addr[0]}:{addr[1]}")
            return 0  # Reachable when resolved from /api/states

    def fake_urlopen(req, timeout=10):
        nonlocal reload_attempts
        url = req.full_url
        if url.endswith("/api/states"):
            events.append("fetch_states")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = (
                b'[{"entity_id": "sensor.tubeszb_2026_zw_esp_ip_address", "state": "198.51.100.50"},'
                b' {"entity_id": "cover.living_room_blinds_1", "state": "open"},'
                b' {"entity_id": "cover.living_room_blinds_2", "state": "open"}]'
            )
            resp.__enter__.return_value = resp
            return resp
        if url.endswith(f"/api/config/config_entries/entry/{ZWAVE_JS_DEFAULT_ENTRY_ID}/reload"):
            reload_attempts += 1
            events.append(f"reload:{reload_attempts}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/config/config_entries/entry"):
            events.append(f"verify:{reload_attempts}")
            state_val = "setup_retry" if reload_attempts == 1 else "loaded"
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = (
                f'[{{"entry_id": "{ZWAVE_JS_DEFAULT_ENTRY_ID}", "state": "{state_val}"}}]'
            ).encode()
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    morning_pt = datetime(2026, 10, 28, 7, 15, tzinfo=PT)
    with (
        patch("tools.nas_config.run_ssh", return_value=(0, "Restarted", "")),
        patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket()),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog.time.sleep", side_effect=lambda s: sleeps.append(s)),
        patch("tools.ha_reauth_watchdog.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = morning_pt
        ok = _auto_heal_zwave_js(
            "http://198.51.100.10:8123",
            "test_token",
            ZWAVE_JS_DEFAULT_ENTRY_ID,
            states=None,
        )
        assert ok is True
        assert ZWAVE_RELOAD_RETRY_SETTLE_SECONDS in sleeps
        assert events == [
            "fetch_states",
            "connect_ex:198.51.100.50:6053",
            "reload:1",
            "verify:1",
            "reload:2",
            "verify:2",
        ]


