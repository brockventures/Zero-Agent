import unittest
from unittest.mock import patch, MagicMock
import json
import sys
sys.path.append("/workspace")

from tools.cubs_notifier import format_game_notification, check_cubs_game

class TestCubsNotifier(unittest.TestCase):
    def setUp(self):
        self.mock_game = {
            "gamePk": 823739,
            "gameDate": "2026-09-09T23:40:00Z",
            "status": {
                "abstractGameState": "Preview",
                "detailedState": "Scheduled"
            },
            "teams": {
                "away": {
                    "team": {"id": 112, "name": "Chicago Cubs"},
                    "leagueRecord": {"wins": 81, "losses": 65}
                },
                "home": {
                    "team": {"id": 158, "name": "Milwaukee Brewers"},
                    "leagueRecord": {"wins": 90, "losses": 56}
                }
            },
            "venue": {"name": "American Family Field"},
            "broadcasts": [
                {
                    "name": "Marquee Sports Network",
                    "type": "TV",
                    "homeAway": "away"
                },
                {
                    "name": "Brewers.TV",
                    "type": "TV",
                    "homeAway": "home"
                }
            ]
        }

    def test_format_game_notification(self):
        msg = format_game_notification(self.mock_game, minutes_left=10)
        self.assertIn("Chicago Cubs Game Alert", msg)
        self.assertIn("**Chicago Cubs** (81-65) @ **Milwaukee Brewers** (90-56)", msg)
        self.assertIn("4:40 PM PT", msg)
        self.assertIn("American Family Field", msg)
        self.assertIn("Marquee Sports Network (Away)", msg)
        self.assertIn("[CHOICES: Cast Cubs on TV | Dismiss]", msg)

    @patch("tools.cubs_notifier.fetch_cubs_game")
    @patch("tools.cubs_notifier.load_state")
    @patch("tools.cubs_notifier.save_state")
    def test_check_cubs_game_no_game(self, mock_save, mock_load, mock_fetch):
        mock_fetch.return_value = []
        ok, rep, meta = check_cubs_game()
        self.assertTrue(ok)
        self.assertEqual(rep, "")
        self.assertEqual(meta.get("status"), "no_game_today")

if __name__ == "__main__":
    unittest.main()
