import unittest
from unittest.mock import patch
from tools.nas_log_triage import run_nas_log_review

class TestNasLogTriage(unittest.TestCase):
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    def test_zero_containers_fails(self, mock_scan):
        mock_scan.return_value = {
            "total_scanned": 0,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {"remote-host.local": "SSH timeout"},
            "duration_sec": 20.0
        }
        ok, rep, extra = run_nas_log_review()
        self.assertFalse(ok)
        self.assertIn("Failed to scan", rep)
        self.assertNotIn("healthy", rep)

    @patch("tools.nas_log_triage.scan_all_nas_containers")
    def test_nominal_scan(self, mock_scan):
        mock_scan.return_value = {
            "total_scanned": 35,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "duration_sec": 3.2
        }
        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertIn("All 35 containers healthy", rep)

    def test_regex_oom_boundary(self):
        import re
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        m = re.search(r"err_re = re\.compile\((r'[^']+')", REMOTE_BATCH_SCANNER)
        self.assertIsNotNone(m)
        pattern = eval(m.group(1))
        rx = re.compile(pattern, re.I)
        self.assertIsNone(rx.search("Living Room TV Connection reestablished!"))
        self.assertIsNotNone(rx.search("kernel: out of memory: oom-killer"))
        self.assertIsNotNone(rx.search("fatal error encountered"))

if __name__ == "__main__":
    unittest.main()
