import sys
import unittest.mock
from pathlib import Path

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from tools.grocery_manager import (
    compile_cart,
    compile_weekly_manifest,
    parse_raw_ingredient,
    sync_costco_tasks,
)
from tools.whole_foods_afx import build_afx_payload, normalize_ingredient_name


class TestGroceryParsing:
    def test_rejects_none_and_headers(self):
        assert parse_raw_ingredient("None") == []
        assert parse_raw_ingredient("none") == []
        assert parse_raw_ingredient("--- Barbecue Spice Blend ---") == []
        assert parse_raw_ingredient("For the garnish:") == []
        assert parse_raw_ingredient("") == []

    def test_splits_ampersand_compound_lines(self):
        res = parse_raw_ingredient("1/4 cup Fresh cilantro, chopped & 2 cloves garlic, minced")
        assert len(res) == 2
        assert res[0]["name"].lower() == "fresh cilantro"
        assert res[0]["amount"] == 0.25
        assert res[0]["unit"] == "CUP"
        assert res[1]["name"].lower() == "garlic"
        assert res[1]["amount"] == 2.0
        assert res[1]["unit"] == "COUNT"

    def test_splits_compound_garnish_line(self):
        res = parse_raw_ingredient("Fresh basil leaves and grated Parmesan, for serving")
        assert len(res) == 2
        assert "basil" in res[0]["name"].lower()
        assert "parmesan" in res[1]["name"].lower()

    def test_detects_proprietary_pantry_spice_blends(self):
        res = parse_raw_ingredient("2 tbsp Chermoula spice blend (paprika, cumin, coriander, garlic powder, cayenne)")
        assert len(res) == 1
        assert res[0].get("is_pantry") is True
        assert "chermoula" in res[0]["name"].lower()

        res2 = parse_raw_ingredient("1 1/2 tsp Garlic-herb seasoning (dried oregano, thyme, garlic powder, onion powder)")
        assert len(res2) == 1
        assert res2[0].get("is_pantry") is True
        assert "garlic-herb" in res2[0]["name"].lower()

    def test_pantry_blend_routes_to_assumed_in_stock(self):
        items = parse_raw_ingredient("2 tbsp Chermoula spice blend (paprika, cumin, coriander, garlic powder, cayenne)")
        _url, manifest, assumed = compile_cart(items)
        assert len(manifest) == 0
        assert len(assumed) == 1
        assert "chermoula" in assumed[0].lower()


class TestAfxRetailNormalization:
    def test_packaged_goods_culinary_fractions_convert_to_count(self):
        items = [{"name": "365 grated parmesan cheese", "amount": 0.5, "unit": "CUP"}]
        payload = build_afx_payload(items)
        assert payload["ingredients"][0]["quantityList"][0]["unit"] == "COUNT"
        assert payload["ingredients"][0]["quantityList"][0]["amount"] >= 1

    def test_packaged_sausage_converts_to_count(self):
        items = [{"name": "365 mild italian pork sausage", "amount": 0.5, "unit": "POUND"}]
        payload = build_afx_payload(items)
        assert payload["ingredients"][0]["quantityList"][0]["unit"] == "COUNT"
        assert payload["ingredients"][0]["quantityList"][0]["amount"] >= 1

    def test_bread_normalizes_to_butterbread(self):
        assert normalize_ingredient_name("sara lee artesano bakery bread") == "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf"

    def test_eggs_and_mandarins_normalize_cleanly_for_afx(self):
        assert normalize_ingredient_name("eggs") == "large brown eggs"
        assert normalize_ingredient_name("large brown grade a eggs") == "large brown eggs"
        assert normalize_ingredient_name("mandarin oranges") == "clementines"
        assert normalize_ingredient_name("cuties") == "clementines"



class TestQuantityWarningsAndCostco:
    def test_detects_multi_count_canned_goods(self):
        from tools.grocery_manager import generate_quantity_warnings
        staged = []
        manifest = [{"name": "365 whole peeled san marzano tomatoes", "amount": 2.0, "unit": "COUNT"}]
        warnings = generate_quantity_warnings(staged, manifest)
        assert len(warnings) == 1
        assert "2 cans/items" in warnings[0]
        assert "San Marzano" in warnings[0]

    def test_suppresses_produce_and_garlic_multi_counts(self):
        from tools.grocery_manager import generate_quantity_warnings
        staged = []
        manifest = [
            {"name": "organic honeycrisp apples", "amount": 3.0, "unit": "COUNT"},
            {"name": "garlic", "amount": 4.0, "unit": "COUNT"},
            {"name": "organic chicken breasts", "amount": 2.0, "unit": "COUNT"},
        ]
        warnings = generate_quantity_warnings(staged, manifest)
        assert len(warnings) == 0

    def test_detects_fractional_sausage_surplus(self):
        from tools.grocery_manager import generate_quantity_warnings
        staged = [{"name": "ground sweet Italian pork sausage", "amount": 0.5, "unit": "POUND"}]
        manifest = [{"name": "365 mild italian pork sausage", "amount": 1.0, "unit": "COUNT"}]
        warnings = generate_quantity_warnings(staged, manifest)
        assert len(warnings) == 1
        assert "1 lb pack" in warnings[0]
        assert "surplus" in warnings[0]

    def test_identifies_costco_recipe_by_tag_or_name(self):
        from tools.grocery_manager import is_costco_recipe
        assert is_costco_recipe({"name": "Costco Crispy Skillet Carnitas", "tags": ["Quick"]}) is True
        assert is_costco_recipe({"name": "15-Minute Pesto Salmon", "tags": ["Costco-Hack", "Seafood"]}) is True
        assert is_costco_recipe({"name": "Caramelized Shallot Pasta", "tags": ["NYT Cooking"]}) is False


class TestTodoistGroceryIntegration:
    def test_sync_costco_tasks_delegates_to_todoist(self):
        existing_items = [{"id": "t1", "title": "Paper Towels"}]
        added_calls = []

        def mock_add(clean_item, list_title="Costco"):
            added_calls.append((clean_item, list_title))
            return {"id": "new-id", "title": clean_item}

        with (
            unittest.mock.patch("tools.todoist_grocery.get_pending_groceries", return_value=existing_items) as mock_get,
            unittest.mock.patch("tools.todoist_grocery.add_grocery_item", side_effect=mock_add),
        ):
            added = sync_costco_tasks(["Paper Towels", "Kirkland Olive Oil", "Rotisserie Chicken"])

            mock_get.assert_called_once_with(title="Costco")
            assert added == ["Kirkland Olive Oil", "Rotisserie Chicken"]
            assert len(added_calls) == 2
            assert added_calls[0] == ("Kirkland Olive Oil", "Costco")
            assert added_calls[1] == ("Rotisserie Chicken", "Costco")

    def test_compile_weekly_manifest_delegates_to_todoist_and_completes_tasks(self):
        def mock_get_pending(title="Whole Foods"):
            if title == "Costco":
                return [{"id": "costco-1", "title": "Kirkland Paper Towels"}]
            return [{"id": "todoist-wf-1", "title": "Organic Honeycrisp Apples"}]

        completed_ids = []

        def mock_complete(task_id):
            completed_ids.append(task_id)
            return {"id": task_id, "status": "completed"}

        with (
            unittest.mock.patch("tools.grocery_manager.extract_active_meal_plan_items", return_value=([], [], [], [])),
            unittest.mock.patch("tools.grocery_manager.get_due_staples", return_value=[]),
            unittest.mock.patch("tools.ha_shopping_list.get_pending_groceries", return_value=[]),
            unittest.mock.patch("tools.todoist_grocery.get_pending_groceries", side_effect=mock_get_pending),
            unittest.mock.patch("tools.todoist_grocery.complete_task", side_effect=mock_complete),
        ):
            res = compile_weekly_manifest(
                include_due_staples=False,
                include_meal_plan=False,
                complete_tasks=True,
                sync_costco_list=False,
            )

            assert res["tasks_ingested"] == 1
            assert any("Kirkland Paper Towels" in c for c in res["costco_items"])
            assert completed_ids == ["todoist-wf-1"]

    def test_compile_weekly_manifest_skips_complete_when_false(self):
        def mock_get_pending(title="Whole Foods"):
            if title == "Costco":
                return []
            return [{"id": "todoist-wf-1", "title": "Organic Avocados"}]

        with (
            unittest.mock.patch("tools.grocery_manager.extract_active_meal_plan_items", return_value=([], [], [], [])),
            unittest.mock.patch("tools.grocery_manager.get_due_staples", return_value=[]),
            unittest.mock.patch("tools.ha_shopping_list.get_pending_groceries", return_value=[]),
            unittest.mock.patch("tools.todoist_grocery.get_pending_groceries", side_effect=mock_get_pending),
            unittest.mock.patch("tools.todoist_grocery.complete_task") as mock_complete,
        ):
            res = compile_weekly_manifest(
                include_due_staples=False,
                include_meal_plan=False,
                complete_tasks=False,
                sync_costco_list=False,
            )

            assert res["tasks_ingested"] == 1
            mock_complete.assert_not_called()


