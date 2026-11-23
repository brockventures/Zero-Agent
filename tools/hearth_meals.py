#!/usr/bin/env python3
"""Hearth Dynamic Meals CLI, Scheduling Synthesizer & Studio Ghibli Meal Card Art Generator.

Manages dynamic dinner feed synthesis and overrides for Hearth's mealie.ics feed on Host 2:
1. Priority (1): Cooked Mealie dinner
2. Priority (2): Explicit override in meal_overrides.json (delivery, dining_out, leftovers, custom, or skip)
3. Priority (3): Family Google Calendar evening dinner/dining-out event (16:30-21:00 PT, excluding
   'Dinner:' Mealie mirrors, all-day events, and routine kids pickup/dropoff/bedtime/nap events)
4. Priority (4): Auto-Leftovers for leftover_days.get(cook_date, 1) days after each cooked Mealie
   dinner (skipping any date already occupied by priorities 1-3 or an explicit 'skip' override).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure workspace roots are in sys.path
for _p in [
    "/workspace",
    "/workspace/tools",
    str(Path(__file__).resolve().parent.parent),
    str(Path(__file__).resolve().parent),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
    from tools.deliver_image import check_image
    from tools.gemini_image import generate_image_api, generate_image_subscription
except ImportError:
    import nas_config  # type: ignore[no-redef]
    from deliver_image import check_image  # type: ignore[no-redef]
    from gemini_image import (  # type: ignore[no-redef]
        generate_image_api,
        generate_image_subscription,
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hearth_meals")

PT_TZ = ZoneInfo("America/Los_Angeles")
VALID_OVERRIDE_KINDS = ("delivery", "dining_out", "leftovers", "custom", "skip")
ART_KINDS = ("leftovers", "delivery", "dining_out")

DEFAULT_LOCAL_ART_DIR = Path("/workspace/data/hearth_meal_art")
DEFAULT_IMAGE_TIMEOUT = 240

DINING_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"dinner|restaurant|supper|reservation|bistro|sushi|pizza|tacos?|ramen|"
    r"thai|indian|italian|steakhouse|pub|tavern|grill|eat(?:ing)?\s+out|dine\s+out|dining"
    r")\b",
    re.IGNORECASE,
)

ROUTINE_KIDS_EXCLUDE_RE = re.compile(
    r"\b(?:pick[\s-]?up|drop[\s-]?off|bedtime|nap)\b",
    re.IGNORECASE,
)

KIND_DEFAULT_TITLES: dict[str, str] = {
    "delivery": "Delivery Night",
    "dining_out": "Dining Out",
    "leftovers": "Leftovers",
    "custom": "Dinner",
}

KIND_EMOJIS: dict[str, str] = {
    "cooked": "🍽️",
    "delivery": "🥡",
    "dining_out": "🥂",
    "leftovers": "🍱",
    "custom": "🍽️",
}

GHIBLI_MEAL_ART_PROMPTS: dict[str, str] = {
    "leftovers": (
        "Hand-painted Studio Ghibli watercolor and gouache cel-animation background masterwork by Kazuo Oga, "
        "warm and cozy country kitchen at golden twilight, a rustic oak kitchen table and open warm-lit pantry "
        "with neatly stacked glass and ceramic containers of home-cooked comfort food, a enameled cast-iron Dutch oven "
        "simmering gently on a vintage stove, braided garlic and dried herbs hanging from timber beams, warm amber "
        "pendant lamp light, and lush potted basil on a paned window overlooking a serene evening garden. "
        "Composed in horizontal widescreen 16:9 landscape aspect ratio. "
        "STRICT ZERO-TEXT INVARIANT: No text, no words, no letters, no numbers, no labels, no signs, no watermarks, no UI."
    ),
    "delivery": (
        "Hand-painted Studio Ghibli watercolor and gouache cel-animation background masterwork by Kazuo Oga, "
        "warm and cozy timber-and-stone dining room and welcoming front veranda at golden-hour dusk, "
        "a wooden dining table arranged with plain unbranded kraft-paper takeout parcels tied with twine, "
        "steaming bamboo dim-sum baskets, and ceramic bowls of savory noodles and comfort food under glowing "
        "warm paper lanterns, a fluffy ginger cat sitting contendedly on a cushioned wooden chair, soft indigo "
        "twilight sky through the window. Composed in horizontal widescreen 16:9 landscape aspect ratio. "
        "STRICT ZERO-TEXT INVARIANT: No text, no words, no letters, no logos, no menus, no signs, no watermarks, no UI."
    ),
    "dining_out": (
        "Hand-painted Studio Ghibli watercolor and gouache cel-animation background masterwork by Kazuo Oga, "
        "charming European-Japanese neighborhood bistro garden terrace at cozy twilight, strings of glowing warm "
        "amber fairy lights and paper lanterns draped across ivy-covered brick arches and cobblestone patio, "
        "intimate wooden bistro tables set with flickering candle lanterns, sparkling water glasses, and artisanal "
        "shared plates of warm food, golden light spilling from wood-framed arched windows into the peaceful evening air. "
        "Composed in horizontal widescreen 16:9 landscape aspect ratio. "
        "STRICT ZERO-TEXT INVARIANT: No text, no words, no letters, no menus, no signboards, no numbers, no watermarks, no UI."
    ),
}


def get_remote_ical_bridge_dir() -> str:
    """Return Host 2 ical-bridge deploy directory using nas_config.DOCKER_ROOT."""
    return f"{nas_config.DOCKER_ROOT}/hearth/deploy/ical-bridge"


def get_remote_overrides_path() -> str:
    """Return Host 2 meal_overrides.json path."""
    return f"{get_remote_ical_bridge_dir()}/meal_overrides.json"


def get_remote_meal_art_dir() -> str:
    """Return Host 2 meal_art directory path."""
    return f"{get_remote_ical_bridge_dir()}/meal_art"


def _parse_iso_date(raw_date: Any) -> datetime.date | None:
    """Parse an ISO YYYY-MM-DD prefix from a string, returning None if malformed."""
    if not isinstance(raw_date, str):
        return None
    s = raw_date.strip()
    if len(s) < 10:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def format_mealie_description(recipe: dict[str, Any], detail: dict[str, Any]) -> str:
    """Format a Mealie recipe & detail dict into a concise VEVENT description."""
    parts: list[str] = []

    total_time = detail.get("totalTime") or recipe.get("totalTime")
    yield_val = detail.get("recipeYield") or recipe.get("recipeYield")
    tags = [t.get("name", "") for t in (detail.get("tags") or []) if isinstance(t, dict) and t.get("name")]
    header_bits: list[str] = []
    if total_time:
        header_bits.append(f"⏱️ {total_time}")
    if yield_val:
        header_bits.append(f"🍽️ Servings: {yield_val}")
    if tags:
        header_bits.append(f"🏷️ {', '.join(tags[:4])}")
    if header_bits:
        parts.append(" · ".join(header_bits))

    desc = (detail.get("description") or recipe.get("description") or "").strip()
    if desc:
        parts.append(desc)

    ings = detail.get("recipeIngredient") or []
    ing_lines: list[str] = []
    for ing in ings[:18]:
        if isinstance(ing, dict):
            disp = (ing.get("display") or ing.get("note") or "").strip()
            if disp:
                ing_lines.append(f"• {disp}")
    if ing_lines:
        parts.append("🛒 Ingredients:\n" + "\n".join(ing_lines))

    steps = detail.get("recipeInstructions") or []
    step_lines: list[str] = []
    for idx, st in enumerate(steps[:12], 1):
        if isinstance(st, dict):
            txt = (st.get("text") or "").strip()
            if txt:
                step_lines.append(f"{idx}. {txt}")
    if step_lines:
        parts.append("👩‍🍳 Instructions:\n" + "\n".join(step_lines))

    return "\n\n".join(parts)[:1800]


def _prefix_summary_emoji(title: str, kind: str) -> str:
    """Add kind-specific emoji prefix to title if not already prefixed."""
    clean = title.strip()
    if any(clean.startswith(e) for e in ("🍽️", "🥡", "🛵", "🥂", "🍱", "🍕", "🍣", "🌮")):
        return clean
    emoji = KIND_EMOJIS.get(kind, "🍽️")
    return f"{emoji} {clean}"


def is_family_dining_out_event(ev: dict[str, Any]) -> tuple[bool, str | None]:
    """Determine if a Family Google Calendar event represents an evening dinner/dining-out event.

    Rules:
    - Exclude cancelled events.
    - Exclude summaries starting with 'Dinner:' (created by Mealie sync).
    - Exclude all-day events (must have start.dateTime).
    - Exclude routine kids pickup/dropoff/bedtime/nap events.
    - Match evening events starting 16:30-21:00 PT with dinner/restaurant/dining-out keywords.

    Returns:
        (is_match, iso_date_str_in_pt)
    """
    if not isinstance(ev, dict):
        return False, None
    if ev.get("status") == "cancelled":
        return False, None

    summary = str(ev.get("summary") or "").strip()
    if not summary:
        return False, None

    # Exclude Mealie-synced Google Calendar events starting with 'Dinner:'
    if summary.lower().startswith("dinner:"):
        return False, None

    start = ev.get("start")
    if isinstance(start, dict):
        if "date" in start and "dateTime" not in start:
            return False, None
        dt_raw = start.get("dateTime")
    else:
        dt_raw = ev.get("dateTime") or ev.get("start_time")

    if not isinstance(dt_raw, str) or not dt_raw.strip():
        return False, None

    try:
        dt_obj = datetime.datetime.fromisoformat(dt_raw.strip())
    except ValueError:
        return False, None

    if dt_obj.tzinfo is None:
        dt_pt = dt_obj.replace(tzinfo=PT_TZ)
    else:
        dt_pt = dt_obj.astimezone(PT_TZ)

    # Evening window: 16:30 to 21:00 PT inclusive
    minutes_of_day = dt_pt.hour * 60 + dt_pt.minute
    if not ((16 * 60 + 30) <= minutes_of_day <= (21 * 60)):
        return False, None

    description = str(ev.get("description") or "").strip()
    location = str(ev.get("location") or "").strip()

    # Exclude routine kids pickup/dropoff/bedtime/nap
    if ROUTINE_KIDS_EXCLUDE_RE.search(f"{summary} {description}"):
        return False, None

    # Match dining/restaurant keywords
    combined_text = f"{summary} {location} {description}"
    if not DINING_KEYWORDS_RE.search(combined_text):
        return False, None

    return True, dt_pt.date().isoformat()


def normalize_overrides_config(overrides_cfg: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Normalize an overrides_cfg dictionary into (overrides_by_date, leftover_days_by_date)."""
    if not isinstance(overrides_cfg, dict):
        return {}, {}

    raw_overrides: dict[str, Any] = {}
    if isinstance(overrides_cfg.get("overrides"), dict):
        raw_overrides.update(overrides_cfg["overrides"])

    # Also support flat date keys at the top level of overrides_cfg for ergonomic test inputs
    for k, v in overrides_cfg.items():
        if k in ("overrides", "leftover_days"):
            continue
        if _parse_iso_date(k) is not None:
            raw_overrides.setdefault(k, v)

    raw_leftover_days = (
        overrides_cfg.get("leftover_days")
        if isinstance(overrides_cfg.get("leftover_days"), dict)
        else {}
    )

    overrides_by_date: dict[str, dict[str, Any]] = {}
    leftover_days_by_date: dict[str, int] = {}

    for d_key, days_val in raw_leftover_days.items():
        d_obj = _parse_iso_date(d_key)
        if d_obj is None:
            continue
        try:
            leftover_days_by_date[d_obj.isoformat()] = max(0, int(days_val))
        except (ValueError, TypeError):
            continue

    for d_key, ov_val in raw_overrides.items():
        d_obj = _parse_iso_date(d_key)
        if d_obj is None:
            continue
        d_iso = d_obj.isoformat()
        if isinstance(ov_val, str):
            ov_dict: dict[str, Any] = {"kind": ov_val}
        elif isinstance(ov_val, dict):
            ov_dict = dict(ov_val)
        else:
            continue

        if "leftover_days" in ov_dict and d_iso not in leftover_days_by_date:
            try:
                leftover_days_by_date[d_iso] = max(0, int(ov_dict["leftover_days"]))
            except (ValueError, TypeError):
                pass

        raw_kind = ov_dict.get("kind")
        if not raw_kind:
            continue
        kind = str(raw_kind).strip().lower()
        if kind not in VALID_OVERRIDE_KINDS:
            continue

        overrides_by_date[d_iso] = {
            "kind": kind,
            "title": str(ov_dict.get("title") or "").strip(),
            "notes": str(ov_dict.get("notes") or ov_dict.get("description") or "").strip(),
        }

    return overrides_by_date, leftover_days_by_date


def synthesize_dynamic_meals(
    mealie_events: list[dict[str, Any]] | None,
    family_events: list[dict[str, Any]] | None,
    overrides_cfg: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Synthesize dynamic meals per date enforcing strict priority (1) -> (2) -> (3) -> (4).

    Priority rules per date:
      (1) Cooked Mealie dinner
      (2) Explicit override in `overrides` dict (delivery, dining_out, leftovers, custom, or skip)
      (3) Family Google Calendar evening dinner/dining-out event (16:30-21:00 PT)
      (4) Auto-Leftovers for `leftover_days.get(cook_date, 1)` days after each cooked Mealie dinner
          (skipping any date already occupied by 1-3 or an explicit 'skip' override).

    Returns:
        Chronologically sorted dict mapping 'YYYY-MM-DD' -> synthesized meal dict.
    """
    overrides_by_date, leftover_days_by_date = normalize_overrides_config(overrides_cfg)

    meals_by_date: dict[str, dict[str, Any]] = {}
    occupied_dates: set[str] = set()
    cooked_meals: list[tuple[datetime.date, dict[str, Any]]] = []

    # --- Priority 1: Cooked Mealie dinners ---
    for item in mealie_events or []:
        if not isinstance(item, dict):
            continue
        d_obj = _parse_iso_date(item.get("date"))
        if d_obj is None:
            continue
        d_iso = d_obj.isoformat()

        recipe = item.get("recipe") if isinstance(item.get("recipe"), dict) else {}
        detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
        title = str(recipe.get("name") or item.get("title") or item.get("text") or "").strip()
        if not title:
            continue

        recipe_id = str(recipe.get("id") or detail.get("id") or item.get("recipe_id") or "").strip()
        has_img = bool(recipe.get("image") or detail.get("image") or item.get("image"))
        img_loc = str(item.get("location") or "").strip()
        if not img_loc and recipe_id and has_img:
            img_loc = f"/hearth-api/mealie/image/{recipe_id}"

        desc_text = format_mealie_description(recipe, detail)
        if not desc_text:
            desc_text = str(item.get("description") or item.get("notes") or "").strip()

        uid = f"mealie_{item.get('id', d_iso)}"
        entry = {
            "date": d_iso,
            "kind": "cooked",
            "source": "mealie",
            "title": title,
            "summary": _prefix_summary_emoji(title, "cooked"),
            "description": desc_text,
            "notes": desc_text,
            "location": img_loc,
            "uid": uid,
            "recipe_id": recipe_id,
        }
        meals_by_date[d_iso] = entry
        occupied_dates.add(d_iso)
        cooked_meals.append((d_obj, entry))

    # --- Priority 2: Explicit Overrides (delivery, dining_out, leftovers, custom, skip) ---
    for d_iso, ov in sorted(overrides_by_date.items()):
        if d_iso in occupied_dates:
            continue
        occupied_dates.add(d_iso)
        kind = ov["kind"]
        if kind == "skip":
            continue

        title = ov["title"] or KIND_DEFAULT_TITLES.get(kind, "Dinner")
        notes = ov["notes"]
        art_slug = f"art_{kind}"
        meals_by_date[d_iso] = {
            "date": d_iso,
            "kind": kind,
            "source": "override",
            "title": title,
            "summary": _prefix_summary_emoji(title, kind),
            "description": notes,
            "notes": notes,
            "location": f"/hearth-api/mealie/image/{art_slug}",
            "uid": f"mealie_override_{d_iso}",
        }

    # --- Priority 3: Family Google Calendar Evening Dinner / Dining-Out Events ---
    for ev in family_events or []:
        matched, d_iso = is_family_dining_out_event(ev)
        if not matched or not d_iso:
            continue
        if d_iso in occupied_dates:
            continue
        occupied_dates.add(d_iso)

        title = str(ev.get("summary") or "Dining Out").strip()
        loc_str = str(ev.get("location") or "").strip()
        desc_str = str(ev.get("description") or "").strip()
        desc_parts = [p for p in (loc_str, desc_str) if p]
        full_desc = " · ".join(desc_parts)

        meals_by_date[d_iso] = {
            "date": d_iso,
            "kind": "dining_out",
            "source": "family_calendar",
            "title": title,
            "summary": _prefix_summary_emoji(title, "dining_out"),
            "description": full_desc,
            "notes": full_desc,
            "location": "/hearth-api/mealie/image/art_dining_out",
            "uid": f"mealie_family_{d_iso}",
        }

    # --- Priority 4: Auto-Leftovers after each Cooked Mealie Dinner ---
    cooked_meals.sort(key=lambda pair: pair[0])
    for cook_date_obj, cooked_entry in cooked_meals:
        cook_iso = cook_date_obj.isoformat()
        num_days = leftover_days_by_date.get(cook_iso, 1)
        if num_days <= 0:
            continue

        cooked_title = cooked_entry["title"]
        for offset in range(1, num_days + 1):
            target_obj = cook_date_obj + datetime.timedelta(days=offset)
            target_iso = target_obj.isoformat()
            if target_iso in occupied_dates:
                continue
            occupied_dates.add(target_iso)

            lo_title = f"Leftovers: {cooked_title}"
            lo_desc = f"Leftovers from {cook_iso} ({cooked_title})"
            meals_by_date[target_iso] = {
                "date": target_iso,
                "kind": "leftovers",
                "source": "auto_leftovers",
                "source_date": cook_iso,
                "cook_date": cook_iso,
                "leftover_day": offset,
                "title": lo_title,
                "summary": _prefix_summary_emoji(lo_title, "leftovers"),
                "description": lo_desc,
                "notes": lo_desc,
                "location": "/hearth-api/mealie/image/art_leftovers",
                "uid": f"mealie_leftovers_{target_iso}",
            }

    return dict(sorted(meals_by_date.items()))


# ---------------------------------------------------------------------------
# Host 2 Remote Overrides Storage via tools.nas_config.run_ssh
# ---------------------------------------------------------------------------
def load_remote_overrides(remote_path: str | None = None) -> dict[str, Any]:
    """Load meal_overrides.json from Host 2 via SSH."""
    target = remote_path or get_remote_overrides_path()
    q_target = shlex.quote(target)
    cmd = f"if [ -f {q_target} ]; then cat {q_target}; else echo '{{}}'; fi"
    rc, stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, cmd, timeout=30)
    if rc != 0:
        raise RuntimeError(f"Failed reading {target} from Host 2 (rc={rc}): {stderr.strip()}")

    raw = (stdout or "").strip() or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    overrides = parsed.get("overrides") if isinstance(parsed.get("overrides"), dict) else {}
    leftover_days = (
        parsed.get("leftover_days") if isinstance(parsed.get("leftover_days"), dict) else {}
    )
    return {
        "overrides": dict(overrides),
        "leftover_days": dict(leftover_days),
    }


def save_remote_overrides(cfg: dict[str, Any], remote_path: str | None = None) -> dict[str, Any]:
    """Atomically write meal_overrides.json to Host 2 via SSH."""
    target = remote_path or get_remote_overrides_path()
    parent_dir = os.path.dirname(target)
    normalized = {
        "overrides": dict(sorted((cfg.get("overrides") or {}).items())),
        "leftover_days": dict(sorted((cfg.get("leftover_days") or {}).items())),
    }
    payload_bytes = (json.dumps(normalized, indent=2) + "\n").encode("utf-8")
    b64_payload = base64.b64encode(payload_bytes).decode("ascii")

    q_parent = shlex.quote(parent_dir)
    q_target = shlex.quote(target)
    q_tmp = shlex.quote(f"{target}.tmp")
    cmd = (
        f"mkdir -p {q_parent} && "
        f"echo {shlex.quote(b64_payload)} | base64 -d > {q_tmp} && "
        f"chmod 644 {q_tmp} && "
        f"mv -f {q_tmp} {q_target}"
    )
    rc, _stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, cmd, timeout=30)
    if rc != 0:
        raise RuntimeError(f"Failed writing {target} on Host 2 (rc={rc}): {stderr.strip()}")
    return normalized


def cmd_set_override(
    date_str: str,
    kind: str,
    title: str = "",
    notes: str = "",
    remote_path: str | None = None,
) -> dict[str, Any]:
    """Set a per-date meal override on Host 2."""
    d_obj = _parse_iso_date(date_str)
    if d_obj is None or len(date_str.strip()) != 10:
        raise ValueError(f"Invalid date {date_str!r}; expected YYYY-MM-DD")
    d_iso = d_obj.isoformat()

    norm_kind = kind.strip().lower()
    if norm_kind not in VALID_OVERRIDE_KINDS:
        raise ValueError(f"Invalid kind {kind!r}; expected one of {VALID_OVERRIDE_KINDS}")

    resolved_title = title.strip()
    if not resolved_title and norm_kind != "skip":
        resolved_title = KIND_DEFAULT_TITLES.get(norm_kind, "Dinner")

    cfg = load_remote_overrides(remote_path=remote_path)
    cfg["overrides"][d_iso] = {
        "kind": norm_kind,
        "title": resolved_title,
        "notes": notes.strip(),
    }
    return save_remote_overrides(cfg, remote_path=remote_path)


def cmd_set_leftover_days(
    date_str: str,
    days: int,
    remote_path: str | None = None,
) -> dict[str, Any]:
    """Set leftover_days count for a cooked dinner date on Host 2."""
    d_obj = _parse_iso_date(date_str)
    if d_obj is None or len(date_str.strip()) != 10:
        raise ValueError(f"Invalid date {date_str!r}; expected YYYY-MM-DD")
    if int(days) < 0:
        raise ValueError(f"Invalid days {days!r}; must be >= 0")
    d_iso = d_obj.isoformat()

    cfg = load_remote_overrides(remote_path=remote_path)
    cfg["leftover_days"][d_iso] = int(days)
    return save_remote_overrides(cfg, remote_path=remote_path)


def cmd_remove_override(
    date_str: str,
    remote_path: str | None = None,
) -> dict[str, Any]:
    """Remove any override and leftover_days entry for date_str on Host 2."""
    d_obj = _parse_iso_date(date_str)
    if d_obj is None or len(date_str.strip()) != 10:
        raise ValueError(f"Invalid date {date_str!r}; expected YYYY-MM-DD")
    d_iso = d_obj.isoformat()

    cfg = load_remote_overrides(remote_path=remote_path)
    cfg["overrides"].pop(d_iso, None)
    cfg["leftover_days"].pop(d_iso, None)
    return save_remote_overrides(cfg, remote_path=remote_path)


# ---------------------------------------------------------------------------
# Studio Ghibli Meal Card Art Generation & Host 2 Upload
# ---------------------------------------------------------------------------
def upload_art_file_to_host2(local_path: Path, remote_dir: str | None = None) -> tuple[bool, str]:
    """Upload a verified meal art JPEG to Host 2 meal_art directory via nas_config.run_ssh."""
    if not local_path.exists() or not local_path.is_file():
        return False, f"Local art file does not exist: {local_path}"

    target_dir = remote_dir or get_remote_meal_art_dir()
    remote_file = f"{target_dir}/{local_path.name}"
    raw_bytes = local_path.read_bytes()
    b64_data = base64.b64encode(raw_bytes).decode("ascii")

    q_dir = shlex.quote(target_dir)
    q_file = shlex.quote(remote_file)
    q_tmp = shlex.quote(f"{remote_file}.tmp")
    remote_cmd = (
        f"mkdir -p {q_dir} && "
        f"base64 -d > {q_tmp} && "
        f"chmod 644 {q_tmp} && "
        f"mv -f {q_tmp} {q_file} && "
        f"stat -c '%s' {q_file}"
    )
    rc, stdout, stderr = nas_config.run_ssh(
        nas_config.HOST_2_IP,
        remote_cmd,
        timeout=60,
        stdin_data=b64_data,
    )
    if rc != 0:
        return False, f"SSH upload failed for {local_path.name} (rc={rc}): {stderr.strip()}"
    return True, f"Uploaded {local_path.name} ({len(raw_bytes)} bytes) to {remote_file} (remote size={(stdout or '').strip()})"


def generate_meal_art(
    output_dir: Path | str | None = None,
    remote_dir: str | None = None,
    force: bool = False,
    no_deploy: bool = False,
    dry_run: bool = False,
    timeout: int = DEFAULT_IMAGE_TIMEOUT,
) -> dict[str, Any]:
    """Generate 3 cozy Studio Ghibli watercolor meal card images and upload to Host 2."""
    out_dir = Path(output_dir) if output_dir else DEFAULT_LOCAL_ART_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {}
    all_ok = True

    for kind in ART_KINDS:
        filename = f"{kind}.jpg"
        target_path = out_dir / filename
        prompt = GHIBLI_MEAL_ART_PROMPTS[kind]

        if dry_run:
            results[kind] = {
                "filename": filename,
                "path": str(target_path),
                "prompt": prompt,
                "valid": True,
                "dry_run": True,
            }
            continue

        # Reuse valid local image unless --force
        generated_ok = False
        if not force and target_path.exists():
            chk = check_image(target_path)
            if chk.get("valid"):
                log.info("Reusing existing verified art for %s: %s (%dx%d)", kind, target_path, chk["width"], chk["height"])
                results[kind] = {
                    "filename": filename,
                    "path": str(target_path),
                    "valid": True,
                    "reused": True,
                    "width": chk["width"],
                    "height": chk["height"],
                    "size_kb": chk["size_kb"],
                }
                generated_ok = True

        if not generated_ok:
            log.info("Generating Studio Ghibli meal art for %s -> %s", kind, target_path)
            gen_res = generate_image_subscription(
                prompt=prompt,
                aspect_ratio="16:9",
                image_name=kind,
                output_dir=str(out_dir),
                exact_filename=True,
                timeout=timeout,
            )
            if not gen_res.get("success"):
                log.warning(
                    "Subscription generation failed for %s (%s); falling back to Gemini Image REST API",
                    kind,
                    gen_res.get("error"),
                )
                gen_res = generate_image_api(
                    prompt=prompt,
                    aspect_ratio="16:9",
                    image_name=kind,
                    output_dir=str(out_dir),
                    exact_filename=True,
                    timeout=min(timeout, 90),
                    allow_paid_api=True,
                )

            if not gen_res.get("success"):
                all_ok = False
                results[kind] = {
                    "filename": filename,
                    "valid": False,
                    "error": gen_res.get("error") or "Generation failed",
                }
                continue

            produced_path = Path(gen_res["path"])
            chk = check_image(produced_path)
            if not chk.get("valid"):
                all_ok = False
                results[kind] = {
                    "filename": filename,
                    "valid": False,
                    "error": chk.get("error") or "Forensic verification failed",
                }
                continue

            results[kind] = {
                "filename": filename,
                "path": str(produced_path),
                "valid": True,
                "width": chk["width"],
                "height": chk["height"],
                "size_kb": chk["size_kb"],
                "model": gen_res.get("model"),
            }
            target_path = produced_path

        if not no_deploy:
            up_ok, up_msg = upload_art_file_to_host2(target_path, remote_dir=remote_dir)
            results[kind]["deployed"] = up_ok
            results[kind]["deploy_message"] = up_msg
            if not up_ok:
                all_ok = False

    return {
        "success": all_ok,
        "output_dir": str(out_dir),
        "remote_dir": remote_dir or get_remote_meal_art_dir(),
        "cards": results,
    }


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hearth Dynamic Meals Override Manager & Ghibli Art Generator"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_set = sub.add_parser("set", help="Set a meal override for a specific date")
    p_set.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    p_set.add_argument(
        "--kind",
        required=True,
        choices=list(VALID_OVERRIDE_KINDS),
        help="Override kind (delivery, dining_out, leftovers, custom, skip)",
    )
    p_set.add_argument("--title", default="", help="Display title for the meal card")
    p_set.add_argument("--notes", default="", help="Optional description/notes")

    p_lo = sub.add_parser("set-leftover-days", help="Configure auto-leftover days for a cooked date")
    p_lo.add_argument("--date", required=True, help="Cooked dinner date YYYY-MM-DD")
    p_lo.add_argument("--days", required=True, type=int, help="Number of leftover days (e.g. 2)")

    p_rm = sub.add_parser("remove", help="Remove meal override or leftover_days exception for a date")
    p_rm.add_argument("--date", required=True, help="Target date YYYY-MM-DD")

    sub.add_parser("list", help="List active meal overrides and leftover_days on Host 2")

    p_art = sub.add_parser("generate-art", help="Generate and deploy Studio Ghibli meal card art")
    p_art.add_argument("--output-dir", default=None, help="Local directory for generated JPEGs")
    p_art.add_argument("--force", action="store_true", help="Regenerate images even if present locally")
    p_art.add_argument("--no-deploy", action="store_true", help="Skip uploading images to Host 2")
    p_art.add_argument("--dry-run", action="store_true", help="Preview prompts without generating")
    p_art.add_argument("--timeout", type=int, default=DEFAULT_IMAGE_TIMEOUT, help="Generation timeout (s)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "set":
        cfg = cmd_set_override(
            date_str=args.date,
            kind=args.kind,
            title=args.title,
            notes=args.notes,
        )
        print(json.dumps(cfg, indent=2))
        return 0

    if args.subcommand == "set-leftover-days":
        cfg = cmd_set_leftover_days(date_str=args.date, days=args.days)
        print(json.dumps(cfg, indent=2))
        return 0

    if args.subcommand == "remove":
        cfg = cmd_remove_override(date_str=args.date)
        print(json.dumps(cfg, indent=2))
        return 0

    if args.subcommand == "list":
        cfg = load_remote_overrides()
        print(json.dumps(cfg, indent=2))
        return 0

    if args.subcommand == "generate-art":
        res = generate_meal_art(
            output_dir=args.output_dir,
            force=args.force,
            no_deploy=args.no_deploy,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("success") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
