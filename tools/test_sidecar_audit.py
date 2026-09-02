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
        ok, results, collisions = sa.audit_sidecars()
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
        ok, results, collisions = sa.audit_sidecars()
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
        ok, results, collisions = sa.audit_sidecars()
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
        ok, results, collisions = sa.audit_sidecars()
        self.assertTrue(ok)
        self.assertEqual(len(collisions), 1)
        self.assertIn("job1", collisions[0])
        self.assertIn("job2", collisions[0])


if __name__ == "__main__":
    unittest.main()
