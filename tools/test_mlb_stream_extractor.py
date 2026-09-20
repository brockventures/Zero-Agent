import unittest
from unittest.mock import patch, MagicMock
import json
import sys
sys.path.append("/workspace")

from tools.mlb_stream_extractor import resolve_team_game, resolve_media_event

class TestMLBStreamExtractor(unittest.TestCase):
    def setUp(self):
        self.mock_data = {
            "teams": [
                {"id": 53, "name": "Chicago Cubs"},
                {"id": 48, "name": "Milwaukee Brewers"}
            ],
            "games": [
                {
                    "id": 6683,
                    "away_team_id": 53,
                    "home_team_id": 48,
                    "status": "L",
                    "scores": {"away": 3, "home": 1}
                }
            ],
            "media_events": [
                {
                    "id": 56958,
                    "game_id": 6683,
                    "title": "Away",
                    "description": "Cubs Broadcast",
                    "status": "L"
                },
                {
                    "id": 56976,
                    "game_id": 6683,
                    "title": "Home",
                    "description": "Brewers Broadcast",
                    "status": "L"
                }
            ],
            "flavors": [
                {
                    "id": "free.live.espn",
                    "media_event_ids": [56958, 56976],
                    "name": "ESPN Live Free"
                }
            ]
        }

    def test_resolve_team_game(self):
        game, teams, tid = resolve_team_game(self.mock_data, "cubs")
        self.assertEqual(tid, 53)
        self.assertEqual(game["id"], 6683)
        self.assertEqual(game["status"], "L")

    def test_resolve_media_event_prefers_team_feed(self):
        game, teams, tid = resolve_team_game(self.mock_data, "cubs")
        event = resolve_media_event(self.mock_data, game, tid)
        # Cubs are away, so Away feed should be selected
        self.assertEqual(event["id"], 56958)
        self.assertEqual(event["title"], "Away")

    def test_resolve_media_event_home_team_feed(self):
        game, teams, tid = resolve_team_game(self.mock_data, "brewers")
        event = resolve_media_event(self.mock_data, game, tid)
        # Brewers are home, so Home feed should be selected
        self.assertEqual(event["id"], 56976)
        self.assertEqual(event["title"], "Home")

    def test_resolve_media_event_feed_override(self):
        game, teams, tid = resolve_team_game(self.mock_data, "cubs")
        # Cubs are away, but user explicitly requests home broadcast (Brewers feed)
        event = resolve_media_event(self.mock_data, game, tid, feed_override="home")
        self.assertEqual(event["id"], 56976)
        self.assertEqual(event["title"], "Home")

    def test_invalid_team_raises(self):
        with self.assertRaises(ValueError):
            resolve_team_game(self.mock_data, "nonexistent_team")

    def _make_mock_resp(self, status=200, data=None):
        m = MagicMock()
        m.__enter__.return_value = m
        m.status = status
        if data is not None:
            m.read.return_value = json.dumps(data).encode("utf-8")
        return m

    @patch("urllib.request.urlopen")
    @patch("time.sleep")
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data='{"url": "http://ha:8123", "token": "abc"}')
    def test_cast_to_home_assistant_cold_start(self, mock_file, mock_sleep, mock_urlopen):
        from tools.mlb_stream_extractor import cast_to_home_assistant

        # Cold responses:
        # 1. _get_state(remote) -> off
        # 2. _get_state(media_player.living_room_tv_2) -> off
        # 3. turn_on remote -> 200
        # 4. _get_state(media_player.living_room_tv) -> off
        # 5. play_media -> 200
        # 6. _get_state(media_player.living_room_tv_2) -> attributes.app_id = org.videolan.vlc
        m1 = self._make_mock_resp(200, {"state": "off"})
        m2 = self._make_mock_resp(200, {"state": "off"})
        m3 = self._make_mock_resp(200)
        m4 = self._make_mock_resp(200, {"state": "off"})
        m5 = self._make_mock_resp(200)
        m6 = self._make_mock_resp(200, {"state": "on", "attributes": {"app_id": "org.videolan.vlc"}})

        mock_urlopen.side_effect = [m1, m2, m3, m4, m5, m6]

        res = cast_to_home_assistant("https://stream/master.m3u8", cold_start_delay=4.0)
        self.assertTrue(res)
        mock_sleep.assert_any_call(4.0)

    @patch("urllib.request.urlopen")
    @patch("time.sleep")
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data='{"url": "http://ha:8123", "token": "abc"}')
    def test_cast_to_home_assistant_warm_start(self, mock_file, mock_sleep, mock_urlopen):
        from tools.mlb_stream_extractor import cast_to_home_assistant

        # Warm responses:
        # 1. _get_state(remote) -> on
        # 2. _get_state(media_player.living_room_tv_2) -> on
        # 3. turn_on remote -> 200
        # 4. _get_state(media_player.living_room_tv) -> off
        # 5. play_media -> 200
        m1 = self._make_mock_resp(200, {"state": "on"})
        m2 = self._make_mock_resp(200, {"state": "on"})
        m3 = self._make_mock_resp(200)
        m4 = self._make_mock_resp(200, {"state": "off"})
        m5 = self._make_mock_resp(200)

        mock_urlopen.side_effect = [m1, m2, m3, m4, m5]

        res = cast_to_home_assistant("https://stream/master.m3u8", cold_start_delay=4.0)
        self.assertTrue(res)
        self.assertNotIn(unittest.mock.call(4.0), mock_sleep.call_args_list)

if __name__ == "__main__":
    unittest.main()
