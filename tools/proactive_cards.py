#!/usr/bin/env python3
"""Compatibility & forwarding module for Workday Proactive Decision Cards (`tools.proactive_cards`).

Re-exports all symbols and workflows from `tools.proactive_decision_cards` so callers
and tests referencing `tools/proactive_cards.py` resolve identically.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_CURRENT_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _CURRENT_DIR.parent
for _p in [str(_CURRENT_DIR), str(_WORKSPACE_DIR)]:
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
if "tools" in sys.modules:
    sys.modules["tools"].__path__ = [str(_CURRENT_DIR)]

# Load sibling proactive_decision_cards.py directly from this worktree directory
_pdc_path = _CURRENT_DIR / "proactive_decision_cards.py"
_spec = importlib.util.spec_from_file_location("tools.proactive_decision_cards", _pdc_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_pdc_path}")
_pdc_mod = importlib.util.module_from_spec(_spec)
sys.modules["tools.proactive_decision_cards"] = _pdc_mod
_spec.loader.exec_module(_pdc_mod)
if "tools" in sys.modules:
    setattr(sys.modules["tools"], "proactive_decision_cards", _pdc_mod)

from tools.proactive_decision_cards import (  # noqa: E402
    ACTIONABLE_COMMS_PATTERNS,
    CHANNEL_BROCK_HOUSE,
    CHANNEL_FINANCES,
    CHANNEL_HOME_ASSISTANT,
    CHANNEL_PROJECTS,
    CHANNEL_SHOPPING,
    CHANNEL_ZERO_CHAT,
    CHANNEL_ZERO_OPS,
    FORGE_DB_PATH,
    PT,
    _classify_comms_item,
    _fetch_live_budget_anomalies,
    _fetch_live_weather_data,
    _get_calendar_seen_path,
    _get_ha_credentials,
    _is_dropoff_pickup_block,
    _load_seen_calendar_keys,
    _make_calendar_issue_key,
    _parse_event_datetime,
    _parse_recipe_minutes,
    _save_seen_calendar_keys,
    compile_cart,
    generate_afx_url,
    get_due_staples,
    get_pending_groceries,
    main,
    run_actual_budget_anomaly_card,
    run_dinner_pivot_card,
    run_evening_pm_build_card,
    run_family_calendar_radar_card,
    run_inbox_rcs_action_triage_card,
    run_midweek_grocery_card,
    run_weather_energy_card,
    stage_decision,
)

__all__ = [
    "ACTIONABLE_COMMS_PATTERNS",
    "CHANNEL_BROCK_HOUSE",
    "CHANNEL_FINANCES",
    "CHANNEL_HOME_ASSISTANT",
    "CHANNEL_PROJECTS",
    "CHANNEL_SHOPPING",
    "CHANNEL_ZERO_CHAT",
    "CHANNEL_ZERO_OPS",
    "FORGE_DB_PATH",
    "PT",
    "_classify_comms_item",
    "_fetch_live_budget_anomalies",
    "_fetch_live_weather_data",
    "_get_calendar_seen_path",
    "_get_ha_credentials",
    "_is_dropoff_pickup_block",
    "_load_seen_calendar_keys",
    "_make_calendar_issue_key",
    "_parse_event_datetime",
    "_parse_recipe_minutes",
    "_save_seen_calendar_keys",
    "compile_cart",
    "generate_afx_url",
    "get_due_staples",
    "get_pending_groceries",
    "main",
    "run_actual_budget_anomaly_card",
    "run_dinner_pivot_card",
    "run_evening_pm_build_card",
    "run_family_calendar_radar_card",
    "run_inbox_rcs_action_triage_card",
    "run_midweek_grocery_card",
    "run_weather_energy_card",
    "stage_decision",
]


if __name__ == "__main__":
    main()
