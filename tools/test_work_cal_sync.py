#!/usr/bin/env python3
"""Unit tests for tools/work_cal_sync.py and workspace_mcp.calendar_list_events integration."""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.work_cal_sync as wcs
import tools.workspace_mcp as wmcp


class TestWorkCalSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "work_calendar_sync.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_extract_work_cal_json_from_message_plain_html_and_snippet(self):
        payload_dict = {
            "schema": "zero.work_cal_sync.v1",
            "generated_at": "2026-11-03T10:51:33-08:00",
            "timezone": "America/Los_Angeles",
            "window_start": "2026-11-03T00:00:00-08:00",
            "window_end": "2026-11-17T23:59:59-08:00",
            "events": [
                {
                    "uid_hash": "aa01cfd50d77c851",
                    "summary": "Work Meeting",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                }
            ],
        }

        # 1) text/plain base64url part
        b64_plain = base64.urlsafe_b64encode(json.dumps(payload_dict).encode("utf-8")).decode("ascii").rstrip("=")
        msg_plain = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64_plain}},
                ],
            }
        }
        extracted_plain = wcs.extract_work_cal_json_from_message(msg_plain)
        self.assertIsNotNone(extracted_plain)
        self.assertEqual(extracted_plain["schema"], "zero.work_cal_sync.v1")
        self.assertEqual(len(extracted_plain["events"]), 1)

        # 2) text/html with tags and HTML entities
        html_raw = (
            "<html><head><style>.foo { color: red; }</style></head>"
            "<body><div>Sync payload:</div><pre>"
            + json.dumps(payload_dict).replace('"', "&quot;")
            + "</pre></body></html>"
        )
        b64_html = base64.urlsafe_b64encode(html_raw.encode("utf-8")).decode("ascii")
        msg_html = {
            "payload": {
                "mimeType": "text/html",
                "body": {"data": b64_html},
            }
        }
        extracted_html = wcs.extract_work_cal_json_from_message(msg_html)
        self.assertIsNotNone(extracted_html)
        self.assertEqual(extracted_html["events"][0]["uid_hash"], "aa01cfd50d77c851")

        # 3) snippet fallback
        msg_snippet = {
            "payload": {"mimeType": "text/plain", "body": {}},
            "snippet": f"Prefix text {json.dumps(payload_dict)} suffix",
        }
        extracted_snippet = wcs.extract_work_cal_json_from_message(msg_snippet)
        self.assertIsNotNone(extracted_snippet)
        self.assertEqual(extracted_snippet["schema"], "zero.work_cal_sync.v1")

        # 4) non-matching schema returns None
        msg_invalid = {
            "snippet": '{"schema": "other.schema.v1", "events": []}',
        }
        self.assertIsNone(wcs.extract_work_cal_json_from_message(msg_invalid))

    def test_ingest_work_cal_payload_and_window_filtering(self):
        payload = {
            "schema": "zero.work_cal_sync.v1",
            "generated_at": "2026-11-03T10:51:33-08:00",
            "timezone": "America/Los_Angeles",
            "window_start": "2026-11-03T00:00:00-08:00",
            "window_end": "2026-11-09T23:59:59-08:00",
            "events": [
                {
                    "uid_hash": "ev2",
                    "summary": "Afternoon Sync",
                    "start": "2026-11-04T17:05:00-08:00",
                    "end": "2026-11-04T17:45:00-08:00",
                    "location": "Zoom",
                },
                {
                    "uid_hash": "ev1",
                    "summary": "Evening Review",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                },
                # Duplicate uid_hash should be deduplicated
                {
                    "uid_hash": "ev1",
                    "summary": "Evening Review Duplicate",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                },
            ],
        }

        state = wcs.ingest_work_cal_payload(payload, state_path=self.state_path)
        self.assertTrue(self.state_path.exists())
        self.assertEqual(len(state["events"]), 2)
        # Sorted by start time
        self.assertEqual(state["events"][0]["uid_hash"], "ev1")
        self.assertEqual(state["events"][1]["uid_hash"], "ev2")

        # Window filtering: only Nov 3 event overlaps [Nov 3 00:00-08:00, Nov 4 00:00-08:00]
        nov3_events = wcs.get_cached_work_events(
            time_min_iso="2026-11-03T00:00:00-08:00",
            time_max_iso="2026-11-04T00:00:00-08:00",
            state_path=self.state_path,
        )
        self.assertEqual(len(nov3_events), 1)
        self.assertEqual(nov3_events[0]["id"], "work_ev1")
        self.assertEqual(nov3_events[0]["calendar"], "Work (Ryan)")
        self.assertEqual(nov3_events[0]["summary"], "Evening Review")
        self.assertEqual(nov3_events[0]["description"], "Synced via [WORK-CAL-SYNC]")

        # Boundary test using UTC Z timestamps:
        # ev1 is 2026-11-03T20:30:00-08:00 -> 2026-11-04T04:30:00Z to 05:30:00Z
        overlap_utc = wcs.get_cached_work_events(
            time_min_iso="2026-11-04T05:00:00Z",
            time_max_iso="2026-11-04T06:00:00Z",
            state_path=self.state_path,
        )
        self.assertEqual(len(overlap_utc), 1)
        self.assertEqual(overlap_utc[0]["id"], "work_ev1")

        # Non-overlapping window right at ev1 end (ev_end <= t_min)
        after_ev1 = wcs.get_cached_work_events(
            time_min_iso="2026-11-04T05:30:00Z",
            time_max_iso="2026-11-04T12:00:00Z",
            state_path=self.state_path,
        )
        self.assertEqual(len(after_ev1), 0)

    def test_workspace_mcp_calendar_list_events_work_and_all(self):
        payload = {
            "schema": "zero.work_cal_sync.v1",
            "generated_at": "2026-11-03T10:51:33-08:00",
            "timezone": "America/Los_Angeles",
            "window_start": "2026-11-03T00:00:00-08:00",
            "window_end": "2026-11-09T23:59:59-08:00",
            "events": [
                {
                    "uid_hash": "aa01cfd50d77c851",
                    "summary": "Work Meeting",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                }
            ],
        }
        wcs.ingest_work_cal_payload(payload, state_path=self.state_path)

        with patch.object(wcs, "WORK_CAL_STATE_PATH", self.state_path):
            # 1) calendar_id='work' makes zero remote Google Calendar calls and returns cached work events
            with patch("tools.workspace_mcp.urllib.request.urlopen") as mock_urlopen, \
                 patch("tools.workspace_mcp._auth_headers", return_value={"Authorization": "Bearer test"}):
                raw_work = wmcp.calendar_list_events(
                    calendar_id="work",
                    time_min_iso="2026-11-03T00:00:00-08:00",
                    time_max_iso="2026-11-05T00:00:00-08:00",
                )
                mock_urlopen.assert_not_called()
                res_work = json.loads(raw_work)
                self.assertTrue(res_work["ok"])
                self.assertEqual(res_work["count"], 1)
                self.assertEqual(res_work["events"][0]["id"], "work_aa01cfd50d77c851")
                self.assertEqual(res_work["events"][0]["calendar"], "Work (Ryan)")

            # 2) calendar_id='all' merges remote Google Calendar events with cached work events sorted by start time
            remote_response_payload = {
                "items": [
                    {
                        "id": "fam_1",
                        "summary": "Pediatrician Appointment",
                        "start": {"dateTime": "2026-11-03T14:00:00-08:00"},
                        "end": {"dateTime": "2026-11-03T15:00:00-08:00"},
                        "location": "Clinic",
                        "description": "Checkup",
                    }
                ]
            }
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(remote_response_payload).encode("utf-8")
            mock_cm = MagicMock()
            mock_cm.__enter__.return_value = mock_resp
            mock_cm.__exit__.return_value = False

            with patch.dict(wmcp.FAMILY_CALENDARS, {"primary": "Ryan (Primary)"}, clear=True), \
                 patch("tools.workspace_mcp.urllib.request.urlopen", return_value=mock_cm), \
                 patch("tools.workspace_mcp._auth_headers", return_value={"Authorization": "Bearer test"}):
                raw_all = wmcp.calendar_list_events(
                    calendar_id="all",
                    time_min_iso="2026-11-03T00:00:00-08:00",
                    time_max_iso="2026-11-05T00:00:00-08:00",
                )
                res_all = json.loads(raw_all)
                self.assertTrue(res_all["ok"])
                self.assertEqual(res_all["count"], 2)
                self.assertEqual(res_all["events"][0]["id"], "fam_1")
                self.assertEqual(res_all["events"][1]["id"], "work_aa01cfd50d77c851")
                self.assertEqual(res_all["events"][1]["calendar"], "Work (Ryan)")

    def test_archive_work_cal_message_removes_inbox_unread_important(self):
        captured_requests = []

        def fake_urlopen(req, timeout=15):
            captured_requests.append(req)
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"{}"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with patch("tools.work_cal_sync.urllib.request.urlopen", side_effect=fake_urlopen):
            ok = wcs.archive_work_cal_message("msg_123", tok="test_token", thread_id="thr_456")
            self.assertTrue(ok)
            self.assertEqual(len(captured_requests), 2)

            msg_req, thr_req = captured_requests
            self.assertEqual(msg_req.full_url, f"{wcs.GMAIL_BASE}/messages/msg_123/modify")
            self.assertEqual(msg_req.get_method(), "POST")
            self.assertEqual(msg_req.get_header("Authorization"), "Bearer test_token")
            msg_body = json.loads(msg_req.data.decode("utf-8"))
            self.assertEqual(msg_body, {"removeLabelIds": ["INBOX", "UNREAD", "IMPORTANT"]})

            self.assertEqual(thr_req.full_url, f"{wcs.GMAIL_BASE}/threads/thr_456/modify")
            self.assertEqual(thr_req.get_method(), "POST")
            thr_body = json.loads(thr_req.data.decode("utf-8"))
            self.assertEqual(thr_body, {"removeLabelIds": ["INBOX", "UNREAD", "IMPORTANT"]})

    def test_sync_latest_from_gmail_ingests_newest_and_archives_all_matches(self):
        payload = {
            "schema": "zero.work_cal_sync.v1",
            "generated_at": "2026-11-03T10:51:33-08:00",
            "timezone": "America/Los_Angeles",
            "window_start": "2026-11-03T00:00:00-08:00",
            "window_end": "2026-11-17T23:59:59-08:00",
            "events": [
                {
                    "uid_hash": "ev_latest",
                    "summary": "Newest Sync Event",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                }
            ],
        }
        b64_plain = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        full_msg_1 = {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": b64_plain},
            },
        }
        listing_data = {
            "messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ]
        }

        captured_urls = []

        def fake_urlopen(req, timeout=15):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            captured_urls.append((req.get_method(), url))
            resp = MagicMock()
            resp.status = 200
            if "/messages?q=" in url:
                resp.read.return_value = json.dumps(listing_data).encode("utf-8")
            elif "/messages/m1?format=full" in url:
                resp.read.return_value = json.dumps(full_msg_1).encode("utf-8")
            else:
                resp.read.return_value = b"{}"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with patch("tools.zero_mail_listener.get_access_token", return_value="tok_abc"), \
             patch("tools.work_cal_sync.urllib.request.urlopen", side_effect=fake_urlopen):
            state = wcs.sync_latest_from_gmail(state_path=self.state_path)
            self.assertIsNotNone(state)
            self.assertEqual(len(state["events"]), 1)
            self.assertEqual(state["events"][0]["uid_hash"], "ev_latest")

            # Verify maxResults=20 in listing query and both m1 and m2 (and t1, t2) were modified
            self.assertTrue(any("maxResults=20" in u for _, u in captured_urls))
            self.assertIn(("POST", f"{wcs.GMAIL_BASE}/messages/m1/modify"), captured_urls)
            self.assertIn(("POST", f"{wcs.GMAIL_BASE}/threads/t1/modify"), captured_urls)
            self.assertIn(("POST", f"{wcs.GMAIL_BASE}/messages/m2/modify"), captured_urls)
            self.assertIn(("POST", f"{wcs.GMAIL_BASE}/threads/t2/modify"), captured_urls)


if __name__ == "__main__":
    unittest.main()
