import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.meal_planner_proposal import (
    is_dinner_recipe,
    filter_eligible_recipes,
    generate_proposal,
    swap_slot,
    format_proposal_markdown,
    get_target_cooking_dates,
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
