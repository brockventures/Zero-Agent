import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
from tools.meal_planner_proposal import (
    is_dinner_recipe,
    filter_eligible_recipes,
    generate_proposal,
    swap_slot,
    format_proposal_markdown,
    build_three_panel_menu_image,
)
from tools.bridge_handlers import handle_button_choice


class TestMealPlannerDinnerFilter(unittest.TestCase):
    def test_excludes_guacamole_and_dips(self):
        guac = {
            "name": "Classic Guacamole Recipe",
            "tags": ["Gluten-Free", "Quick", "Dips & Apps", "Serious Eats"]
        }
        self.assertFalse(is_dinner_recipe(guac))

    def test_excludes_side_dishes(self):
        fries = {
            "name": "Perfect Thin and Crispy French Fries Recipe",
            "tags": ["Gluten-Free", "Kenji", "Sides", "Serious Eats"]
        }
        potatoes = {
            "name": "The Best Crispy Roast Potatoes Ever",
            "tags": ["Gluten-Free", "Kenji", "Sides", "Serious Eats"]
        }
        self.assertFalse(is_dinner_recipe(fries))
        self.assertFalse(is_dinner_recipe(potatoes))

    def test_excludes_desserts_and_baking(self):
        pancake = {
            "name": "Apple Dessert Pancake",
            "tags": ["Comfort Food", "Classics", "Desserts", "Kid-Friendly", "Baking"]
        }
        self.assertFalse(is_dinner_recipe(pancake))

    def test_excludes_breakfast(self):
        oats = {
            "name": "Snickers Overnight Oats",
            "tags": ["Snickers Overnight Oats"],
            "categories": ["Breakfast"]
        }
        self.assertFalse(is_dinner_recipe(oats))

    def test_excludes_standalone_sauces(self):
        sauce1 = {
            "name": "Marcella Hazan’s Tomato Sauce",
            "tags": ["Gluten-Free", "Pasta & Sauces", "Classics", "NYT Cooking"]
        }
        sauce2 = {
            "name": "The Best Italian-American Tomato Sauce",
            "tags": ["Gluten-Free", "Pasta & Sauces", "Kenji", "Serious Eats"]
        }
        self.assertFalse(is_dinner_recipe(sauce1))
        self.assertFalse(is_dinner_recipe(sauce2))

    def test_preserves_dinners_with_sauce_or_salsa_in_title(self):
        tacos = {
            "name": "No-Waste Tacos de Carnitas With Salsa Verde",
            "tags": ["Gluten-Free", "Kenji", "Mexican", "Low & Slow", "Serious Eats"]
        }
        chicken = {
            "name": "Peruvian-Style Grilled Chicken With Green Sauce Recipe",
            "tags": ["Gluten-Free", "Kenji", "Serious Eats", "Grill", "Chicken"]
        }
        carbonara = {
            "name": "Spaghetti With Carbonara Sauce Recipe",
            "tags": ["GF-Adaptable", "Pasta & Sauces", "Classics", "Kenji", "Serious Eats"]
        }
        meatballs = {
            "name": "Soft Ricotta Meatballs in Gentle Red Sauce",
            "tags": ["Comfort Food", "GF-Adaptable", "Pasta & Sauces", "Kid-Friendly", "Melissa Clark", "NYT Cooking"]
        }
        bolognese = {
            "name": "The Best Slow-Cooked Bolognese Sauce Recipe",
            "tags": ["GF-Adaptable", "Pasta & Sauces", "Kenji", "Weekend Project", "Serious Eats"]
        }
        self.assertTrue(is_dinner_recipe(tacos))
        self.assertTrue(is_dinner_recipe(chicken))
        self.assertTrue(is_dinner_recipe(carbonara))
        self.assertTrue(is_dinner_recipe(meatballs))
        self.assertTrue(is_dinner_recipe(bolognese))

    def test_slot_pools_never_include_non_dinners(self):
        sample_recipes = [
            {"name": "Classic Guacamole Recipe", "slug": "guac", "tags": ["Gluten-Free", "Quick", "Dips & Apps"]},
            {"name": "Apple Dessert Pancake", "slug": "pancake", "tags": ["Gluten-Free", "Comfort Food", "Desserts"]},
            {"name": "Quick Smash Burger", "slug": "burger", "tags": ["Gluten-Free", "Quick", "Weeknight"]},
            {"name": "Slow Beef Stew", "slug": "stew", "tags": ["Gluten-Free", "Comfort Food", "Low & Slow"]},
            {"name": "Kid Chicken Tenders", "slug": "chicken", "tags": ["Gluten-Free", "Kid-Friendly", "Chicken"]},
        ]
        tue = filter_eligible_recipes(sample_recipes, set(), "tuesday")
        sun = filter_eligible_recipes(sample_recipes, set(), "sunday")
        thu = filter_eligible_recipes(sample_recipes, set(), "thursday")

        self.assertEqual([r["slug"] for r in tue], ["burger"])
        self.assertEqual([r["slug"] for r in sun], ["stew"])
        self.assertEqual(set(r["slug"] for r in thu), {"burger", "chicken"})


class TestMealPlanGenerationAndSwap(unittest.TestCase):
    def setUp(self):
        import tempfile
        import os
        self.test_dir = tempfile.mkdtemp()
        self.temp_plan = os.path.join(self.test_dir, "test_active_plan.json")
        self.patcher = patch("tools.meal_planner_proposal.ACTIVE_PLAN_FILE", self.temp_plan)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_proposal_structure(self):
        plan = generate_proposal(force=False)
        self.assertIn("meals", plan)
        self.assertEqual(set(plan["meals"].keys()), {"sunday", "tuesday", "thursday"})
        for slot in ("sunday", "tuesday", "thursday"):
            recipe = plan["meals"][slot]["recipe"]
            self.assertTrue(is_dinner_recipe(recipe))

    def test_proposal_reused_when_valid(self):
        plan1 = generate_proposal(force=False)
        plan2 = generate_proposal(force=False)
        self.assertEqual(
            plan1["meals"]["sunday"]["recipe"]["slug"],
            plan2["meals"]["sunday"]["recipe"]["slug"]
        )
        self.assertEqual(
            plan1["meals"]["tuesday"]["recipe"]["slug"],
            plan2["meals"]["tuesday"]["recipe"]["slug"]
        )

    def test_swap_slot_modifies_only_target_slot(self):
        plan_before = generate_proposal(force=False)
        sun_before = plan_before["meals"]["sunday"]["recipe"]["slug"]
        thu_before = plan_before["meals"]["thursday"]["recipe"]["slug"]

        ok, markdown = swap_slot("tuesday")
        self.assertTrue(ok)
        self.assertIn("CHOICES:", markdown)

        plan_after = generate_proposal(force=False)
        self.assertEqual(plan_after["meals"]["sunday"]["recipe"]["slug"], sun_before)
        self.assertEqual(plan_after["meals"]["thursday"]["recipe"]["slug"], thu_before)
        self.assertTrue(is_dinner_recipe(plan_after["meals"]["tuesday"]["recipe"]))

    def test_default_stage_false_behavior(self):
        plan = generate_proposal(force=True)
        self.assertFalse(plan.get("decision_staged", False))

        with patch("tools.meal_planner_proposal.stage_meal_proposal_decision", return_value={"id": "mock_dec"}) as mock_stage:
            plan_staged = generate_proposal(force=True, stage=True)
            self.assertTrue(plan_staged.get("decision_staged", False))
            mock_stage.assert_called_once()

    def test_plan_and_swap_populate_image_path(self):
        plan = generate_proposal(force=False)
        self.assertIn("image_path", plan)
        self.assertTrue(os.path.exists(plan["image_path"]))

        ok, _ = swap_slot("thursday")
        self.assertTrue(ok)
        plan_after = generate_proposal(force=False)
        self.assertIn("image_path", plan_after)
        self.assertTrue(os.path.exists(plan_after["image_path"]))

    def test_build_three_panel_menu_image_creates_valid_collage(self):
        from PIL import Image
        plan = {
            "meals": {
                "tuesday": {
                    "day_name": "Tuesday",
                    "date": "2026-11-03",
                    "date_display": "Tue, Nov 03",
                    "recipe": {"name": "Tuesday Test Tacos", "id": "none_123"},
                },
                "thursday": {
                    "day_name": "Thursday",
                    "date": "2026-11-05",
                    "date_display": "Thu, Nov 05",
                    "recipe": {"name": "Thursday Slow Stew", "id": "none_456"},
                },
                "sunday": {
                    "day_name": "Sunday",
                    "date": "2026-11-08",
                    "date_display": "Sun, Nov 08",
                    "recipe": {"name": "Sunday Roast Chicken", "id": "none_789"},
                },
            }
        }
        out_path = os.path.join(self.test_dir, "test_triptych.jpg")
        res = build_three_panel_menu_image(plan, out_path=out_path)
        self.assertEqual(res, out_path)
        self.assertTrue(os.path.exists(out_path))

        with Image.open(out_path) as img:
            self.assertEqual(img.size, (1500, 600))
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.mode, "RGB")

    def test_build_three_panel_menu_image_empty_meals(self):
        self.assertIsNone(build_three_panel_menu_image({}))
        self.assertIsNone(build_three_panel_menu_image({"meals": {}}))

    def test_build_three_panel_menu_image_with_mocked_images(self):
        import io
        import urllib.error
        from PIL import Image

        landscape_img = Image.new("RGB", (1200, 600), color=(200, 100, 50))
        buf_l = io.BytesIO()
        landscape_img.save(buf_l, format="JPEG")
        landscape_bytes = buf_l.getvalue()

        portrait_img = Image.new("RGB", (400, 800), color=(50, 150, 200))
        buf_p = io.BytesIO()
        portrait_img.save(buf_p, format="JPEG")
        portrait_bytes = buf_p.getvalue()

        mock_resp_l = MagicMock()
        mock_resp_l.status = 200
        mock_resp_l.read.return_value = landscape_bytes
        mock_resp_l.__enter__.return_value = mock_resp_l

        mock_resp_p = MagicMock()
        mock_resp_p.status = 200
        mock_resp_p.read.return_value = portrait_bytes
        mock_resp_p.__enter__.return_value = mock_resp_p

        plan = {
            "meals": {
                "tuesday": {
                    "day_name": "Tuesday",
                    "date": "2026-11-03",
                    "date_display": "Tue, Nov 03",
                    "recipe": {"name": "Very Long Recipe Name That Wraps Cleanly Across Multiple Lines For Testing Typography", "id": "recipe_l"},
                },
                "thursday": {
                    "day_name": "Thursday",
                    "date": "2026-11-05",
                    "date_display": "Thu, Nov 05",
                    "recipe": {"name": "Thursday Stew", "id": "recipe_p"},
                },
                "sunday": {
                    "day_name": "Sunday",
                    "date": "2026-11-08",
                    "date_display": "Sun, Nov 08",
                    "recipe": {"name": "Sunday Roast"},
                },
            }
        }
        out_path = os.path.join(self.test_dir, "mocked_triptych.jpg")

        def _fake_urlopen(req, timeout=3):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "recipe_l" in url:
                return mock_resp_l
            elif "recipe_p" in url:
                return mock_resp_p
            raise urllib.error.URLError("Not found")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = build_three_panel_menu_image(plan, out_path=out_path)
            self.assertEqual(res, out_path)
            self.assertTrue(os.path.exists(out_path))
            with Image.open(out_path) as img:
                self.assertEqual(img.size, (1500, 600))
                self.assertEqual(img.format, "JPEG")

    def test_format_proposal_markdown_rich_content(self):
        plan = {
            "chef_note": "A bright citrus glaze on Thursday balances the rich Sunday braise.",
            "meals": {
                "tuesday": {
                    "day_name": "Tuesday",
                    "date": "2026-11-03",
                    "date_display": "Tue, Nov 03",
                    "slot_type": "Fast Weeknight (<30m)",
                    "recipe": {"name": "Quick Salmon Bowl", "slug": "quick-salmon-bowl", "total_time": "25 mins", "tags": ["Gluten-Free", "Quick"], "description": "Crispy salmon over rice"},
                },
                "thursday": {
                    "day_name": "Thursday",
                    "date": "2026-11-05",
                    "date_display": "Thu, Nov 05",
                    "slot_type": "Kid-Friendly Family Staple",
                    "recipe": {"name": "Chicken Tacos", "slug": "chicken-tacos", "total_time": "30 mins", "tags": ["Gluten-Free", "Kid-Friendly"], "description": "Tacos for the family"},
                },
                "sunday": {
                    "day_name": "Sunday",
                    "date": "2026-11-08",
                    "date_display": "Sun, Nov 08",
                    "slot_type": "Comfort Batch / Weekend Classic",
                    "recipe": {"name": "Braised Short Ribs", "slug": "braised-short-ribs", "total_time": "3 hours", "tags": ["Comfort Food", "Gluten-Free"], "description": "Tender beef ribs"},
                },
            }
        }
        md = format_proposal_markdown(plan)
        self.assertIn("Weekly Dinner Rotation Proposal (3 Meals)", md)
        self.assertIn("[Quick Salmon Bowl]", md)
        self.assertIn("[Chicken Tacos]", md)
        self.assertIn("[Braised Short Ribs]", md)
        self.assertIn("Chef's Pairing Note", md)
        self.assertIn("A bright citrus glaze on Thursday balances the rich Sunday braise.", md)
        self.assertIn("[CHOICES: Lock In Menu | Swap Tuesday | Swap Thursday | Swap Sunday]", md)


class TestButtonInterception(unittest.IsolatedAsyncioTestCase):
    async def test_swap_button_fast_path(self):
        mock_interaction = MagicMock()
        mock_interaction.id = 9991234
        mock_interaction.channel = AsyncMock()
        mock_turn_queue = AsyncMock()

        with patch("tools.meal_planner_proposal.swap_slot", return_value=(True, "Proposal [CHOICES: Lock In Menu]")) as mock_swap:
            await handle_button_choice("Swap Tuesday", mock_interaction, mock_turn_queue)
            mock_swap.assert_called_once_with("tuesday")
            mock_turn_queue.put.assert_not_called()
            self.assertTrue(mock_interaction.channel.send.called)

    async def test_lock_in_button_fast_path(self):
        mock_interaction = MagicMock()
        mock_interaction.id = 9991235
        mock_interaction.channel = AsyncMock()
        mock_turn_queue = AsyncMock()

        with patch("tools.meal_planner_proposal.lock_in_menu", return_value=(True, "Locked in!")) as mock_lock:
            await handle_button_choice("Lock In Menu", mock_interaction, mock_turn_queue)
            mock_lock.assert_called_once()
            mock_turn_queue.put.assert_not_called()
            self.assertTrue(mock_interaction.channel.send.called)


if __name__ == "__main__":
    unittest.main()
