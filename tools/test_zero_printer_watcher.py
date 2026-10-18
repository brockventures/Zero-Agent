#!/usr/bin/env python3
"""
Unit tests for tools/zero_printer_watcher.py
Verifies state machine transitions, vision evaluation, Option B auto-pause,
cooldown teardown logic, and accurate OrcaSlicer ETA parsing.
"""

import os
import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add workspace to path
sys.path.insert(0, "/workspace")
from tools.zero_printer_watcher import (
    OctoPrintClient,
    PrinterWatcherDaemon,
    evaluate_print_image,
    is_running,
    stop_daemon,
    PID_FILE,
    parse_time_string_to_seconds,
    extract_gcode_sliced_seconds,
    estimate_eta_str,
)


class TestZeroPrinterWatcher(unittest.TestCase):
    @patch("subprocess.run")
    def test_evaluate_print_image_nominal(self, mock_subproc):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"failure": false, "confidence": 0.95, "issue": "nominal", "summary": "clean print"}'
        mock_subproc.return_value = mock_proc

        # Create dummy image
        dummy_img = Path("/workspace/data/3dprint/dummy_test.jpg")
        dummy_img.parent.mkdir(parents=True, exist_ok=True)
        dummy_img.write_bytes(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00C\x00\xFF\xD9")

        try:
            res = evaluate_print_image(dummy_img)
            self.assertFalse(res["failure"])
            self.assertEqual(res["confidence"], 0.95)
            self.assertEqual(res["issue"], "nominal")
        finally:
            if dummy_img.exists():
                dummy_img.unlink()

    @patch("subprocess.run")
    def test_evaluate_print_image_failure(self, mock_subproc):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"failure": true, "confidence": 0.88, "issue": "Spaghetti detected", "summary": "Filament nesting into air"}'
        mock_subproc.return_value = mock_proc

        dummy_img = Path("/workspace/data/3dprint/dummy_test_fail.jpg")
        dummy_img.parent.mkdir(parents=True, exist_ok=True)
        dummy_img.write_bytes(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00C\x00\xFF\xD9")

        try:
            res = evaluate_print_image(dummy_img)
            self.assertTrue(res["failure"])
            self.assertEqual(res["confidence"], 0.88)
            self.assertEqual(res["issue"], "Spaghetti detected")
        finally:
            if dummy_img.exists():
                dummy_img.unlink()

    @patch("urllib.request.urlopen")
    def test_octoprint_pause_job(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        client = OctoPrintClient("http://127.0.0.1:8888", "test_key")
        success = client.pause_job()
        self.assertTrue(success)

    @patch("tools.zero_printer_watcher.speak_announcement", return_value=True)
    @patch("tools.zero_printer_watcher.cut_printer_power", return_value=True)
    @patch("tools.zero_printer_watcher.stop_webcam", return_value=True)
    @patch("tools.zero_printer_watcher.capture_webcam_snapshot", return_value=True)
    @patch("tools.zero_printer_watcher.deliver_discord_photo", return_value=True)
    @patch("tools.zero_printer_watcher.get_nozzle_temp", return_value=42.0)
    @patch.object(OctoPrintClient, "get_job")
    def test_daemon_terminal_to_cooldown_to_teardown(
        self,
        mock_get_job,
        mock_temp,
        mock_deliver,
        mock_snap,
        mock_stop_cam,
        mock_cut_pwr,
        mock_speak,
    ):
        # Simulate print finished (completion: 100%, state: Operational)
        mock_get_job.return_value = {
            "state": "Operational",
            "progress": {"completion": 100.0, "printTime": 1200},
            "job": {"file": {"name": "test_part.gcode"}},
        }

        daemon = PrinterWatcherDaemon(
            octoprint_url="http://127.0.0.1:8888",
            api_key="test_key",
            interval_print=1,
            interval_cooldown=1,
            cooldown_temp=50.0,
            channel="projects",
        )

        with patch.object(daemon, "write_pid"), patch.object(daemon, "remove_pid"), patch("tools.zero_printer_watcher.time.sleep", side_effect=[None, None]):
            daemon.run()

        # Verify teardown sequence executed
        mock_snap.assert_called()
        mock_deliver.assert_called()
        mock_stop_cam.assert_called_once()
        mock_cut_pwr.assert_called_once()

    @patch("tools.zero_printer_watcher.deliver_discord_photo")
    @patch("tools.zero_printer_watcher.evaluate_print_image")
    @patch("tools.zero_printer_watcher.capture_webcam_snapshot", return_value=True)
    @patch.object(OctoPrintClient, "get_job")
    def test_daemon_nominal_progress_snapshot(self, mock_get_job, mock_snap, mock_eval, mock_deliver):
        mock_get_job.return_value = {
            "state": "Printing",
            "progress": {"completion": 45.0, "printTime": 600},
            "job": {"file": {"name": "test_part.gcode"}},
        }
        mock_eval.return_value = {"failure": False, "confidence": 0.98, "issue": "nominal", "summary": "clean"}

        daemon = PrinterWatcherDaemon(
            octoprint_url="http://127.0.0.1:8888",
            api_key="test_key",
            interval_print=0,
            channel="3d-print",
            post_progress=True,
        )

        with patch.object(daemon, "write_pid"), patch.object(daemon, "remove_pid"), patch("tools.zero_printer_watcher.get_nozzle_temp", return_value=205.0), patch("tools.zero_printer_watcher.time.sleep", side_effect=KeyboardInterrupt):
            try:
                daemon.run()
            except KeyboardInterrupt:
                pass

        mock_snap.assert_called_once()
        mock_eval.assert_called_once()
        mock_deliver.assert_called_once()
        delivered_caption = mock_deliver.call_args[0][1]
        self.assertIn("📸 **Zero Watcher Progress Check: Nominal**", delivered_caption)
        self.assertIn("- **ETA:**", delivered_caption)

    @patch("tools.zero_printer_watcher.speak_announcement")
    def test_clean_job_name_and_voice_announcement(self, mock_speak):
        from tools.zero_printer_watcher import clean_job_name
        self.assertEqual(clean_job_name("zero_casual_robot_6in.gcode"), "zero casual robot 6in")
        self.assertEqual(clean_job_name("bench-boat.gcode"), "bench boat")

    @patch("tools.zero_printer_watcher.PID_FILE")
    @patch("tools.zero_printer_watcher.os.kill")
    def test_is_running_cleans_stale_pid_file(self, mock_kill, mock_pid_file):
        mock_pid_file.exists.return_value = True
        mock_pid_file.read_text.return_value = "999999"
        mock_kill.side_effect = ProcessLookupError("No such process")

        running, pid = is_running()
        self.assertFalse(running)
        self.assertIsNone(pid)
        mock_pid_file.unlink.assert_called_once()

    @patch("tools.zero_printer_watcher.time.sleep")
    @patch("tools.zero_printer_watcher.is_running")
    @patch("tools.zero_printer_watcher.os.kill")
    def test_stop_daemon_clean_exit(self, mock_kill, mock_is_running, mock_sleep):
        import signal
        mock_is_running.side_effect = [(True, 4321), (False, None)]
        success = stop_daemon(timeout=2.0)
        self.assertTrue(success)
        mock_kill.assert_called_once_with(4321, signal.SIGTERM)

    @patch("tools.zero_printer_watcher.PID_FILE")
    @patch("tools.zero_printer_watcher.time.sleep")
    @patch("tools.zero_printer_watcher.is_running")
    @patch("tools.zero_printer_watcher.os.kill")
    def test_stop_daemon_escalates_to_sigkill(self, mock_kill, mock_is_running, mock_sleep, mock_pid_file):
        import signal
        mock_pid_file.exists.return_value = True
        mock_is_running.return_value = (True, 4321)
        success = stop_daemon(timeout=0.1)
        self.assertTrue(success)
        mock_kill.assert_any_call(4321, signal.SIGTERM)
        mock_kill.assert_any_call(4321, signal.SIGKILL)
        mock_pid_file.unlink.assert_called()

    def test_parse_time_string_to_seconds(self):
        self.assertEqual(parse_time_string_to_seconds("4h 53m 2s"), 17582)
        self.assertEqual(parse_time_string_to_seconds("35m 12s"), 2112)
        self.assertEqual(parse_time_string_to_seconds("2h"), 7200)
        self.assertEqual(parse_time_string_to_seconds("45s"), 45)
        self.assertEqual(parse_time_string_to_seconds("0s"), 0)
        self.assertEqual(parse_time_string_to_seconds("invalid"), 0)

    def test_extract_gcode_sliced_seconds_m73_and_comment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Test M73 header
            m73_file = Path(tmpdir) / "m73.gcode"
            m73_file.write_text("; Header\nM73 P0 R293\nG28\n")
            self.assertEqual(extract_gcode_sliced_seconds(m73_file), 293 * 60)

            # Test OrcaSlicer comment header
            comment_file = Path(tmpdir) / "comment.gcode"
            comment_file.write_text("; Header\n; estimated printing time (normal mode) = 4h 53m 2s\nG28\n")
            self.assertEqual(extract_gcode_sliced_seconds(comment_file), 17582)

            # Test tail comment for large file (>16KB)
            large_file = Path(tmpdir) / "large.gcode"
            padding = "G1 X0 Y0 E1\n" * 1500  # ~18KB
            large_file.write_text(padding + "; estimated printing time (normal mode) = 1h 30m 0s\n; End of G-code\n")
            self.assertEqual(extract_gcode_sliced_seconds(large_file), 5400)

            # Non-existent file
            non_existent = Path(tmpdir) / "missing.gcode"
            self.assertEqual(extract_gcode_sliced_seconds(non_existent), 0)

    @patch("tools.zero_printer_watcher.DATA_DIR")
    def test_estimate_eta_str(self, mock_data_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_data_dir.__truediv__.side_effect = lambda filename: Path(tmpdir) / filename

            gcode_path = Path(tmpdir) / "test_model.gcode"
            gcode_path.write_text("M73 P0 R60\n")  # 60m = 3600s + 300s baseline = 3900s total

            # 1. At start (print_time = 0, completion = 0.0) -> ~1h 00m remaining (clock PT)
            eta_start = estimate_eta_str("test_model.gcode", completion=0.0, print_time=0.0)
            self.assertIn("~1h 00m remaining", eta_start)
            self.assertIn("PT)", eta_start)

            # 2. Mid print (print_time = 1800, completion = 50.0)
            eta_mid = estimate_eta_str("test_model.gcode", completion=50.0, print_time=1800.0)
            self.assertIn("remaining", eta_mid)
            self.assertIn("PT)", eta_mid)

            # 3. Fallback when gcode has no metadata but octo_time_left exists
            eta_fallback = estimate_eta_str("nonexistent.gcode", completion=10.0, print_time=300.0, octo_time_left=1200.0)
            self.assertIn("~20m remaining", eta_fallback)
            self.assertIn("PT)", eta_fallback)

            # 4. Zero/invalid remaining
            eta_done = estimate_eta_str("nonexistent.gcode", completion=0.0, print_time=0.0, octo_time_left=0.0)
            self.assertEqual(eta_done, "Calculating...")


if __name__ == "__main__":
    unittest.main()
