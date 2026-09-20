#!/usr/bin/env python3
"""
Mealie Recipe Management CLI Tool
Handles recipe imports, inspections, and cache refreshes via Mealie REST API.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

CONFIG_FILE = "/secrets/env.json"

def get_config():
    base_url = os.environ.get("MEALIE_BASE_URL", "https://mealie.brock.ventures")
    token = os.environ.get("MEALIE_API_TOKEN")
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                base_url = cfg.get("MEALIE_BASE_URL", base_url)
                token = cfg.get("MEALIE_API_TOKEN", token)
        except Exception:
            pass
    return base_url.rstrip("/"), token

def api_request(path, method="GET", data=None):
    base_url, token = get_config()
    if not token:
        raise RuntimeError("MEALIE_API_TOKEN not found in /secrets/env.json or environment")
    
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode("utf-8")
        if content:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return None

def import_url(url: str, tags: list = None, categories: list = None):
    payload = {
        "url": url,
        "includeTags": True,
        "includeCategories": True
    }
    slug = api_request("/api/recipes/create/url", method="POST", data=payload)
    if isinstance(slug, str):
        slug = slug.strip('"')
    
    recipe = get_recipe(slug)
    
    if tags or categories:
        patch_payload = {}
        if tags:
            current_tags = [t.get("name") for t in recipe.get("tags", [])]
            all_tags = list(set(current_tags + tags))
            patch_payload["tags"] = [{"name": t} for t in all_tags]
        if categories:
            current_cats = [c.get("name") for c in recipe.get("recipeCategory", [])]
            all_cats = list(set(current_cats + categories))
            patch_payload["recipeCategory"] = [{"name": c} for c in all_cats]
        if patch_payload:
            recipe = api_request(f"/api/recipes/{slug}", method="PATCH", data=patch_payload)

    try:
        from tools.meal_planner_proposal import refresh_recipes_cache
        refresh_recipes_cache(force=True)
    except Exception:
        pass

    return recipe

def get_recipe(slug: str):
    return api_request(f"/api/recipes/{slug}", method="GET")

def list_recipes(per_page: int = 50):
    return api_request(f"/api/recipes?perPage={per_page}", method="GET")

def main():
    parser = argparse.ArgumentParser(description="Mealie Recipe Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_p = subparsers.add_parser("import", help="Import recipe from URL")
    import_p.add_argument("url", help="URL of recipe to scrape")
    import_p.add_argument("--tags", nargs="*", help="Optional extra tags")
    import_p.add_argument("--categories", nargs="*", help="Optional extra categories")

    get_p = subparsers.add_parser("get", help="Get recipe details by slug")
    get_p.add_argument("slug", help="Recipe slug")

    list_p = subparsers.add_parser("list", help="List recipes")
    list_p.add_argument("--limit", type=int, default=20, help="Number of recipes to fetch")

    args = parser.parse_args()

    try:
        if args.command == "import":
            res = import_url(args.url, tags=args.tags, categories=args.categories)
            base_url, _ = get_config()
            slug = res.get("slug")
            web_url = os.environ.get("MEALIE_PUBLIC_URL", "https://mealie.brock.ventures")
            print(json.dumps({
                "status": "success",
                "name": res.get("name"),
                "slug": slug,
                "url": f"{web_url}/g/home/r/{slug}",
                "total_time": res.get("totalTime"),
                "yield": res.get("recipeYield"),
                "ingredients_count": len(res.get("recipeIngredient", [])),
                "categories": [c.get("name") for c in res.get("recipeCategory", [])],
                "tags": [t.get("name") for t in res.get("tags", [])]
            }, indent=2))
        elif args.command == "get":
            res = get_recipe(args.slug)
            print(json.dumps(res, indent=2))
        elif args.command == "list":
            res = list_recipes(args.limit)
            items = res.get("items", [])
            print(f"Total recipes: {res.get('total')}, showing {len(items)}:")
            for item in items:
                print(f" - [{item.get('slug')}] {item.get('name')}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
