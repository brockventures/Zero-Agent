import json
import sqlite3
import pytest
from pathlib import Path
from tools.backfill_telemetry import (
    init_db,
    classify_conversation,
    extract_turns_from_transcript,
    backfill_transcripts,
)
from tools.zero_dashboard import (
    get_telemetry_payload,
    get_channel_rollups,
    get_daemon_rollups,
)


def test_init_db(tmp_path):
    test_db = tmp_path / "test_init.db"
    conn = sqlite3.connect(str(test_db))
    init_db(conn)

    # Verify table and columns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)").fetchall()]
    assert "is_backfill" in cols
    assert "total_tokens" in cols
    assert "conv_id" in cols
    conn.close()


def test_classify_conversation():
    # 1. Mapped channel
    chid, chname, mode = classify_conversation(
        conv_id="conv-1",
        first_prompt="hello",
        text_sample="",
        step_count=20,
        mapped_ch=(1544955535722545253, "homelab"),
    )
    assert chname == "homelab"
    assert mode == "home"

    # 2. Sidecar - session compactor
    chid, chname, mode = classify_conversation(
        conv_id="conv-2",
        first_prompt="You are Zero, synthesizing session carry-forward context for session X",
        text_sample="",
        step_count=4,
        mapped_ch=None,
    )
    assert chname == "session-compactor"
    assert mode == "sidecar"

    # 3. Crab Cavern
    chid, chname, mode = classify_conversation(
        conv_id="conv-3",
        first_prompt="Task dispatch",
        text_sample="[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT] in #the-banana-stand",
        step_count=50,
        mapped_ch=None,
    )
    assert chname == "the-banana-stand"
    assert mode == "external"


def test_extract_turns_from_transcript(tmp_path):
    tfile = tmp_path / "sample_transcript.jsonl"
    steps = [
        {"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "How are the containers?", "created_at": "2026-09-28T10:00:00Z"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "Checking containers...", "thinking": "Let me inspect docker ps", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "docker ps"}}], "created_at": "2026-09-28T10:00:02Z"},
        {"step_index": 3, "type": "TOOL_OUTPUT", "source": "SYSTEM", "content": "CONTAINER ID IMAGE STATUS\n123 ha Up", "created_at": "2026-09-28T10:00:04Z"},
        {"step_index": 4, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "All containers are healthy.", "thinking": "Output response", "tool_calls": [], "created_at": "2026-09-28T10:00:06Z"},
        # Turn 2
        {"step_index": 5, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Great, thanks.", "created_at": "2026-09-28T10:01:00Z"},
        {"step_index": 6, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "Anytime.", "thinking": "Done", "tool_calls": [], "created_at": "2026-09-28T10:01:02Z"},
    ]
    with open(tfile, "w") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")

    turns, first_prompt, sample, total_steps = extract_turns_from_transcript(str(tfile))
    assert total_steps == 6
    assert len(turns) == 2
    assert "How are the containers?" in first_prompt
    assert turns[0]["step_count"] == 4
    assert turns[1]["step_count"] == 2
    assert turns[0]["input_tokens"] > 0
    assert turns[0]["output_tokens"] > 0
    assert turns[0]["thinking_tokens"] > 0


def test_dashboard_payload_and_rollups():
    payload = get_telemetry_payload()
    assert payload["status"] == "healthy"
    assert "sessions" in payload
    assert "selected_session" in payload
    assert "channel_rollups" in payload
    assert len(payload["channel_rollups"]) > 0
    assert "daemon_rollups" in payload
    assert len(payload["daemon_rollups"]) > 0
    # Confirm no channel has 'sidecar' or starts with '#internal'
    for ch in payload["channel_rollups"]:
        assert "sidecar" not in ch["display_name"].lower()
        assert "internal" not in ch["display_name"].lower()
