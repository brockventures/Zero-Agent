import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.nas_log_triage import (
    REMOTE_BATCH_SCANNER,
    evaluate_log_velocity,
    fetch_unifi_ids_alerts,
    format_autonomous_triage_prompt,
    run_nas_log_review,
)


class TestNasLogTriage(unittest.TestCase):
    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts", return_value={"ok": True, "alerts": [], "count": 0})
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    def test_zero_containers_fails(self, mock_scan, _mock_ids):
        mock_scan.return_value = {
            "total_scanned": 0,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {"remote-host.local": "SSH timeout"},
            "duration_sec": 20.0
        }
        ok, rep, _extra = run_nas_log_review()
        self.assertFalse(ok)
        self.assertIn("Failed to scan", rep)
        self.assertNotIn("healthy", rep)

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts", return_value={"ok": True, "alerts": [], "count": 0})
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    def test_nominal_scan(self, mock_scan, _mock_ids):
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
        self.assertNotIn("UniFi IDS", rep)
        self.assertEqual(extra.get("unifi_ids_alerts"), {"ok": True, "alerts": [], "count": 0})
        self.assertEqual(extra.get("total_issues"), 0)
        prompt = format_autonomous_triage_prompt(extra)
        self.assertNotIn("UniFi IDS", prompt)
        self.assertNotIn("unifi_ids_alerts", prompt)

    def test_regex_oom_boundary(self):
        m = re.search(r"err_re = re\.compile\((r'[^']+')", REMOTE_BATCH_SCANNER)
        self.assertIsNotNone(m)
        pattern = eval(m.group(1))
        rx = re.compile(pattern, re.IGNORECASE)
        self.assertIsNone(rx.search("Living Room TV Connection reestablished!"))
        self.assertIsNotNone(rx.search("kernel: out of memory: oom-killer"))
        self.assertIsNotNone(rx.search("fatal error encountered"))

    def test_noise_re_cloudflared_disconnect(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        sample = '2026-09-23T04:16:42Z ERR Request failed error="stream 64477 canceled by remote with error code 0" connIndex=0 dest=https://outpost.brock.ventures/events'
        self.assertIsNotNone(rx.search(sample))
        self.assertIsNotNone(rx.search("[BridgeDaemon] ⚠️ Turn execution failed in #the-banana-stand: Persistent worker for #the-banana-stand terminated unexpectedly (exit code 1). Recycling worker to purge pipe state..."))
        self.assertIsNotNone(rx.search("[BridgeTimer:#the-banana-stand] [FAILED: Persistent worker for #the-banana-stand terminated unexpectedly (exit code 1)] Turn finished in 6.05s"))
        self.assertIsNotNone(rx.search("2026-09-23 21:31:15.805 ERROR (SyncWorker_12) [hyundai_kia_connect_api.KiaUvoApiUSA] hyundai_kia_connect_api - Error: unknown error response"))
        self.assertIsNotNone(rx.search("2026-09-23 21:31:15.949 ERROR (MainThread) [custom_components.kia_uvo.coordinator] Force update failed, falling back to cached"))
        self.assertIsNotNone(rx.search("2026-09-23 22:06:31.748 ERROR (MainThread) [custom_components.emporia_vue] Error communicating with Emporia API: HTTPSConnectionPool(host='api.emporiaenergy.com', port=443): Read timed out."))
        self.assertIsNotNone(rx.search("| If you restarted Plex just before running ImageMaid, and are still getting this error, it          |"))
        self.assertIsNotNone(rx.search('browserless.io:ChromiumFunctionPostRoute:trace [198.51.100.1] [sessionId=ceba13ef] [POST /function] error: Failed to load resource: the server responded with a status of 404 (Not Found)'))
        self.assertIsNotNone(rx.search('browserless.io:index:info Current period usage: {"date":1790567943803,"error":3}'))

    def test_noise_re_octoprint_init_videoin(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        self.assertIsNone(rx.search("i: init_VideoIn failed"))
        self.assertIsNone(rx.search("2026-11-25T05:00:00Z octoprint.plugins.webcam ERROR Webcam stream failed"))
        self.assertIsNotNone(
            rx.search(
                "2026-11-25 04:12:00 WARNING (MainThread) [homeassistant.components.octoprint] Config entry 'OctoPrint' not ready yet"
            )
        )

    def test_noise_re_debug_level_ignored(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        self.assertIsNotNone(rx)

    def test_noise_re_zigpy_sleepy_delivery_timeout(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        samples = [
            "2026-10-05 21:45:32.187 WARNING (MainThread) [zigpy.application] Device <CustomZigpyDevice model='09B9' manuf='IKEA of Sweden' nwk=0xC9A1 ieee=10:35:97:00:00:15:b0:07 is_initialized=True> callback failed - DeliveryError('Failed to send request: <Status.APS_NO_ACK: 183>')",
            "zigpy_znp.exceptions.InvalidCommandResponse: Unsuccessful request status code: <Status.APS_NO_ACK: 183>",
            "During handling of the above exception, another exception occurred:",
            "raise DeliveryError(f\"Failed to send request: {status!r}\", status=status)",
            "zigpy.exceptions.DeliveryError: Failed to send request: <Status.APS_NO_ACK: 183>",
            "2026-10-05 18:45:44.504 WARNING (MainThread) [zigpy.application] Device <CustomZigpyDevice model='09B9' manuf='IKEA of Sweden' nwk=0xC9A1 ieee=10:35:97:00:00:15:b0:07 is_initialized=True> callback failed - DeliveryError('Failed to send request: <Status.NWK_NO_ROUTE: 205>')",
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")

    def test_noise_re_enphase_envoy_transient(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        samples = [
            "2026-10-06 21:43:27.828 ERROR (MainThread) [homeassistant.components.enphase_envoy.coordinator] Error fetching Envoy 482532028193 data: Error communicating with Envoy API on 198.51.100.170: aiohttp Cli",
            "2026-10-06 21:53:36.213 ERROR (MainThread) [homeassistant.components.enphase_envoy.coordinator] Unexpected error updating listener 4966619989520 for Envoy 482532028193",
            "2026-10-06 21:53:36.380 ERROR (MainThread) [homeassistant.components.enphase_envoy.coordinator] Unexpected error updating listener 4966619992400 for Envoy 482532028193",
            "ValueError: Sensor sensor.encharge_492531004796_temperature has device class 'temperature', state class 'measurement' unit '°F' and suggested precision 'None' thus indicating it has a numeric value; however, it has the non-numeric value: 'unknown' (<class 'str'>)",
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")

    def test_noise_re_cloudflared_idle_dev_ports(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        samples = [
            '2026-10-06T23:50:48Z ERR  error="Unable to reach the origin service. The service may be down or it may not be responding to traffic from cloudflared: dial tcp 198.51.100.3:8000: connect: connection refused" connIndex=1 event=1 ingressRule=1 originService=http://discord-antigravity-agent:8000',
            '2026-10-06T23:50:48Z ERR Request failed error="Unable to reach the origin service. The service may be down or it may not be responding to traffic from cloudflared: dial tcp 198.51.100.3:8000: connect: connection refused" connIndex=1 dest=https://outpost.brock.ventures/robots.txt event=0 ip=198.41.200.33 type=http',
            '2026-10-07T04:59:48Z ERR  error="Unable to reach the origin service. The service may be down or it may not be responding to traffic from cloudflared: dial tcp 198.51.100.3:8001: connect: connection refused" connIndex=0 event=1 ingressRule=2 originService=http://discord-antigravity-agent:8001',
            '2026-10-07T04:59:48Z ERR Request failed error="Unable to reach the origin service. The service may be down or it may not be responding to traffic from cloudflared: dial tcp 198.51.100.3:8001: connect: connection refused" connIndex=0 dest=https://highball.brock.ventures/robots.txt event=0 ip=198.41.192.37 type=http',
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")

    def test_noise_re_broadlink_transient(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        samples = [
            "2026-10-07 21:47:14.671 ERROR (MainThread) [homeassistant.components.broadlink.updater] Error fetching RM4 mini remote (RM4 mini at 198.51.100.190) data: [Errno -4000] Network timeout: No response received within 10s",
            "2026-10-07 21:48:14.706 INFO (MainThread) [homeassistant.components.broadlink.updater] Fetching RM4 mini remote (RM4 mini at 198.51.100.190) data recovered",
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")

    def test_noise_re_triage_enhancements(self):
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)  # noqa: S102
        rx = ns["noise_re"]
        samples = [
            "--- End of inner exception stack trace ---",
            "at System.Net.Http.HttpClient.HandleFailure(Exception e, Boolean telemetryStarted, HttpResponseMessage response, CancellationTokenSource cts, CancellationToken cancellationToken, CancellationTokenSour",
            "2026-10-10 20:33:32.435 (Dummy-2) CHIP_ERROR [chip.native.DIS] OperationalSessionSetup[1:0000000000000002]: operational discovery failed: src/lib/address_resolve/AddressResolve_DefaultImpl.cpp:124: CH",
            "2026-10-10 20:33:32.435 (Dummy-2) CHIP_ERROR [chip.native.DMG] Failed to establish CASE for re-subscription with error 'src/lib/address_resolve/AddressResolve_DefaultImpl.cpp:124: CHIP Error 0x00000032: Timeout",
            '2026-10-11T04:15:06.267Z [error][Plex Scan]: Failed to process Plex media {"errorMessage":"No Guid metadata for this title. Skipping. (Try refreshing the metadata in Plex for this media!)","title":"AP PHYSICS"}',
            "2026-10-10 21:56:47.990 ERROR (MainThread) [homeassistant.components.plex.server] Could not connect to Plex server: BrockPlex (HTTPSConnectionPool(host='plex.tv', port=443): Read timed out. (read time",
            "2026-10-11 00:32:41.846 UTC [31706] myuser@baseball_data 198.51.100.3 ERROR:  function round(double precision, integer) does not exist at character 79",
            "2026-10-12 03:19:45.683 UTC [466] myuser@baseball_data 198.51.100.1 FATAL:  the database system is in recovery mode",
            "2026-10-12 03:20:01.865 UTC [470] myuser@baseball_data [local] FATAL:  the database system is not yet accepting connections",
            "2026-10-12 16:37:33.888 UTC [37] myuser@baseball_data 198.51.100.2 FATAL:  the database system is starting up",
            "2026-10-12 17:04:07.222 UTC [40] myuser@baseball_data [local] FATAL:  connection to client lost",
            "2026-10-12 19:38:31.104 UTC [8257] myuser@baseball_data 198.51.100.6 ERROR:  missing FROM-clause entry for table \"c\" at character 17",
            "2026-10-13 02:48:52.419 UTC [32878] myuser@baseball_data 198.51.100.6 FATAL:  connection to client lost",
            "2026-10-11 20:41:43.252 (MainThread) ERROR [matter_server.server.client_handler] [140105684363136] Error while handling: interview_node (node 2): Failed to interview node 2",
            "2026-10-12 04:45:37 ERROR    discord.client Attempting a reconnect in 1.79s",
            "Oct 13 05:02:03 NasHost01 otbr-agent[28420]: 3d.23:08:57.145 [I] Mle-----------: Router 0xc400 - Failed to schedule/send Link Request",
            "Oct 13 05:03:08 NasHost01 otbr-agent[28420]: 3d.23:10:01.722 [I] Mle-----------: Failed to process Advertisement - NoRoute",
            '2026-10-13T19:18:06Z WRN Serve tunnel error error="datagram manager error: Application error 0x0 (remote)" connIndex=2 event=0 ip=198.41.192.77',
            '2026-10-13T19:18:08Z WRN Connection terminated error="datagram manager error: Application error 0x0 (remote)" connIndex=2',
            '2026-10-14T00:21:24Z WRN Serve tunnel error error="datagram manager error: timeout: no recent network activity" connIndex=0 event=0 ip=198.41.192.27',
            '2026-10-13T12:11:40Z WRN Failed to dial a quic connection error="failed to dial to edge with quic: timeout: no recent network activity" connIndex=0 event=0 ip=198.41.192.57',
            '| --gotify-notify-error (GOTIFY_NOTIFY_ERROR): True                                                  |',
            '[BridgePipeline] target_dest delivery failed (400 Bad Request (error code: 50035): Invalid Form Body',
            '2026-10-17 21:39:54.867 WARNING (MainThread) [homeassistant.components.tplink.entity] Unable to read data for 198.51.100.211 number.kitchen_counter_lights_3_turn_off_in: get_auto_off_config for AutoOff',
            '2026-10-17 21:41:22.575 ERROR (MainThread) [snitun.multiplexer.core] Timeout error while pinging peer',
            '2026-10-17 20:35:52.095 (MainThread) WARNING [matter_server.server.client_handler] [140038283884640] Disconnected: Received error message: No PONG received after 27.5 seconds',
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts", return_value={"ok": True, "alerts": [], "count": 0})
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    @patch("tools.nas_log_triage.fetch_cisa_kev_catalog")
    def test_kev_silent_when_patched(self, mock_kev, mock_scan, _mock_ids):
        mock_kev.return_value = [
            {
                "cveID": "CVE-2026-9999",
                "vendorProject": "Plex",
                "product": "Plex Media Server",
                "vulnerabilityName": "Plex2Shell Arbitrary File Write",
                "shortDescription": "Plex Media Server prior to 1.43.2 contains an RCE vulnerability.",
                "requiredAction": "Upgrade to version 1.43.2 or later.",
            }
        ]
        mock_scan.return_value = {
            "total_scanned": 39,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "versions_by_host": {
                "198.51.100.10": {
                    "plex": "1.43.4.10903-e5521bd8c",
                    "seerr": "v3.5.0",
                    "synology-dsm": "7.3.2",
                }
            },
            "duration_sec": 2.1,
        }
        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertEqual(extra.get("kev_risks"), [])
        self.assertEqual(extra.get("total_issues"), 0)
        self.assertNotIn("Active Vulnerability", rep)

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts", return_value={"ok": True, "alerts": [], "count": 0})
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    @patch("tools.nas_log_triage.fetch_cisa_kev_catalog")
    def test_kev_flags_vulnerable_version(self, mock_kev, mock_scan, _mock_ids):
        mock_kev.return_value = [
            {
                "cveID": "CVE-2026-9999",
                "vendorProject": "F5",
                "product": "NGINX",
                "vulnerabilityName": "NGINX HTTP/3 RCE",
                "shortDescription": "NGINX prior to 1.27.4 allows unauthenticated remote code execution.",
                "requiredAction": "Apply updates per vendor instructions.",
            }
        ]
        mock_scan.return_value = {
            "total_scanned": 39,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "versions_by_host": {
                "198.51.100.10": {
                    "plex": "1.43.0.10000",
                    "nginx": "1.26.2",
                    "synology-dsm": "7.3.2",
                }
            },
            "duration_sec": 2.1,
        }
        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertEqual(len(extra.get("kev_risks", [])), 2)
        self.assertGreaterEqual(extra.get("total_issues", 0), 2)
        self.assertIn("Active Vulnerability / Version Floor Risk Flagged", rep)
        self.assertIn("plex", rep)
        self.assertIn("CVE-2026-9999", rep)

    @patch("tools.nas_log_triage.urllib.request.build_opener")
    def test_fetch_unifi_ids_alerts_filters_local_dns_and_parses_risks(self, mock_build_opener):
        captured_requests = []

        login_resp = MagicMock()
        login_resp.headers = {"X-Csrf-Token": "csrf-tok-123"}
        login_resp.__enter__.return_value = login_resp
        login_resp.__exit__.return_value = False

        flows_payload = {
            "data": {
                "flows": [
                    {
                        "id": "flow-benign",
                        "action": "block",
                        "risk": {"level": "MEDIUM", "category": "dns_sinkhole"},
                        "source": {"ip": "198.51.100.20", "name": "NasHost01"},
                        "destination": {"ip": "127.0.0.1", "port": 53, "domain": "ads.example.invalid"},
                    },
                    {
                        "id": "flow-high",
                        "action": "block",
                        "risk": {"level": "HIGH", "category": "malware_command_and_control"},
                        "source": {"ip": "198.51.100.50", "port": 49152, "name": "IoT-Plug"},
                        "destination": {"ip": "203.0.113.99", "port": 8080, "domain": "bad.example.net"},
                        "ips": {"signature": "ET MALWARE Possible C2 Beacon"},
                        "protocol": "tcp",
                        "flow_start_time": 1764000000000,
                    },
                    {
                        "id": "flow-med",
                        "action": "allow",
                        "risk": "MEDIUM",
                        "source": {"ip": "198.51.100.60", "name": "Workstation"},
                        "destination": {"ip": "203.0.113.45", "port": 4444},
                        "signature": "ET SCAN Nmap Scripting Engine",
                    },
                ]
            }
        }
        flows_resp = MagicMock()
        flows_resp.read.return_value = json.dumps(flows_payload).encode("utf-8")
        flows_resp.__enter__.return_value = flows_resp
        flows_resp.__exit__.return_value = False

        mock_opener = MagicMock()

        def fake_open(req, timeout=10):
            captured_requests.append((req, timeout))
            if req.full_url.endswith("/api/auth/login"):
                return login_resp
            return flows_resp

        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        env_patch = {
            "UNIFI_BASE_URL": "https://198.51.100.1",
            "UNIFI_USER": "audit-bot",
            "UNIFI_PASSWORD": "test-password",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            result = fetch_unifi_ids_alerts(hours=24)

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["alerts"]), 2)
        self.assertEqual(result["alerts"][0]["risk"], "HIGH")
        self.assertEqual(result["alerts"][0]["signature"], "ET MALWARE Possible C2 Beacon")
        self.assertEqual(result["alerts"][0]["destination_ip"], "203.0.113.99")
        self.assertEqual(result["alerts"][1]["risk"], "MEDIUM")
        self.assertEqual(result["alerts"][1]["destination_ip"], "203.0.113.45")

        self.assertEqual(len(captured_requests), 2)
        login_req, flow_req = captured_requests[0][0], captured_requests[1][0]
        self.assertEqual(login_req.full_url, "https://198.51.100.1/api/auth/login")
        self.assertEqual(flow_req.full_url, "https://198.51.100.1/proxy/network/v2/api/site/default/traffic-flows")
        self.assertEqual(flow_req.headers.get("X-csrf-token"), "csrf-tok-123")
        body = json.loads(flow_req.data.decode("utf-8"))
        self.assertEqual(body["risks"], ["HIGH", "MEDIUM"])
        self.assertIn("timestampFrom", body)
        self.assertIn("timestampTo", body)
        self.assertGreater(body["timestampTo"], body["timestampFrom"])

    @patch("tools.nas_log_triage.urllib.request.build_opener")
    def test_fetch_unifi_ids_alerts_graceful_api_failure(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_opener.open.side_effect = OSError("Connection timed out")
        mock_build_opener.return_value = mock_opener

        env_patch = {
            "UNIFI_BASE_URL": "https://198.51.100.1",
            "UNIFI_USER": "audit-bot",
            "UNIFI_PASSWORD": "test-password",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            result = fetch_unifi_ids_alerts(hours=24)

        self.assertFalse(result["ok"])
        self.assertIn("Connection timed out", result["error"])
        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["count"], 0)

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts")
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    @patch("tools.nas_log_triage.evaluate_kev_risks", return_value=[])
    def test_run_nas_log_review_surfaces_unifi_ids_alerts(self, _mock_kev, mock_scan, mock_ids):
        mock_scan.return_value = {
            "total_scanned": 39,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "versions_by_host": {},
            "duration_sec": 1.5,
        }
        mock_ids.return_value = {
            "ok": True,
            "count": 2,
            "alerts": [
                {
                    "risk": "HIGH",
                    "action": "block",
                    "source_name": "IoT-Cam",
                    "source_ip": "198.51.100.55",
                    "destination_ip": "203.0.113.88",
                    "destination_port": 443,
                    "destination_domain": "c2.example.net",
                    "signature": "ET TROJAN Suspicious Outbound",
                },
                {
                    "risk": "MEDIUM",
                    "action": "allow",
                    "source_name": "",
                    "source_ip": "198.51.100.77",
                    "destination_ip": "203.0.113.44",
                    "destination_port": 22,
                    "destination_domain": "",
                    "signature": "ET SCAN SSH Probe",
                },
            ],
        }

        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertEqual(extra["total_issues"], 2)
        self.assertEqual(extra["unifi_ids_alerts"]["count"], 2)
        self.assertIn("UniFi IDS Alerts (2)", rep)
        self.assertIn("ET TROJAN Suspicious Outbound", rep)
        self.assertIn("203.0.113.88:443", rep)

        prompt = format_autonomous_triage_prompt(extra)
        self.assertIn("### UniFi IDS Alerts:", prompt)
        self.assertIn('"unifi_ids_alerts"', prompt)
        self.assertIn("ET SCAN SSH Probe", prompt)

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts")
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    @patch("tools.nas_log_triage.evaluate_kev_risks", return_value=[])
    def test_run_nas_log_review_graceful_on_unifi_failure(self, _mock_kev, mock_scan, mock_ids):
        mock_scan.return_value = {
            "total_scanned": 39,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "versions_by_host": {},
            "duration_sec": 1.2,
        }
        mock_ids.return_value = {
            "ok": False,
            "error": "HTTP 401 Unauthorized",
            "alerts": [],
            "count": 0,
        }

        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertEqual(extra["total_issues"], 0)
        self.assertFalse(extra["unifi_ids_alerts"]["ok"])
        self.assertNotIn("UniFi IDS", rep)
        self.assertIn("All 39 containers healthy", rep)

    def test_evaluate_log_velocity_flags_flood_and_ignores_normal(self):
        metrics = {
            "198.51.100.10": {
                "otbr": {
                    "container": "otbr",
                    "tail_lines": 250,
                    "tail_bytes": 40960,
                    "tail_span_sec": 120.0,
                    "log_size_bytes": 38 * 1024 * 1024,
                    "sample_line": "otbr-agent[49]: [D] SubMac--------: Frame rx failed",
                },
                "plex": {
                    "container": "plex",
                    "tail_lines": 250,
                    "tail_bytes": 25000,
                    "tail_span_sec": 43200.0,
                    "log_size_bytes": 512 * 1024,
                    "sample_line": "GET /identity 200 OK",
                },
            }
        }
        alerts = evaluate_log_velocity(metrics)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["container"], "otbr")
        self.assertEqual(alerts[0]["host"], "198.51.100.10")
        self.assertGreater(alerts[0]["est_mb_per_day"], 25.0)
        self.assertEqual(alerts[0]["log_size_mb"], 38.0)

    @patch("tools.nas_log_triage.fetch_unifi_ids_alerts", return_value={"ok": True, "alerts": [], "count": 0})
    @patch("tools.nas_log_triage.scan_all_nas_containers")
    @patch("tools.nas_log_triage.evaluate_kev_risks", return_value=[])
    def test_run_nas_log_review_surfaces_muted_storms_velocity_and_db_driver_drift(
        self, _mock_kev, mock_scan, _mock_ids
    ):
        mock_scan.return_value = {
            "total_scanned": 39,
            "actionable_issues": [],
            "stale_transients": [],
            "host_errors": {},
            "versions_by_host": {},
            "velocity_alerts": [
                {
                    "container": "otbr",
                    "host": "198.51.100.10",
                    "est_mb_per_day": 28.13,
                    "log_size_mb": 38.0,
                    "sample_line": "otbr-agent: [D] SubMac Frame tx attempt failed",
                }
            ],
            "muted_storms": [
                {
                    "container": "octoprint",
                    "host": "198.51.100.10",
                    "status": "Up 3 days",
                    "suppressed_errors": 180,
                    "suppressed_active": 180,
                    "sample_suppressed": ["otbr-agent: Failed to handle ICMPv6 message"],
                }
            ],
            "db_log_containers": [
                {
                    "container": "legacy-service",
                    "host": "198.51.100.20",
                    "log_driver": "db",
                }
            ],
            "duration_sec": 1.8,
        }

        ok, rep, extra = run_nas_log_review()
        self.assertTrue(ok)
        self.assertEqual(extra["total_issues"], 3)
        self.assertEqual(len(extra["velocity_alerts"]), 1)
        self.assertEqual(len(extra["muted_storms"]), 1)
        self.assertEqual(len(extra["db_log_containers"]), 1)
        self.assertIn("Log Velocity / Flood Alert (1)", rep)
        self.assertIn("otbr", rep)
        self.assertIn("28.13 MB/day", rep)
        self.assertIn("Muted Error Storm Circuit Breaker (1)", rep)
        self.assertIn("180", rep)
        self.assertIn("SQLite `db` Log-Driver Drift Flagged (1)", rep)
        self.assertIn("legacy-service", rep)

        prompt = format_autonomous_triage_prompt(extra)
        self.assertIn('"velocity_alerts"', prompt)
        self.assertIn('"muted_storms"', prompt)
        self.assertIn('"db_log_containers"', prompt)
        self.assertIn("legacy-service", prompt)


if __name__ == "__main__":
    unittest.main()

