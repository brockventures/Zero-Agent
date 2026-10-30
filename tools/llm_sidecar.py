#!/usr/bin/env python3
"""Central LLM Sidecar Engine, Model Tier Registry, and Cross-Container Ivy Bridge.

Governs all LLM-driven and hybrid sidecars across Zero's 5-layer scheduling stack:
- Enforces the canonical 3-Tier Sidecar Model Routing Table (`SIDECAR_MODEL_MAP`).
- Validates model IDs via `bridge_models.preflight_model` before spawning `agy`.
- Provides structured JSON and markdown synthesis helpers with deterministic fallbacks.
- Bridges fantasy baseball dilemma drafting to Ivy (`discord-ivy-agent` on Host 2) with
  live `baseball_db` (`projections` + `actual_stats`) player context pre-injection.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

# ---------------------------------------------------------------------------
# Canonical 3-Tier Sidecar Model Routing Registry
# ---------------------------------------------------------------------------
SIDECAR_MODEL_MAP: dict[str, str] = {
    # Tier 1: High-Conviction Decisions & Code Architecture (Gemini 4 Argon High)
    "kalshi_paper_bot": "gemini-4-argon-high",
    "kalshi_evening_review": "gemini-4-argon-high",
    "kalshi_night_audit": "gemini-4-argon-high",
    "monthly_hardcode_regex_audit": "gemini-4-argon-high",

    # Tier 2: Domain Synthesis, Quant Track Sweeps, Ivy Baseball Brain & Forensics (Gemini 4 Argon Medium / Low)
    "quant_daily_sweep": "gemini-4-argon-medium",
    "quant_cross_venue_arb": "gemini-4-argon-medium",
    "quant_polymarket_standalone": "gemini-4-argon-medium",
    "quant_hyperliquid_basis": "gemini-4-argon-medium",
    "quant_trend_momentum": "gemini-4-argon-medium",
    "reddit_fantasy_scanner": "gemini-4-argon-medium",
    "cash_sweep_monitor": "gemini-4-argon-medium",
    "monthly_variance_digest": "gemini-4-argon-medium",
    "weekly_social_review": "gemini-4-argon-low",
    "weekly_meal_proposal": "gemini-4-argon-low",
    "nas_logs": "gemini-4-argon-low",
    "scheduled_agent_turn": "gemini-4-argon-low",

    # Tier 3: High-Speed Curation, Filtering & Triage (Gemini 3.8 Flash Low)
    "nightly_triage": "gemini-3.8-flash-low",
    "nas_container_diag": "gemini-3.8-flash-low",
    "dreaming": "gemini-3.8-flash-low",
    "personal_data_sync": "gemini-3.8-flash-low",
    "amazon_order_listener": "gemini-3.8-flash-low",
    "resident_agents_seeding": "gemini-3.8-flash-high",
}


def get_sidecar_model(job_key: str, default: str = "gemini-4-argon-low") -> str:
    """Resolve and preflight-validate the configured model for a given sidecar job."""
    env_override = os.environ.get(f"SIDECAR_MODEL_{job_key.upper()}")
    configured = env_override or SIDECAR_MODEL_MAP.get(job_key, default)
    try:
        from tools.bridge_models import preflight_model
        valid_model, _ = preflight_model(configured)
        return valid_model or configured
    except Exception:
        return configured


def _is_test_fast_mode() -> bool:
    """Return True if running inside pytest or a unittest test runner without explicit live LLM opt-in."""
    if os.environ.get("FORCE_LLM_SIDECAR") == "1":
        return False
    if os.environ.get("LLM_SIDECAR_DISABLED") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return True
    if "unittest" in sys.modules:
        main_file = os.path.basename(getattr(sys.modules.get("__main__"), "__file__", "") or "")
        if main_file.startswith("test_") or any(k.startswith("test_") or ".test_" in k for k in sys.modules):
            return True
    return False


def extract_json_payload(raw_text: str) -> dict | list | None:
    """Robustly extract and parse a JSON object or array from LLM output."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Try locating outermost JSON object or array
    obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
    if obj_match:
        try:
            return json.loads(obj_match.group(1))
        except Exception:
            pass

    arr_match = re.search(r"(\[[\s\S]*\])", cleaned)
    if arr_match:
        try:
            return json.loads(arr_match.group(1))
        except Exception:
            pass

    return None


def call_sidecar_llm(
    job_key: str,
    prompt: str,
    model: str | None = None,
    timeout: int = 90,
) -> str:
    """Execute a single-pass headless `agy` LLM call for a sidecar task."""
    if _is_test_fast_mode():
        return ""

    resolved_model = model or get_sidecar_model(job_key)
    try:
        res = subprocess.run(
            [
                "agy",
                f"--model={resolved_model}",
                "--disable-slash-commands",
                f"-p={prompt}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (res.stdout or "").strip()
        if res.returncode == 0 and out:
            return out
    except Exception as e:
        print(f"[LLMSidecar:{job_key}] Warning calling {resolved_model}: {e}", file=sys.stderr)
    return ""


def call_sidecar_llm_json(
    job_key: str,
    prompt: str,
    model: str | None = None,
    timeout: int = 90,
) -> dict | list | None:
    """Execute a single-pass headless `agy` LLM call and parse the JSON response."""
    raw = call_sidecar_llm(job_key=job_key, prompt=prompt, model=model, timeout=timeout)
    if not raw:
        return None
    return extract_json_payload(raw)


# ---------------------------------------------------------------------------
# Quant Portfolio Manager LLM Decision Engine (Tier 1: Gemini 4 Argon High)
# ---------------------------------------------------------------------------
def evaluate_quant_candidates_llm(
    track_key: str,
    track_name: str,
    candidates: list[dict[str, Any]],
    portfolio_context: dict[str, Any],
    max_picks: int = 6,
) -> dict[str, Any]:
    """Run Gemini 4 Argon (High) as the Quant Portfolio Manager over candidate setups.

    Evaluates candidate trades for resolution-rule divergence, weather/macro regime traps,
    orderbook/liquidity quality, and correlation against existing open positions.

    Returns a dictionary:
      {
        "approved_indices": [0, 1, ...],          # 0-based indices of approved candidates
        "sizing_multipliers": {"0": 1.0, ...},    # Conviction multiplier (0.25 to 1.25)
        "rationale_by_index": {"0": "...", ...},  # Concise 1-line PM note per candidate
        "unwinds": ["ASSET_OR_TICKER", ...],      # Open positions recommended for early exit
        "pm_thesis": "..."                        # 2-3 sentence Portfolio Manager executive summary
      }
    """
    default_result = {
        "approved_indices": list(range(len(candidates))),
        "sizing_multipliers": {str(i): 1.0 for i in range(len(candidates))},
        "rationale_by_index": {},
        "unwinds": [],
        "pm_thesis": "",
        "model_used": get_sidecar_model(track_key, "gemini-4-argon-medium"),
    }
    if _is_test_fast_mode() or not candidates:
        return default_result

    # Summarize candidates compactly (cap at top 12 to keep prompt lean and fast)
    compact_candidates = []
    for idx, c in enumerate(candidates[:12]):
        item = {"index": idx}
        for k, v in c.items():
            if k in ("market_obj", "basket", "raw"):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                item[k] = v
            elif isinstance(v, dict):
                item[k] = {sk: sv for sk, sv in list(v.items())[:8] if isinstance(sv, (str, int, float, bool))}
        compact_candidates.append(item)

    prompt = (
        f"You are Zero's Quantitative Portfolio Manager running on {default_result['model_used']}.\n"
        f"Track: {track_name} ({track_key})\n\n"
        f"Current Portfolio State & Open Exposure:\n"
        f"{json.dumps(portfolio_context, indent=2, default=str)}\n\n"
        f"Deterministic Candidate Setups ({len(compact_candidates)} candidates):\n"
        f"{json.dumps(compact_candidates, indent=2, default=str)}\n\n"
        "Your job is to apply intelligent trading judgment rather than blind mechanical rules:\n"
        "1. Vet each candidate for contract resolution risk (e.g., mismatched Kalshi vs. Polymarket wording/dates), "
        "weather or macro regime traps, illiquid spread traps, or over-concentration in correlated strikes/sectors.\n"
        "2. Reject dubious or low-quality setups by omitting their index from `approved_indices`.\n"
        "3. Assign a conviction `sizing_multipliers` value between 0.35 and 1.25 for each approved index.\n"
        "4. Check open positions in Portfolio State: if any open position faces broken thesis or adverse regime reversal, list its identifier in `unwinds`.\n"
        "5. Provide a sharp, insightful 2-sentence `pm_thesis` summarizing today's market regime and capital allocation posture, "
        "plus a 1-sentence `rationale_by_index` for each candidate.\n\n"
        "Respond ONLY with valid JSON matching this schema:\n"
        "{\n"
        '  "approved_indices": [0, 1],\n'
        '  "sizing_multipliers": {"0": 1.0, "1": 0.75},\n'
        '  "rationale_by_index": {"0": "Concise thesis for trade 0", "1": "Concise thesis for trade 1"},\n'
        '  "unwinds": [],\n'
        '  "pm_thesis": "2-sentence executive summary of market regime and execution decisions."\n'
        "}"
    )

    parsed = call_sidecar_llm_json(job_key=track_key, prompt=prompt, timeout=90)
    if isinstance(parsed, dict) and "pm_thesis" in parsed:
        approved = parsed.get("approved_indices")
        if isinstance(approved, list):
            default_result["approved_indices"] = [int(i) for i in approved if str(i).isdigit() or isinstance(i, int)]
        mults = parsed.get("sizing_multipliers")
        if isinstance(mults, dict):
            clean_mults = {}
            for k, v in mults.items():
                try:
                    clean_mults[str(k)] = max(0.2, min(1.5, float(v)))
                except Exception:
                    clean_mults[str(k)] = 1.0
            default_result["sizing_multipliers"] = clean_mults
        rats = parsed.get("rationale_by_index")
        if isinstance(rats, dict):
            default_result["rationale_by_index"] = {str(k): str(v) for k, v in rats.items()}
        unwinds = parsed.get("unwinds")
        if isinstance(unwinds, list):
            default_result["unwinds"] = [str(u) for u in unwinds if u]
        default_result["pm_thesis"] = str(parsed.get("pm_thesis") or "").strip()

    return default_result


# ---------------------------------------------------------------------------
# Cross-Container Ivy Brain for Fantasy Baseball Reddit Drafts (Tier 2: Argon Medium)
# ---------------------------------------------------------------------------
NICKNAME_MAP: dict[str, str] = {
    "PCA": "Pete Crow-Armstrong",
    "MIZ": "Jacob Misiorowski",
    "ELLY": "Elly De La Cruz",
    "EDLC": "Elly De La Cruz",
    "JROD": "Julio Rodriguez",
    "J-ROD": "Julio Rodriguez",
    "CJ": "CJ Abrams",
    "VLAD": "Vladimir Guerrero Jr.",
    "VLADDY": "Vladimir Guerrero Jr.",
    "ACUNA": "Ronald Acuna Jr.",
    "TATIS": "Fernando Tatis Jr.",
    "WITT": "Bobby Witt Jr.",
    "CHOURIO": "Jackson Chourio",
    "MERRILL": "Jackson Merrill",
    "CAMINERO": "Junior Caminero",
    "KURTZ": "Nick Kurtz",
    "BENGE": "Carson Benge",
    "SKUBAL": "Tarik Skubal",
    "SKENES": "Paul Skenes",
    "WHEELER": "Zack Wheeler",
    "RAGANS": "Cole Ragans",
    "CEASE": "Dylan Cease",
    "KIRBY": "George Kirby",
    "GILBERT": "Logan Gilbert",
    "CROCHET": "Garrett Crochet",
    "SCHWELLENBACH": "Spencer Schwellenbach",
    "STRIDER": "Spencer Strider",
    "LANGFORD": "Wyatt Langford",
    "HOLLIDAY": "Jackson Holliday",
    "CREWS": "Dylan Crews",
    "JENKINS": "Walker Jenkins",
    "MAYER": "Marcelo Mayer",
    "ANTHONY": "Roman Anthony",
    "CAMPBELL": "Kristian Campbell",
    "PAINTER": "Andrew Painter",
}


def extract_fantasy_players(
    text: str,
    player_pool: list[str] | None = None,
    allow_llm_fallback: bool = True,
) -> list[str]:
    """Perform fast deterministic candidate player token matching first.

    Spawns the gemini-3.8-flash-low player extraction subprocess only if
    deterministic matching finds 0 players.
    """
    found: list[str] = []
    tokens = re.findall(r"\b[A-Za-z0-9\-']+\b", text)
    for tok in tokens:
        upper = tok.upper()
        if upper in NICKNAME_MAP:
            mapped = NICKNAME_MAP[upper]
            if mapped not in found:
                found.append(mapped)

    if player_pool:
        text_lower = text.lower()
        for p in player_pool:
            if p and len(p) > 2 and re.search(r"\b" + re.escape(p.lower()) + r"\b", text_lower):
                if p not in found:
                    found.append(p)

    if found:
        return found

    if allow_llm_fallback:
        try:
            import subprocess
            extract_prompt = (
                "Extract any MLB / fantasy baseball player names mentioned in this Reddit comment.\n\n"
                f"Comment:\n{text}\n\n"
                "Respond ONLY with valid JSON array of player names:\n[\"Full Name 1\"]"
            )
            ext_res = subprocess.run(
                ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={extract_prompt}"],
                capture_output=True, text=True, timeout=15
            )
            raw = (ext_res.stdout or "").strip()
            m = re.search(r"(\[[\s\S]*\])", raw)
            if m:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
        except Exception:
            pass

    return found


def call_ivy_fantasy_brain(
    candidate: dict[str, Any],
    model: str | None = None,
    timeout: int = 120,
    return_context: bool = False,
) -> str | tuple[str, dict[str, Any]]:
    """Delegate fantasy baseball Reddit comment drafting to Ivy (`discord-ivy-agent`) on Host 2.

    Executes inside `discord-ivy-agent` (/workspace) so Ivy can:
    1. Parse league settings from the comment (team count, keeper count, points vs roto/cats, dynasty/rebuild/contender).
    2. Resolve all mentioned players (including nicknames like PCA, Miz, Elly, J-Rod) to canonical FanGraphs IDs via `player_lookup.py`.
    3. Query 2027 BIG projections (`projections WHERE year = 2027 AND projection_system = 'big'`) and multi-year `dynasty_values`, dynamically computing league-specific 2027 Points $ and Roto $ against the parsed league replacement baseline.
    4. Draft a natural, casual 1-2 sentence dugout reply grounded in those exact league-specific dollar values.
    """
    if _is_test_fast_mode():
        return ("", {}) if return_context else ""

    resolved_model = model or get_sidecar_model("reddit_fantasy_scanner", "gemini-4-argon-medium")
    comment_body = (candidate.get("text") or candidate.get("body") or "").strip()
    thread_title = (candidate.get("thread_title") or "r/fantasybaseball Daily Thread").strip()
    author = (candidate.get("author") or "leaguemate").strip()

    if not comment_body:
        return ("", {}) if return_context else ""

    remote_payload = {
        "model": resolved_model,
        "thread_title": thread_title,
        "author": author,
        "body": comment_body,
    }
    b64_payload = base64.b64encode(json.dumps(remote_payload).encode("utf-8")).decode("ascii")

    remote_py = f"""
import base64, json, re, subprocess, sys
sys.path.insert(0, '/workspace/tools')
payload = json.loads(base64.b64decode('{b64_payload}').decode('utf-8'))
body = payload['body']
model = payload['model']

NICKNAME_MAP = {{
    'PCA': 'Pete Crow-Armstrong',
    'MIZ': 'Jacob Misiorowski',
    'ELLY': 'Elly De La Cruz',
    'EDLC': 'Elly De La Cruz',
    'JROD': 'Julio Rodriguez',
    'J-ROD': 'Julio Rodriguez',
    'CJ': 'CJ Abrams',
    'VLAD': 'Vladimir Guerrero Jr.',
    'VLADDY': 'Vladimir Guerrero Jr.',
    'ACUNA': 'Ronald Acuna Jr.',
    'TATIS': 'Fernando Tatis Jr.',
    'WITT': 'Bobby Witt Jr.',
    'CHOURIO': 'Jackson Chourio',
    'MERRILL': 'Jackson Merrill',
    'CAMINERO': 'Junior Caminero',
    'KURTZ': 'Nick Kurtz',
    'BENGE': 'Carson Benge',
    'SKUBAL': 'Tarik Skubal',
    'SKENES': 'Paul Skenes',
    'WHEELER': 'Zack Wheeler',
    'RAGANS': 'Cole Ragans',
    'CEASE': 'Dylan Cease',
    'KIRBY': 'George Kirby',
    'GILBERT': 'Logan Gilbert',
    'CROCHET': 'Garrett Crochet',
    'SCHWELLENBACH': 'Spencer Schwellenbach',
    'STRIDER': 'Spencer Strider',
    'LANGFORD': 'Wyatt Langford',
    'HOLLIDAY': 'Jackson Holliday',
    'CREWS': 'Dylan Crews',
    'JENKINS': 'Walker Jenkins',
    'MAYER': 'Marcelo Mayer',
    'ANTHONY': 'Roman Anthony',
    'CAMPBELL': 'Kristian Campbell',
    'PAINTER': 'Andrew Painter',
}}

# 1. Parse League Context from Comment
body_low = body.lower()
teams_m = re.search(r'\\b(\\d{{1,2}})\\s*(?:-|\\s)?(?:team|tm|t)\\b', body_low)
num_teams = int(teams_m.group(1)) if teams_m and 6 <= int(teams_m.group(1)) <= 30 else 12

keep_m = re.search(r'\\bkeep(?:er|ing)?\\s*(\\d{{1,2}})\\b|\\b(\\d{{1,2}})\\s*keepers?\\b', body_low)
num_keepers = int(keep_m.group(1) or keep_m.group(2)) if keep_m else None

is_points = bool(re.search(r'\\b(?:points|pts)\\b', body_low))
is_dynasty = bool(re.search(r'\\bdynasty\\b', body_low)) or (num_keepers is not None and num_keepers >= 15)
is_rebuild = bool(re.search(r'\\b(?:rebuild|rebuilding|tear down)\\b', body_low))
is_contender = bool(re.search(r'\\b(?:contend|contender|win now)\\b', body_low))

scoring_format = 'points' if is_points else 'roto_5x5'
horizon_format = 'dynasty' if is_dynasty else (f'keep_{{num_keepers}}' if num_keepers else 'redraft_or_keeper')

league_context = {{
    'teams': num_teams,
    'scoring': scoring_format,
    'horizon': horizon_format,
    'keepers_per_team': num_keepers,
    'total_kept_pool': (num_teams * num_keepers) if num_keepers else None,
}}

valuations = []
try:
    import bb_db
    from player_lookup import resolve_player
    conn = bb_db.get_connection()
    cur = conn.cursor()

    # Dynamically resolve the latest active BIG projection season year from the database
    cur.execute("SELECT COALESCE(MAX(year), 2027) FROM projections WHERE projection_system = 'big'")
    target_year = int(cur.fetchone()[0])
    league_context['projection_year'] = target_year

    # Load full BIG projection pool + dynasty_values to compute accurate league-specific replacement & dollar values
    cur.execute('''
        SELECT p.player_id, p.player_name, p.stats_type, p.primary_position, p.team,
               p.pa, p.ab, p.h, p."1b", p."2b", p."3b", p.home_runs, p.runs, p.runs_batted_in,
               p.bb, p.batter_strikeouts, p.stolen_bases, p.batting_average, p.obp, p.slg, p.ops,
               p.innings_pitched, p.gs, p.h_allowed, p.er, p.bb_allowed, p.pitcher_strikeouts,
               p.wins, p.saves, p.holds, p.qs, p.earned_run_average, p.walks_hits_per_inning_pitched,
               p.dollar_value, p.total_z,
               d.dynasty_dollar_balanced, d.dynasty_dollar_contender, d.dynasty_dollar_rebuild
        FROM projections p
        LEFT JOIN dynasty_values d ON p.player_id = d.player_id
        WHERE p.year = %s AND p.projection_system = 'big'
    ''', (target_year,))
    rows = cur.fetchall()
    cols = [c[0] for c in cur.description]
    all_pool = [dict(zip(cols, r)) for r in rows]

    for r in all_pool:
        if r['stats_type'] == 'bat':
            b1 = float(r['1b'] or 0)
            b2 = float(r['2b'] or 0)
            b3 = float(r['3b'] or 0)
            hr = float(r['home_runs'] or 0)
            h = float(r['h'] or 0)
            if b1 == 0 and h > 0:
                b1 = max(0.0, h - b2 - b3 - hr)
            tb = b1 + 2*b2 + 3*b3 + 4*hr
            pts = tb + float(r['runs'] or 0) + float(r['runs_batted_in'] or 0) + float(r['bb'] or 0) + float(r['stolen_bases'] or 0) - float(r['batter_strikeouts'] or 0)
            r['proj_points'] = round(pts, 1)
        else:
            ip = float(r['innings_pitched'] or 0)
            k = float(r['pitcher_strikeouts'] or 0)
            w = float(r['wins'] or 0)
            qs = float(r['qs'] or 0)
            sv = float(r['saves'] or 0)
            hld = float(r['holds'] or 0)
            ha = float(r['h_allowed'] or 0)
            bba = float(r['bb_allowed'] or 0)
            er = float(r['er'] or 0)
            pts = ip*3.0 + k*1.0 + w*5.0 + qs*3.0 + sv*5.0 + hld*3.0 - ha*1.0 - bba*1.0 - er*2.0
            r['proj_points'] = round(pts, 1)

    bats = sorted([r for r in all_pool if r['stats_type'] == 'bat'], key=lambda x: x['proj_points'], reverse=True)
    pits = sorted([r for r in all_pool if r['stats_type'] == 'pit'], key=lambda x: x['proj_points'], reverse=True)
    bat_Starter_count = num_teams * 13
    pit_starter_count = num_teams * 9
    rep_bat = bats[min(bat_Starter_count - 1, len(bats) - 1)]['proj_points'] if bats else 200.0
    rep_pit = pits[min(pit_starter_count - 1, len(pits) - 1)]['proj_points'] if pits else 200.0
    total_surplus = (
        sum(max(0.0, r['proj_points'] - rep_bat) for r in bats[:bat_Starter_count])
        + sum(max(0.0, r['proj_points'] - rep_pit) for r in pits[:pit_starter_count])
    )
    budget_pool = num_teams * 260 - (bat_Starter_count + pit_starter_count)

    for r in all_pool:
        rep = rep_bat if r['stats_type'] == 'bat' else rep_pit
        surplus = r['proj_points'] - rep
        r['points_dollar'] = round(1.0 + (surplus / max(1.0, total_surplus)) * budget_pool, 1)

    by_id = {{str(r['player_id']): r for r in all_pool}}
    by_name_low = {{(r['player_name'] or '').lower(): r for r in all_pool if r.get('player_name')}}

    # Compute league-specific dollar value across the entire Big Board database upfront
    for r in all_pool:
        roto_d = round(float(r.get('dollar_value') or 0.0), 1)
        pts_d = round(float(r.get('points_dollar') or 0.0), 1)
        dyn_bal = round(float(r.get('dynasty_dollar_balanced') or 0.0), 1)
        dyn_cont = round(float(r.get('dynasty_dollar_contender') or 0.0), 1)
        dyn_reb = round(float(r.get('dynasty_dollar_rebuild') or 0.0), 1)
        r['roto_d'] = roto_d
        r['pts_d'] = pts_d
        r['dyn_bal'] = dyn_bal
        if is_points:
            r['league_val'] = pts_d if not is_dynasty else round(0.6 * pts_d + 0.4 * dyn_bal, 1)
        elif is_dynasty:
            r['league_val'] = dyn_reb if is_rebuild else (dyn_cont if is_contender else dyn_bal)
        elif num_keepers:
            r['league_val'] = round(0.5 * roto_d + 0.5 * dyn_bal, 1)
        else:
            r['league_val'] = roto_d

    # 1. Fast deterministic player token matching against NICKNAME_MAP and Big Board player pool first
    extracted_players = []
    tokens = re.findall(r"\\b[A-Za-z0-9\\-']+\\b", body)
    for tok in tokens:
        upper = tok.upper()
        if upper in NICKNAME_MAP and NICKNAME_MAP[upper] not in extracted_players:
            extracted_players.append(NICKNAME_MAP[upper])

    for r in all_pool:
        p_name = r.get('player_name')
        if p_name and len(p_name) > 3 and p_name.lower() in body_low and p_name not in extracted_players:
            extracted_players.append(p_name)

    # Spawn the gemini-3.8-flash-low player extraction subprocess ONLY if deterministic matching finds 0 players
    if not extracted_players:
        extract_prompt = (
            "Extract any MLB / fantasy baseball player names or nicknames mentioned in this Reddit comment, "
            "along with the league settings.\\n\\n"
            f"Comment:\\n{{body}}\\n\\n"
            "If NO specific baseball players are mentioned (e.g. 'Tommy John' refers to the surgery, not a player on their roster), "
            "return an empty `players` list `[]`.\\n"
            "Respond ONLY with valid JSON:\\n"
            '{{"players": ["Full Name 1", "Full Name 2"], "teams": 12, "keepers": null, "scoring": "roto_5x5", "horizon": "redraft_or_keeper", "is_points": false, "is_dynasty": false, "is_rebuild": false, "is_contender": false}}'
        )
        ext_res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={{extract_prompt}}"],
            capture_output=True, text=True, timeout=30
        )
        ext_raw = (ext_res.stdout or "").strip()
        ext_match = re.search(r"(\\{{[\\s\\S]*\\}})", ext_raw)
        if ext_match:
            try:
                ext_json = json.loads(ext_match.group(1))
                if isinstance(ext_json.get("players"), list):
                    extracted_players = [str(x).strip() for x in ext_json["players"] if str(x).strip()]
                if isinstance(ext_json.get("teams"), int) and 6 <= ext_json["teams"] <= 30:
                    num_teams = ext_json["teams"]
                    league_context["teams"] = num_teams
                if ext_json.get("keepers") is not None and isinstance(ext_json.get("keepers"), int):
                    num_keepers = ext_json["keepers"]
                    league_context["keepers_per_team"] = num_keepers
                    league_context["total_kept_pool"] = num_teams * num_keepers
                if isinstance(ext_json.get("is_points"), bool):
                    is_points = ext_json["is_points"]
                    league_context["scoring"] = "points" if is_points else "roto_5x5"
                if isinstance(ext_json.get("is_dynasty"), bool):
                    is_dynasty = ext_json["is_dynasty"]
                if isinstance(ext_json.get("is_rebuild"), bool):
                    is_rebuild = ext_json["is_rebuild"]
                if isinstance(ext_json.get("is_contender"), bool):
                    is_contender = ext_json["is_contender"]
            except Exception:
                pass

    seen_fg = set()
    for tok in extracted_players:
        clean_tok = tok.strip()
        if not clean_tok:
            continue
        mapped_query = NICKNAME_MAP.get(clean_tok.upper(), clean_tok)

        # Direct exact match against active Big Board pool first, then fallback to player_lookup.resolve_player
        row = by_name_low.get(mapped_query.lower())
        if not row:
            res = resolve_player(mapped_query, active_only=True)
            p = res.get('player')
            if not p and res.get('candidates'):
                for c in res['candidates']:
                    if str(c.get('fg_id') or '') in by_id:
                        p = c
                        break
            if p and str(p.get('fg_id') or '') in by_id:
                row = by_id[str(p.get('fg_id'))]
        if not row:
            continue
        fg = str(row.get('player_id') or '')
        if not fg or fg in seen_fg:
            continue
        seen_fg.add(fg)

        valuations.append({{
            'player_name': row['player_name'],
            'pos': row['primary_position'],
            'type': row['stats_type'],
            'league_specific_dollar': row['league_val'],
            'big_roto_dollar': row['roto_d'],
            'big_points_dollar': row['pts_d'],
            'dynasty_balanced_dollar': row['dyn_bal'],
        }})
        if len(valuations) >= 14:
            break

    # Sort valuations descending by the league_specific_dollar
    valuations.sort(key=lambda x: x['league_specific_dollar'], reverse=True)
    conn.close()
except Exception as e:
    valuations.append({{'db_error': str(e)}})
    target_year = 2027

prompt = (
    "You are writing a quick, natural comment on r/fantasybaseball replying to another manager's question.\\n\\n"
    f"Thread: {{payload['thread_title']}}\\n"
    f"Comment by u/{{payload['author']}}:\\n{{body}}\\n\\n"
    f"Parsed League Context:\\n{{json.dumps(league_context)}}\\n\\n"
    f"Big Board {{target_year}} & Dynasty Player Valuations (sorted by league-specific value):\\n{{json.dumps(valuations, indent=2)}}\\n\\n"
    "Rules for writing like a real r/fantasybaseball regular (CRITICAL):\\n"
    "1. Ground your take in the Big Board valuations above: Check who actually ranks in their keeper cutoff or who wins the trade according to `league_specific_dollar`, `big_points_dollar`, `big_roto_dollar`, and `dynasty_balanced_dollar` (for example, noticing when a pitcher like Gilbert or Miz surges in points formats while a speed-heavy roto bat like PCA drops, or when Merrill/Gilbert outvalue Kurtz).\\n"
    "2. Length & Pacing: 1 to 2 short, conversational sentences (~25-45 words total). Never write long run-on sentences stuffed with 5+ names.\\n"
    "3. DO NOT recite raw stat lines or robotic exact decimals: Don't shoehorn random ERA/AVG/HR projections into the comment. If citing a dollar valuation helps make the point, round to a clean whole number (e.g. 'around $35' or 'a $30+ arm') or just state the ranking/tier takeaway naturally.\\n"
    "4. Opposing trade partner perspective: Whenever a trade is discussed, explicitly note why the other owner would or wouldn't accept it given the format.\\n"
    "5. Casual names & zero AI tells: Use last names or nicknames only ('Elly', 'Miz', 'Merrill', 'PCA', 'Caminero', 'Gilbert'). No em dashes (—), no bullet points. Output ONLY the 1-2 sentence reply."
)

res = subprocess.run(
    ["agy", f"--model={{model}}", "--disable-slash-commands", f"-p={{prompt}}"],
    capture_output=True, text=True, timeout=90
)
draft_out = (res.stdout or "").strip()
print(json.dumps({{
    "draft": draft_out,
    "league_context": league_context,
    "valuations": valuations,
}}))
"""
    b64_script = base64.b64encode(remote_py.strip().encode("utf-8")).decode("ascii")

    try:
        from tools.nas_config import resolve_nas_config
        from tools.sidecars import _ssh_cmd
        _, host2_ip, _, _, _ = resolve_nas_config()
        ssh_cmd_str = (
            f"sudo /usr/local/bin/docker exec -w /workspace discord-ivy-agent "
            f"python3 -c \"import base64; exec(base64.b64decode('{b64_script}').decode('utf-8'))\""
        )
        res = _ssh_cmd(host2_ip, ssh_cmd_str, timeout=timeout)
        raw_out = (res.stdout or "").strip()
        parsed_out = extract_json_payload(raw_out)
        if isinstance(parsed_out, dict) and parsed_out.get("draft"):
            draft = str(parsed_out["draft"]).strip()
            draft = re.sub(r"^```[a-zA-Z]*\s*", "", draft)
            draft = re.sub(r"\s*```$", "", draft).strip().strip('"')
            if len(draft.split()) >= 8:
                return (draft, parsed_out) if return_context else draft
    except Exception as e:
        print(f"[IvyBrain] Warning calling discord-ivy-agent on Host 2: {e}", file=sys.stderr)

    # Fallback to local Zero LLM if Host 2 SSH / Ivy container is temporarily unreachable
    fallback_prompt = (
        f"Write a quick, casual 1-2 sentence reply on r/fantasybaseball (~25-40 words max).\n"
        f"Comment by u/{author}:\n{comment_body}\n\n"
        "Use last names or nicknames only. Do NOT shoehorn random stats or projections. Focus on keeper/roster math and why the other owner would or wouldn't accept the trade. No em dashes, output ONLY the 1-2 sentences."
    )
    fb_draft = call_sidecar_llm("reddit_fantasy_scanner", fallback_prompt, model=resolved_model, timeout=60)
    return (fb_draft, {}) if return_context else fb_draft
