#!/usr/bin/env python3
"""Unit tests for tools/plex_play.py and batch_guard.py main() turn reset."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tools")
sys.path.insert(0, "/workspace/.agents/hooks")

import plex_play
import batch_guard


class TestPlexPlayAndTurnReset(unittest.TestCase):
    def test_build_plex_android_deep_link(self):
        uri = plex_play.build_plex_android_deep_link("1767", "server-abc-123")
        self.assertEqual(
            uri,
            "plex://play/?metadataKey=%2Flibrary%2Fmetadata%2F1767&server=server-abc-123",
        )

    @patch("plex_play.requests.get")
    def test_resolve_plex_media_movie(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "MediaContainer": {
                "Hub": [
                    {
                        "type": "movie",
                        "Metadata": [
                            {
                                "ratingKey": "1767",
                                "key": "/library/metadata/1767",
                                "type": "movie",
                                "title": "The Emperor's New Groove",
                                "year": 2000,
                                "librarySectionTitle": "Movies",
                            }
                        ],
                    }
                ]
            }
        }
        mock_get.return_value = mock_resp

        res = plex_play.resolve_plex_media(
            "Emperor's New Groove",
            plex_url="http://198.51.100.10:32400",
            plex_token="dummy-tok",
        )
        self.assertEqual(res["ratingKey"], "1767")
        self.assertEqual(res["title"], "The Emperor's New Groove")
        self.assertEqual(res["type"], "movie")

    def test_batch_guard_main_does_not_pre_overwrite_turn_anchor_before_check_tool_use(self):
        # Verify that main() leaves fresh["agy_ppid"] empty on turn_anchor change so check_tool_use()
        # sees a clean turn transition and resets turn_start_time.
        src = Path("/workspace/.agents/hooks/batch_guard.py").read_text(encoding="utf-8")
        main_body = src.split("def main():", 1)[1]
        before_check = main_body.split("check_tool_use(", 1)[0]
        self.assertNotIn('fresh["agy_ppid"] = turn_anchor', before_check)


if __name__ == "__main__":
    unittest.main()
