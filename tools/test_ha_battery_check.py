#!/usr/bin/env python3
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tools.ha_battery_check import check_batteries, check_google_home_batteries
from tools.sidecars import run_ha_battery_check


class TestHABatteryCheck(unittest.TestCase):

    @patch('tools.ha_battery_check.check_google_home_batteries', return_value=[])
    @patch('tools.ha_battery_check.urllib.request.urlopen')
    @patch('tools.ha_battery_check.get_ha_config')
    def test_check_batteries_healthy(self, mock_config, mock_urlopen, mock_gh):
        mock_config.return_value = ('http://fake-ha:8123', 'fake-token')
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"entity_id": "sensor.temp_battery", "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Temp"}, "state": "85"}]'
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        rc = check_batteries(threshold=15.0, quiet=True)
        self.assertEqual(rc, 0)

    @patch('tools.ha_battery_check.check_google_home_batteries', return_value=[])
    @patch('tools.ha_battery_check.urllib.request.urlopen')
    @patch('tools.ha_battery_check.get_ha_config')
    def test_check_batteries_low_alert(self, mock_config, mock_urlopen, mock_gh):
        mock_config.return_value = ('http://fake-ha:8123', 'fake-token')
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"entity_id": "sensor.govee_battery", "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Govee Sensor"}, "state": "2"}]'
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        rc = check_batteries(threshold=15.0, quiet=True)
        self.assertEqual(rc, 0)

    @patch('tools.google_home_mcp.list_home_states')
    def test_check_google_home_batteries_door_lock_low(self, mock_list_states):
        inner = {
            "states": [
                {
                    "id": "device@lock1",
                    "type": "DoorLockDevice",
                    "displayName": "Front Door",
                    "traits": {
                        "PowerSource": {
                            "type": "DoorLockDevice",
                            "state": {
                                "batPercentRemaining": 24,
                                "batChargeLevel": "BAT_CHARGE_LEVEL_ENUM_OK",
                                "batReplacementNeeded": False,
                            },
                        }
                    },
                }
            ]
        }
        mock_list_states.return_value = json.dumps({
            "ok": True,
            "result": {"content": [{"text": json.dumps(inner)}]},
        })
        low = check_google_home_batteries(threshold=15.0)
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0][0], 12.0)
        self.assertIn("Front Door (Google Home)", low[0][1])

    @patch('subprocess.run')
    def test_run_ha_battery_check_with_findings(self, mock_subproc):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = 'Home Assistant Low Battery Alert: Sensor: 2%'
        mock_subproc.return_value = mock_res
        ok, out, extra = run_ha_battery_check(threshold=15.0)
        self.assertTrue(ok)
        self.assertTrue(extra.get('has_alert'))
        self.assertIn('Low Battery Alert', out)

    @patch('subprocess.run')
    def test_run_ha_battery_check_failure(self, mock_subproc):
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = 'Failed to query Home Assistant states: Connection refused'
        mock_subproc.return_value = mock_res
        ok, out, extra = run_ha_battery_check(threshold=15.0)
        self.assertFalse(ok)
        self.assertFalse(extra.get('has_alert'))


if __name__ == '__main__':
    unittest.main()
