#!/usr/bin/env python3
import json
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, "/workspace/tools")
import google_home_mcp

class TestGoogleHomeMCP(unittest.TestCase):
    def test_load_credentials(self):
        creds = google_home_mcp.load_credentials("/workspace/config/google_oauth.json.example")
        self.assertIn("client_id", creds)
        self.assertIn("client_secret", creds)

    @patch("google_home_mcp._call_home_mcp")
    def test_list_homes(self, mock_call):
        mock_call.return_value = {"ok": True, "result": {"homes": [{"id": "home-123"}]}}
        res = json.loads(google_home_mcp.list_homes())
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["homes"][0]["id"], "home-123")

    @patch("google_home_mcp._call_home_mcp")
    def test_list_home_resources(self, mock_call):
        mock_call.return_value = {"ok": True, "result": {"resources": []}}
        res = json.loads(google_home_mcp.list_home_resources("structure-1"))
        mock_call.assert_called_once_with("list_home_resources", {"structureId": "structure-1"})
        self.assertTrue(res["ok"])

    @patch("google_home_mcp._call_home_mcp")
    def test_list_home_history(self, mock_call):
        mock_call.return_value = {"ok": True, "result": {"entries": []}}
        res = json.loads(google_home_mcp.list_home_history("structure-1", start_time="2026-09-16T10:00:00Z"))
        mock_call.assert_called_once_with("list_home_history", {
            "structureId": "structure-1",
            "includeMediaUrls": True,
            "startTime": "2026-09-16T10:00:00Z"
        })
        self.assertTrue(res["ok"])

if __name__ == "__main__":
    unittest.main()
