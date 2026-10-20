#!/usr/bin/env python3
"""Unit tests for `/workspace/tools/gotchi_zero_adapter.py`."""

import base64
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.insert(0, "/workspace/gotchi")

from fastapi.testclient import TestClient

from tools.gotchi_zero_adapter import (
    GotchiZeroBrainAdapter,
    build_gotchi_zero_system_prompt,
    clean_spoken_text_for_tts,
    create_zero_gotchi_app,
    record_gotchi_turn_to_channels,
)
from poc.gotchi_poc import wrap_pcm16_to_wav


def test_clean_spoken_text_for_tts():
    raw = (
        "### Status Update\n"
        "- **Printer:** Ready at [OctoPrint](<https://198.51.100.10:8888>)!\n"
        "- Check `build_case.py` or <https://example.com/test>."
    )
    cleaned = clean_spoken_text_for_tts(raw)
    assert "###" not in cleaned
    assert "**" not in cleaned
    assert "`" not in cleaned
    assert "https://" not in cleaned
    assert "OctoPrint" in cleaned
    assert "build_case.py" in cleaned


def test_build_gotchi_zero_system_prompt_shares_zero_voice_context():
    prompt = build_gotchi_zero_system_prompt("What is on my task list?", has_vision=True)
    assert "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS" in prompt
    assert "Schedar" in prompt
    assert "live camera snapshot or 5-frame burst" in prompt
    assert "search_zero_memory" in prompt


def test_gotchi_zero_adapter_end_to_end_and_channel_logging(monkeypatch, tmp_path):
    enqueued: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: enqueued.append((int(ch), content)),
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda speaker, text, **kw: enqueued.append(("history", speaker, text, kw.get("source_tag"))),
    )
    monkeypatch.setattr(
        "tools.deliver_image.deliver_image",
        lambda path, channel_input=None, caption=None: enqueued.append(("image", channel_input, caption)),
    )

    def fake_llm(turn: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reply": "**Garage sensor** is at **68°F**. Details at [Home Assistant](<https://example.com>).",
        }

    adapter = GotchiZeroBrainAdapter(llm_caller=fake_llm, dispatch_discord=True)
    app = create_zero_gotchi_app(adapter=adapter)
    client = TestClient(app)

    wav = wrap_pcm16_to_wav(b"\x18\x00" * 8000)
    resp = client.post(
        "/api/voice/interact",
        data={
            "node_id": "gotchi-ryan-01",
            "session_id": "sess-test-42",
            "prompt": "How warm is the garage?",
        },
        files={"audio": ("ptt.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200
    assert '"text": "Garage sensor is at 68\\u00b0F."' in resp.text or "Garage sensor is at 68" in resp.text
    assert any(item[0] == 1562246590943461429 for item in enqueued if len(item) == 2)

    # Also test direct /api/gotchi/brain subscription_url endpoint with burst images
    b64_img = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode("ascii")
    brain_resp = client.post(
        "/api/gotchi/brain",
        json={
            "node_id": "gotchi-ryan-01",
            "session_id": "sess-burst-99",
            "prompt": "What part is this?",
            "burst_images_b64": [b64_img, b64_img],
        },
    )
    assert brain_resp.status_code == 200
    data = brain_resp.json()
    assert data["transcript"] == "What part is this?"
    assert "https://" not in data["reply"]
    assert len(data["sentences"]) == 2
