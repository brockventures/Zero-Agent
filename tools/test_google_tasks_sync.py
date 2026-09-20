#!/usr/bin/env python3
"""
Unit tests for google_tasks_sync.py
Tests two-way synchronization, ID extraction, and state persistence with mocks.
"""
import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from tools.google_tasks_sync import (
    extract_task_id_from_notes,
    sync_tasks,
    load_sync_state,
    save_sync_state,
    load_local_tasks,
    save_local_tasks,
)

class TestGoogleTasksSync(unittest.TestCase):

    def test_extract_task_id_from_notes(self):
        self.assertEqual(extract_task_id_from_notes("[Zero Task #4 | Priority: P2]"), 4)
        self.assertEqual(extract_task_id_from_notes("[Zero Task #123 | Priority: P1]\nSome extra notes"), 123)
        self.assertIsNone(extract_task_id_from_notes(None))
        self.assertIsNone(extract_task_id_from_notes("Regular note without zero tag"))
        self.assertIsNone(extract_task_id_from_notes("[Some other tag #5]"))

    @patch("tools.google_tasks_sync.get_or_create_target_list", return_value="list_123")
    @patch("tools.google_tasks_sync.fetch_all_google_tasks")
    @patch("tools.google_tasks_sync.create_google_task")
    @patch("tools.google_tasks_sync.patch_google_task")
    @patch("tools.google_tasks_sync.load_local_tasks")
    @patch("tools.google_tasks_sync.save_local_tasks")
    @patch("tools.google_tasks_sync.load_sync_state")
    @patch("tools.google_tasks_sync.save_sync_state")
    def test_export_new_local_task_to_google(
        self, mock_save_state, mock_load_state, mock_save_local, mock_load_local,
        mock_patch, mock_create, mock_fetch_google, mock_get_list
    ):
        mock_load_local.return_value = [
            {"id": 101, "title": "Buy Cat6 cables", "priority": "p2", "status": "pending"}
        ]
        mock_load_state.return_value = {
            "list_id": "list_123", "mappings": {}
        }
        mock_fetch_google.return_value = []
        mock_create.return_value = {"id": "gt_101", "title": "Buy Cat6 cables", "status": "needsAction"}

        res = sync_tasks(quiet=True)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["changes"]["created_in_google"]), 1)
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        self.assertEqual(call_kwargs["title"], "Buy Cat6 cables")
        self.assertEqual(call_kwargs["status"], "needsAction")
        self.assertIn("[Zero Task #101 | Priority: P2]", call_kwargs["notes"])

    @patch("tools.google_tasks_sync.get_or_create_target_list", return_value="list_123")
    @patch("tools.google_tasks_sync.fetch_all_google_tasks")
    @patch("tools.google_tasks_sync.patch_google_task")
    @patch("tools.google_tasks_sync.load_local_tasks")
    @patch("tools.google_tasks_sync.save_local_tasks")
    @patch("tools.google_tasks_sync.load_sync_state")
    @patch("tools.google_tasks_sync.save_sync_state")
    def test_import_completed_status_from_google(
        self, mock_save_state, mock_load_state, mock_save_local, mock_load_local,
        mock_patch, mock_fetch_google, mock_get_list
    ):
        # Local task is currently pending
        local_task = {"id": 5, "title": "Fix sink sensor", "priority": "p2", "status": "pending", "google_task_id": "gt_5"}
        mock_load_local.return_value = [local_task]
        mock_load_state.return_value = {
            "list_id": "list_123",
            "mappings": {
                "5": {
                    "google_id": "gt_5",
                    "last_title": "Fix sink sensor",
                    "last_status": "pending",
                    "last_google_status": "needsAction"
                }
            }
        }
        # Ryan marked it completed in Google Tasks
        mock_fetch_google.return_value = [
            {"id": "gt_5", "title": "Fix sink sensor", "status": "completed", "notes": "[Zero Task #5 | Priority: P2]"}
        ]

        res = sync_tasks(quiet=True)
        self.assertTrue(res["ok"])
        self.assertEqual(local_task["status"], "completed")
        self.assertIn("Completed #5: Fix sink sensor", res["changes"]["pulled_to_local"])

    @patch("tools.google_tasks_sync.get_or_create_target_list", return_value="list_123")
    @patch("tools.google_tasks_sync.fetch_all_google_tasks")
    @patch("tools.google_tasks_sync.patch_google_task")
    @patch("tools.google_tasks_sync.load_local_tasks")
    @patch("tools.google_tasks_sync.save_local_tasks")
    @patch("tools.google_tasks_sync.load_sync_state")
    @patch("tools.google_tasks_sync.save_sync_state")
    def test_import_new_task_created_in_google_app(
        self, mock_save_state, mock_load_state, mock_save_local, mock_load_local,
        mock_patch, mock_fetch_google, mock_get_list
    ):
        local_tasks = [{"id": 10, "title": "Existing task", "priority": "p2", "status": "pending"}]
        mock_load_local.return_value = local_tasks
        mock_load_state.return_value = {
            "list_id": "list_123",
            "mappings": {
                "10": {
                    "google_id": "gt_10",
                    "last_title": "Existing task",
                    "last_status": "pending",
                    "last_google_status": "needsAction"
                }
            }
        }
        # Ryan added a brand new task on his phone
        mock_fetch_google.return_value = [
            {"id": "gt_10", "title": "Existing task", "status": "needsAction", "notes": "[Zero Task #10 | Priority: P2]"},
            {"id": "gt_new", "title": "Clean out garage workbench", "status": "needsAction", "notes": ""}
        ]

        res = sync_tasks(quiet=True)
        self.assertTrue(res["ok"])
        self.assertEqual(len(local_tasks), 2)
        new_task = local_tasks[-1]
        self.assertEqual(new_task["id"], 11)
        self.assertEqual(new_task["title"], "Clean out garage workbench")
        self.assertEqual(new_task["status"], "pending")
        self.assertEqual(new_task["google_task_id"], "gt_new")
        # Ensure Zero ID tag was patched to Google task
        mock_patch.assert_called_with("list_123", "gt_new", {"notes": "[Zero Task #11 | Priority: P2]"})

    @patch("tools.google_tasks_sync.get_or_create_target_list", return_value="list_123")
    @patch("tools.google_tasks_sync.fetch_all_google_tasks")
    @patch("tools.google_tasks_sync.patch_google_task")
    @patch("tools.google_tasks_sync.load_local_tasks")
    @patch("tools.google_tasks_sync.save_local_tasks")
    @patch("tools.google_tasks_sync.load_sync_state")
    @patch("tools.google_tasks_sync.save_sync_state")
    def test_push_local_completion_to_google(
        self, mock_save_state, mock_load_state, mock_save_local, mock_load_local,
        mock_patch, mock_fetch_google, mock_get_list
    ):
        # Local task was marked completed in Zero
        local_task = {"id": 8, "title": "Ubiquiti UniFi upgrade", "priority": "p2", "status": "completed"}
        mock_load_local.return_value = [local_task]
        mock_load_state.return_value = {
            "list_id": "list_123",
            "mappings": {
                "8": {
                    "google_id": "gt_8",
                    "last_title": "Ubiquiti UniFi upgrade",
                    "last_status": "pending",
                    "last_google_status": "needsAction"
                }
            }
        }
        mock_fetch_google.return_value = [
            {"id": "gt_8", "title": "Ubiquiti UniFi upgrade", "status": "needsAction", "notes": "[Zero Task #8 | Priority: P2]"}
        ]

        res = sync_tasks(quiet=True)
        self.assertTrue(res["ok"])
        mock_patch.assert_called_with("list_123", "gt_8", {"status": "completed"})

    @patch("tools.google_tasks_sync.urllib.request.urlopen")
    @patch("tools.google_tasks_sync.time.sleep")
    def test_api_request_retries_on_timeout(self, mock_sleep, mock_urlopen):
        import socket
        import urllib.request
        from tools.google_tasks_sync import _api_request

        # First attempt raises socket.timeout, second succeeds
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": "ok"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.side_effect = [socket.timeout("The read operation timed out"), mock_resp]

        req = urllib.request.Request("https://tasks.googleapis.com/test")
        result = _api_request(req, timeout=10.0, retries=2, backoff=0.01)

        self.assertEqual(result, b'{"status": "ok"}')
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(0.01)

    @patch("tools.google_tasks_sync.urllib.request.urlopen")
    @patch("tools.google_tasks_sync.time.sleep")
    def test_api_request_exhaustion(self, mock_sleep, mock_urlopen):
        import urllib.error
        import urllib.request
        from tools.google_tasks_sync import _api_request

        mock_urlopen.side_effect = urllib.error.URLError("The read operation timed out")
        req = urllib.request.Request("https://tasks.googleapis.com/test")

        with self.assertRaises(urllib.error.URLError):
            _api_request(req, timeout=10.0, retries=2, backoff=0.01)

        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

if __name__ == "__main__":
    unittest.main()
