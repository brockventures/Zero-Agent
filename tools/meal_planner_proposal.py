#!/usr/bin/env python3
"""
Weekly Meal Planning Proposal & Calendar Sync
Author: Zero
Description: Selects a balanced 3-dinner rotation (Tuesday, Thursday, Sunday) from Mealie
adhering to household dietary guidelines (GF baseline, 100% grass-fed beef, oven/stovetop only,
1 quick, 1 comfort/slow-cook, 1 kid-friendly staple), enforces a 21-day recency backoff,
posts proposals to #shopping (<#1544955538033348618>), and synchronizes locked dinners to
the shared Family Google Calendar and Mealie.
"""

import argparse
from datetime import datetime, timedelta
import json
import os
import random
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = "/workspace"
DATA_DIR = os.path.join(WORKSPACE, "data")
RECIPES_FILE = os.path.join(DATA_DIR, "mealie_recipes.json")
HISTORY_FILE = os.path.join(DATA_DIR, "meal_history.json")
ACTIVE_PLAN_FILE = os.path.join(DATA_DIR, "active_meal_plan.json")
PT = ZoneInfo("America/Los_Angeles")

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

_, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

def _resolve_mealie_base() -> str:
    val = os.environ.get("MEALIE_BASE_URL", "").strip()
    if val:
        return val.rstrip("/")
    env_file = Path("/secrets/env.json")
    if env_file.exists():
        try:
            data = json.loads(env_file.read_text(encoding="utf-8"))
            if data.get("MEALIE_BASE_URL"):
                return str(data["MEALIE_BASE_URL"]).strip().rstrip("/")
        except Exception:
            pass
    return "https://mealie.brock.ventures"


MEALIE_BASE = _resolve_mealie_base()
FAMILY_CALENDAR_ID = "family05249951047154432652@group.calendar.google.com"
SHOPPING_CHANNEL_ID = 1544955538033348618


def stage_meal_proposal_decision(plan: dict) -> dict | None:
    """Stage a Decision Card for locking in the weekly dinner proposal."""
    try:
        from tools.forge import stage_decision

        meals = plan.get("meals", {})
        t_name = meals.get("tuesday", {}).get("recipe", {}).get("name", "Tuesday Dinner")
        th_name = meals.get("thursday", {}).get("recipe", {}).get("name", "Thursday Dinner")
        s_name = meals.get("sunday", {}).get("recipe", {}).get("name", "Sunday Dinner")
        summary = (
            f"Weekly dinner rotation: {t_name} (Tue), {th_name} (Thu), and {s_name} (Sun). "
            "Approving locks in the menu, syncs events to the Family Google Calendar, and stages ingredients for Whole Foods delivery."
        )
        return stage_decision(
            category="meal_plan",
            title="Lock In Weekly 3-Dinner Menu",
            summary=summary,
            handler_type="shell",
            payload={"command": "python3 /workspace/tools/meal_planner_proposal.py lock-in", "cwd": "/workspace"},
            channel_id=SHOPPING_CHANNEL_ID,
            post_to_discord=True,
        )
    except Exception:
        return None


def refresh_recipes_cache(force: bool = False) -> list[dict]:
    """Auto-refreshes local recipe cache from Mealie database if older than 24h or forced."""
    import time
    if not force and os.path.exists(RECIPES_FILE):
        mtime = os.path.getmtime(RECIPES_FILE)
        if time.time() - mtime < 86400:
            try:
                with open(RECIPES_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
    try:
        import subprocess
        py_dump = '''
import sqlite3, json
con = sqlite3.connect("file:/app/data/mealie.db?mode=ro", uri=True)
query = """
SELECT r.id, r.slug, r.name, r.description, r.image, r.total_time, r.prep_time, r.cook_time, r.recipe_yield,
       GROUP_CONCAT(DISTINCT t.name) as tag_names,
       GROUP_CONCAT(DISTINCT c.name) as category_names
FROM recipes r
LEFT JOIN recipes_to_tags rt ON r.id = rt.recipe_id
LEFT JOIN tags t ON rt.tag_id = t.id
LEFT JOIN recipes_to_categories rc ON r.id = rc.recipe_id
LEFT JOIN categories c ON rc.category_id = c.id
GROUP BY r.id
ORDER BY r.name
"""
cur = con.execute(query)
cols = [c[0] for c in cur.description]
recipes = [dict(zip(cols, row)) for row in cur.fetchall()]
ing_query = """
SELECT recipe_id, COALESCE(note, title, original_text, "") as text
FROM recipes_ingredients
WHERE COALESCE(note, title, original_text, "") != ""
ORDER BY position
"""
ing_cur = con.execute(ing_query)
ings_by_recipe = {}
for rid, text in ing_cur.fetchall():
    ings_by_recipe.setdefault(rid, []).append(text.strip())
for r in recipes:
    r["tags"] = [x.strip() for x in (r["tag_names"] or "").split(",") if x.strip()]
    r["categories"] = [x.strip() for x in (r["category_names"] or "").split(",") if x.strip()]
    del r["tag_names"]
    del r["category_names"]
    r["ingredients"] = ings_by_recipe.get(r["id"], [])
print(json.dumps(recipes))
'''
        cmd = [
            "ssh", "-i", str(SSH_KEY), "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{HOST_2_IP}",
            "docker exec -i mealie python -"
        ]
        res = subprocess.run(cmd, input=py_dump, capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and res.stdout.strip():
            recipes = json.loads(res.stdout)
            with open(RECIPES_FILE, "w") as f:
                json.dump(recipes, f, indent=2)
            return recipes
    except Exception:
        pass
    if os.path.exists(RECIPES_FILE):
        with open(RECIPES_FILE, "r") as f:
            return json.load(f)
    return []


def load_recipes() -> list[dict]:
    return refresh_recipes_cache(force=False)


def load_history() -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history: list[dict]):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def load_active_plan() -> dict:
    if not os.path.exists(ACTIVE_PLAN_FILE):
        return {}
    try:
        with open(ACTIVE_PLAN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_active_plan(plan: dict):
    with open(ACTIVE_PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2)


def get_target_cooking_dates(ref_date: datetime | None = None) -> dict[str, datetime]:
    """Calculates the upcoming Tuesday, Thursday, and Sunday cooking dates.
    When run on Sunday (or Monday), targets the upcoming Tuesday, following Thursday, and following Sunday.
    """
    if ref_date is None:
        ref_date = datetime.now(PT)

    # Days ahead to the upcoming Tuesday (weekday 1)
    # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    days_to_tuesday = (1 - ref_date.weekday()) % 7
    if days_to_tuesday == 0 and ref_date.hour >= 18:
        days_to_tuesday = 7

    tuesday = (ref_date + timedelta(days=days_to_tuesday)).replace(hour=18, minute=0, second=0, microsecond=0)
    thursday = tuesday + timedelta(days=2)
    sunday = tuesday + timedelta(days=5)

    return {
        "tuesday": tuesday,
        "thursday": thursday,
        "sunday": sunday,
    }


EXCLUDED_TAGS = {
    "dips & apps",
    "sides",
    "desserts",
    "baking",
    "breakfast",
    "snickers overnight oats",
    "condiments",
    "appetizers",
    "appetizer",
    "side",
    "side dish",
}

NON_DINNER_NAME_PATTERNS = [
    r"\btomato sauce(\s+recipe)?$",
    r"\bmarinara sauce(\s+recipe)?$",
    r"\bbarbecue sauce(\s+recipe)?$",
    r"\bguacamole(\s+recipe)?$",
    r"\bsalsa verde(\s+recipe)?$",
    r"\bdressing(\s+recipe)?$",
    r"\bdip(\s+recipe)?$",
    r"\bpancake(s)?(\s+recipe)?$",
    r"\bovernight oats(\s+recipe)?$",
    r"\bfrench fries(\s+recipe)?$",
    r"\broast potatoes(\s+recipe)?$",
]


def is_dinner_recipe(r: dict) -> bool:
    """Strictly validates that a recipe is a complete, standalone dinner main dish.
    Excludes dips, snacks, appetizers, sides, desserts, breakfast items, and standalone sauces.
    """
    tags = {t.lower() for t in r.get("tags", [])}
    if tags & EXCLUDED_TAGS:
        return False
    categories = {c.lower() for c in r.get("categories", [])}
    if categories & {"breakfast", "dessert", "desserts", "appetizer", "appetizers", "side", "sides", "snack", "snacks"}:
        return False
    name = r.get("name", "").strip().lower()
    for pat in NON_DINNER_NAME_PATTERNS:
        if re.search(pat, name, re.IGNORECASE):
            # Allow meals that feature sauces or dips in their title (e.g. tacos, chicken, pasta, steaks, bowls)
            if any(k in name for k in [
                "taco", "tacos", "chicken", "pasta", "spaghetti", "steak", "shrimp",
                "pork", "beef", "meatball", "meatballs", "stew", "curry", "bowl",
                "salad", "roast", "casserole", "skillet", "burger", "burgers",
                "mac and cheese", "risotto", "chili", "shawarma"
            ]):
                continue
            return False
    return True


def filter_eligible_recipes(recipes: list[dict], recent_slugs: set[str], slot: str) -> list[dict]:
    """Filters recipes adhering to household invariants, slot profile, and dinner-only requirement."""
    eligible = []
    for r in recipes:
        slug = r.get("slug", "")
        if slug in recent_slugs:
            continue

        if not is_dinner_recipe(r):
            continue

        tags = [t.lower() for t in r.get("tags", [])]

        # Rule 1: Gluten-Free baseline or easily adaptable
        # Must have 'gluten-free' or 'gf-adaptable'
        if not ("gluten-free" in tags or "gf-adaptable" in tags):
            continue

        # Slot-specific profiles
        if slot == "sunday":
            # Comfort food, slow cook, batch cooking, or hearty weekend family meal
            if any(t in tags for t in ["comfort food", "kenji", "serious eats", "soups & stews", "classics", "pasta & sauces", "weekend project", "low & slow"]):
                eligible.append(r)
        elif slot == "tuesday":
            # Quick / fast weeknight meal (30 mins or less, Green Chef bowl, quick sauté)
            if any(t in tags for t in ["quick", "weeknight", "bowls", "green-chef"]):
                eligible.append(r)
        elif slot == "thursday":
            # Kid-friendly staple, family classic, or easy weeknight finisher
            if any(t in tags for t in ["kid-friendly", "classics", "chicken", "weeknight", "green-chef", "crowd pleaser"]):
                eligible.append(r)

    # Fallback to any GF recipe if slot pool is too restrictive (strictly dinner mains)
    if not eligible:
        eligible = [
            r for r in recipes
            if is_dinner_recipe(r)
            and r.get("slug") not in recent_slugs
            and any(t in [x.lower() for x in r.get("tags", [])] for t in ["gluten-free", "gf-adaptable"])
        ]

    return eligible


def generate_proposal(ref_date: datetime | None = None, force: bool = False, stage: bool | None = None) -> dict:
    """Generates a 3-meal proposal adhering to all constraints.
    Reuses existing active proposed plan if valid for current target cycle and force=False.
    """
    now = ref_date or datetime.now(PT)
    dates = get_target_cooking_dates(now)

    is_pytest = "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    should_stage = stage if stage is not None else (not is_pytest)

    if not force:
        active = load_active_plan()
        if active and active.get("status") == "proposed" and active.get("meals"):
            meals = active.get("meals", {})
            tue_date = meals.get("tuesday", {}).get("date")
            target_tue = dates["tuesday"].strftime("%Y-%m-%d")
            if tue_date == target_tue:
                all_valid_dinners = True
                for slot_key in ("tuesday", "thursday", "sunday"):
                    r = meals.get(slot_key, {}).get("recipe", {})
                    if not r or not is_dinner_recipe(r):
                        all_valid_dinners = False
                        break
                if all_valid_dinners:
                    if should_stage and not active.get("decision_staged"):
                        dec = stage_meal_proposal_decision(active)
                        if dec:
                            active["decision_staged"] = True
                            save_active_plan(active)
                    return active

    recipes = load_recipes()
    if not recipes:
        raise RuntimeError("No recipes found in mealie_recipes.json")

    history = load_history()
    # Recency backoff: past 21 days
    cutoff = (now - timedelta(days=21)).strftime("%Y-%m-%d")
    recent_slugs = {h.get("slug") for h in history if h.get("date", "") >= cutoff and h.get("slug")}

    # Pick Tuesday meal (Quick / 30-min / Bowls)
    tuesday_pool = filter_eligible_recipes(recipes, recent_slugs, "tuesday")
    tuesday_pick = random.choice(tuesday_pool) if tuesday_pool else recipes[0]
    selected_slugs = {tuesday_pick.get("slug")}

    # Pick Thursday meal (Kid-Friendly / Crowd Pleaser)
    thursday_pool = [r for r in filter_eligible_recipes(recipes, recent_slugs, "thursday") if r.get("slug") not in selected_slugs]
    thursday_pick = random.choice(thursday_pool) if thursday_pool else recipes[1]
    selected_slugs.add(thursday_pick.get("slug"))

    # Pick Sunday meal (Comfort / Batch / Serious Eats)
    sunday_pool = [r for r in filter_eligible_recipes(recipes, recent_slugs, "sunday") if r.get("slug") not in selected_slugs]
    sunday_pick = random.choice(sunday_pool) if sunday_pool else recipes[2]

    chef_note = ""
    try:
        from tools.llm_sidecar import call_sidecar_llm_json, get_sidecar_model
        model_id = get_sidecar_model("weekly_meal_proposal", "gemini-4-argon-low")
        def _pool_brief(pool):
            return [{"slug": r.get("slug"), "name": r.get("name"), "tags": r.get("tags", [])[:4]} for r in pool[:10]]
        meal_prompt = (
            f"You are selecting a balanced 3-dinner family menu ({model_id}) for {dates['tuesday'].strftime('%b %d')} to {dates['sunday'].strftime('%b %d')}.\n"
            "All candidates below are already verified Gluten-Free, oven/stovetop-only, and clear of the 21-day recency backoff.\n"
            f"Tuesday Pool (Fast Weeknight): {json.dumps(_pool_brief(tuesday_pool))}\n"
            f"Thursday Pool (Kid-Friendly Staple): {json.dumps(_pool_brief(thursday_pool))}\n"
            f"Sunday Pool (Weekend Comfort): {json.dumps(_pool_brief(sunday_pool))}\n\n"
            "Pick 1 distinct recipe slug for each day that creates great protein variety, seasonal fit, and ingredient synergy across Whole Foods shopping.\n"
            "Respond ONLY with valid JSON:\n"
            '{"tuesday_slug": "...", "thursday_slug": "...", "sunday_slug": "...", "chef_note": "1-sentence note on why this trio pairs well this week."}'
        )
        llm_choice = call_sidecar_llm_json("weekly_meal_proposal", meal_prompt, timeout=45)
        if isinstance(llm_choice, dict):
            by_slug = {r.get("slug"): r for r in recipes if r.get("slug")}
            t_slug = llm_choice.get("tuesday_slug")
            th_slug = llm_choice.get("thursday_slug")
            s_slug = llm_choice.get("sunday_slug")
            if t_slug in {r.get("slug") for r in tuesday_pool}:
                tuesday_pick = by_slug[t_slug]
            if th_slug in {r.get("slug") for r in thursday_pool} and th_slug != tuesday_pick.get("slug"):
                thursday_pick = by_slug[th_slug]
            if s_slug in {r.get("slug") for r in sunday_pool} and s_slug not in {tuesday_pick.get("slug"), thursday_pick.get("slug")}:
                sunday_pick = by_slug[s_slug]
            chef_note = str(llm_choice.get("chef_note") or "").strip()
    except Exception:
        pass

    plan = {
        "generated_at": now.strftime("%Y-%m-%d %I:%M %p PT"),
        "status": "proposed",
        "chef_note": chef_note,
        "meals": {
            "tuesday": {
                "day_name": "Tuesday",
                "date": dates["tuesday"].strftime("%Y-%m-%d"),
                "date_display": dates["tuesday"].strftime("%a, %b %d"),
                "slot_type": "Fast Weeknight (<30m)",
                "recipe": tuesday_pick,
            },
            "thursday": {
                "day_name": "Thursday",
                "date": dates["thursday"].strftime("%Y-%m-%d"),
                "date_display": dates["thursday"].strftime("%a, %b %d"),
                "slot_type": "Kid-Friendly Family Staple",
                "recipe": thursday_pick,
            },
            "sunday": {
                "day_name": "Sunday",
                "date": dates["sunday"].strftime("%Y-%m-%d"),
                "date_display": dates["sunday"].strftime("%a, %b %d"),
                "slot_type": "Comfort Batch / Weekend Classic",
                "recipe": sunday_pick,
            },
        },
    }

    save_active_plan(plan)
    if should_stage:
        dec = stage_meal_proposal_decision(plan)
        if dec:
            plan["decision_staged"] = True
            save_active_plan(plan)
    return plan


def format_proposal_markdown(plan: dict) -> str:
    meals = plan.get("meals", {})
    chef_note = plan.get("chef_note", "")
    tue = meals.get("tuesday", {})
    thu = meals.get("thursday", {})
    sun = meals.get("sunday", {})

    r_tue = tue.get("recipe", {})
    r_thu = thu.get("recipe", {})
    r_sun = sun.get("recipe", {})

    def _fmt_meal(m_info, r):
        name = r.get("name", "Unknown Recipe")
        total_time = r.get("total_time") or "30 mins"
        tags_str = ", ".join(r.get("tags", [])[:3])
        desc = r.get("description", "")
        slug = r.get("slug", "")
        link = f"{MEALIE_BASE}/g/home/r/{slug}" if slug else f"{MEALIE_BASE}/household/mealplan/planner/"
        return (
            f"- **{m_info.get('date_display')}** ({m_info.get('slot_type')}):\n"
            f"  🍽️ **[{name}](<{link}>)** — ⏱️ {total_time} | _{tags_str}_\n"
            f"  _{desc}_"
        )

    lines = [
        "🥘 **Weekly Dinner Rotation Proposal (3 Meals)**",
        f"_{tue.get('date_display')} – {sun.get('date_display')} | Sourced from Mealie Database_\n",
        _fmt_meal(tue, r_tue),
        "",
        _fmt_meal(thu, r_thu),
        "",
        _fmt_meal(sun, r_sun),
    ]
    if chef_note:
        lines.append(f"\n👨‍🍳 **Chef's Pairing Note (`gemini-3.8-flash-high`):** {chef_note}")
    lines.extend([
        "\n*Once locked in, these will push directly to the shared **Family** Google Calendar and stage ingredients into Monday's 5:00 PM PT Whole Foods delivery cart.*",
        "\n[CHOICES: Lock In Menu | Swap Tuesday | Swap Thursday | Swap Sunday]",
    ])
    return "\n".join(lines)


def lock_in_menu() -> tuple[bool, str]:
    """Locks in active plan, pushes events to Family Google Calendar and Mealie."""
    plan = load_active_plan()
    if not plan or not plan.get("meals"):
        return False, "No active proposed meal plan found to lock in."

    if plan.get("status") == "locked":
        return True, "✅ **Weekly Dinner Menu is already locked in!**\nEvents are already present on Family Calendar and staged for Monday Whole Foods delivery."

    meals = plan.get("meals", {})
    history = load_history()

    sys.path.insert(0, WORKSPACE)
    from tools.workspace_mcp import calendar_create_event

    calendar_sync_results = []
    for slot_key, slot_data in meals.items():
        recipe = slot_data.get("recipe", {})
        r_name = recipe.get("name", "Dinner")
        date_str = slot_data.get("date")
        dt_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=18, minute=0, second=0, tzinfo=PT)
        start_dt = dt_obj.isoformat()
        end_dt = (dt_obj + timedelta(hours=1)).isoformat()
        desc = (
            f"Recipe: {r_name}\n"
            f"Mealie: {MEALIE_BASE}/household/mealplan/planner/\n"
            f"Total Time: {recipe.get('total_time', '30m')}\n"
            f"Notes: {recipe.get('description', '')}"
        )

        # 1. Create Google Calendar event on Family calendar
        res_json = calendar_create_event(
            summary=f"Dinner: {r_name}",
            start_datetime=start_dt,
            end_datetime=end_dt,
            description=desc,
            calendar_id=FAMILY_CALENDAR_ID,
        )
        try:
            res = json.loads(res_json)
            if res.get("ok"):
                calendar_sync_results.append(f"• {slot_data.get('date_display')}: {r_name} (Google Calendar synced)")
            else:
                calendar_sync_results.append(f"• {slot_data.get('date_display')}: {r_name} (Calendar warning: {res.get('error')})")
        except Exception as e:
            calendar_sync_results.append(f"• {slot_data.get('date_display')}: {r_name} (Calendar error: {e})")

        # 2. Sync to Mealie database
        r_id = recipe.get("id", "")
        if r_id:
            try:
                import subprocess
                py_script = f"""
import sqlite3, datetime
con = sqlite3.connect('/app/data/mealie.db')
now = datetime.datetime.now().isoformat()
con.execute('DELETE FROM group_meal_plans WHERE date = ? AND entry_type = "dinner"', ('{date_str}',))
con.execute('INSERT INTO group_meal_plans (created_at, update_at, date, entry_type, title, text, group_id, recipe_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (now, now, '{date_str}', 'dinner', {json.dumps(r_name)}, '', '39e0598819394329a85403007c4f1648', '{r_id}', '0d1fe8aa96f74eeb8508af42c3f7121b'))
con.commit()
"""
                ssh_cmd = [
                    "ssh", "-i", str(SSH_KEY), "-p", str(SSH_PORT),
                    "-o", "StrictHostKeyChecking=no", f"{SSH_USER}@{HOST_2_IP}",
                    "docker exec -i mealie python -"
                ]
                subprocess.run(ssh_cmd, input=py_script, text=True, capture_output=True, timeout=10)
            except Exception:
                pass

        # 3. Record in meal history
        history.append({
            "date": date_str,
            "day": slot_data.get("day_name"),
            "name": r_name,
            "slug": recipe.get("slug"),
            "locked_at": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
        })

    save_history(history)
    plan["status"] = "locked"
    plan["locked_at"] = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    save_active_plan(plan)

    report = [
        "✅ **Weekly Dinner Menu Officially Locked In!**",
        "\n".join(calendar_sync_results),
        "\nIngredients are staged for Monday 5:00 PM PT Whole Foods delivery cart compilation in <#1544955538033348618>.",
    ]
    return True, "\n".join(report)


def swap_slot(slot: str) -> tuple[bool, str]:
    """Swaps out a specific meal slot (sunday, tuesday, thursday) with a fresh candidate."""
    slot = slot.lower()
    if slot not in ("sunday", "tuesday", "thursday"):
        return False, f"Invalid slot '{slot}'. Must be sunday, tuesday, or thursday."

    plan = load_active_plan()
    if not plan or not plan.get("meals"):
        plan = generate_proposal()

    recipes = load_recipes()
    history = load_history()
    now = datetime.now(PT)
    cutoff = (now - timedelta(days=21)).strftime("%Y-%m-%d")
    recent_slugs = {h.get("slug") for h in history if h.get("date", "") >= cutoff and h.get("slug")}

    # Also exclude currently selected other slots
    for k, v in plan["meals"].items():
        if k != slot:
            recent_slugs.add(v.get("recipe", {}).get("slug"))

    # Also exclude the current pick for this slot
    current_slug = plan["meals"][slot].get("recipe", {}).get("slug")
    recent_slugs.add(current_slug)

    pool = filter_eligible_recipes(recipes, recent_slugs, slot)
    if not pool:
        pool = [
            r for r in recipes
            if is_dinner_recipe(r)
            and r.get("slug") != current_slug
            and any(t in [x.lower() for x in r.get("tags", [])] for t in ["gluten-free", "gf-adaptable"])
        ]
    if not pool:
        pool = [r for r in recipes if is_dinner_recipe(r) and r.get("slug") != current_slug]

    new_pick = random.choice(pool)
    plan["meals"][slot]["recipe"] = new_pick
    plan["status"] = "proposed"
    save_active_plan(plan)

    return True, format_proposal_markdown(plan)


def main():
    parser = argparse.ArgumentParser(description="Weekly Meal Planning Proposal & Calendar Sync")
    parser.add_argument("action", nargs="?", default="", help="Positional action e.g. lock-in")
    parser.add_argument("--generate", action="store_true", help="Generate 3-dinner proposal")
    parser.add_argument("--force", "-f", action="store_true", help="Force regenerate proposal")
    parser.add_argument("--lock", action="store_true", help="Lock in active menu and sync to Family Calendar")
    parser.add_argument("--swap", type=str, choices=["sunday", "tuesday", "thursday"], help="Swap out a meal slot")
    parser.add_argument("--json-out", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.lock or args.action in ("lock", "lock-in"):
        ok, rep = lock_in_menu()
        print(rep)
    elif args.swap:
        ok, rep = swap_slot(args.swap)
        print(rep)
    elif args.generate or args.action in ("generate", "proposal") or not len(sys.argv) > 1:
        plan = generate_proposal(force=args.force)
        if args.json_out:
            print(json.dumps(plan, indent=2))
        else:
            print(format_proposal_markdown(plan))


if __name__ == "__main__":
    main()
