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
    assert "aerial" in active_agents
    assert "zero" in active_agents
    assert "amos" in active_agents
    assert len(active) == 3


def test_rotation_target_cycles_active_roster():
    # Day 279 -> Zero (279 % 3 == 0)
    # Day 280 -> Amos (280 % 3 == 1)
    # Day 281 -> Aerial (281 % 3 == 2)
    dt_day_279 = datetime(2026, 10, 6, 9, 30, tzinfo=PT)
    dt_day_280 = datetime(2026, 10, 7, 9, 30, tzinfo=PT)
    dt_day_281 = datetime(2026, 10, 8, 9, 30, tzinfo=PT)

    assert get_rotation_target(dt_day_279)["agent"] == "zero"
    assert get_rotation_target(dt_day_280)["agent"] == "amos"
    assert get_rotation_target(dt_day_281)["agent"] == "aerial"


def test_generate_morning_payload_v1_1_envelope():
    dt_day_279 = datetime(2026, 10, 6, 9, 30, tzinfo=PT)
    target = get_rotation_target(dt_day_279)
    payload = generate_morning_payload(target)

    assert '"v": 1.1' in payload
    assert '"kind": "finding"' in payload
    assert '"to": "all"' in payload
    assert '"floor": "open"' in payload
    assert '"max_rounds": 10' in payload
    assert "morning-topic-2026-10-06" in payload
    assert "<@&1543462881624858624> <@&1544591666671915070>" in payload
