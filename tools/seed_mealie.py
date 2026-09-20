#!/usr/bin/env python3
import json
import urllib.parse
import urllib.request
import sys

import os

def get_env_config():
    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

_env = get_env_config()
MEALIE_URL = _env.get("MEALIE_BASE_URL", "http://127.0.0.1:9090")

def get_token():
    if _env.get("MEALIE_API_TOKEN"):
        return _env["MEALIE_API_TOKEN"]
    auth_data = urllib.parse.urlencode({"username": AUTH_USER, "password": AUTH_PASS}).encode()
    req = urllib.request.Request(f"{MEALIE_URL}/api/auth/token", data=auth_data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]

def get_all_tags(token):
    req = urllib.request.Request(f"{MEALIE_URL}/api/organizers/tags?perPage=100", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return {t["name"]: t for t in json.loads(resp.read().decode())["items"]}

def ensure_tag(token, tag_name, existing_tags):
    if tag_name in existing_tags:
        return existing_tags[tag_name]
    req = urllib.request.Request(
        f"{MEALIE_URL}/api/organizers/tags",
        data=json.dumps({"name": tag_name}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req) as resp:
        new_tag = json.loads(resp.read().decode())
        existing_tags[tag_name] = new_tag
        return new_tag

def tag_recipe(token, slug, tag_names, all_tags):
    r_req = urllib.request.Request(f"{MEALIE_URL}/api/recipes/{slug}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(r_req) as resp:
        rec = json.loads(resp.read().decode())
    
    tags = [ensure_tag(token, name, all_tags) for name in tag_names]
    rec["tags"] = tags
    
    patch_req = urllib.request.Request(
        f"{MEALIE_URL}/api/recipes/{slug}",
        data=json.dumps(rec).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="PATCH"
    )
    with urllib.request.urlopen(patch_req) as resp:
        return json.loads(resp.read().decode())

def main():
    token = get_token()
    all_tags = get_all_tags(token)
    print(f"Loaded {len(all_tags)} existing tags.")

    tag_mappings = {
        "the-best-chili-ever": ["Serious Eats", "Kenji", "Comfort Food", "Slow Cook"],
        "perfect-thin-and-crispy-french-fries-recipe": ["Serious Eats", "Kenji", "Sides"],
        "classic-guacamole-recipe": ["Serious Eats", "Dips & Apps", "Quick"],
        "the-best-italian-american-tomato-sauce": ["Serious Eats", "Pasta & Sauces", "Kenji"],
        "all-american-beef-stew": ["Serious Eats", "Kenji", "Comfort Food", "Slow Cook"],
        "gochujang-buttered-noodles": ["NYT Cooking", "Eric Kim", "Weeknight", "Pasta & Sauces", "Quick"],
        "marcella-hazans-tomato-sauce": ["NYT Cooking", "Classics", "Pasta & Sauces"],
        "peruvian-style-grilled-chicken-with-green-sauce-recipe": ["Serious Eats", "Kenji", "Chicken", "Grill"],
        "the-best-crispy-roast-potatoes-ever": ["Serious Eats", "Kenji", "Sides"],
        "buttermilk-brined-roast-chicken": ["NYT Cooking", "Samin Nosrat", "Chicken", "Classics"],
        "oven-roasted-chicken-shawarma": ["NYT Cooking", "Sam Sifton", "Weeknight", "Chicken"],
        "crispy-gnocchi-with-burst-tomatoes-and-mozzarella": ["NYT Cooking", "Ali Slagle", "Weeknight", "Quick", "Vegetarian"],
        "red-lentil-soup": ["NYT Cooking", "Melissa Clark", "Soups & Stews", "Vegetarian", "Quick"],
        "original-plum-torte": ["NYT Cooking", "Marian Burros", "Baking", "Desserts", "Classics"],
        "the-best-slow-cooked-bolognese-sauce-recipe": ["Serious Eats", "Kenji", "Pasta & Sauces", "Weekend Project"],
        "foolproof-pan-pizza": ["Serious Eats", "Kenji", "Pizza", "Weekend Project"],
        "the-ultimate-smash-cheeseburger": ["Serious Eats", "Kenji", "Burgers", "Quick"],
        "spiced-chickpea-stew-with-coconut-and-turmeric": ["NYT Cooking", "Alison Roman", "Soups & Stews", "Vegetarian"],
        "serious-eats-halal-cart-style-chicken-and-rice-with-white-sauce": ["Serious Eats", "Kenji", "Weeknight", "Crowd Pleaser"]
    }

    for slug, tags in tag_mappings.items():
        try:
            res = tag_recipe(token, slug, tags, all_tags)
            print(f"✓ Tagged [{slug}] -> {[t['name'] for t in res.get('tags', [])]}")
        except Exception as e:
            print(f"✗ Failed to tag [{slug}]: {e}")

if __name__ == "__main__":
    main()
