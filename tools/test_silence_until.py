import sys
sys.path.insert(0, "/workspace")

import unittest
from datetime import datetime

from tools.bridge_state import PT_TZ, parse_silence_until, is_crab_cavern_silenced

NOW = datetime(2026, 10, 12, 14, 30, tzinfo=PT_TZ)


class TestParseSilenceUntil(unittest.TestCase):
    def test_default_is_next_8pm(self):
        self.assertEqual(parse_silence_until("", NOW).isoformat(), "2026-10-12T20:00:00-07:00")
        late = datetime(2026, 10, 12, 21, 0, tzinfo=PT_TZ)
        self.assertEqual(parse_silence_until(None, late).isoformat(), "2026-10-13T20:00:00-07:00")

    def test_tomorrow_variants(self):
        self.assertEqual(parse_silence_until("tomorrow 8pm", NOW).isoformat(), "2026-10-13T20:00:00-07:00")
        self.assertEqual(parse_silence_until("until tomorrow", NOW).isoformat(), "2026-10-13T20:00:00-07:00")
        self.assertEqual(parse_silence_until("tomorrow at 7:30am", NOW).isoformat(), "2026-10-13T07:30:00-07:00")

    def test_hours_and_iso(self):
        self.assertEqual(parse_silence_until("6h", NOW).isoformat(), "2026-10-12T20:30:00-07:00")
        self.assertEqual(parse_silence_until("2026-10-14", NOW).isoformat(), "2026-10-14T20:00:00-07:00")
        self.assertEqual(parse_silence_until("2026-10-14 09:15", NOW).isoformat(), "2026-10-14T09:15:00-07:00")

    def test_result_is_never_hardcoded_past(self):
        self.assertGreater(parse_silence_until("", NOW), NOW)

    def test_silence_window_honored(self):
        future = {"crab_cavern_silenced_until": "2999-01-01T00:00:00-07:00"}
        past = {"crab_cavern_silenced_until": "2026-10-06T20:00:00-07:00"}
        self.assertTrue(is_crab_cavern_silenced(future)[0])
        self.assertFalse(is_crab_cavern_silenced(past)[0])


if __name__ == "__main__":
    unittest.main()
