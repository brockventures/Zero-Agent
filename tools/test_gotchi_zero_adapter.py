#!/usr/bin/env python3
"""Unit tests for `/workspace/tools/gotchi_zero_adapter.py`."""

import asyncio
import base64
from pathlib import Path
import sys
import time
from typing import Any, Dict, List

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.insert(0, "/workspace/gotchi")

from fastapi.testclient import TestClient

from tools.gotchi_zero_adapter import (
    DEFAULT_GOTCHI_MODEL,
    GOTCHI_THINKING_TELEMETRY,
    GotchiZeroBrainAdapter,
    build_gotchi_zero_system_prompt,
    build_thinking_config,
    clean_spoken_text_for_tts,
    create_zero_gotchi_app,
    execute_gotchi_tool_call,
    extract_and_forward_artifacts_to_zero_chat,
    is_allowed_voice_read_path,
    prefetch_zero_memory,
    select_thinking_budget,
    select_thinking_level,
    send_gotchi_artifact_to_zero_chat,
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


def test_select_thinking_budget_and_fast_memory_prefetch():
    assert DEFAULT_GOTCHI_MODEL == "gemini-3.8-flash"
    # Tier 1: Step 0 and Step 1 default to LOW / 0
    assert select_thinking_level("How warm is the garage?", step=0) == "LOW"
    assert select_thinking_level("Summarize the memory lookup", step=1) == "LOW"
    assert select_thinking_budget("How warm is the garage?", step=0) == 0
    assert select_thinking_budget("Summarize the memory lookup", step=1) == 0

    # Tier 2: Escalates to MEDIUM / 512 on tool error, Step 2+, or 5-frame camera burst
    assert select_thinking_level("Retry after error", step=1, tool_had_error=True) == "MEDIUM"
    assert select_thinking_level("Chained multi-tool step", step=2) == "MEDIUM"
    assert select_thinking_level("Burst analysis", step=0, burst_count=5) == "MEDIUM"
    assert select_thinking_budget("Retry after error", step=1, tool_had_error=True) == 512
    assert select_thinking_budget("Chained multi-tool step", step=2) == 512
    assert select_thinking_budget("Burst analysis", step=0, burst_count=5) == 512

    # Tier 3: Escalates to HIGH / 1024 when model invokes request_deep_thinking
    prev_high = GOTCHI_THINKING_TELEMETRY["HIGH"]
    assert (
        select_thinking_level(
            "Deep battery tradeoff",
            step=1,
            model_requested_deep_thinking=True,
        )
        == "HIGH"
    )
    assert GOTCHI_THINKING_TELEMETRY["HIGH"] == prev_high + 1
    assert (
        select_thinking_budget(
            "Deep battery tradeoff",
            step=1,
            model_requested_deep_thinking=True,
        )
        == 1024
    )

    cfg_38_low = build_thinking_config("gemini-3.8-flash", "Quick status check", step=1)
    assert str(cfg_38_low.thinking_level).endswith("LOW")
    cfg_38_med = build_thinking_config("gemini-3.8-flash", "Step 2 chain", step=2)
    assert str(cfg_38_med.thinking_level).endswith("MEDIUM")
    cfg_38_high = build_thinking_config(
        "gemini-3.8-flash",
        "Deep math",
        step=1,
        model_requested_deep_thinking=True,
    )
    assert str(cfg_38_high.thinking_level).endswith("HIGH")

    # Warm up connection on first call, then verify sub-50ms warm recall
    _ = prefetch_zero_memory("gotchi hardware companion")
    t0 = time.monotonic()
    _ = prefetch_zero_memory("gotchi hardware companion")
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    assert elapsed_ms < 50.0


def test_build_gotchi_zero_system_prompt_shares_zero_voice_context():
    prompt = build_gotchi_zero_system_prompt("What is on my task list?", has_vision=True)
    assert "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS" in prompt
    assert "Schedar" in prompt
    assert "live camera snapshot or 5-frame burst" in prompt
    assert "post_note_to_zero_chat" in prompt
    assert "request_deep_thinking(spoken_preamble, reason)" in prompt
    assert "PRE-TOOL SPOKEN PREAMBLE" in prompt


def test_voice_security_gate_and_realpath_read_allowlist(monkeypatch, tmp_path):
    staged_msgs: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: staged_msgs.append((int(ch), content)),
    )

    # 1. All run_shell_command and write_local_file calls on voice turns stage in #zero-chat (1542081375287640084)
    shell_res = asyncio.run(
        execute_gotchi_tool_call("run_shell_command", {"command": "git status -s"})
    )
    assert shell_res["confirmation_required"] is True
    assert shell_res["status"] == "staged_in_zero_chat"
    assert any(ch == 1542081375287640084 and "git status -s" in body for ch, body in staged_msgs)

    target_write = tmp_path / "should_not_exist.txt"
    write_res = asyncio.run(
        execute_gotchi_tool_call(
            "write_local_file",
            {"file_path": str(target_write), "content": "unconfirmed write"},
        )
    )
    assert write_res["confirmation_required"] is True
    assert not target_write.exists()

    # 2. read_local_file allowlist resolves os.path.realpath BEFORE checking prefixes and blocks .env / .ssh / traversal
    ok_readme, resolved_readme = is_allowed_voice_read_path("/workspace/gotchi/README.md")
    assert ok_readme is True
    assert resolved_readme.endswith("/gotchi/README.md")

    # Rejects traversal outside /workspace/ or /baseball/
    ok_trav, _ = is_allowed_voice_read_path("/workspace/../etc/passwd")
    assert ok_trav is False

    # Rejects .env and .ssh by basename and path segment
    ok_env, _ = is_allowed_voice_read_path("/workspace/.env")
    assert ok_env is False
    ok_env_prod, _ = is_allowed_voice_read_path("/workspace/gotchi/.env.production")
    assert ok_env_prod is False
    ok_ssh, _ = is_allowed_voice_read_path("/workspace/.ssh/id_ed25519")
    assert ok_ssh is False

    # Rejects symlink inside allowed root pointing to .env or outside root
    ws_FakeRoot = tmp_path / "workspace"
    ws_FakeRoot.mkdir()
    secret_env = ws_FakeRoot / ".env"
    secret_env.write_text("SECRET=123")
    symlink_to_env = ws_FakeRoot / "innocent_notes.txt"
    symlink_to_env.symlink_to(secret_env)
    ok_sym, reason_sym = is_allowed_voice_read_path(
        str(symlink_to_env),
        allowed_prefixes=(str(ws_FakeRoot),),
    )
    assert ok_sym is False
    assert ".env" in reason_sym

    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("OUTSIDE=1")
    symlink_outside = ws_FakeRoot / "escape.txt"
    symlink_outside.symlink_to(outside_file)
    ok_escape, reason_escape = is_allowed_voice_read_path(
        str(symlink_outside),
        allowed_prefixes=(str(ws_FakeRoot),),
    )
    assert ok_escape is False
    assert "outside allowed voice read roots" in reason_escape


def test_streaming_gemini_sentence_splitter_and_deep_thinking_escalation(monkeypatch):
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )

    recorded_configs: List[Any] = []

    class FakeChunk:
        def __init__(self, text: str = "", function_calls: Any = None):
            self.text = text
            self.function_calls = function_calls
            self.candidates = []

    class FakeFunctionCall:
        def __init__(self, name: str, args: Dict[str, Any]):
            self.name = name
            self.args = args

    class FakeModels:
        def __init__(self):
            self.call_idx = 0

        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            recorded_configs.append(config.thinking_config)
            idx = self.call_idx
            self.call_idx += 1
            if idx == 0:
                # Step 0 (LOW): calls request_deep_thinking with a spoken preamble
                yield FakeChunk(
                    function_calls=[
                        FakeFunctionCall(
                            "request_deep_thinking",
                            {
                                "spoken_preamble": "Let me run the numbers on that.",
                                "reason": "battery life calculation",
                            },
                        )
                    ]
                )
            else:
                # Step 1 (escalated to HIGH): streams token deltas across sentence boundaries
                yield FakeChunk(text="Dr. Smith confirmed ")
                yield FakeChunk(text="the rail holds 4.1 volts. ")
                yield FakeChunk(text="Current draw is 18 milliamps.")

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

    class FakeGenaiClient:
        def __init__(self):
            self.aio = FakeAio()

    adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    adapter._genai_client = FakeGenaiClient()

    streamed_live: List[str] = []
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-stream-1",
                "prompt": "Calculate battery life under load.",
                "_on_sentence": streamed_live.append,
            }
        )
    )
    # Step 0 ran at LOW, Step 1 escalated to HIGH via request_deep_thinking
    assert str(recorded_configs[0].thinking_level).endswith("LOW")
    assert str(recorded_configs[1].thinking_level).endswith("HIGH")
    # Spoken preamble + streamed sentences arrived in order without splitting on 'Dr.' or '4.1'
    assert streamed_live == [
        "Let me run the numbers on that.",
        "Dr. Smith confirmed the rail holds 4.1 volts.",
        "Current draw is 18 milliamps.",
    ]
    assert "Dr. Smith confirmed the rail holds 4.1 volts." in res["reply"]


def test_extract_and_forward_artifacts_to_zero_chat(monkeypatch, tmp_path):
    enqueued: List[Any] = []
    delivered_files: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: enqueued.append((int(ch), content)),
    )
    monkeypatch.setattr(
        "tools.deliver_image.deliver_file",
        lambda path, channel_input=None, caption=None, **kw: (
            delivered_files.append((str(path), int(channel_input), caption)) or {"success": True}
        ),
    )

    # 1. Hyperlink in raw_reply -> auto-forwarded to #zero-chat (1542081375287640084) + spoken cue added
    raw = "Found the switch at [DigiKey](<https://example.com/switch>)."
    spoken = clean_spoken_text_for_tts(raw)
    updated_spoken, sent = extract_and_forward_artifacts_to_zero_chat(
        raw_reply=raw,
        spoken_reply=spoken,
        explicitly_posted_in_turn=False,
        dispatch_discord=True,
    )
    assert sent is True
    assert "Zero-Chat" in updated_spoken
    assert any(ch == 1542081375287640084 and "https://example.com/switch" in body for ch, body in enqueued)

    # 2. Explicit file attachment via send_gotchi_artifact_to_zero_chat
    dummy_stl = tmp_path / "case.stl"
    dummy_stl.write_bytes(b"solid gotchi_case\nendsolid gotchi_case\n")
    res = send_gotchi_artifact_to_zero_chat(
        message="Here is the STL render and [Printables Link](<https://example.com/model>).",
        file_path=str(dummy_stl),
        mirror_gotchi_chat=True,
    )
    assert res["ok"] is True
    assert res["file_delivered"] is True
    assert any(cid == 1542081375287640084 for _, cid, _ in delivered_files)


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
    assert "Garage sensor is at 68" in resp.text
    assert "Zero-Chat" in resp.text
    # Logged to #gotchi-chat (1562246590943461429) AND hyperlink forwarded to #zero-chat (1542081375287640084)
    assert any(item[0] == 1562246590943461429 for item in enqueued if len(item) == 2)
    assert any(item[0] == 1542081375287640084 for item in enqueued if len(item) == 2)

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
    assert data["sent_to_zero_chat"] is True
    assert any("Zero-Chat" in s for s in data["sentences"])
