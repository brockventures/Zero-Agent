import pytest
from tools.grocery_manager import parse_raw_ingredient, compile_cart
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
        url, manifest, assumed = compile_cart(items)
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

    def test_bread_normalizes_to_365_sandwich_bread(self):
        assert normalize_ingredient_name("sara lee artesano bakery bread") == "365 classic white sandwich bread"


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
