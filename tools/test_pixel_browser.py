"""Unit tests for Pixel 8 Pro Mobile Browser Automation (tools/pixel_browser.py)."""
# ruff: noqa: I001, RUF059, RUF100, SIM117

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
for _p in (_REPO_ROOT, "/workspace"):
    if _p not in sys.path:
        sys.path.append(_p)
if sys.path[0] != _REPO_ROOT:
    sys.path.insert(0, _REPO_ROOT)

from tools.pixel_browser import (
    ADB_BIN,
    ADB_KEYEVENT_TIMEOUT_SEC,
    ADB_PORT,
    CDP_PORT,
    USB_SERIAL,
    check_printer_conflict,
    force_stop_browsers,
    get_adb_command_prefix,
    get_device_status,
    pixel_session,
    probe_cdp_version,
    setup_cdp_forwarding,
    wake_and_unlock,
    sleep_screen,
    launch_chrome,
    launch_chrome_beta,
)

TEST_HOST2_IP = "198.51.100.84"


class TestPixelBrowser(unittest.TestCase):

    @patch("tools.pixel_browser.urllib.request.urlopen")
    def test_check_printer_conflict_active(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        conflict, msg = check_printer_conflict()
        self.assertTrue(conflict)
        self.assertIn("IP Webcam is currently active", msg)

    @patch("tools.pixel_browser.urllib.request.urlopen")
    def test_check_printer_conflict_offline(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        conflict, msg = check_printer_conflict()
        self.assertFalse(conflict)
        self.assertIn("Clear", msg)

    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.run_adb_shell")
    @patch("tools.pixel_browser.check_printer_conflict")
    def test_get_device_status_parsing(self, mock_conflict, mock_adb_shell, mock_prefix):
        mock_conflict.return_value = (False, "Clear")

        def adb_side_effect(cmd, **kwargs):
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            if "battery" in cmd:
                mock_proc.stdout = "level: 42\nstatus: 2\ntemperature: 253\n"
            elif "power" in cmd:
                mock_proc.stdout = "mWakefulness=Awake\n"
            else:
                mock_proc.stdout = ""
            return mock_proc

        mock_adb_shell.side_effect = adb_side_effect

        st = get_device_status()
        self.assertEqual(st["device"], "Pixel 8 Pro (husky)")
        self.assertEqual(st["battery_pct"], 42)
        self.assertEqual(st["temperature_c"], 25.3)
        self.assertTrue(st["charging"])
        self.assertTrue(st["screen_awake"])
        self.assertFalse(st["conflict"])

    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_wake_and_unlock(self, mock_run, mock_prefix, mock_sleep):
        ok = wake_and_unlock()
        self.assertTrue(ok)
        # Verify both wakeup (224) and menu/unlock (82) keyevents are sent with 8s timeout
        calls = [c[0][0] for c in mock_run.call_args_list]
        timeouts = [c.kwargs.get("timeout") for c in mock_run.call_args_list]
        self.assertEqual(ADB_KEYEVENT_TIMEOUT_SEC, 8)
        self.assertTrue(any("224" in arg_list for arg_list in calls))
        self.assertTrue(any("82" in arg_list for arg_list in calls))
        self.assertTrue(all(t == 8 for t in timeouts))

    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_sleep_screen(self, mock_run, mock_prefix):
        ok = sleep_screen()
        self.assertTrue(ok)
        # Verify unconditional KEYCODE_SLEEP (223) is sent with 8s timeout
        calls = [c[0][0] for c in mock_run.call_args_list]
        timeouts = [c.kwargs.get("timeout") for c in mock_run.call_args_list]
        self.assertTrue(any("223" in arg_list for arg_list in calls))
        self.assertEqual(timeouts, [8])

    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_wake_and_unlock_retries_transient_timeout(self, mock_run, mock_prefix, mock_sleep):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="adb", timeout=8),
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]
        ok = wake_and_unlock()
        self.assertTrue(ok)
        keyevents = [c[0][0][-1] for c in mock_run.call_args_list]
        self.assertEqual(keyevents, ["224", "224", "82"])

    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_sleep_screen_survives_persistent_timeout(self, mock_run, mock_prefix, mock_sleep):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=8)
        ok = sleep_screen()
        self.assertFalse(ok)
        self.assertEqual(mock_run.call_count, 2)

    @patch("tools.pixel_browser.probe_cdp_version", return_value=True)
    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.setup_cdp_forwarding", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "local"))
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_pixel_session_proceeds_when_wake_and_sleep_timeout(
        self, mock_run, mock_conflict, mock_prefix, mock_cdp, mock_chrome, mock_sleep, mock_probe
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=8)
        with pixel_session() as cdp_url:
            self.assertEqual(cdp_url, "http://127.0.0.1:9223")
            mock_cdp.assert_called_once()
            mock_chrome.assert_called_once()
            mock_probe.assert_called_once_with("127.0.0.1")
        # 2 retries * 2 keyevents in wake_and_unlock + 2 retries * 1 keyevent in sleep_screen = 6
        self.assertEqual(mock_run.call_count, 6)

    @patch("tools.pixel_browser.check_printer_conflict")
    def test_pixel_session_conflict_guard(self, mock_conflict):
        mock_conflict.return_value = (True, "IP Webcam active")

        with self.assertRaises(RuntimeError) as ctx:
            with pixel_session():
                pass
        self.assertIn("Printer conflict guard active", str(ctx.exception))

    @patch("tools.pixel_browser.probe_cdp_version", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.setup_cdp_forwarding")
    @patch("tools.pixel_browser.wake_and_unlock")
    @patch("tools.pixel_browser.check_printer_conflict")
    def test_pixel_session_force_and_cleanup(
        self, mock_conflict, mock_wake, mock_cdp, mock_chrome, mock_sleep, mock_prefix, mock_probe
    ):
        mock_conflict.return_value = (True, "IP Webcam active")

        # With force=True, session succeeds and always calls sleep_screen on exit
        with pixel_session(force=True) as cdp_url:
            self.assertIn("http://", cdp_url)
            mock_wake.assert_called_once()
            mock_cdp.assert_called_once()
            mock_chrome.assert_called_once()

        mock_sleep.assert_called_once()

    def test_close_extra_tabs(self):
        from tools.pixel_browser import close_extra_tabs

        mock_page1 = MagicMock()
        mock_page2 = MagicMock()
        mock_page3 = MagicMock()
        mock_context = MagicMock()
        mock_context.pages = [mock_page1, mock_page2, mock_page3]

        closed = close_extra_tabs(mock_context, keep_page=mock_page1)
        self.assertEqual(closed, 2)
        mock_page1.close.assert_not_called()
        mock_page2.close.assert_called_once()
        mock_page3.close.assert_called_once()

    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_launch_chrome_stops_beta_first(self, mock_run, mock_prefix, mock_sleep):
        launch_chrome()
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], ["adb", "-s", "husky", "shell", "am", "force-stop", "com.chrome.beta"])
        self.assertEqual(
            cmds[1],
            ["adb", "-s", "husky", "shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"],
        )
        mock_sleep.assert_called_once_with(1.0)

    @patch("tools.pixel_browser.time.sleep")
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_launch_chrome_beta_stops_stable_first(self, mock_run, mock_prefix, mock_sleep):
        launch_chrome_beta()
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], ["adb", "-s", "husky", "shell", "am", "force-stop", "com.android.chrome"])
        self.assertEqual(
            cmds[1],
            ["adb", "-s", "husky", "shell", "am", "start", "-n", "com.chrome.beta/com.google.android.apps.chrome.Main"],
        )
        mock_sleep.assert_called_once_with(1.5)

    @patch("tools.pixel_browser.probe_cdp_version", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.launch_chrome_beta")
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.setup_cdp_forwarding")
    @patch("tools.pixel_browser.wake_and_unlock")
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    def test_pixel_session_chrome_beta_and_cleanup(
        self, mock_conflict, mock_wake, mock_cdp, mock_chrome, mock_chrome_beta, mock_sleep, mock_prefix, mock_probe
    ):
        with pixel_session(browser="chrome_beta") as cdp_url:
            self.assertIn("http://", cdp_url)
            mock_wake.assert_called_once()
            mock_chrome_beta.assert_called_once()
            mock_chrome.assert_not_called()
            mock_cdp.assert_called_once()
        mock_sleep.assert_called_once()

    def test_pixel_session_invalid_browser_raises_value_error(self):
        with self.assertRaises(ValueError):
            with pixel_session(browser="firefox"):
                pass

    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.subprocess.run")
    def test_get_adb_command_prefix_host2_healthy(self, mock_run, mock_ensure):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"List of devices attached\n{USB_SERIAL}\tdevice\n",
            stderr="",
        )
        prefix, transport = get_adb_command_prefix()
        self.assertEqual(prefix, [ADB_BIN, "-H", TEST_HOST2_IP, "-P", ADB_PORT, "-s", USB_SERIAL])
        self.assertEqual(transport, "usb")
        mock_ensure.assert_not_called()

    @patch("tools.pixel_browser.WIFI_DEVICE", "")
    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.subprocess.run")
    def test_get_adb_command_prefix_recovers_when_host2_refuses_connection(
        self, mock_run, mock_ensure
    ):
        mock_ensure.return_value = {"ok": True, "device_online": True, "forward_active": True}
        mock_run.side_effect = [
            MagicMock(
                returncode=1,
                stdout="",
                stderr=f"adb: failed to connect to {TEST_HOST2_IP}:{ADB_PORT}: Connection refused",
            ),
            MagicMock(returncode=0, stdout="List of devices attached\n\n", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"List of devices attached\n{USB_SERIAL}\tdevice\n",
                stderr="",
            ),
        ]

        prefix, transport = get_adb_command_prefix()
        self.assertEqual(prefix, [ADB_BIN, "-H", TEST_HOST2_IP, "-P", ADB_PORT, "-s", USB_SERIAL])
        self.assertEqual(transport, "usb")
        mock_ensure.assert_called_once_with()

    @patch("tools.pixel_browser.WIFI_DEVICE", "")
    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.subprocess.run")
    def test_get_adb_command_prefix_recovers_when_usb_serial_missing(
        self, mock_run, mock_ensure
    ):
        mock_ensure.return_value = {"ok": True, "device_online": True, "forward_active": True}
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="List of devices attached\n\n", stderr=""),
            MagicMock(returncode=0, stdout="List of devices attached\n\n", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"List of devices attached\n{USB_SERIAL}\tdevice\n",
                stderr="",
            ),
        ]

        prefix, transport = get_adb_command_prefix()
        self.assertEqual(prefix, [ADB_BIN, "-H", TEST_HOST2_IP, "-P", ADB_PORT, "-s", USB_SERIAL])
        self.assertEqual(transport, "usb")
        mock_ensure.assert_called_once_with()

    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_setup_cdp_forwarding_default_and_remove_existing(self, mock_run, mock_prefix):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(setup_cdp_forwarding())
        self.assertEqual(
            mock_run.call_args_list[0][0][0],
            ["adb", "-s", "husky", "forward", f"tcp:{CDP_PORT}", "localabstract:chrome_devtools_remote"],
        )

        mock_run.reset_mock()
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(setup_cdp_forwarding(remove_existing=True))
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(
            cmds,
            [
                ["adb", "-s", "husky", "forward", "--remove", f"tcp:{CDP_PORT}"],
                ["adb", "-s", "husky", "forward", f"tcp:{CDP_PORT}", "localabstract:chrome_devtools_remote"],
            ],
        )

    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_setup_cdp_forwarding_handles_timeout(self, mock_run, mock_prefix):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=5)
        self.assertFalse(setup_cdp_forwarding())

    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_force_stop_browsers_stops_both_packages(self, mock_run, mock_prefix):
        force_stop_browsers()
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(
            cmds,
            [
                ["adb", "-s", "husky", "shell", "am", "force-stop", "com.android.chrome"],
                ["adb", "-s", "husky", "shell", "am", "force-stop", "com.chrome.beta"],
            ],
        )

    @patch("tools.pixel_browser.urllib.request.urlopen")
    def test_probe_cdp_version_success_and_failures(self, mock_urlopen):
        ok_resp = MagicMock()
        ok_resp.status = 200
        ok_resp.read.return_value = b'{"Browser": "Chrome/130.0.0.0", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser"}'
        mock_urlopen.return_value.__enter__.return_value = ok_resp

        self.assertTrue(probe_cdp_version(TEST_HOST2_IP))
        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.full_url, f"http://{TEST_HOST2_IP}:{CDP_PORT}/json/version")

        # Empty JSON object -> False
        ok_resp.read.return_value = b"{}"
        self.assertFalse(probe_cdp_version(TEST_HOST2_IP))

        # Timeout / URLError -> False
        mock_urlopen.side_effect = urllib.error.URLError("timed out")
        self.assertFalse(probe_cdp_version(TEST_HOST2_IP))

    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.probe_cdp_version")
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.setup_cdp_forwarding", return_value=True)
    @patch("tools.pixel_browser.launch_chrome_beta")
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.force_stop_browsers")
    @patch("tools.pixel_browser.wake_and_unlock", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    def test_pixel_session_cdp_recovery_restarts_both_browsers_and_rebinds(
        self,
        mock_conflict,
        mock_prefix,
        mock_wake,
        mock_force_stop,
        mock_chrome,
        mock_chrome_beta,
        mock_fwd,
        mock_sleep,
        mock_probe,
        mock_ensure,
    ):
        mock_probe.side_effect = [False, True]

        with pixel_session(browser="chrome_beta") as cdp_url:
            self.assertEqual(cdp_url, f"http://{TEST_HOST2_IP}:{CDP_PORT}")

        mock_force_stop.assert_called_once_with(prefix=["adb", "-s", "husky"])
        self.assertEqual(mock_chrome_beta.call_count, 2)
        mock_chrome.assert_not_called()
        self.assertEqual(mock_fwd.call_args_list[0].kwargs, {})
        self.assertEqual(mock_fwd.call_args_list[1].kwargs, {"remove_existing": True})
        mock_ensure.assert_not_called()
        mock_sleep.assert_called_once()

    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.probe_cdp_version")
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.setup_cdp_forwarding", return_value=True)
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.force_stop_browsers")
    @patch("tools.pixel_browser.wake_and_unlock", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    def test_pixel_session_cdp_recovery_falls_back_to_ensure_host2_adb_once(
        self,
        mock_conflict,
        mock_prefix,
        mock_wake,
        mock_force_stop,
        mock_chrome,
        mock_fwd,
        mock_sleep,
        mock_probe,
        mock_ensure,
    ):
        mock_probe.side_effect = [False, False, True]
        mock_ensure.return_value = {"ok": True, "device_online": True, "forward_active": True}

        with pixel_session() as cdp_url:
            self.assertEqual(cdp_url, f"http://{TEST_HOST2_IP}:{CDP_PORT}")

        mock_force_stop.assert_called_once_with(prefix=["adb", "-s", "husky"])
        self.assertEqual(mock_chrome.call_count, 2)
        mock_ensure.assert_called_once_with()
        self.assertEqual(mock_fwd.call_count, 3)
        self.assertEqual(mock_fwd.call_args_list[1].kwargs, {"remove_existing": True})
        self.assertEqual(mock_fwd.call_args_list[2].kwargs, {"remove_existing": True})
        mock_sleep.assert_called_once()

    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.probe_cdp_version", return_value=False)
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.setup_cdp_forwarding", return_value=False)
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.force_stop_browsers")
    @patch("tools.pixel_browser.wake_and_unlock", return_value=True)
    @patch("tools.pixel_browser.get_adb_command_prefix", return_value=(["adb", "-s", "husky"], "usb"))
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    def test_pixel_session_cdp_recovery_exhausted_raises_and_sleeps(
        self,
        mock_conflict,
        mock_prefix,
        mock_wake,
        mock_force_stop,
        mock_chrome,
        mock_fwd,
        mock_sleep,
        mock_probe,
        mock_ensure,
    ):
        mock_ensure.return_value = {"ok": False, "device_online": False, "forward_active": False}

        with self.assertRaises(RuntimeError) as ctx:
            with pixel_session():
                pass

        self.assertIn("/json/version", str(ctx.exception))
        mock_ensure.assert_called_once_with()
        self.assertEqual(mock_probe.call_count, 3)
        mock_sleep.assert_called_once()

    @patch("tools.pixel_browser.WIFI_DEVICE", "")
    @patch("tools.pixel_browser.HOST2_IP", TEST_HOST2_IP)
    @patch("tools.pixel_browser.dsm_update_monitor.ensure_host2_adb")
    @patch("tools.pixel_browser.probe_cdp_version")
    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.setup_cdp_forwarding", return_value=True)
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.force_stop_browsers")
    @patch("tools.pixel_browser.wake_and_unlock", return_value=True)
    @patch("tools.pixel_browser.check_printer_conflict", return_value=(False, "Clear"))
    @patch("tools.pixel_browser.subprocess.run")
    def test_pixel_session_step1_adb_down_does_not_repeat_ensure_host2_adb(
        self,
        mock_run,
        mock_conflict,
        mock_wake,
        mock_force_stop,
        mock_chrome,
        mock_fwd,
        mock_sleep,
        mock_probe,
        mock_ensure,
    ):
        # Step 1: Host 2 ADB refuses connection, ensure_host2_adb fails; session must not loop SSH calls
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Connection refused")
        mock_ensure.return_value = {"ok": False, "device_online": False, "forward_active": False}
        mock_probe.return_value = False

        with self.assertRaises(RuntimeError):
            with pixel_session():
                pass

        mock_ensure.assert_called_once_with()
        mock_sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
