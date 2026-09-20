#!/usr/bin/env python3
"""
Whole Foods AFX (Amazon Fresh Experience) Cart Staging Tool
Author: Zero
Description: Converts ingredient / grocery manifests into official Amazon AFX
landing URLs that natively stage items into Whole Foods carts with 1-click.
"""

import argparse
import base64
import gzip
import json
import re
import sys
from urllib.parse import quote

# Whole Foods brand identifiers on Amazon AFX
AFX_BRAND_WHOLE_FOODS = "Whole Foods"
AFX_ALM_BRAND_ID_WF = "VUZHIFdob2xlIEZvb2Rz"  # Base64 for "UFG Whole Foods"

# Supported AFX units
AFX_UNITS = {
    "count": "COUNT",
    "can": "COUNT",
    "cans": "COUNT",
    "bottle": "COUNT",
    "bottles": "COUNT",
    "bunch": "COUNT",
    "bunches": "COUNT",
    "bag": "COUNT",
    "bags": "COUNT",
    "box": "COUNT",
    "boxes": "COUNT",
    "package": "COUNT",
    "pkg": "COUNT",
    "cup": "CUP",
    "cups": "CUP",
    "tablespoon": "TABLESPOON",
    "tablespoons": "TABLESPOON",
    "tbsp": "TABLESPOON",
    "teaspoon": "TEASPOON",
    "teaspoons": "TEASPOON",
    "tsp": "TEASPOON",
    "pound": "POUND",
    "pounds": "POUND",
    "lb": "POUND",
    "lbs": "POUND",
    "ounce": "OUNCE",
    "ounces": "OUNCE",
    "oz": "OUNCE",
    "gallon": "GALLON",
    "gallons": "GALLON",
    "gal": "GALLON",
}


# Household dietary & brand rules
THIN_SKIN_ORGANIC = {
    "berry", "berries", "strawberry", "strawberries", "blueberry", "blueberries",
    "raspberry", "raspberries", "blackberry", "blackberries",
    "spinach", "kale", "greens", "lettuce", "spring mix", "arugula",
    "grape", "grapes", "apple", "apples", "peach", "peaches", "nectarine", "nectarines",
    "cherry", "cherries", "bell pepper", "bell peppers", "pepper", "peppers",
    "celery", "cucumber", "cucumbers", "tomato", "tomatoes", "cilantro", "parsley"
}

GRASS_FED_MEATS = {
    "beef", "ground beef", "steak", "ribeye", "sirloin", "bison", "flank steak"
}

# Generic recipe terms mapped to Whole Foods catalog search keywords
WF_INGREDIENT_NORMALIZER = {
    "milk": "365 organic whole milk",
    "whole milk": "365 organic whole milk",
    "egg": "large brown eggs",
    "eggs": "large brown eggs",
    "large eggs": "large brown eggs",
    "brown eggs": "large brown eggs",
    "ranch dressing": "365 ranch dressing",
    "ranch": "365 ranch dressing",
    "barbecue sauce": "365 organic barbecue sauce",
    "bbq sauce": "365 organic barbecue sauce",
    "ketchup": "365 organic tomato ketchup",
    "mayo": "365 organic mayonnaise",
    "mayonnaise": "365 organic mayonnaise",
    "dijon mustard": "365 organic dijon mustard",
    "yellow mustard": "365 organic yellow mustard",
    "sour cream": "365 organic sour cream",
    "heavy cream": "365 organic heavy whipping cream",
    "cream cheese": "365 organic cream cheese",
    "butter": "365 salted butter",
    "salted butter": "365 salted butter",
    "olive oil": "365 extra virgin olive oil",
    "garbanzo beans": "365 organic garbanzo beans",
    "black beans": "365 organic black beans",
    "quinoa": "365 organic tricolor quinoa",
    "sourdough bread": "365 organic sourdough bread",
    "bread": "365 organic sourdough bread",
    "avocados": "hass avocados",
    "avocado": "hass avocados",
    "red onion": "red onion",
    "sweet potatoes": "sweet potatoes",
    "bananas": "bananas",
    "sweet italian pork sausage": "365 mild italian pork sausage",
    "ground sweet italian pork sausage": "365 mild italian pork sausage",
    "sweet italian sausage": "365 mild italian pork sausage",
    "mild italian pork sausage": "365 mild italian pork sausage",
    "mild italian sausage": "365 mild italian pork sausage",
    "italian pork sausage": "365 mild italian pork sausage",
    "italian sausage": "365 mild italian pork sausage",
    "parmesan": "365 grated parmesan cheese",
    "grated parmesan": "365 grated parmesan cheese",
    "grated parmesan cheese": "365 grated parmesan cheese",
    "parmigiano-reggiano": "365 grated parmesan cheese",
    "parmigiano reggiano": "365 grated parmesan cheese",
    "freshly grated parmigiano-reggiano": "365 grated parmesan cheese",
    "fresh basil": "organic fresh basil",
    "fresh basil leaves": "organic fresh basil",
    "basil leaves": "organic fresh basil",
    "fresh parsley": "organic fresh parsley",
    "fresh italian parsley": "organic fresh parsley",
    "chopped fresh italian parsley": "organic fresh parsley",
    "fresh cilantro": "organic fresh cilantro",
    "chopped fresh cilantro": "organic fresh cilantro",
    "sara lee artesano bakery bread": "365 classic white sandwich bread",
    "white bread": "365 classic white sandwich bread",
    "sandwich bread": "365 classic white sandwich bread",
    "whole-milk ricotta cheese": "365 organic whole milk ricotta cheese",
    "ricotta cheese": "365 organic whole milk ricotta cheese",
    "feta cheese": "365 organic feta cheese",
    "crumbled feta cheese": "365 organic feta cheese",
    "cotija cheese": "cotija cheese",
    "cremini mushrooms": "organic cremini mushrooms",
    "grape tomatoes": "organic grape tomatoes",
    "butternut squash": "organic butternut squash",
    "baby broccoli": "organic baby broccoli",
    "quinoa and millet blend": "365 organic tricolor quinoa",
    "roasted red peppers": "365 roasted red peppers",
    "roasted pistachios": "365 roasted pistachios",
    "whole peeled san marzano tomatoes": "365 whole peeled san marzano tomatoes",
    "san marzano tomatoes": "365 whole peeled san marzano tomatoes",
    "gluten-free breadcrumbs": "gluten free breadcrumbs",
    "chicken breasts": "organic chicken breasts",
}

PACKAGED_RETAIL_KEYWORDS = {
    "cheese", "parmesan", "parmigiano", "ricotta", "feta", "cotija", "mozzarella",
    "cheddar", "sausage", "bacon", "breadcrumbs", "crackers", "rice", "quinoa",
    "beans", "broth", "stock", "noodles", "pasta", "glaze", "mayo", "aioli",
    "dressing", "sauce", "mustard", "vinegar", "oil", "pistachios", "almonds",
    "walnuts", "peanuts", "seeds", "tortillas", "pita", "bread"
}


def normalize_ingredient_name(name: str) -> str:
    """Normalizes generic recipe strings into Whole Foods catalog-friendly terms
    enforcing household dietary preferences (thin-skin organic, 100% grass-fed meat, 365 brand)."""
    clean = name.strip().lower()

    # Exact table match
    if clean in WF_INGREDIENT_NORMALIZER:
        return WF_INGREDIENT_NORMALIZER[clean]

    # Rule 1: Thin-skin produce must be organic
    for term in THIN_SKIN_ORGANIC:
        if term in clean and "organic" not in clean:
            return f"organic {clean}"

    # Rule 2: Beef/red meat preferred 100% grass-fed
    for term in GRASS_FED_MEATS:
        if term in clean and "grass-fed" not in clean:
            return f"100% grass-fed {clean}"

    return name.strip()


def build_afx_payload(items: list[dict]) -> dict:
    """Formats a list of item dicts into Amazon AFX ingredient schema."""
    ingredients = []
    for item in items:
        name = normalize_ingredient_name(item.get("name", ""))
        amount = item.get("amount", 1)
        unit = item.get("unit", "COUNT").upper()
        if unit.lower() in AFX_UNITS:
            unit = AFX_UNITS[unit.lower()]
        else:
            unit = "COUNT"

        # Retail packaging normalization: packaged items measured in culinary fractions
        # (e.g. 0.5 cup parmesan, 0.5 lb sausage) must be ordered in discrete container counts.
        name_lower = name.lower()
        if any(kw in name_lower for kw in PACKAGED_RETAIL_KEYWORDS):
            if unit in ("CUP", "TABLESPOON", "TEASPOON", "OUNCE") or (unit == "POUND" and amount <= 1.0):
                unit = "COUNT"
                amount = max(1, int(round(amount))) if amount >= 1 else 1

        if unit == "COUNT":
            amount = int(round(amount))

        ingredients.append({
            "name": name,
            "quantityList": [
                {
                    "unit": unit,
                    "amount": amount
                }
            ]
        })
    return {"ingredients": ingredients}


def encode_afx_payload(payload: dict) -> str:
    """Gzip and base64url encode the payload for Amazon's landingencoded URL."""
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(json_bytes)
    encoded = base64.urlsafe_b64encode(compressed).decode("utf-8").rstrip("=")
    return encoded


def generate_afx_url(items: list[dict], brand_id: str = AFX_ALM_BRAND_ID_WF) -> str:
    """Generates the direct, 1-click Amazon Whole Foods AFX URL."""
    payload = build_afx_payload(items)
    encoded_str = encode_afx_payload(payload)
    return f"https://www.amazon.com/afx/ingredients/landingencoded?almBrandId={brand_id}&encodedIngredients={encoded_str}"


def get_recipe_bbq_garbanzo_bowl() -> list[dict]:
    """Returns structured ingredients for BBQ Garbanzo Bean Bowls."""
    return [
        {"name": "organic garbanzo beans", "amount": 2, "unit": "COUNT"},
        {"name": "365 organic barbecue sauce", "amount": 1, "unit": "COUNT"},
        {"name": "sweet potatoes", "amount": 2, "unit": "COUNT"},
        {"name": "organic tricolor quinoa", "amount": 1, "unit": "COUNT"},
        {"name": "shredded coleslaw mix", "amount": 1, "unit": "COUNT"},
        {"name": "hass avocados", "amount": 2, "unit": "COUNT"},
        {"name": "organic red onion", "amount": 1, "unit": "COUNT"},
        {"name": "ranch dressing", "amount": 1, "unit": "COUNT"},
        {"name": "fresh organic cilantro", "amount": 1, "unit": "COUNT"},
    ]


def main():
    parser = argparse.ArgumentParser(description="Whole Foods AFX Cart Generator")
    parser.add_argument("--recipe", choices=["bbq_garbanzo"], default="bbq_garbanzo", help="Predefined recipe")
    parser.add_argument("--json-out", action="store_true", help="Output JSON")
    args = parser.parse_args()

    if args.recipe == "bbq_garbanzo":
        items = get_recipe_bbq_garbanzo_bowl()
        title = "BBQ Garbanzo Bean Bowls"

    url = generate_afx_url(items)

    if args.json_out:
        print(json.dumps({
            "title": title,
            "item_count": len(items),
            "items": items,
            "afx_url": url
        }, indent=2))
    else:
        print(f"Recipe: {title}")
        print(f"Items: {len(items)}")
        for i, item in enumerate(items, 1):
            print(f"  {i}. {item['amount']}x {item['name']} ({item['unit']})")
        print("\nDirect 1-Click Amazon Whole Foods Staging Link:")
        print(url)


if __name__ == "__main__":
    main()
