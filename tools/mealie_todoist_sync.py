#!/usr/bin/env python3
"""Deterministic One-Way Mealie Shopping List -> Todoist Grocery Sync Engine.

Polls unchecked items from Mealie's household shopping list, filters baseline
pantry staples for recipe-sourced items (while allowing manual non-recipe items
through), routes items to Todoist 'Whole Foods' or 'Costco' projects, skips
duplicates already open in Todoist, and checks processed items off in Mealie.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path("/workspace")
for _p in (str(REPO_ROOT), str(WORKSPACE), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.grocery_manager import (
    is_pantry_item,
    load_pantry_exclusions,
    load_staples,
    parse_raw_ingredient,
)
from tools.todoist_grocery import (
    add_grocery_item,
    get_pending_groceries,
)
from tools.whole_foods_afx import normalize_ingredient_name

CONFIG_FILE = "/secrets/env.json"
DEFAULT_MEALIE_BASE_URL = "https://mealie.brock.ventures"
DEFAULT_SHOPPING_LIST_NAME = "Shopping list"
DEFAULT_SYNC_NOTE = "Synced from Mealie"
STORE_WHOLE_FOODS = "Whole Foods"
STORE_COSTCO = "Costco"

SHOPPING_ITEM_UPDATE_FIELDS = (
    "id",
    "quantity",
    "unit",
    "food",
    "note",
    "display",
    "shoppingListId",
    "checked",
    "position",
    "foodId",
    "labelId",
    "unitId",
    "extras",
    "recipeReferences",
)


def get_config(config_file: str | Path = CONFIG_FILE) -> tuple[str, str | None]:
    """Load MEALIE_BASE_URL and MEALIE_API_TOKEN from environment or /secrets/env.json."""
    base_url = (os.environ.get("MEALIE_BASE_URL") or "").strip()
    token = (os.environ.get("MEALIE_API_TOKEN") or "").strip() or None

    cfg_path = Path(config_file)
    if (not base_url or not token) and cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
                if not base_url and cfg.get("MEALIE_BASE_URL"):
                    base_url = str(cfg["MEALIE_BASE_URL"]).strip()
                if not token and cfg.get("MEALIE_API_TOKEN"):
                    token = str(cfg["MEALIE_API_TOKEN"]).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed reading %s: %s", cfg_path, exc)

    resolved_url = (base_url or DEFAULT_MEALIE_BASE_URL).rstrip("/")
    return resolved_url, token


def api_request(
    path: str,
    method: str = "GET",
    data: dict[str, Any] | list[Any] | None = None,
) -> Any:
    """Perform an authenticated HTTP request against the Mealie REST API."""
    base_url, token = get_config()
    if not token:
        raise RuntimeError("MEALIE_API_TOKEN not found in /secrets/env.json or environment")

    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        content = resp.read().decode("utf-8")
        if content:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return None


def fetch_shopping_lists() -> list[dict[str, Any]]:
    """Fetch all household shopping lists from Mealie."""
    res = api_request("/api/households/shopping/lists", method="GET")
    if isinstance(res, dict):
        items = res.get("items", [])
        return [it for it in items if isinstance(it, dict)]
    if isinstance(res, list):
        return [it for it in res if isinstance(it, dict)]
    return []


def resolve_shopping_list_id(list_name: str = DEFAULT_SHOPPING_LIST_NAME) -> str:
    """Resolve a Mealie shopping list UUID by name, falling back to the first available list."""
    lists = fetch_shopping_lists()
    if not lists:
        raise RuntimeError("No Mealie shopping lists found")

    target = (list_name or DEFAULT_SHOPPING_LIST_NAME).strip().lower()
    for lst in lists:
        name = str(lst.get("name") or "").strip().lower()
        if name == target and lst.get("id"):
            return str(lst["id"])

    for lst in lists:
        name = str(lst.get("name") or "").strip().lower()
        if target and target in name and lst.get("id"):
            return str(lst["id"])

    first_id = lists[0].get("id")
    if not first_id:
        raise RuntimeError("Mealie shopping list entry is missing 'id'")
    return str(first_id)


def fetch_unchecked_items(list_id: str, per_page: int = 200) -> list[dict[str, Any]]:
    """Fetch all unchecked shopping list items for the given Mealie shoppingListId across pages."""
    unchecked: list[dict[str, Any]] = []
    page = 1

    while True:
        query = urllib.parse.urlencode({
            "shoppingListId": list_id,
            "perPage": per_page,
            "page": page,
        })
        res = api_request(f"/api/households/shopping/items?{query}", method="GET")
        if isinstance(res, dict):
            items = res.get("items") or []
            total_pages = int(res.get("total_pages") or res.get("totalPages") or 1)
        elif isinstance(res, list):
            items = res
            total_pages = 1
        else:
            break

        if not isinstance(items, list) or not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            item_list_id = item.get("shoppingListId")
            if item_list_id and str(item_list_id) != str(list_id):
                continue
            if not bool(item.get("checked", False)):
                unchecked.append(item)

        if page >= total_pages:
            break
        page += 1

    return unchecked


def mark_item_checked(item: dict[str, Any]) -> Any:
    """Mark a Mealie shopping list item as checked=True via PUT /api/households/shopping/items/{item_id}."""
    item_id = item.get("id")
    if not item_id:
        raise ValueError("Mealie shopping list item is missing 'id'")

    payload: dict[str, Any] = {
        field: item[field]
        for field in SHOPPING_ITEM_UPDATE_FIELDS
        if field in item
    }
    payload["checked"] = True
    return api_request(
        f"/api/households/shopping/items/{item_id}",
        method="PUT",
        data=payload,
    )


def _extract_named_field(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("name") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


def extract_item_text(item: dict[str, Any]) -> str:
    """Extract human-readable item text from 'display', falling back to quantity/unit/food/note."""
    display = str(item.get("display") or "").strip()
    if display:
        return display

    unit_name = _extract_named_field(item.get("unit"))
    food_name = _extract_named_field(item.get("food"))
    note = str(item.get("note") or "").strip()

    qty_val = item.get("quantity")
    qty_str = ""
    if qty_val is not None and qty_val != "":
        try:
            qty_num = float(qty_val)
            if qty_num > 0 and (unit_name or (food_name and qty_num != 1.0) or (not food_name and not note)):
                qty_str = str(int(qty_num)) if qty_num.is_integer() else str(round(qty_num, 2))
        except (TypeError, ValueError):
            qty_str = str(qty_val).strip()

    parts: list[str] = []
    if qty_str:
        parts.append(qty_str)
    if unit_name:
        parts.append(unit_name)
    if food_name:
        parts.append(food_name)
    if note and note.lower() != food_name.lower():
        parts.append(note)

    return " ".join(parts).strip()


def is_recipe_sourced(item: dict[str, Any]) -> bool:
    """Return True if the shopping list item originated from a Mealie recipe."""
    if item.get("recipeReferences"):
        return True
    if item.get("referencedRecipe"):
        return True
    if item.get("recipeId"):
        return True
    extras = item.get("extras")
    return bool(isinstance(extras, dict) and extras.get("recipeReferences"))


def is_pantry_filtered_item(
    item: dict[str, Any],
    raw_text: str,
    exclusions: set[str],
    exclude_pantry: bool = True,
) -> bool:
    """Return True if a recipe-sourced item is covered by baseline pantry exclusions.

    Manual (non-recipe) items always bypass pantry filtering.
    """
    if not exclude_pantry or not is_recipe_sourced(item):
        return False

    food_name = _extract_named_field(item.get("food"))
    if food_name and is_pantry_item(food_name, exclusions):
        return True

    if is_pantry_item(raw_text, exclusions):
        return True

    parsed_list = parse_raw_ingredient(raw_text)
    if parsed_list:
        return all(
            bool(p.get("is_pantry")) or is_pantry_item(str(p.get("name") or ""), exclusions)
            for p in parsed_list
        )

    return False


def route_target_store(
    raw_text: str,
    item: dict[str, Any] | None = None,
    staples: list[dict[str, Any]] | None = None,
) -> str:
    """Route item to 'Costco' if it mentions costco/kirkland or matches a Costco staple, else 'Whole Foods'."""
    item = item or {}
    food_name = _extract_named_field(item.get("food"))
    note = str(item.get("note") or "").strip()
    label_name = _extract_named_field(item.get("label"))

    combined_lower = " ".join(part for part in (raw_text, food_name, note, label_name) if part).lower()
    if "costco" in combined_lower or "kirkland" in combined_lower:
        return STORE_COSTCO

    if staples is None:
        staples = load_staples()

    parsed_names = [
        str(p.get("name") or "").strip().lower()
        for p in parse_raw_ingredient(raw_text)
        if p.get("name")
    ]
    candidates = [c for c in [raw_text.strip().lower(), food_name.lower(), *parsed_names] if c]

    for staple in staples:
        if not isinstance(staple, dict):
            continue
        if str(staple.get("store") or "").strip().lower() != "costco":
            continue

        s_name = str(staple.get("name") or "").strip().lower()
        s_name_clean = re.sub(r"\s*\([^)]*\)", "", s_name).strip()
        s_query = str(staple.get("search_query") or "").strip().lower()
        s_id = str(staple.get("id") or "").strip().lower().replace("_", " ")

        staple_terms = [t for t in (s_name, s_name_clean, s_query, s_id) if t]
        for term in staple_terms:
            for cand in candidates:
                if cand == term or term in cand or (len(cand) >= 4 and cand in term):
                    return STORE_COSTCO

    return STORE_WHOLE_FOODS


def _normalized_keys_for_text(raw_text: str, item: dict[str, Any] | None = None) -> set[str]:
    """Build a set of normalized ingredient keys for deduplication."""
    keys: set[str] = set()
    clean = raw_text.strip().lower()
    if clean:
        keys.add(clean)
        keys.add(normalize_ingredient_name(clean).strip().lower())

    if item:
        food_name = _extract_named_field(item.get("food")).lower()
        if food_name:
            keys.add(food_name)
            keys.add(normalize_ingredient_name(food_name).strip().lower())

    for parsed in parse_raw_ingredient(raw_text):
        p_name = str(parsed.get("name") or "").strip().lower()
        if p_name:
            keys.add(p_name)
            keys.add(normalize_ingredient_name(p_name).strip().lower())

    keys.discard("")
    return keys


def get_primary_normalized_name(raw_text: str, item: dict[str, Any] | None = None) -> str:
    """Return the primary normalized ingredient name for an item."""
    if item:
        food_name = _extract_named_field(item.get("food"))
        if food_name:
            return normalize_ingredient_name(food_name).strip().lower()

    parsed = parse_raw_ingredient(raw_text)
    if parsed and parsed[0].get("name"):
        return normalize_ingredient_name(str(parsed[0]["name"])).strip().lower()

    return normalize_ingredient_name(raw_text).strip().lower()


def sync_mealie_to_todoist(
    list_id: str | None = None,
    list_name: str = DEFAULT_SHOPPING_LIST_NAME,
    exclude_pantry: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Perform a deterministic one-way sync from Mealie's shopping list into Todoist."""
    result: dict[str, Any] = {
        "ok": True,
        "added": [],
        "skipped_pantry": [],
        "skipped_duplicate": [],
        "checked_in_mealie": 0,
        "errors": [],
    }

    try:
        resolved_list_id = list_id or resolve_shopping_list_id(list_name=list_name)
        unchecked_items = fetch_unchecked_items(resolved_list_id)
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["errors"].append(f"Mealie fetch failed: {exc}")
        return result

    if not unchecked_items:
        return result

    pantry_exclusions = load_pantry_exclusions() if exclude_pantry else set()
    staples = load_staples()
    pending_keys_by_store: dict[str, set[str]] = {}

    def _get_store_keys(store_title: str) -> set[str]:
        if store_title not in pending_keys_by_store:
            tasks = get_pending_groceries(title=store_title)
            keys: set[str] = set()
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                t_title = str(task.get("title") or task.get("content") or "").strip()
                if t_title:
                    keys.update(_normalized_keys_for_text(t_title))
            pending_keys_by_store[store_title] = keys
        return pending_keys_by_store[store_title]

    for item in unchecked_items:
        item_id = str(item.get("id") or "")
        item_text = extract_item_text(item)
        if not item_text:
            continue

        norm_name = get_primary_normalized_name(item_text, item=item)

        # 1. Pantry filtering (recipe-sourced items only)
        if is_pantry_filtered_item(item, item_text, pantry_exclusions, exclude_pantry=exclude_pantry):
            result["skipped_pantry"].append({
                "id": item_id,
                "title": item_text,
                "normalized": norm_name,
            })
            if not dry_run:
                try:
                    mark_item_checked(item)
                    result["checked_in_mealie"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"Failed checking off pantry item '{item_text}' ({item_id}): {exc}")
            continue

        # 2. Store routing ('Costco' vs 'Whole Foods')
        target_store = route_target_store(item_text, item=item, staples=staples)

        # 3. Deduplication against open Todoist tasks in target_store
        try:
            store_keys = _get_store_keys(target_store)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"Failed fetching Todoist '{target_store}' tasks for '{item_text}': {exc}")
            continue

        item_keys = _normalized_keys_for_text(item_text, item=item)
        if item_keys & store_keys:
            result["skipped_duplicate"].append({
                "id": item_id,
                "title": item_text,
                "normalized": norm_name,
                "store": target_store,
            })
            if not dry_run:
                try:
                    mark_item_checked(item)
                    result["checked_in_mealie"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"Failed checking off duplicate item '{item_text}' ({item_id}): {exc}")
            continue

        # 4. Push new item to Todoist & check off in Mealie
        if not dry_run:
            try:
                add_grocery_item(
                    item_text,
                    notes=DEFAULT_SYNC_NOTE,
                    list_title=target_store,
                )
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    f"Failed adding '{item_text}' to Todoist '{target_store}': {exc}"
                )
                continue

            store_keys.update(item_keys)
            result["added"].append({
                "id": item_id,
                "title": item_text,
                "normalized": norm_name,
                "store": target_store,
            })

            try:
                mark_item_checked(item)
                result["checked_in_mealie"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"Failed checking off synced item '{item_text}' ({item_id}): {exc}")
        else:
            store_keys.update(item_keys)
            result["added"].append({
                "id": item_id,
                "title": item_text,
                "normalized": norm_name,
                "store": target_store,
            })

    result["ok"] = len(result["errors"]) == 0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-way Mealie shopping list to Todoist grocery sync."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview items to add/skip without modifying Todoist or Mealie.",
    )
    parser.add_argument(
        "--no-pantry-filter",
        action="store_true",
        help="Disable baseline pantry staple filtering for recipe-sourced items.",
    )
    parser.add_argument(
        "--list-id",
        default=None,
        help="Explicit Mealie shoppingListId UUID (optional).",
    )
    parser.add_argument(
        "--list-name",
        default=DEFAULT_SHOPPING_LIST_NAME,
        help=f"Mealie shopping list name to resolve (default: {DEFAULT_SHOPPING_LIST_NAME!r}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output sync result as JSON.",
    )
    args = parser.parse_args(argv)

    res = sync_mealie_to_todoist(
        list_id=args.list_id,
        list_name=args.list_name,
        exclude_pantry=not args.no_pantry_filter,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(
            f"[mealie_todoist_sync:{mode}] ok={res['ok']} "
            f"added={len(res['added'])} "
            f"skipped_pantry={len(res['skipped_pantry'])} "
            f"skipped_duplicate={len(res['skipped_duplicate'])} "
            f"checked_in_mealie={res['checked_in_mealie']} "
            f"errors={len(res['errors'])}"
        )
        for entry in res["added"]:
            print(f"  + [{entry['store']}] {entry['title']}")
        for entry in res["skipped_pantry"]:
            print(f"  ~ [Pantry Skip] {entry['title']}")
        for entry in res["skipped_duplicate"]:
            print(f"  = [Duplicate in {entry['store']}] {entry['title']}")
        for err in res["errors"]:
            print(f"  ! [Error] {err}", file=sys.stderr)

    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
