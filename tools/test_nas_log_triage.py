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

    def test_noise_re_cloudflared_disconnect(self):
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
        rx = ns["noise_re"]
        sample = '2026-09-23T04:16:42Z ERR Request failed error="stream 64477 canceled by remote with error code 0" connIndex=0 dest=https://outpost.brock.ventures/events'
        self.assertIsNotNone(rx.search(sample))
        self.assertIsNotNone(rx.search("[BridgeDaemon] ⚠️ Turn execution failed in #the-banana-stand: Persistent worker for #the-banana-stand terminated unexpectedly (exit code 1). Recycling worker to purge pipe state..."))
        self.assertIsNotNone(rx.search("[BridgeTimer:#the-banana-stand] [FAILED: Persistent worker for #the-banana-stand terminated unexpectedly (exit code 1)] Turn finished in 6.05s"))
        self.assertIsNotNone(rx.search("2026-09-23 21:31:15.805 ERROR (SyncWorker_12) [hyundai_kia_connect_api.KiaUvoApiUSA] hyundai_kia_connect_api - Error: unknown error response"))
        self.assertIsNotNone(rx.search("2026-09-23 21:31:15.949 ERROR (MainThread) [custom_components.kia_uvo.coordinator] Force update failed, falling back to cached"))
        self.assertIsNotNone(rx.search("2026-09-23 22:06:31.748 ERROR (MainThread) [custom_components.emporia_vue] Error communicating with Emporia API: HTTPSConnectionPool(host='api.emporiaenergy.com', port=443): Read timed out."))



if __name__ == "__main__":
    unittest.main()
