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
    "cherry", "cherries", "bell pepper", "bell peppers", "sweet peppers", "mini peppers",
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
    "large brown eggs": "large brown eggs",
    "large brown grade a eggs": "large brown eggs",
    "organic eggs": "organic large brown eggs",
    "mandarin oranges": "clementines",
    "mandarins": "clementines",
    "cuties": "clementines",
    "clementines": "clementines",
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
    "parmesan": "grated parmesan cheese",
    "grated parmesan": "grated parmesan cheese",
    "grated parmesan cheese": "grated parmesan cheese",
    "parmigiano-reggiano": "grated parmesan cheese",
    "parmigiano reggiano": "grated parmesan cheese",
    "freshly grated parmigiano-reggiano": "grated parmesan cheese",
    "fresh basil": "organic fresh basil",
    "fresh basil leaves": "organic fresh basil",
    "basil leaves": "organic fresh basil",
    "fresh parsley": "organic fresh parsley",
    "fresh italian parsley": "organic fresh parsley",
    "chopped fresh italian parsley": "organic fresh parsley",
    "fresh cilantro": "organic fresh cilantro",
    "chopped fresh cilantro": "organic fresh cilantro",
    "sara lee artesano bakery bread": "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf",
    "white bread": "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf",
    "sandwich bread": "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf",
    "nature's own butterbread": "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf",
    "butterbread": "Nature's Own Butterbread, Non-GMO Bread, Sandwich Bread, 20 oz Loaf",
    "honey nut cheerios": "Honey Nut Cheerios Cereal, Limited Edition Heart Shapes, Affirmations Box, Heart Healthy, Gluten Free, Large Size, 15.4 oz",
    "oatmeal": "365 by Whole Foods Market, Organic Maple & Brown Sugar Instant Oatmeal Packets, 1.41 Ounce (Pack of 8)",
    "instant oatmeal": "365 by Whole Foods Market, Organic Maple & Brown Sugar Instant Oatmeal Packets, 1.41 Ounce (Pack of 8)",
    "purely elizabeth oatmeal": "365 by Whole Foods Market, Organic Maple & Brown Sugar Instant Oatmeal Packets, 1.41 Ounce (Pack of 8)",
    "purely elizabeth cinnamon instant oatmeal": "365 by Whole Foods Market, Organic Maple & Brown Sugar Instant Oatmeal Packets, 1.41 Ounce (Pack of 8)",
    "ground turkey": "organic ground turkey",
    "ground pork": "ground pork",
    "whole-milk ricotta cheese": "365 organic whole milk ricotta cheese",
    "ricotta cheese": "365 organic whole milk ricotta cheese",
    "feta cheese": "crumbled feta cheese",
    "crumbled feta cheese": "crumbled feta cheese",
    "shredded carrots": "organic shredded carrots",
    "whole jalapeño chiles": "organic jalapeno peppers",
    "jalapeño chiles": "organic jalapeno peppers",
    "jalapeno chiles": "organic jalapeno peppers",
    "jalapeños": "organic jalapeno peppers",
    "jalapeno": "organic jalapeno peppers",
    "jalapeño": "organic jalapeno peppers",
    "ají amarillo pepper paste": "organic jalapeno peppers",
    "aji amarillo pepper paste": "organic jalapeno peppers",
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
    "chicken breasts": "organic boneless skinless chicken breast",
    "chicken breast": "organic boneless skinless chicken breast",
    "boneless skinless chicken breasts": "organic boneless skinless chicken breast",
    "boneless skinless chicken breast": "organic boneless skinless chicken breast",
    "boneless skinless chicken breasts or thighs": "organic boneless skinless chicken breast",
    "chicken breasts or thighs": "organic boneless skinless chicken breast",
    "chicken thighs or breasts": "organic boneless skinless chicken breast",
    "chicken thighs": "organic boneless skinless chicken thighs",
    "boneless skinless chicken thighs": "organic boneless skinless chicken thighs",
    "ground pepper": "365 organic ground black pepper",
    "pepper": "365 organic ground black pepper",
    "black pepper": "365 organic ground black pepper",
    "ground black pepper": "365 organic ground black pepper",
    "ground allspice": "365 organic allspice ground",
    "allspice": "365 organic allspice ground",
}

PACKAGED_RETAIL_KEYWORDS = {
    "cheese", "parmesan", "parmigiano", "ricotta", "feta", "cotija", "mozzarella",
    "cheddar", "sausage", "bacon", "breadcrumbs", "crackers", "rice", "quinoa",
    "beans", "broth", "stock", "noodles", "pasta", "glaze", "mayo", "aioli",
    "dressing", "sauce", "mustard", "vinegar", "oil", "pistachios", "almonds",
    "walnuts", "peanuts", "seeds", "tortillas", "pita", "bread"
}


def apply_365_store_brand(name: str) -> str:
    """Prioritizes Whole Foods store brand (365 by Whole Foods Market) for packaged goods,
    pantry staples, canned goods, dairy, grains, and condiments whenever available."""
    clean = name.strip().lower()

    # Do not override explicit specialty or requested third-party brands
    specialty_brands = [
        "purely elizabeth", "simply elizabeth", "banza", "jovial", "barilla",
        "tate's", "schär", "schar", "rao's", "raos", "one degree", "dr praegers",
        "dr. praeger's", "yasso", "cheerios", "waterloo", "vital farms", "shin ramyun",
        "sara lee"
    ]
    if any(b in clean for b in specialty_brands):
        return name.strip()

    # Canned beans & legumes
    if any(w in clean for w in ["chickpea", "garbanzo"]):
        return "365 organic garbanzo beans"
    if "black bean" in clean:
        return "365 organic black beans"
    if "kidney bean" in clean:
        return "365 organic kidney beans"
    if "cannellini" in clean or ("white bean" in clean and "green" not in clean):
        return "365 organic cannellini beans"
    if "pinto bean" in clean:
        return "365 organic pinto beans"

    # Coconut milk & canned milks
    if "coconut milk" in clean:
        return "365 organic coconut milk"
    if "evaporated milk" in clean:
        return "365 organic evaporated milk"

    # Broth & stocks
    if any(w in clean for w in ["broth", "stock"]):
        if any(w in clean for w in ["veg", "vegetable"]):
            return "365 organic vegetable broth"
        elif "beef" in clean:
            return "365 organic beef broth"
        elif "bone" in clean:
            return "365 organic chicken bone broth"
        else:
            return "365 organic chicken broth"

    # Canned tomatoes & sauces
    if "tomato paste" in clean:
        return "365 organic tomato paste"
    if "tomato sauce" in clean:
        return "365 organic tomato sauce"
    if "diced tomato" in clean or "canned tomato" in clean:
        return "365 organic diced tomatoes"
    if "crushed tomato" in clean:
        return "365 organic crushed tomatoes"
    if "san marzano" in clean or "peeled tomato" in clean:
        return "365 whole peeled san marzano tomatoes"

    # Pasta & grains
    if "macaroni" in clean or "elbow" in clean:
        return "365 organic pasta elbows"
    if "penne" in clean:
        return "365 organic penne pasta"
    if "spaghetti" in clean:
        return "365 organic spaghetti"
    if "jasmine rice" in clean:
        return "365 organic jasmine rice"
    if "basmati rice" in clean:
        return "365 organic basmati rice"
    if "brown rice" in clean:
        return "365 organic brown rice"
    if "quinoa" in clean:
        return "365 organic tricolor quinoa"
    if "rolled oats" in clean or "quick oats" in clean:
        return "365 organic old fashioned rolled oats"

    # Tortillas
    if "tortilla" in clean and ("corn" in clean or "yellow" in clean or "white" in clean):
        return "365 organic yellow corn tortillas"

    # Oils & vinegars
    if "olive oil" in clean:
        return "365 extra virgin olive oil"
    if "white wine vinegar" in clean:
        return "365 organic white wine vinegar"
    if "red wine vinegar" in clean:
        return "365 organic red wine vinegar"
    if "apple cider vinegar" in clean:
        return "365 organic apple cider vinegar"
    if "balsamic vinegar" in clean:
        return "365 balsamic vinegar of modena"
    if "canola oil" in clean or "vegetable oil" in clean:
        return "365 organic canola oil"

    # Cheeses & dairy
    if "cheddar" in clean:
        if any(w in clean for w in ["shredded", "grated", "mild", "monterey", "jack"]):
            return "365 shredded mild cheddar cheese"
        return "365 mild cheddar cheese"
    if "monterey jack" in clean or "jack cheese" in clean:
        return "365 shredded monterey jack cheese"
    if "mozzarella" in clean:
        if "shredded" in clean or "grated" in clean:
            return "365 shredded mozzarella cheese"
        return "365 mozzarella cheese"
    if "parmesan" in clean or "parmigiano" in clean:
        return "365 grated parmesan cheese"
    if "ricotta" in clean:
        return "365 organic whole milk ricotta cheese"
    if "cream cheese" in clean:
        return "365 organic cream cheese"
    if "sour cream" in clean:
        return "365 organic sour cream"
    if "heavy whipping cream" in clean or "heavy cream" in clean:
        return "365 organic heavy whipping cream"
    if "butter" in clean:
        if "unsalted" in clean:
            return "365 unsalted butter"
        return "365 salted butter"

    # Spices (if requested or not in pantry)
    if "black pepper" in clean or ("pepper" in clean and any(w in clean for w in ["ground", "coarse", "cracked", "whole"])):
        return "365 organic ground black pepper"
    if "allspice" in clean:
        return "365 organic allspice ground"
    if "cinnamon" in clean and any(w in clean for w in ["ground", "spice"]):
        return "365 organic ground cinnamon"
    if "cumin" in clean:
        return "365 organic ground cumin"
    if "garlic powder" in clean:
        return "365 organic garlic powder"
    if "onion powder" in clean:
        return "365 organic onion powder"
    if "paprika" in clean:
        if "smoked" in clean:
            return "365 organic smoked paprika"
        return "365 organic paprika"
    if "chili powder" in clean:
        return "365 organic chili powder"

    # Condiments
    if "maple syrup" in clean:
        return "365 organic maple syrup"
    if re.search(r"\bhoney\b", clean) and not any(w in clean for w in ["nut", "apple", "honeycrisp", "melon", "dew", "mustard"]):
        return "365 organic mountain forest raw honey"
    if "peanut butter" in clean:
        return "365 organic creamy peanut butter"
    if "almond butter" in clean:
        return "365 organic creamy almond butter"
    if "mustard" in clean:
        if "dijon" in clean:
            return "365 organic dijon mustard"
        return "365 organic yellow mustard"
    if "ketchup" in clean:
        return "365 organic tomato ketchup"
    if "mayonnaise" in clean or "mayo" in clean:
        return "365 organic mayonnaise"
    if "bbq sauce" in clean or "barbecue sauce" in clean:
        return "365 organic barbecue sauce"
    if "ranch" in clean:
        return "365 ranch dressing"

    # Frozen
    if "frozen peas" in clean:
        return "365 organic sweet peas"
    if "frozen corn" in clean:
        return "365 organic sweet corn"

    return name.strip()


def normalize_ingredient_name(name: str) -> str:
    """Normalizes generic recipe strings into Whole Foods catalog-friendly terms
    enforcing household dietary preferences (store brand 365 default, thin-skin organic, 100% grass-fed meat)."""
    clean = name.strip().lower()

    # Exact table match
    if clean in WF_INGREDIENT_NORMALIZER:
        return WF_INGREDIENT_NORMALIZER[clean]

    # Store brand (365) default for packaged pantry, canned goods, dairy, grains, condiments
    store_brand_name = apply_365_store_brand(name)
    if store_brand_name != name.strip():
        return store_brand_name

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
