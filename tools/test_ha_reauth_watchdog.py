"""Unit tests for Home Assistant Re-Auth & Generalized Matter-over-Thread Self-Healing Watchdog."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ha_reauth_watchdog import (
    ENVOY_HEAL_COOLDOWN_SECONDS,
    ENVOY_RECONNECT_POLL_ATTEMPTS,
    ENVOY_RECONNECT_POLL_INTERVAL_SECONDS,
    ENVOY_RELOAD_TIMEOUT_SECONDS,
    PT,
    ZHA_RELOAD_TIMEOUT_SECONDS,
    ZWAVE_CONTAINER_STARTUP_SETTLE_SECONDS,
    ZWAVE_JS_DEFAULT_ENTRY_ID,
    ZWAVE_RADIO_RESET_SETTLE_SECONDS,
    ZWAVE_RELOAD_RETRY_SETTLE_SECONDS,
    ZWAVE_RELOAD_TIMEOUT_SECONDS,
    _auto_heal_enphase_envoy,
    _auto_heal_matter_thread_nodes,
    _auto_heal_zha_entry,
    _auto_heal_zwave_js,
    _find_stale_matter_thread_services,
    _get_unifi_config,
    _kick_unifi_envoy_client,
    _parse_active_srp_matter_services,
    _probe_tcp_port,
    _zwave_logs_indicate_wedged_driver,
    check_ha_integrations,
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


def test_envoy_constants():
    assert ENVOY_HEAL_COOLDOWN_SECONDS == 1800
    assert ENVOY_RECONNECT_POLL_ATTEMPTS == 9
    assert ENVOY_RECONNECT_POLL_INTERVAL_SECONDS == 5.0
    assert ENVOY_RELOAD_TIMEOUT_SECONDS == 30


def test_get_unifi_config_env_vars():
    with patch.dict(
        "os.environ",
        {
            "UNIFI_BASE_URL": "https://198.51.100.1:8443",
            "UNIFI_USER": "test_unifi_user",
            "UNIFI_PASSWORD": "test_unifi_password",
        },
        clear=True,
    ):
        url, user, pw = _get_unifi_config()
        assert url == "https://198.51.100.1:8443"
        assert user == "test_unifi_user"
        assert pw == "test_unifi_password"


def test_get_unifi_config_env_json():
    json_data = json.dumps(
        {
            "UNIFI_URL": "https://198.51.100.2:8443",
            "UNIFI_USERNAME": "json_user",
            "UNIFI_PASS": "json_pass",
        }
    )
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=json_data)),
    ):
        url, user, pw = _get_unifi_config()
        assert url == "https://198.51.100.2:8443"
        assert user == "json_user"
        assert pw == "json_pass"


def test_get_unifi_config_default_fallback():
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("os.path.exists", return_value=False),
    ):
        url, user, pw = _get_unifi_config()
        assert url == "https://127.0.0.1"
        assert user == ""
        assert pw == ""


def test_probe_tcp_port():
    class FakeSocket:
        def __init__(self, code):
            self.code = code

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return self.code

    with patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket(0)):
        assert _probe_tcp_port("198.51.100.170", 443) is True

    with patch("tools.ha_reauth_watchdog.socket.socket", return_value=FakeSocket(111)):
        assert _probe_tcp_port("198.51.100.170", 443) is False


def test_kick_unifi_envoy_client_success():
    reqs: list[str] = []

    class FakeResponse:
        def __init__(self, status=200, data=b"{}", headers=None):
            self.status = status
            self._data = data
            self.headers = headers or {}

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

    def fake_open(req, timeout=10):
        url = req.full_url
        reqs.append(url)
        if url.endswith("/api/auth/login"):
            return FakeResponse(200, b"{}", {"X-CSRF-Token": "test_csrf_token"})
        if url.endswith("/proxy/network/api/s/default/stat/sta"):
            body = json.dumps({
                "data": [
                    {"name": "Living Room TV", "mac": "00:00:5e:00:53:10", "ip": "198.51.100.10"},
                    {"name": "Envoy Gateway", "mac": "00:00:5e:00:53:61", "ip": "198.51.100.170"},
                ]
            }).encode()
            return FakeResponse(200, body)
        if url.endswith("/proxy/network/api/s/default/cmd/stamgr"):
            payload = json.loads(req.data.decode())
            assert payload == {"cmd": "kick-sta", "mac": "00:00:5e:00:53:61"}
            assert req.headers.get("X-csrf-token") == "test_csrf_token"
            return FakeResponse(200, b"{}")
        raise AssertionError(f"Unexpected URL: {url}")

    mock_opener = MagicMock()
    mock_opener.open.side_effect = fake_open

    ok, mac, ip = _kick_unifi_envoy_client(
        "https://198.51.100.1:8443",
        "test_user",
        "test_pass",
        opener=mock_opener,
    )
    assert ok is True
    assert mac == "00:00:5e:00:53:61"
    assert ip == "198.51.100.170"
    assert any("/api/auth/login" in r for r in reqs)
    assert any("/stat/sta" in r for r in reqs)
    assert any("/cmd/stamgr" in r for r in reqs)


def test_kick_unifi_envoy_client_fallback_alluser():
    reqs: list[str] = []

    class FakeResponse:
        def __init__(self, status=200, data=b"{}", headers=None):
            self.status = status
            self._data = data
            self.headers = headers or {}

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

    def fake_open(req, timeout=10):
        url = req.full_url
        reqs.append(url)
        if url.endswith("/api/auth/login"):
            return FakeResponse(200, b"{}", {"X-CSRF-Token": "csrf_123"})
        if url.endswith("/proxy/network/api/s/default/stat/sta"):
            # Not in active stations
            return FakeResponse(200, json.dumps({"data": []}).encode())
        if url.endswith("/proxy/network/api/s/default/stat/alluser"):
            # Found in alluser history
            body = json.dumps({
                "data": [
                    {"hostname": "envoy-s-metered", "mac": "00:00:5e:00:53:61", "last_ip": "198.51.100.170"}
                ]
            }).encode()
            return FakeResponse(200, body)
        if url.endswith("/proxy/network/api/s/default/cmd/stamgr"):
            return FakeResponse(200, b"{}")
        raise AssertionError(f"Unexpected URL: {url}")

    mock_opener = MagicMock()
    mock_opener.open.side_effect = fake_open

    ok, mac, ip = _kick_unifi_envoy_client(
        "https://198.51.100.1:8443",
        "user",
        "pass",
        opener=mock_opener,
    )
    assert ok is True
    assert mac == "00:00:5e:00:53:61"
    assert ip == "198.51.100.170"
    assert any("/stat/alluser" in r for r in reqs)


def test_kick_unifi_envoy_client_missing_or_not_found():
    ok, mac, ip = _kick_unifi_envoy_client("", "", "")
    assert ok is False
    assert mac is None
    assert ip is None

    class FakeResponse:
        def __init__(self, data=b"{}"):
            self.status = 200
            self._data = data
            self.headers = {}

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

    mock_opener = MagicMock()
    mock_opener.open.side_effect = [
        FakeResponse(b"{}"),
        FakeResponse(b'{"data": []}'),
        FakeResponse(b'{"data": []}'),
    ]
    ok, mac, ip = _kick_unifi_envoy_client("https://198.51.100.1:8443", "u", "p", opener=mock_opener)
    assert ok is False
    assert mac is None
    assert ip is None


def test_auto_heal_enphase_envoy_success():
    events: list[str] = []
    sleeps: list[float] = []
    probe_attempts = 0

    def fake_probe(ip, port=443, timeout=2.0):
        nonlocal probe_attempts
        probe_attempts += 1
        events.append(f"probe:{ip}:{port}:attempt={probe_attempts}")
        return probe_attempts >= 2

    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/config/config_entries/entry"):
            events.append(f"ha_entries:timeout={timeout}")
            resp = MagicMock()
            resp.status = 200
            state_val = "loaded" if any(e.startswith("reload") for e in events) else "setup_retry"
            resp.read.return_value = json.dumps([
                {
                    "entry_id": "envoy_entry_1",
                    "domain": "enphase_envoy",
                    "state": state_val,
                    "data": {"host": "198.51.100.170"},
                }
            ]).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/reload"):
            events.append(f"reload:timeout={timeout}")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected url: {url}")

    state = {}
    with (
        patch("tools.ha_reauth_watchdog._probe_tcp_port", side_effect=fake_probe),
        patch(
            "tools.ha_reauth_watchdog._kick_unifi_envoy_client",
            return_value=(True, "00:00:5e:00:53:61", "198.51.100.170"),
        ) as mock_kick,
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog.time.sleep", side_effect=lambda s: sleeps.append(s)),
        patch("tools.ha_reauth_watchdog._save_state") as mock_save,
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is True
        assert mock_kick.call_count == 1
        assert "envoy_last_heal_ts" in state
        assert mock_save.call_count >= 1
        assert ENVOY_RECONNECT_POLL_INTERVAL_SECONDS in sleeps
        assert any(e.startswith(f"reload:timeout={ENVOY_RELOAD_TIMEOUT_SECONDS}") for e in events)


def test_auto_heal_enphase_envoy_cooldown_suppression():
    state = {"envoy_last_heal_ts": int(time.time()) - 300}
    with (
        patch("tools.ha_reauth_watchdog._kick_unifi_envoy_client") as mock_kick,
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen") as mock_url,
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is False
        assert mock_kick.call_count == 0
        assert mock_url.call_count == 0


def test_auto_heal_enphase_envoy_unifi_kick_fails():
    state = {}
    with (
        patch("tools.ha_reauth_watchdog._probe_tcp_port", return_value=False),
        patch("tools.ha_reauth_watchdog._kick_unifi_envoy_client", return_value=(False, None, None)),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen") as mock_url,
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is False
        assert "envoy_last_heal_ts" not in state
        assert mock_url.call_count <= 1


def test_auto_heal_enphase_envoy_tcp_does_not_recover():
    state = {}
    with (
        patch("tools.ha_reauth_watchdog._probe_tcp_port", return_value=False),
        patch(
            "tools.ha_reauth_watchdog._kick_unifi_envoy_client",
            return_value=(True, "00:00:5e:00:53:61", "198.51.100.170"),
        ),
        patch("tools.ha_reauth_watchdog.time.sleep"),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen"),
        patch("tools.ha_reauth_watchdog._save_state"),
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is False
        assert "envoy_last_heal_ts" in state


def test_auto_heal_enphase_envoy_reload_failure():
    state = {}

    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/config/config_entries/entry"):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps([
                {"entry_id": "envoy_entry_1", "state": "setup_retry", "data": {"host": "198.51.100.170"}}
            ]).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/reload"):
            resp = MagicMock()
            resp.status = 500
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected url: {url}")

    with (
        patch("tools.ha_reauth_watchdog._probe_tcp_port", side_effect=[False, True]),
        patch(
            "tools.ha_reauth_watchdog._kick_unifi_envoy_client",
            return_value=(True, "00:00:5e:00:53:61", "198.51.100.170"),
        ),
        patch("tools.ha_reauth_watchdog.time.sleep"),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog._save_state"),
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is False


def test_auto_heal_enphase_envoy_skips_kick_if_port_443_already_open():
    events: list[str] = []

    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/config/config_entries/entry"):
            events.append("ha_entries")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps([
                {"entry_id": "envoy_entry_1", "state": "loaded", "data": {"host": "198.51.100.170"}}
            ]).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/reload"):
            events.append("reload")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected url: {url}")

    state = {}
    with (
        patch("tools.ha_reauth_watchdog._probe_tcp_port", return_value=True),
        patch("tools.ha_reauth_watchdog._kick_unifi_envoy_client") as mock_kick,
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        ok = _auto_heal_enphase_envoy("http://198.51.100.10:8123", "test_tok", "envoy_entry_1", state=state)
        assert ok is True
        assert mock_kick.call_count == 0
        assert "reload" in events


def test_check_ha_integrations_enphase_envoy_healed():
    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/config/config_entries/entry"):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps([
                {
                    "entry_id": "envoy_123",
                    "domain": "enphase_envoy",
                    "title": "Envoy",
                    "state": "setup_retry",
                    "reason": "Could not connect",
                    "data": {"host": "198.51.100.170"},
                }
            ]).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/states"):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"[]"
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected url: {url}")

    with (
        patch("tools.ha_reauth_watchdog._get_ha_config", return_value=("http://198.51.100.10:8123", "tok")),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog._auto_heal_enphase_envoy", return_value=True) as mock_heal,
        patch("tools.ha_reauth_watchdog._save_state"),
    ):
        has_activity, summary, items = check_ha_integrations(force=True)
        assert mock_heal.call_count == 1
        assert has_activity is False
        assert "0 HA integration failures" in summary
        assert items == []


def test_check_ha_integrations_enphase_envoy_unhealed_alerts():
    def fake_urlopen(req, timeout=10):
        url = req.full_url
        if url.endswith("/api/config/config_entries/entry"):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps([
                {
                    "entry_id": "envoy_123",
                    "domain": "enphase_envoy",
                    "title": "Envoy Gateway",
                    "state": "setup_retry",
                    "reason": "Could not connect",
                    "data": {"host": "198.51.100.170"},
                }
            ]).encode()
            resp.__enter__.return_value = resp
            return resp
        if url.endswith("/api/states"):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"[]"
            resp.__enter__.return_value = resp
            return resp
        raise AssertionError(f"Unexpected url: {url}")

    with (
        patch("tools.ha_reauth_watchdog._get_ha_config", return_value=("http://198.51.100.10:8123", "tok")),
        patch("tools.ha_reauth_watchdog.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.ha_reauth_watchdog._auto_heal_enphase_envoy", return_value=False) as mock_heal,
        patch("tools.ha_reauth_watchdog._save_state"),
    ):
        has_activity, summary, items = check_ha_integrations(force=True)
        assert mock_heal.call_count == 1
        assert has_activity is True
        assert len(items) == 1
        assert items[0]["domain"] == "enphase_envoy"
        assert "Envoy Gateway" in summary



