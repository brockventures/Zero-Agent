#!/usr/bin/env python3
"""Unit tests for tools/hearth_meals.py using RFC 5737 mock data only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

for _p in [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools import hearth_meals


class TestSynthesizeDynamicMeals(unittest.TestCase):
    """Test strict priority (1) Cooked -> (2) Override -> (3) Family GCal -> (4) Auto-Leftovers."""

    def test_default_1_day_auto_leftovers(self):
        mealie_events = [
            {
                "id": "m1",
                "date": "2026-11-24",
                "recipe": {
                    "id": "rec_tikka",
                    "name": "Chicken Tikka Masala",
                    "image": "tikka.webp",
                    "description": "Creamy spiced curry",
                },
            }
        ]
        meals = hearth_meals.synthesize_dynamic_meals(mealie_events, [], {})
        self.assertEqual(sorted(meals.keys()), ["2026-11-24", "2026-11-25"])

        cooked = meals["2026-11-24"]
        self.assertEqual(cooked["kind"], "cooked")
        self.assertEqual(cooked["title"], "Chicken Tikka Masala")
        self.assertEqual(cooked["location"], "/hearth-api/mealie/image/rec_tikka")

        leftover = meals["2026-11-25"]
        self.assertEqual(leftover["kind"], "leftovers")
        self.assertEqual(leftover["source"], "auto_leftovers")
        self.assertEqual(leftover["source_date"], "2026-11-24")
        self.assertEqual(leftover["leftover_day"], 1)
        self.assertEqual(leftover["title"], "Leftovers: Chicken Tikka Masala")
        self.assertEqual(leftover["location"], "/hearth-api/mealie/image/art_leftovers")

    def test_thanksgiving_2_day_leftovers_exception(self):
        mealie_events = [
            {
                "id": "tg1",
                "date": "2026-11-26",
                "recipe": {
                    "id": "rec_turkey",
                    "name": "Thanksgiving Roast Turkey",
                    "image": "turkey.webp",
                },
            }
        ]
        overrides_cfg = {
            "overrides": {},
            "leftover_days": {"2026-11-26": 2},
        }
        meals = hearth_meals.synthesize_dynamic_meals(mealie_events, [], overrides_cfg)
        self.assertEqual(sorted(meals.keys()), ["2026-11-26", "2026-11-27", "2026-11-28"])

        self.assertEqual(meals["2026-11-26"]["kind"], "cooked")
        self.assertEqual(meals["2026-11-27"]["kind"], "leftovers")
        self.assertEqual(meals["2026-11-27"]["leftover_day"], 1)
        self.assertEqual(meals["2026-11-27"]["title"], "Leftovers: Thanksgiving Roast Turkey")
        self.assertEqual(meals["2026-11-27"]["location"], "/hearth-api/mealie/image/art_leftovers")

        self.assertEqual(meals["2026-11-28"]["kind"], "leftovers")
        self.assertEqual(meals["2026-11-28"]["leftover_day"], 2)
        self.assertEqual(meals["2026-11-28"]["title"], "Leftovers: Thanksgiving Roast Turkey")
        self.assertEqual(meals["2026-11-28"]["location"], "/hearth-api/mealie/image/art_leftovers")

    def test_monday_delivery_override_preempts_sunday_auto_leftovers(self):
        mealie_events = [
            {
                "id": "sun1",
                "date": "2026-11-22",
                "recipe": {"id": "rec_roast", "name": "Sunday Pot Roast", "image": "roast.webp"},
            }
        ]
        overrides_cfg = {
            "overrides": {
                "2026-11-23": {
                    "kind": "delivery",
                    "title": "Delivery Night",
                    "notes": "Ordering in tonight",
                }
            },
            "leftover_days": {},
        }
        meals = hearth_meals.synthesize_dynamic_meals(mealie_events, [], overrides_cfg)
        self.assertEqual(sorted(meals.keys()), ["2026-11-22", "2026-11-23"])
        self.assertEqual(meals["2026-11-22"]["kind"], "cooked")

        mon = meals["2026-11-23"]
        self.assertEqual(mon["kind"], "delivery")
        self.assertEqual(mon["source"], "override")
        self.assertEqual(mon["title"], "Delivery Night")
        self.assertEqual(mon["description"], "Ordering in tonight")
        self.assertEqual(mon["location"], "/hearth-api/mealie/image/art_delivery")

    def test_priority_hierarchy_cooked_beats_override_beats_family_beats_leftovers(self):
        # 2026-11-20: Cooked Mealie dinner AND override AND family event -> Cooked wins (Priority 1)
        # 2026-11-21: Override AND family event AND auto-leftover from Nov 20 -> Override wins (Priority 2)
        # 2026-11-22: Cooked Mealie dinner
        # 2026-11-23: Family dinner event AND auto-leftover from Nov 22 -> Family event wins (Priority 3)
        mealie_events = [
            {"id": "c1", "date": "2026-11-20", "recipe": {"id": "r1", "name": "Beef Bourguignon"}},
            {"id": "c2", "date": "2026-11-22", "recipe": {"id": "r2", "name": "Lasagna Bolognese"}},
        ]
        overrides_cfg = {
            "overrides": {
                "2026-11-20": {"kind": "delivery", "title": "Override Ignored By Cooked"},
                "2026-11-21": {"kind": "delivery", "title": "Thai Delivery", "notes": "Pad Thai"},
            }
        }
        family_events = [
            {
                "id": "g1",
                "summary": "Steakhouse Reservation",
                "start": {"dateTime": "2026-11-20T18:00:00-08:00"},
            },
            {
                "id": "g2",
                "summary": "Sushi Bistro Night",
                "start": {"dateTime": "2026-11-21T19:00:00-08:00"},
            },
            {
                "id": "g3",
                "summary": "Italian Trattoria Dinner",
                "start": {"dateTime": "2026-11-23T18:30:00-08:00"},
                "location": "Downtown",
            },
        ]

        meals = hearth_meals.synthesize_dynamic_meals(mealie_events, family_events, overrides_cfg)
        self.assertEqual(meals["2026-11-20"]["kind"], "cooked")
        self.assertEqual(meals["2026-11-20"]["title"], "Beef Bourguignon")

        self.assertEqual(meals["2026-11-21"]["kind"], "delivery")
        self.assertEqual(meals["2026-11-21"]["source"], "override")
        self.assertEqual(meals["2026-11-21"]["title"], "Thai Delivery")

        self.assertEqual(meals["2026-11-22"]["kind"], "cooked")
        self.assertEqual(meals["2026-11-22"]["title"], "Lasagna Bolognese")

        self.assertEqual(meals["2026-11-23"]["kind"], "dining_out")
        self.assertEqual(meals["2026-11-23"]["source"], "family_calendar")
        self.assertEqual(meals["2026-11-23"]["title"], "Italian Trattoria Dinner")
        self.assertEqual(meals["2026-11-23"]["location"], "/hearth-api/mealie/image/art_dining_out")

    def test_family_calendar_filtering_rules(self):
        family_events = [
            # 1. Mealie sync mirror starting with 'Dinner: ' -> MUST be excluded
            {
                "id": "mirror1",
                "summary": "Dinner: Roast Chicken",
                "start": {"dateTime": "2026-11-24T18:00:00-08:00"},
            },
            # 2. All-day event with dinner keyword -> MUST be excluded
            {
                "id": "allday1",
                "summary": "Pizza Night Fundraiser",
                "start": {"date": "2026-11-25"},
            },
            # 3. Routine kids pickup/dropoff/bedtime/nap in evening window -> MUST be excluded
            {
                "id": "kids1",
                "summary": "Isaac pickup & dinner prep",
                "start": {"dateTime": "2026-11-26T17:00:00-08:00"},
            },
            {
                "id": "kids2",
                "summary": "Eve bedtime routine",
                "start": {"dateTime": "2026-11-27T19:30:00-08:00"},
            },
            # 4. Non-dinner evening event -> MUST be excluded
            {
                "id": "nondinner1",
                "summary": "Public Library Board Meeting",
                "start": {"dateTime": "2026-11-28T18:00:00-08:00"},
            },
            # 5. Lunch event outside 16:30-21:00 PT -> MUST be excluded
            {
                "id": "lunch1",
                "summary": "Tacos & Ramen Lunch",
                "start": {"dateTime": "2026-11-29T12:30:00-08:00"},
            },
            # 6. Cancelled dinner event -> MUST be excluded
            {
                "id": "canc1",
                "status": "cancelled",
                "summary": "Sushi Reservation",
                "start": {"dateTime": "2026-11-29T18:00:00-08:00"},
            },
            # 7. Valid evening dining-out event at 17:45 PT -> MUST match
            {
                "id": "valid1",
                "summary": "Ramen & Sushi with Friends",
                "start": {"dateTime": "2026-11-30T17:45:00-08:00"},
            },
        ]

        meals = hearth_meals.synthesize_dynamic_meals([], family_events, {})
        self.assertEqual(list(meals.keys()), ["2026-11-30"])
        self.assertEqual(meals["2026-11-30"]["kind"], "dining_out")
        self.assertEqual(meals["2026-11-30"]["title"], "Ramen & Sushi with Friends")
        self.assertEqual(meals["2026-11-30"]["location"], "/hearth-api/mealie/image/art_dining_out")

    def test_skip_override_suppresses_auto_leftovers_and_family_events(self):
        mealie_events = [
            {"id": "m1", "date": "2026-11-24", "recipe": {"id": "r1", "name": " Roast Pork"}}
        ]
        family_events = [
            {
                "id": "f1",
                "summary": "Pizza Tavern Night",
                "start": {"dateTime": "2026-11-25T18:00:00-08:00"},
            }
        ]
        overrides_cfg = {
            "overrides": {
                "2026-11-25": {"kind": "skip"},
            }
        }
        meals = hearth_meals.synthesize_dynamic_meals(mealie_events, family_events, overrides_cfg)
        self.assertEqual(list(meals.keys()), ["2026-11-24"])
        self.assertNotIn("2026-11-25", meals)


class TestCLIRemoteOperations(unittest.TestCase):
    """Test CLI commands interacting with Host 2 via mocked nas_config.run_ssh."""

    def setUp(self):
        self.remote_state: dict[str, str] = {}

        def fake_run_ssh(host, remote_cmd, timeout=120, **kwargs):
            # Simulate reading/writing meal_overrides.json and uploading art in memory
            if "if [ -f " in remote_cmd:
                content = self.remote_state.get("meal_overrides.json", "{}")
                return 0, content, ""
            if "base64 -d >" in remote_cmd and "meal_overrides.json" in remote_cmd:
                import base64 as b64
                import re as _re

                m = _re.search(r"echo\s+'?([A-Za-z0-9+/=]+)'?\s+\|\s+base64\s+-d", remote_cmd)
                if m:
                    decoded = b64.b64decode(m.group(1)).decode("utf-8")
                    self.remote_state["meal_overrides.json"] = decoded
                return 0, "", ""
            if "stat -c '%s'" in remote_cmd:
                stdin_data = kwargs.get("stdin_data") or ""
                return 0, str(len(stdin_data)), ""
            return 0, "", ""

        self.ssh_patcher = patch.object(hearth_meals.nas_config, "run_ssh", side_effect=fake_run_ssh)
        self.mock_ssh = self.ssh_patcher.start()

    def tearDown(self):
        self.ssh_patcher.stop()

    def test_remote_paths_use_host2_docker_root(self):
        with (
            patch.object(hearth_meals.nas_config, "DOCKER_ROOT", "/srv/test-h1-docker"),
            patch.object(hearth_meals.nas_config, "HOST_2_DOCKER_ROOT", "/srv/test-h2-docker"),
        ):
            self.assertEqual(
                hearth_meals.get_remote_ical_bridge_dir(),
                "/srv/test-h2-docker/hearth/deploy/ical-bridge",
            )
            self.assertEqual(
                hearth_meals.get_remote_overrides_path(),
                "/srv/test-h2-docker/hearth/deploy/ical-bridge/meal_overrides.json",
            )
            self.assertEqual(
                hearth_meals.get_remote_meal_art_dir(),
                "/srv/test-h2-docker/hearth/deploy/ical-bridge/meal_art",
            )

    def test_cli_set_leftover_days_list_and_remove(self):
        # 1. Seed Monday Nov 23 Delivery
        rc = hearth_meals.main(
            [
                "set",
                "--date",
                "2026-11-23",
                "--kind",
                "delivery",
                "--title",
                "Delivery Night",
                "--notes",
                "Ordering in tonight",
            ]
        )
        self.assertEqual(rc, 0)

        # 2. Seed Thursday Nov 26 2-day Thanksgiving leftovers
        rc = hearth_meals.main(
            [
                "set-leftover-days",
                "--date",
                "2026-11-26",
                "--days",
                "2",
            ]
        )
        self.assertEqual(rc, 0)

        saved = json.loads(self.remote_state["meal_overrides.json"])
        self.assertEqual(
            saved["overrides"]["2026-11-23"],
            {"kind": "delivery", "title": "Delivery Night", "notes": "Ordering in tonight"},
        )
        self.assertEqual(saved["leftover_days"]["2026-11-26"], 2)

        # 3. Remove 2026-11-23
        rc = hearth_meals.main(["remove", "--date", "2026-11-23"])
        self.assertEqual(rc, 0)
        saved_after = json.loads(self.remote_state["meal_overrides.json"])
        self.assertNotIn("2026-11-23", saved_after["overrides"])
        self.assertEqual(saved_after["leftover_days"]["2026-11-26"], 2)

    def test_generate_art_dry_run_and_mocked_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res_dry = hearth_meals.generate_meal_art(output_dir=tmpdir, dry_run=True)
            self.assertTrue(res_dry["success"])
            self.assertEqual(set(res_dry["cards"].keys()), {"leftovers", "delivery", "dining_out"})

            def fake_gen_sub(prompt, aspect_ratio, image_name, output_dir, **kwargs):
                p = Path(output_dir) / f"{image_name}.jpg"
                p.write_bytes(b"\xff\xd8\xff\xe0MOCKJPEG")
                return {"success": True, "path": str(p), "model": "subscription:test"}

            def fake_check_img(path):
                return {"valid": True, "width": 1280, "height": 720, "size_kb": 120, "format": "JPEG"}

            with (
                patch.object(hearth_meals, "generate_image_subscription", side_effect=fake_gen_sub),
                patch.object(hearth_meals, "check_image", side_effect=fake_check_img),
            ):
                res = hearth_meals.generate_meal_art(output_dir=tmpdir, force=True)
                self.assertTrue(res["success"])
                for kind in ("leftovers", "delivery", "dining_out"):
                    self.assertTrue(res["cards"][kind]["valid"])
                    self.assertTrue(res["cards"][kind]["deployed"])


if __name__ == "__main__":
    unittest.main()
