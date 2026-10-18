#!/usr/bin/env python3
"""
Unit tests for morning_dispatcher.py rotation logic and skip list behavior.
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from morning_dispatcher import (
    ROSTER,
    SKIP_LIST,
    get_active_roster,
    get_rotation_target,
    generate_morning_payload,
)

PT = ZoneInfo("America/Los_Angeles")


def test_active_roster_excludes_benched_agents():
    active = get_active_roster()
    active_agents = {entry["agent"] for entry in active}

    assert "marvin" not in active_agents
    assert "aerial" not in active_agents
    assert "zero" in active_agents
    assert "amos" in active_agents
    assert len(active) == 2


def test_rotation_target_alternates_zero_and_amos():
    # Day 278 -> Zero (278 % 2 == 0)
    # Day 279 -> Amos (279 % 2 == 1)
    dt_day_278 = datetime(2026, 10, 5, 9, 30, tzinfo=PT)
    dt_day_279 = datetime(2026, 10, 6, 9, 30, tzinfo=PT)

    target_278 = get_rotation_target(dt_day_278)
    target_279 = get_rotation_target(dt_day_279)

    assert target_278["agent"] == "zero"
    assert target_279["agent"] == "amos"


def test_generate_morning_payload_v1_1_envelope():
    dt_day_278 = datetime(2026, 10, 5, 9, 30, tzinfo=PT)
    target = get_rotation_target(dt_day_278)
    payload = generate_morning_payload(target)

    assert '\"v\": 1.1' in payload
    assert '\"kind\": \"finding\"' in payload
    assert "morning-topic-2026-10-05" in payload
