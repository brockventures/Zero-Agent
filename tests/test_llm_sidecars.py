#!/usr/bin/env python3
"""Unit tests for the Central LLM Sidecar Engine, 3-Tier Model Routing, and Ivy Fantasy Brain."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.llm_sidecar import (
    SIDECAR_MODEL_MAP,
    call_ivy_fantasy_brain,
    evaluate_quant_candidates_llm,
    extract_json_payload,
    get_sidecar_model,
)
from tools.reddit_fantasy_scanner import format_draft_card, generate_draft_comment


def test_sidecar_model_map_tiers():
    """Verify the 3-tier model assignments across all LLM-driven sidecars."""
    # Tier 1: Gemini 4 Argon High
    for job in (
        "kalshi_paper_bot",
        "kalshi_evening_review",
        "kalshi_night_audit",
        "monthly_hardcode_regex_audit",
    ):
        assert SIDECAR_MODEL_MAP[job] == "gemini-4-argon-high"

    # Tier 2: Gemini 4 Argon Medium / Low
    for job in (
        "quant_daily_sweep",
        "quant_cross_venue_arb",
        "quant_polymarket_standalone",
        "quant_hyperliquid_basis",
        "quant_trend_momentum",
        "reddit_fantasy_scanner",
        "cash_sweep_monitor",
        "monthly_variance_digest",
    ):
        assert SIDECAR_MODEL_MAP[job] == "gemini-4-argon-medium"
    for job in ("weekly_social_review", "weekly_meal_proposal", "nas_logs"):
        assert SIDECAR_MODEL_MAP[job] == "gemini-4-argon-low"

    # Tier 3: Gemini 3.8 Flash Low
    for job in ("nightly_triage", "dreaming"):
        assert SIDECAR_MODEL_MAP[job] == "gemini-3.8-flash-low"


def test_extract_json_payload():
    """Verify markdown fence stripping and JSON extraction."""
    fenced = '```json\n{"approved_indices": [0], "pm_thesis": "Hold core exposure."}\n```'
    parsed = extract_json_payload(fenced)
    assert isinstance(parsed, dict)
    assert parsed["approved_indices"] == [0]
    assert parsed["pm_thesis"] == "Hold core exposure."


def test_evaluate_quant_candidates_llm_veto_and_sizing(monkeypatch):
    """Verify Quant PM LLM evaluator applies vetoes, conviction multipliers, and unwinds."""
    monkeypatch.setenv("FORCE_LLM_SIDECAR", "1")
    candidates = [
        {"ticker": "KXHIGHNY-26OCT14-T65", "edge": 0.08, "side": "YES"},
        {"ticker": "KXHIGHCHI-26OCT14-T70", "edge": 0.05, "side": "NO"},
    ]
    mock_llm_json = {
        "approved_indices": [0],
        "sizing_multipliers": {"0": 1.2},
        "rationale_by_index": {"0": "Clean NOAA ASOS cold front alignment."},
        "unwinds": ["STALE_POS"],
        "pm_thesis": "Selective weather deployment; vetoed Chicago due to frontal boundary uncertainty.",
    }
    with patch("tools.llm_sidecar.call_sidecar_llm_json", return_value=mock_llm_json):
        res = evaluate_quant_candidates_llm(
            "kalshi_paper_bot",
            "Kalshi Weather CLOB",
            candidates,
            {"cash": 950.0},
        )
    assert res["approved_indices"] == [0]
    assert res["sizing_multipliers"]["0"] == 1.2
    assert res["unwinds"] == ["STALE_POS"]
    assert "Selective weather deployment" in res["pm_thesis"]


def test_reddit_fantasy_scanner_ivy_delegation(monkeypatch):
    """Verify generate_draft_comment delegates to Ivy (discord-ivy-agent) and formats card."""
    monkeypatch.setenv("FORCE_LLM_SIDECAR", "1")
    expected_ivy_reply = (
        "Hanging onto Gunnar Henderson is the sharp move in a 12-team dynasty format given his age and elite barrel floor. "
        "Unless they attach a frontline starter to the package, keep the cornerstone bat and build around him."
    )
    mock_stdout = json.dumps({"draft": expected_ivy_reply, "league_context": {"teams": 12}, "valuations": []})
    mock_proc = MagicMock(returncode=0, stdout=mock_stdout, stderr="")
    with patch("tools.sidecars._ssh_cmd", return_value=mock_proc), patch(
        "tools.nas_config.resolve_nas_config", return_value=("198.51.100.10", "198.51.100.20", 22, "admin", "/key")
    ):
        draft = generate_draft_comment(
            "Should I trade Gunnar Henderson for two prospects in a 12-team dynasty league?",
            "baseball_fan",
            "Daily Anything Goes",
        )
    assert draft == expected_ivy_reply
    card = format_draft_card(
        {
            "author": "baseball_fan",
            "thread_title": "Daily Anything Goes",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/",
            "quality_score": 88,
            "score_reasons": ["Dynasty Question (+20)"],
            "text": "Should I trade Gunnar Henderson for two prospects?",
        },
        draft,
    )
    assert "Ivy Brain · Gemini 4 Argon" in card
    assert "Gunnar Henderson" in card
