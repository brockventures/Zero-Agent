#!/usr/bin/env python3
"""
Unit tests for tools/zero_printer_watcher.py
Verifies state machine transitions, vision evaluation, Option B auto-pause,
cooldown teardown logic, and accurate OrcaSlicer ETA parsing.
"""

import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path
from PIL import Image
import numpy as np

# Add workspace to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
from tools.zero_printer_watcher import (
    OctoPrintClient,
    PrinterWatcherDaemon,
    evaluate_print_image,
    is_running,
    stop_daemon,
    parse_time_string_to_seconds,
    extract_gcode_sliced_seconds,
    estimate_eta_str,
    compile_timelapse_gif,
)
from tools.webcam_control import (
    score_burst_frame,
    is_frame_unoccluded,
    detect_bed_and_toolhead_pose,
    DEFAULT_TARGET_BED_Y,
    DEFAULT_TARGET_HEAD_X,
)


class TestZeroPrinterWatcher(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self._pid_patcher = patch("tools.zero_printer_watcher.PID_FILE", self.tmp_path / "watcher.pid")
        self._status_patcher = patch("tools.zero_printer_watcher.STATUS_FILE", self.tmp_path / "watcher_status.json")
        self._orphan_patcher = patch("tools.zero_printer_watcher._find_running_watcher_pids", return_value=[])
        self._pid_patcher.start()
        self._status_patcher.start()
        self.mock_orphans = self._orphan_patcher.start()

    def tearDown(self):
        self._orphan_patcher.stop()
        self._status_patcher.stop()
        self._pid_patcher.stop()
        self._tmpdir.cleanup()

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

    @patch("tools.zero_printer_watcher.time.sleep")
    @patch("tools.zero_printer_watcher.os.kill")
    def test_stop_daemon_kills_orphans_when_pid_file_missing(self, mock_kill, mock_sleep):
        import signal
        # Simulate watcher.pid missing on disk while an orphaned --internal-run process (PID 8888) is alive
        self.mock_orphans.side_effect = [[8888], [8888], [], []]
        success = stop_daemon(timeout=2.0)
        self.assertTrue(success)
        mock_kill.assert_called_once_with(8888, signal.SIGTERM)

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

    def test_archive_and_compile_timelapse_gif(self):
        from PIL import Image
        import tools.zero_printer_watcher as zpw

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(zpw, "TIMELAPSE_ROOT", Path(tmpdir)):
                src_img = Path(tmpdir) / "snap.jpg"
                Image.new("RGB", (640, 480), color=(120, 140, 160)).save(src_img, "JPEG")

                f1 = zpw.archive_timelapse_frame(src_img, "gotchi_case_v4_plate.gcode", reset_if_new=True)
                self.assertIsNotNone(f1)
                self.assertEqual(f1.name, "frame_0001.jpg")

                Image.new("RGB", (640, 480), color=(180, 190, 200)).save(src_img, "JPEG")
                f2 = zpw.archive_timelapse_frame(src_img, "gotchi_case_v4_plate.gcode", reset_if_new=False)
                self.assertIsNotNone(f2)
                self.assertEqual(f2.name, "frame_0002.jpg")

                gif_path = zpw.compile_timelapse_gif("gotchi_case_v4_plate.gcode", max_width=320)
                self.assertIsNotNone(gif_path)
                self.assertTrue(gif_path.exists())
                self.assertGreater(gif_path.stat().st_size, 100)


    @patch("tools.zero_printer_watcher.compile_timelapse_gif")
    @patch("tools.zero_printer_watcher.archive_timelapse_frame")
    @patch("tools.zero_printer_watcher.speak_announcement", return_value=True)
    @patch("tools.zero_printer_watcher.cut_printer_power", return_value=True)
    @patch("tools.zero_printer_watcher.stop_webcam", return_value=True)
    @patch("tools.zero_printer_watcher.capture_webcam_snapshot", return_value=True)
    @patch("tools.zero_printer_watcher.deliver_discord_photo", return_value=True)
    @patch("tools.zero_printer_watcher.get_nozzle_temp", return_value=42.0)
    @patch.object(OctoPrintClient, "get_job")
    def test_daemon_terminal_done_triggers_timelapse_gif(
        self,
        mock_get_job,
        mock_temp,
        mock_deliver,
        mock_snap,
        mock_stop_cam,
        mock_cut_pwr,
        mock_speak,
        mock_archive,
        mock_compile,
    ):
        mock_get_job.return_value = {
            "state": "Operational",
            "progress": {"completion": 100.0, "printTime": 1800},
            "job": {"file": {"name": "gear_bearing.gcode"}},
        }
        mock_gif_path = MagicMock(spec=Path)
        mock_gif_path.exists.return_value = True
        mock_compile.return_value = mock_gif_path

        daemon = PrinterWatcherDaemon(
            octoprint_url="http://127.0.0.1:8888",
            api_key="test_key",
            interval_print=1,
            interval_cooldown=1,
            cooldown_temp=50.0,
            channel="3d-print",
        )

        with patch.object(daemon, "write_pid"), patch.object(daemon, "remove_pid"), patch("tools.zero_printer_watcher.time.sleep", side_effect=[None, None]):
            daemon.run()

        # Verify archive_timelapse_frame called on terminal snapshot
        mock_archive.assert_called_with(unittest.mock.ANY, "gear_bearing.gcode", reset_if_new=False)
        # Verify compile_timelapse_gif called with job filename
        mock_compile.assert_called_once_with("gear_bearing.gcode")
        # Verify deliver_discord_photo delivered the GIF
        delivery_calls = mock_deliver.call_args_list
        gif_deliveries = [call for call in delivery_calls if "🎬 **Zero Watcher: Print Timelapse GIF**" in call[0][1]]
        self.assertEqual(len(gif_deliveries), 1)
        self.assertEqual(gif_deliveries[0][0][0], mock_gif_path)
        self.assertEqual(gif_deliveries[0][0][2], "3d-print")

    @patch("tools.zero_printer_watcher.archive_timelapse_frame")
    @patch("tools.zero_printer_watcher.evaluate_print_image")
    @patch("tools.zero_printer_watcher.capture_webcam_snapshot", return_value=True)
    @patch.object(OctoPrintClient, "get_job")
    def test_daemon_printing_archives_timelapse_frame_fresh_start(
        self,
        mock_get_job,
        mock_snap,
        mock_eval,
        mock_archive,
    ):
        mock_get_job.return_value = {
            "state": "Printing",
            "progress": {"completion": 1.0, "printTime": 30},
            "job": {"file": {"name": "test_fresh.gcode"}},
        }
        mock_eval.return_value = {"failure": False, "confidence": 0.99, "issue": "nominal", "summary": "clean"}

        daemon = PrinterWatcherDaemon(
            octoprint_url="http://127.0.0.1:8888",
            api_key="test_key",
            interval_print=0,
            interval_timelapse=180,
            channel="3d-print",
            post_progress=False,
        )

        with patch.object(daemon, "write_pid"), patch.object(daemon, "remove_pid"), patch("tools.zero_printer_watcher.get_nozzle_temp", return_value=205.0), patch("tools.zero_printer_watcher.time.sleep", side_effect=KeyboardInterrupt):
            try:
                daemon.run()
            except KeyboardInterrupt:
                pass

        # Since was_printing was False and completion < 2.0, reset_if_new should be True
        mock_archive.assert_called_once_with(unittest.mock.ANY, "test_fresh.gcode", reset_if_new=True)
        mock_eval.assert_called_once_with(unittest.mock.ANY, completion=1.0)

    @patch("tools.zero_printer_watcher.archive_timelapse_frame")
    @patch("tools.zero_printer_watcher.evaluate_print_image")
    @patch("tools.zero_printer_watcher.capture_webcam_snapshot", return_value=True)
    @patch.object(OctoPrintClient, "get_job")
    def test_daemon_timelapse_interval_archiving(
        self,
        mock_get_job,
        mock_snap,
        mock_eval,
        mock_archive,
    ):
        mock_get_job.return_value = {
            "state": "Printing",
            "progress": {"completion": 25.0, "printTime": 600},
            "job": {"file": {"name": "mid_job.gcode"}},
        }

        daemon = PrinterWatcherDaemon(
            octoprint_url="http://127.0.0.1:8888",
            api_key="test_key",
            interval_print=10000,
            interval_timelapse=10,
            channel="3d-print",
        )

        with patch.object(daemon, "write_pid"), patch.object(daemon, "remove_pid"), patch("tools.zero_printer_watcher.get_nozzle_temp", return_value=200.0), patch("time.time", return_value=1000.0), patch("tools.zero_printer_watcher.time.sleep", side_effect=KeyboardInterrupt):
            try:
                daemon.run()
            except KeyboardInterrupt:
                pass

        # Eval was skipped because (1000 - 0) < 10000, but timelapse triggered because (1000 - 0) >= 10
        mock_eval.assert_not_called()
        mock_archive.assert_called_once_with(unittest.mock.ANY, "mid_job.gcode", reset_if_new=False)

    def test_weekly_bed_mesh_policy_profile_selection(self):
        import tools.zero_printer_watcher as zpw

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "bed_mesh_state.json"
            # 1. Missing state file -> probe is due, selects my_machine_probe.json and writes state
            p1 = zpw.select_machine_profile(now_epoch=1792764600, state_file=state_path)
            self.assertEqual(p1, "/prints/my_machine_probe.json")
            self.assertTrue(state_path.exists())

            # 2. Within 7 days (e.g. +2 days) -> probe NOT due, selects fast my_machine.json
            p2 = zpw.select_machine_profile(now_epoch=1792764600 + 2 * 86400, state_file=state_path)
            self.assertEqual(p2, "/prints/my_machine.json")

            # 3. After 7 days (+7 days) -> probe IS due, selects my_machine_probe.json
            p3 = zpw.select_machine_profile(now_epoch=1792764600 + 7 * 86400, state_file=state_path)
            self.assertEqual(p3, "/prints/my_machine_probe.json")

            # 4. Explicit force_probe=True within 7-day window -> selects my_machine_probe.json
            p4 = zpw.select_machine_profile(force_probe=True, now_epoch=1792764600 + 8 * 86400, state_file=state_path)
            self.assertEqual(p4, "/prints/my_machine_probe.json")

    def test_xy_phase_locked_burst_scoring_and_unoccluded_check(self):
        def _make_frame(bed_y=DEFAULT_TARGET_BED_Y, head_x=DEFAULT_TARGET_HEAD_X, head_y=800.0):
            arr = np.full((1920, 1080, 3), 100, dtype=np.uint8)
            by_px = int(round(bed_y))
            # PEI lip step-edge in x=520..850
            arr[1220:by_px, 520:850] = [200, 180, 50]
            arr[by_px:1760, 520:850] = [30, 30, 30]
            # COMGROW silkscreen fiducial
            bx = 255.0 + (-0.35) * (bed_y - DEFAULT_TARGET_BED_Y)
            bx_px = int(round(bx))
            arr[by_px - 40:by_px - 20, bx_px - 30:bx_px + 30] = [240, 240, 240]
            # Toolhead black shroud
            hx_px = int(round(head_x))
            hy_px = int(round(head_y))
            arr[hy_px - 40:hy_px + 40, hx_px - 40:hx_px + 40] = [20, 20, 20]
            # Printed model
            arr[1050:1150, 500:600] = [255, 255, 255]
            return Image.fromarray(arr)

        # 1. Phase-locked frame (bed Y at 1385, toolhead X at 370)
        f_locked = _make_frame(DEFAULT_TARGET_BED_Y, DEFAULT_TARGET_HEAD_X, 800.0)
        score_locked, details_locked = score_burst_frame(f_locked)
        self.assertLessEqual(details_locked["bed_y_err"], 14.0)
        self.assertLessEqual(details_locked["head_x_err"], 45.0)
        self.assertTrue(is_frame_unoccluded(details_locked))

        # 2. Out-of-phase bed Y frame (bed_y=1350, err=145.0 > 14.0)
        f_bed_out = _make_frame(1350.0, DEFAULT_TARGET_HEAD_X, 800.0)
        score_bed_out, details_bed_out = score_burst_frame(f_bed_out)
        self.assertGreater(details_bed_out["bed_y_err"], 14.0)
        self.assertFalse(is_frame_unoccluded(details_bed_out))
        self.assertGreater(score_locked, score_bed_out)

        # 3. Out-of-phase toolhead X frame (head_x=650, err=280.0 > 45.0)
        f_head_out = _make_frame(DEFAULT_TARGET_BED_Y, 650.0, 800.0)
        score_head_out, details_head_out = score_burst_frame(f_head_out)
        self.assertGreater(details_head_out["head_x_err"], 45.0)
        self.assertFalse(is_frame_unoccluded(details_head_out))
        self.assertGreater(score_locked, score_head_out)

        # 4. Out-of-phase frame with artificially forced high score still rejected
        fake_out_details = {
            "score": 999999.0,
            "shroud_px": 0,
            "dist": 350.0,
            "bed_y_err": 25.0,  # exceeds max_bed_y_err 14.0
            "head_x_err": 10.0,
        }
        self.assertFalse(is_frame_unoccluded(fake_out_details))

    def test_compile_timelapse_gif_bed_stabilization(self):
        def _make_frame(bed_y=DEFAULT_TARGET_BED_Y):
            arr = np.full((1920, 1080, 3), 100, dtype=np.uint8)
            by_px = int(round(bed_y))
            arr[1220:by_px, 520:850] = [200, 180, 50]
            arr[by_px:1760, 520:850] = [30, 30, 30]
            bx = 255.0 + (-0.35) * (bed_y - DEFAULT_TARGET_BED_Y)
            bx_px = int(round(bx))
            arr[by_px - 40:by_px - 20, bx_px - 30:bx_px + 30] = [240, 240, 240]
            arr[760:840, 330:410] = [20, 20, 20]
            arr[1050:1150, 500:600] = [255, 255, 255]
            return Image.fromarray(arr)

        tl_dir = self.tmp_path / "timelapse" / "stab_job"
        tl_dir.mkdir(parents=True, exist_ok=True)

        # 6 frames: 5 near nominal 1385, 1 extreme out-of-phase outlier at 1280
        test_ys = [1380.0, 1385.0, 1280.0, 1390.0, 1382.0, 1388.0]
        for idx, by in enumerate(test_ys, start=1):
            _make_frame(by).save(tl_dir / f"frame_{idx:04d}.jpg")

        with patch("tools.zero_printer_watcher.TIMELAPSE_ROOT", self.tmp_path / "timelapse"):
            # Compile with stationary-camera outlier filtering (zero 2D canvas translation)
            out_gif = compile_timelapse_gif("stab_job.gcode", max_width=540, frame_ms=100, final_hold_ms=500, stabilize_bed=True)
            self.assertIsNotNone(out_gif)
            self.assertTrue(out_gif.exists())
            self.assertGreater(out_gif.stat().st_size, 0)

            gif_img = Image.open(out_gif)
            detected_ys = []
            for i in range(gif_img.n_frames):
                gif_img.seek(i)
                fr = gif_img.convert("RGB")
                pose = detect_bed_and_toolhead_pose(fr)
                detected_ys.append(pose["bed_y"])

            # The 1280.0 outlier frame should be dropped while keeping the 5 phase-locked frames
            self.assertEqual(len(detected_ys), 5)
            raw_variance = float(np.var(test_ys))
            stab_variance = float(np.var(detected_ys))
            self.assertLess(stab_variance, raw_variance / 5.0)


if __name__ == "__main__":
    unittest.main()
