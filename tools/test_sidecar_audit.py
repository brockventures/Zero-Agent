#!/usr/bin/env python3
import sys
import unittest
import json
import tempfile
from pathlib import Path

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.sidecar_audit as sa


class TestSidecarAudit(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        sa.SCHEDULE_FILE = self.temp_path / "schedule.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_schedule(self):
        sa.SCHEDULE_FILE.write_text("[]")
        ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=False)
        self.assertTrue(ok)
        self.assertEqual(len(results), 0)

    def test_missing_script_failure(self):
        fake_schedule = [
            {
                "id": "nonexistent_job",
                "name": "Ghost Job",
                "enabled": True,
                "schedule_type": "daily",
                "hour_pt": 12,
                "minute_pt": 0,
                "prompt": "Run script using /workspace/tools/does_not_exist.py --check"
            }
        ]
        sa.SCHEDULE_FILE.write_text(json.dumps(fake_schedule))
        ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=False)
        self.assertFalse(ok)
        self.assertEqual(results[0]["status"], "FAIL")
        self.assertIn("Target script does not exist", results[0]["issues"][0])

    def test_invalid_schedule_type(self):
        fake_schedule = [
            {
                "id": "bad_type_job",
                "name": "Bad Type",
                "enabled": True,
                "schedule_type": "hourly",  # invalid
                "prompt": "[INTERNAL_SESSION_ROLLOVER]"
            }
        ]
        sa.SCHEDULE_FILE.write_text(json.dumps(fake_schedule))
        ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=False)
        self.assertFalse(ok)
        self.assertEqual(results[0]["status"], "FAIL")
        self.assertTrue(any("Invalid schedule_type" in i for i in results[0]["issues"]))

    def test_collision_detection(self):
        fake_schedule = [
            {
                "id": "job1",
                "name": "Job 1",
                "enabled": True,
                "schedule_type": "daily",
                "hour_pt": 5,
                "minute_pt": 30,
                "prompt": "[INTERNAL_SESSION_ROLLOVER]"
            },
            {
                "id": "job2",
                "name": "Job 2",
                "enabled": True,
                "schedule_type": "daily",
                "hour_pt": 5,
                "minute_pt": 30,
                "prompt": "[INTERNAL_SESSION_ROLLOVER]"
            }
        ]
        sa.SCHEDULE_FILE.write_text(json.dumps(fake_schedule))
        ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=False)
        self.assertTrue(ok)
        self.assertEqual(len(collisions), 1)
        self.assertIn("job1", collisions[0])
        self.assertIn("job2", collisions[0])

    def test_orphan_detection(self):
        fake_schedule = [
            {
                "id": "known_job",
                "name": "Known Job",
                "enabled": True,
                "schedule_type": "daily",
                "hour_pt": 10,
                "minute_pt": 0,
                "prompt": "[INTERNAL_SESSION_ROLLOVER]"
            }
        ]
        sa.SCHEDULE_FILE.write_text(json.dumps(fake_schedule))
        
        fake_sidecars = self.temp_path / "fake_sidecars.py"
        fake_sidecars.write_text("""
run_sidecar_job("known_job", "Known", lambda: (True, ""))
run_sidecar_job("unregistered_ghost_job", "Ghost", lambda: (True, ""))
""")
        orig_sidecars = sa.SIDECARS_FILE
        try:
            sa.SIDECARS_FILE = fake_sidecars
            ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=True)
            self.assertFalse(ok)
            self.assertEqual(len(orphans), 1)
            self.assertIn("unregistered_ghost_job", orphans[0])
        finally:
            sa.SIDECARS_FILE = orig_sidecars

    def test_nine_workday_proactive_decision_card_sidecars(self):
        """Assert all 9 workday proactive decision card sidecars pass 5-layer audit."""
        repo_root = Path(__file__).resolve().parent.parent
        sched_file = repo_root / "data" / "schedule.json"
        sidecars_file = repo_root / "tools" / "sidecars.py"
        commands_file = repo_root / "tools" / "bridge_commands.py"

        orig_schedule = sa.SCHEDULE_FILE
        orig_sidecars = sa.SIDECARS_FILE
        orig_commands = sa.COMMANDS_FILE
        try:
            sa.SCHEDULE_FILE = sched_file
            sa.SIDECARS_FILE = sidecars_file
            sa.COMMANDS_FILE = commands_file

            ok, results, collisions, orphans = sa.audit_sidecars(check_orphans=True)

            expected_jobs = [
                "morning_inbox_triage_card",
                "commute_inbox_triage_card",
                "family_calendar_radar_card",
                "dinner_pivot_card",
                "actual_budget_anomaly_card",
                "weather_energy_card",
                "midweek_grocery_card",
                "evening_pm_build_card",
                "usage_telemetry_audit",
            ]

            self.assertTrue(ok, f"Sidecar audit reported failures: {[r for r in results if r['status'] == 'FAIL']}")
            self.assertEqual(len(collisions), 0, f"Unexpected schedule collisions: {collisions}")
            self.assertEqual(len(orphans), 0, f"Unexpected orphaned sidecars: {orphans}")

            result_map = {r["id"]: r for r in results}
            for jid in expected_jobs:
                self.assertIn(jid, result_map, f"Expected job {jid} missing from audit results")
                r = result_map[jid]
                self.assertEqual(r["status"], "PASS", f"Job {jid} did not pass: issues={r['issues']}, warnings={r['warnings']}")
                self.assertEqual(len(r["issues"]), 0, f"Job {jid} had issues: {r['issues']}")
                self.assertEqual(len(r["warnings"]), 0, f"Job {jid} had warnings: {r['warnings']}")
        finally:
            sa.SCHEDULE_FILE = orig_schedule
            sa.SIDECARS_FILE = orig_sidecars
            sa.COMMANDS_FILE = orig_commands


if __name__ == "__main__":
    unittest.main()
