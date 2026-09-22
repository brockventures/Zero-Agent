#!/usr/bin/env python3
"""
Zero Grocery Manager
Orchestrates household staples tracking, pantry exclusion, ad-hoc item staging,
and 1-click Whole Foods AFX cart generation.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
STAPLES_FILE = WORKSPACE / "data" / "grocery_staples.json"
PANTRY_FILE = WORKSPACE / "data" / "pantry_staples.json"
PT = ZoneInfo("America/Los_Angeles")

# Import the core AFX compiler
sys.path.insert(0, str(WORKSPACE))
from tools.whole_foods_afx import generate_afx_url, normalize_ingredient_name, AFX_UNITS


def load_pantry_exclusions() -> set[str]:
    if not PANTRY_FILE.exists():
        return set()
    try:
        with open(PANTRY_FILE, "r") as f:
            data = json.load(f)
            return set(item.strip().lower() for item in data.get("excluded_items", []))
    except Exception:
        return set()


def load_staples() -> list[dict]:
    if not STAPLES_FILE.exists():
        return []
    try:
        with open(STAPLES_FILE, "r") as f:
            data = json.load(f)
            return data.get("staples", [])
    except Exception:
        return []


def save_staples(staples: list[dict]):
    with open(STAPLES_FILE, "w") as f:
        json.dump({"staples": staples}, f, indent=2)


def get_due_staples() -> list[dict]:
    """Returns staples where today >= last_purchased + cadence_days."""
    staples = load_staples()
    now_pt = datetime.now(PT).date()
    due = []

    for item in staples:
        last_str = item.get("last_purchased")
        cadence = item.get("cadence_days", 7)
        if not last_str:
            due.append(item)
            continue
        try:
            last_date = datetime.strptime(last_str, "%Y-%m-%d").date()
            days_elapsed = (now_pt - last_date).days
            if days_elapsed >= cadence:
                item_copy = dict(item)
                item_copy["days_elapsed"] = days_elapsed
                due.append(item_copy)
        except Exception:
            due.append(item)

    return due


def compile_cart(items: list[dict], exclude_pantry: bool = True) -> tuple[str, list[dict], list[str]]:
    """Filters, deduplicates, and compiles items into a 1-click Whole Foods AFX URL,
    returning (url, final_manifest, assumed_in_stock)."""
    exclusions = load_pantry_exclusions() if exclude_pantry else set()

    # Deduplicate and aggregate
    aggregated = {}
    assumed_in_stock = []
    for it in items:
        raw_name = it.get("name", "").strip()
        if not raw_name:
            continue
        
        # Check pantry exclusion
        if it.get("is_pantry") or raw_name.lower() in exclusions:
            display_name = it.get("pantry_display") or raw_name
            if display_name.lower() not in [a.lower() for a in assumed_in_stock]:
                assumed_in_stock.append(display_name)
            continue

        normalized_search = it.get("search_query") or normalize_ingredient_name(raw_name)
        key = normalized_search.lower()
        amount = float(it.get("amount", 1))
        unit = it.get("unit", "COUNT").upper()

        if key in aggregated:
            aggregated[key]["amount"] += amount
        else:
            aggregated[key] = {
                "name": normalized_search,
                "amount": amount,
                "unit": unit
            }

    final_manifest = list(aggregated.values())
    url = generate_afx_url(final_manifest)
    return url, final_manifest, assumed_in_stock


def parse_single_item(text_clean: str, is_pantry: bool = False, pantry_display: str = "") -> dict:
    import re
    text_clean = re.split(r",|\bto taste\b|\bplus more\b|\bfor serving\b|\boptional\b", text_clean, flags=re.IGNORECASE)[0].strip()
    qty_match = re.match(r"^([\d\s\/\.\-]+)\s*(.*)$", text_clean)
    qty = 1.0
    rem = text_clean
    if qty_match:
        qty_str = qty_match.group(1).strip()
        rem = qty_match.group(2).strip()
        try:
            parts = qty_str.split()
            if len(parts) == 2 and "/" in parts[1]:
                n, d = parts[1].split("/")
                qty = float(parts[0]) + float(n) / float(d)
            elif len(parts) == 1 and "/" in parts[0]:
                n, d = parts[0].split("/")
                qty = float(n) / float(d)
            elif len(parts) == 1:
                qty = float(parts[0].replace("-", "."))
        except Exception:
            qty = 1.0

    unit = "COUNT"
    unit_match = re.match(r"^(pounds?|lbs?|ounces?|oz|cups?|tablespoons?|tbsp|teaspoons?|tsp|cloves?|cans?|bottles?|bunches?|bunch|bags?|packages?|pkg|heads?)\b\s*(.*)$", rem, flags=re.IGNORECASE)
    if unit_match:
        u_raw = unit_match.group(1).lower()
        rem = unit_match.group(2).strip()
        if u_raw in ("pound", "pounds", "lb", "lbs"):
            unit = "POUND"
        elif u_raw in ("ounce", "ounces", "oz"):
            unit = "OUNCE"
        elif u_raw in ("cup", "cups"):
            unit = "CUP"
        elif u_raw in ("tablespoon", "tablespoons", "tbsp"):
            unit = "TABLESPOON"
        elif u_raw in ("teaspoon", "teaspoons", "tsp"):
            unit = "TEASPOON"
        else:
            unit = "COUNT"

    name = re.sub(r"\s+", " ", rem.strip())
    if not name:
        name = text_clean

    item_dict = {"name": name, "amount": round(qty, 2), "unit": unit}
    if is_pantry:
        item_dict["is_pantry"] = True
    if pantry_display:
        item_dict["pantry_display"] = pantry_display
    return item_dict


def parse_raw_ingredient(raw_text: str) -> list[dict]:
    text = raw_text.strip()
    if not text or text.lower() == "none" or text.startswith("---") or text.endswith(":"):
        return []

    import re
    pantry_exclusions = load_pantry_exclusions()

    # Check for proprietary spice blends / seasonings with parentheticals
    blend_match = re.search(r"^(.*?)\s*\(([^)]+)\)\s*$", text)
    if blend_match:
        title = blend_match.group(1).strip()
        sub_str = blend_match.group(2).strip()
        sub_items = [s.strip().lower() for s in sub_str.split(",") if s.strip()]
        if any(w in title.lower() for w in ["blend", "seasoning", "spice", "rub", "chermoula", "herb", "blackening", "shawarma", "curry"]):
            pantry_hits = [s for s in sub_items if any(s in p or p in s for p in pantry_exclusions)]
            if len(pantry_hits) >= max(1, len(sub_items) * 0.5):
                return [parse_single_item(title, is_pantry=True, pantry_display=f"{title} ({sub_str})")]

    # Split compound lines with '&' or ' + '
    sub_lines = []
    for part in re.split(r"\s+[\&\+]\s+", text):
        part = part.strip()
        if not part:
            continue
        # Split 'Fresh basil leaves and grated Parmesan, for serving'
        m = re.match(r"^(.*?)\s+and\s+(.*?)(,\s*for serving)?$", part, re.IGNORECASE)
        if m and not re.search(r"salt and|peeled and|cut and|drained and|halved and", part, re.IGNORECASE):
            l_item, r_item, garnish = m.group(1).strip(), m.group(2).strip(), m.group(3) or ""
            if any(w in l_item.lower() for w in ["basil", "parsley", "cilantro", "mint", "leaves"]) and any(w in r_item.lower() for w in ["cheese", "parmesan", "parmigiano", "butter"]):
                sub_lines.append(l_item + garnish)
                sub_lines.append(r_item + garnish)
                continue
        sub_lines.append(part)

    results = []
    for sl in sub_lines:
        sl_clean = re.sub(r"\([^)]*\)", "", sl).strip()
        parsed = parse_single_item(sl_clean)
        if parsed and parsed["name"]:
            results.append(parsed)
    return results


def extract_active_meal_plan_items() -> tuple[list[dict], list[str]]:
    """Reads /workspace/data/active_meal_plan.json and extracts recipe ingredients."""
    plan_file = WORKSPACE / "data" / "active_meal_plan.json"
    if not plan_file.exists():
        return [], []
    try:
        with open(plan_file, "r") as f:
            plan = json.load(f)
    except Exception:
        return [], []

    meals = plan.get("meals", {})
    if not meals:
        return [], []

    raw_items = []
    recipes_loaded = []
    for slot, data in meals.items():
        rec = data.get("recipe", {})
        r_name = rec.get("name", "Unknown")
        recipes_loaded.append(r_name)
        for line in rec.get("ingredients", []):
            parsed = parse_raw_ingredient(line)
            if isinstance(parsed, list):
                raw_items.extend(parsed)
            elif parsed:
                raw_items.append(parsed)
    return raw_items, recipes_loaded


def generate_quantity_warnings(staged_items: list[dict], manifest: list[dict]) -> list[str]:
    """Detects actionable quantity discrepancies:
    1. Multi-count packaged items where Amazon AFX landing page defaults to 1.
    2. Fractional culinary amounts that require purchasing full retail packages (surplus).
    """
    warnings = []

    # 1. Multi-count packaged goods (cans, cartons, jars, boxes) where Amazon defaults to 1
    for m in manifest:
        name_lower = m["name"].lower()
        amt = m["amount"]
        unit = m["unit"].upper()

        if unit == "COUNT" and amt > 1:
            # Exclude produce, garlic, eggs, herbs, and multi-piece meat trays where multiple count is intuitive or 1 purchase unit covers multiple
            is_canned = any(c in name_lower for c in ["canned", "peeled", "diced", "crushed", "san marzano", "paste"])
            is_ignored = not is_canned and any(p in name_lower for p in [
                "apple", "banana", "orange", "avocado", "lemon", "lime",
                "sweet potato", "potato", "onion", "shallot", "scallion", "pepper", "garlic", "ginger",
                "carrot", "tomato", "cucumber", "zucchini", "squash", "mushroom", "radish", "celery",
                "chicken breast", "egg", "herb", "parsley", "cilantro", "basil"
            ])
            if not is_ignored:
                item_disp = m["name"].title()
                warnings.append(
                    f"**{item_disp}:** Recipe needs **{int(amt)} cans/items** — Amazon landing page defaults to 1 (tap `+` on Amazon to set to {int(amt)})."
                )

    # 2. Packaged retail goods with culinary fractions (surplus)
    for it in staged_items:
        name_lower = it["name"].lower()
        amt = it.get("amount", 1)
        unit = it.get("unit", "COUNT").upper()

        if "sausage" in name_lower and unit == "POUND" and amt < 1.0:
            item_disp = it["name"].title()
            surplus_oz = int((1.0 - amt) * 16)
            warnings.append(
                f"**{item_disp}:** Ordered **1 lb pack** (Whole Foods minimum) — recipe uses **{amt} lb** (~{surplus_oz} oz surplus)."
            )

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique


def compile_weekly_manifest(include_due_staples: bool = True, include_meal_plan: bool = True, complete_tasks: bool = False) -> dict:
    """Ingests pending items from Home Assistant todo.shopping_list + active meal plan recipes + due staples,
    filters pantry exclusions, and compiles a 1-click AFX URL with quantity mismatch and Costco alerts."""
    try:
        from tools.google_tasks_grocery import get_pending_groceries, complete_task
    except ImportError:
        sys.path.insert(0, str(WORKSPACE))
        from tools.google_tasks_grocery import get_pending_groceries, complete_task

    staged_items = []
    task_records = []
    recipes_included = []

    # 1. Fetch ingredients from active meal plan (if present)
    if include_meal_plan:
        meal_items, recipes_included = extract_active_meal_plan_items()
        staged_items.extend(meal_items)
    task_records = []
    
    # 1. Fetch pending items from Home Assistant native 'todo.shopping_list'
    try:
        from tools.ha_shopping_list import get_pending_groceries as get_ha_groceries
        ha_items = get_ha_groceries()
        for it in ha_items:
            title = it.get("title", "").strip()
            if title:
                staged_items.append({"name": title, "amount": 1, "unit": "COUNT"})
                task_records.append({"id": it["id"], "source": "ha", "title": title})
    except Exception as e:
        print(f"[GroceryManager] Note: Could not fetch Home Assistant items: {e}", file=sys.stderr)

    # 2. Fetch pending items from Google Tasks '🛒 Whole Foods' (if any)
    try:
        from tools.google_tasks_grocery import get_pending_groceries as get_gtasks_groceries
        pending_tasks = get_gtasks_groceries()
        for t in pending_tasks:
            title = t.get("title", "").strip()
            # Avoid duplicate if already pulled from HA
            if title and title.lower() not in [x["name"].lower() for x in staged_items]:
                staged_items.append({"name": title, "amount": 1, "unit": "COUNT"})
                task_records.append({"id": t["id"], "source": "gtasks", "title": title})
    except Exception as e:
        print(f"[GroceryManager] Note: Could not fetch Google Tasks: {e}", file=sys.stderr)

    # 2. Add due staples & collect Costco items
    due = []
    costco_items = []
    due_wf_count = 0
    if include_due_staples:
        due = get_due_staples()
        for s in due:
            store = s.get("store")
            if store in ("whole_foods", "whole_foods_or_costco", "costco_or_wf"):
                staged_items.append({
                    "name": s["search_query"],
                    "amount": s.get("default_amount", 1),
                    "unit": s.get("unit", "COUNT")
                })
                due_wf_count += 1
            elif store == "costco":
                cadence_info = f"Due — {s.get('cadence_days')}d cadence"
                costco_items.append(f"{s['name']} ({cadence_info})")

    # Also check Google Tasks '🛒 Costco' list
    try:
        costco_tasks = get_pending_groceries(title="🛒 Costco")
        for ct in costco_tasks:
            t_title = ct.get("title", "").strip()
            if t_title and not any(t_title.lower() in c.lower() for c in costco_items):
                costco_items.append(f"{t_title} (from Google Tasks)")
    except Exception as e:
        pass

    # 3. Compile cart
    url, manifest, assumed = compile_cart(staged_items)
    
    # 4. Generate high-signal quantity & packaging warnings
    warnings = generate_quantity_warnings(staged_items, manifest)

    # 5. Optional: complete tasks
    if complete_tasks and task_records:
        for t in task_records:
            try:
                if t.get("source") == "ha":
                    from tools.ha_shopping_list import complete_grocery_item
                    complete_grocery_item(t["id"])
                elif t.get("source") == "gtasks":
                    complete_task(t["id"])
            except Exception:
                pass

    return {
        "manifest": manifest,
        "assumed_in_stock": assumed,
        "url": url,
        "tasks_ingested": len(task_records),
        "staples_ingested": due_wf_count,
        "recipes_ingested": len(recipes_included),
        "recipe_names": recipes_included,
        "quantity_warnings": warnings,
        "costco_items": costco_items,
    }


def main():
    parser = argparse.ArgumentParser(description="Zero Grocery Manager")
    parser.add_argument("--due-staples", action="store_true", help="List staples due for replenishment")
    parser.add_argument("--stage-due", action="store_true", help="Compile cart containing all due staples")
    parser.add_argument("--weekly-staging", action="store_true", help="Ingest Google Tasks + due staples and compile weekly Whole Foods cart")
    parser.add_argument("--complete-tasks", action="store_true", help="Mark ingested Google Tasks complete (use post-checkout)")
    parser.add_argument("--json-out", action="store_true", help="Output JSON format")
    args = parser.parse_args()

    if args.due_staples:
        due = get_due_staples()
        if args.json_out:
            print(json.dumps(due, indent=2))
        else:
            print(f"Found {len(due)} staple(s) due for replenishment:")
            for s in due:
                print(f"• {s['name']} ({s.get('search_query')}) - {s.get('days_elapsed', '?')} days since last order (cadence: {s.get('cadence_days')}d)")

    elif args.stage_due:
        due = get_due_staples()
        items = [
            {"name": s["search_query"], "amount": s.get("default_amount", 1), "unit": s.get("unit", "COUNT")}
            for s in due
        ]
        url, manifest, assumed = compile_cart(items)
        if args.json_out:
            print(json.dumps({"manifest": manifest, "assumed_in_stock": assumed, "url": url}, indent=2))
        else:
            print(f"Staged {len(manifest)} due staple(s):")
            for m in manifest:
                print(f"• {int(m['amount']) if m['amount'].is_integer() else m['amount']}x {m['name']} ({m['unit']})")
            if assumed:
                print(f"\nAssumed in stock ({len(assumed)}): {', '.join(assumed)}")
            print(f"\n1-Click URL:\n{url}")

    elif args.weekly_staging:
        res = compile_weekly_manifest(include_due_staples=True, complete_tasks=args.complete_tasks)
        if args.json_out:
            print(json.dumps(res, indent=2))
        else:
            manifest = res["manifest"]
            assumed = res["assumed_in_stock"]
            url = res["url"]
            print(f"🛒 **Weekly Whole Foods Manifest ({len(manifest)} Items)**")
            print(f"• Ingested: {res['tasks_ingested']} task(s) from Google Tasks, {res['staples_ingested']} due recurring staple(s)\n")
            print("### Staged Cart Items:")
            for m in manifest:
                qty = int(m['amount']) if isinstance(m['amount'], float) and m['amount'].is_integer() else m['amount']
                print(f"- {qty}x {m['name']} ({m['unit']})")
            if assumed:
                print(f"\n### 🧂 Assumed in Stock (Pantry Excluded):")
                print(", ".join(assumed))
            print(f"\n🛒 [**1-Click Whole Foods Cart**](<{url}>)")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
