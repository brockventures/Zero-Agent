"""Unit tests for Pixel 8 Pro Mobile Browser Automation (tools/pixel_browser.py)."""

import subprocess
import unittest
from unittest.mock import MagicMock, call, patch
import urllib.error

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tools.pixel_browser import (
    check_printer_conflict,
    get_adb_command_prefix,
    get_device_status,
    pixel_session,
    wake_and_unlock,
    sleep_screen,
    setup_cdp_forwarding,
    launch_chrome,
)


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

    @patch("tools.pixel_browser.run_adb_shell")
    @patch("tools.pixel_browser.check_printer_conflict")
    def test_get_device_status_parsing(self, mock_conflict, mock_adb_shell):
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

    @patch("tools.pixel_browser.subprocess.run")
    def test_wake_and_unlock(self, mock_run):
        wake_and_unlock()
        # Verify both wakeup (224) and menu/unlock (82) keyevents are sent
        calls = [c[0][0] for c in mock_run.call_args_list]
        self.assertTrue(any("224" in arg_list for arg_list in calls))
        self.assertTrue(any("82" in arg_list for arg_list in calls))

    @patch("tools.pixel_browser.subprocess.run")
    def test_sleep_screen(self, mock_run):
        sleep_screen()
        # Verify power keyevent (26) is sent
        calls = [c[0][0] for c in mock_run.call_args_list]
        self.assertTrue(any("26" in arg_list for arg_list in calls))

    @patch("tools.pixel_browser.check_printer_conflict")
    def test_pixel_session_conflict_guard(self, mock_conflict):
        mock_conflict.return_value = (True, "IP Webcam active")

        with self.assertRaises(RuntimeError) as ctx:
            with pixel_session():
                pass
        self.assertIn("Printer conflict guard active", str(ctx.exception))

    @patch("tools.pixel_browser.sleep_screen")
    @patch("tools.pixel_browser.launch_chrome")
    @patch("tools.pixel_browser.setup_cdp_forwarding")
    @patch("tools.pixel_browser.wake_and_unlock")
    @patch("tools.pixel_browser.check_printer_conflict")
    def test_pixel_session_force_and_cleanup(
        self, mock_conflict, mock_wake, mock_cdp, mock_chrome, mock_sleep
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


if __name__ == "__main__":
    unittest.main()
