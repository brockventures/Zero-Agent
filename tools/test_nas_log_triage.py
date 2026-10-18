import unittest
from unittest.mock import patch
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
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
        self.assertIsNotNone(rx.search("| If you restarted Plex just before running ImageMaid, and are still getting this error, it          |"))
        self.assertIsNotNone(rx.search('browserless.io:ChromiumFunctionPostRoute:trace [198.51.100.1] [sessionId=ceba13ef] [POST /function] error: Failed to load resource: the server responded with a status of 404 (Not Found)'))
        self.assertIsNotNone(rx.search('browserless.io:index:info Current period usage: {"date":1790567943803,"error":3}'))

    def test_noise_re_octoprint_init_videoin(self):
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
        rx = ns["noise_re"]
        sample = "i: init_VideoIn failed"
        self.assertIsNotNone(rx.search(sample))
    def test_noise_re_debug_level_ignored(self):
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
        rx = ns["noise_re"]
    def test_noise_re_zigpy_sleepy_delivery_timeout(self):
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
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
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
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
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
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
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
        rx = ns["noise_re"]
        samples = [
            "2026-10-07 21:47:14.671 ERROR (MainThread) [homeassistant.components.broadlink.updater] Error fetching RM4 mini remote (RM4 mini at 198.51.100.190) data: [Errno -4000] Network timeout: No response received within 10s",
            "2026-10-07 21:48:14.706 INFO (MainThread) [homeassistant.components.broadlink.updater] Fetching RM4 mini remote (RM4 mini at 198.51.100.190) data recovered",
        ]
        for s in samples:
            self.assertIsNotNone(rx.search(s), f"Failed to match: {s}")



    def test_noise_re_triage_enhancements(self):
        from tools.nas_log_triage import REMOTE_BATCH_SCANNER
        ns = {}
        exec("import re\n" + REMOTE_BATCH_SCANNER[:REMOTE_BATCH_SCANNER.find("err_re =")], ns)
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

if __name__ == "__main__":
    unittest.main()

