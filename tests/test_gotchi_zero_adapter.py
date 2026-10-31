#!/usr/bin/env python3
"""Unit tests for `backend/gotchi_zero_adapter.py`."""

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from backend.gotchi_zero_adapter import (
    DEFAULT_GOTCHI_MODEL,
    GOTCHI_THINKING_TELEMETRY,
    GotchiZeroBrainAdapter,
    _build_gotchi_tools,
    build_contextual_tool_filler,
    build_gotchi_zero_system_prompt,
    build_image_inspection_filler,
    build_thinking_config,
    clean_spoken_text_for_tts,
    create_zero_gotchi_app,
    delegate_coding_task_to_zero_chat,
    execute_gotchi_tool_call,
    extract_and_forward_artifacts_to_zero_chat,
    fetch_live_weather,
    fetch_web_search_results,
    is_allowed_voice_read_path,
    is_destructive_voice_command,
    is_destructive_voice_write,
    is_filler_or_silent,
    load_zero_skills_catalog,
    prefetch_zero_memory,
    read_zero_skill,
    record_gotchi_turn_to_channels,
    select_thinking_budget,
    select_thinking_level,
    send_gotchi_artifact_to_zero_chat,
)
from poc.gotchi_poc import wrap_pcm16_to_wav


def test_get_weather_forecast_tool_declared():
    """Verifies that _build_gotchi_tools declares get_weather_forecast, request_deep_thinking, delegate_coding_task_to_zero_chat, and read_zero_skill."""
    tools = _build_gotchi_tools()
    assert len(tools) >= 1
    t = tools[0]
    decl_names = [getattr(d, "name", "") for d in getattr(t, "function_declarations", [])]
    assert "get_weather_forecast" in decl_names
    assert "request_deep_thinking" in decl_names
    assert "delegate_coding_task_to_zero_chat" in decl_names
    assert "web_and_reddit_search" in decl_names
    assert "read_zero_skill" in decl_names

    weather_decl = next(d for d in t.function_declarations if d.name == "get_weather_forecast")
    assert "location" in weather_decl.parameters.properties

    web_decl = next(d for d in t.function_declarations if d.name == "web_and_reddit_search")
    assert "mode='web'" in web_decl.description

    skill_decl = next(d for d in t.function_declarations if d.name == "read_zero_skill")
    assert "skill_name" in skill_decl.parameters.properties


def test_zero_skills_catalog_and_read_zero_skill():
    """Verifies cached skills catalog loading, alias resolution, and read_zero_skill tool execution."""
    catalog = load_zero_skills_catalog()
    assert len(catalog) >= 30, f"Expected at least 30 skills in catalog, found {len(catalog)}"
    assert "crontab-verify" in catalog
    assert "whole-foods-cart" in catalog
    assert "api-and-interface-design" in catalog

    # Verify 0ms caching: repeated calls return the same in-memory dictionary instance
    catalog2 = load_zero_skills_catalog()
    assert catalog is catalog2, "load_zero_skills_catalog must return cached instance"

    # Verify read_zero_skill exact match
    crontab_content = read_zero_skill("crontab-verify")
    assert "crontab-verify" in crontab_content
    assert ("Sidecar" in crontab_content or "cron" in crontab_content)

    # Verify normalized alias resolution (underscore to hyphen)
    wf_content = read_zero_skill("whole_foods_cart")
    assert "whole-foods-cart" in wf_content

    # Verify missing/unknown skill handling
    unknown_res = read_zero_skill("non_existent_skill_xyz")
    assert "Skill 'non_existent_skill_xyz' not found" in unknown_res
    assert "crontab-verify" in unknown_res

    empty_res = read_zero_skill("")
    assert "Error: skill_name must not be empty" in empty_res

    # Verify tool execution via execute_gotchi_tool_call
    t_res = asyncio.run(execute_gotchi_tool_call("read_zero_skill", {"skill_name": "crontab-verify"}))
    assert "result" in t_res
    assert "crontab-verify" in t_res["result"]

    t_empty = asyncio.run(execute_gotchi_tool_call("read_zero_skill", {}))
    assert "error" in t_empty

    # Verify contextual spoken filler
    filler = build_contextual_tool_filler("read_zero_skill", {"skill_name": "crontab-verify"})
    assert "crontab-verify" in filler


def test_gotchi_zero_system_prompt_skills_and_on_demand_memory(monkeypatch):
    """Verifies that the system prompt includes the Zero Skills Catalog and configures on-demand memory search."""
    # 1. Default (pre_search_memory=False / On-Demand mode)
    monkeypatch.delenv("GOTCHI_PRE_SEARCH_MEMORY", raising=False)
    sys_prompt = build_gotchi_zero_system_prompt("Tell me about my tasks and projects")
    assert "ZERO SKILLS CATALOG (ON-DEMAND VIA read_zero_skill)" in sys_prompt
    assert "read_zero_skill" in sys_prompt
    assert "ON-DEMAND MEMORY SEARCH" in sys_prompt
    assert "crontab-verify" in sys_prompt
    assert "whole-foods-cart" in sys_prompt
    assert "=== PRE-FETCHED ZERO MEMORY FOR THIS TURN ===" not in sys_prompt

    # 2. Configurable pre_search_memory=True
    monkeypatch.setattr(
        "backend.gotchi_zero_adapter.prefetch_zero_memory",
        lambda q, limit=4: f"- Mocked recall for query: {q}",
    )
    prompt_with_pre_fetch = build_gotchi_zero_system_prompt(
        "Tell me about my tasks and projects", pre_search_memory=True
    )
    assert "=== PRE-FETCHED ZERO MEMORY FOR THIS TURN ===" in prompt_with_pre_fetch
    assert "Mocked recall for query: Tell me about my tasks and projects" in prompt_with_pre_fetch

    # 3. Environment variable toggle GOTCHI_PRE_SEARCH_MEMORY=1
    monkeypatch.setenv("GOTCHI_PRE_SEARCH_MEMORY", "1")
    prompt_env_toggle = build_gotchi_zero_system_prompt("What is my dog's name?")
    assert "=== PRE-FETCHED ZERO MEMORY FOR THIS TURN ===" in prompt_env_toggle
    assert "Mocked recall for query: What is my dog's name?" in prompt_env_toggle


def test_gotchi_zero_brain_adapter_pre_search_memory_toggle(monkeypatch):
    """Verifies GotchiZeroBrainAdapter constructor, env toggle, and audio turn routing with pre_search_memory."""
    # 1. Constructor defaults
    monkeypatch.delenv("GOTCHI_PRE_SEARCH_MEMORY", raising=False)
    adapter_default = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    assert adapter_default.pre_search_memory is False

    adapter_explicit_true = GotchiZeroBrainAdapter(
        api_key="fake-key", dispatch_discord=False, pre_search_memory=True
    )
    assert adapter_explicit_true.pre_search_memory is True

    monkeypatch.setenv("GOTCHI_PRE_SEARCH_MEMORY", "1")
    adapter_env = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    assert adapter_env.pre_search_memory is True

    # 2. Audio turn routing: pre_search_memory=False (0ms direct-audio streaming)
    fake_wav = wrap_pcm16_to_wav(b"\x00\x00" * 4000)

    async def mock_transcribe(audio_bytes):
        return "What is the status of the homelab?"

    gemini_calls_off = []
    async def mock_run_gemini_off(prompt, image_bytes, burst_images, on_sentence=None, audio_bytes=None):
        gemini_calls_off.append({"prompt": prompt, "audio_bytes": audio_bytes})
        return "Simulated direct audio reply", False, []

    adapter_off = GotchiZeroBrainAdapter(
        api_key="fake-key", dispatch_discord=False, pre_search_memory=False
    )
    adapter_off._run_gemini_with_tools = mock_run_gemini_off
    adapter_off._transcribe_audio_with_gemini = mock_transcribe

    res_off = asyncio.run(adapter_off.handle_turn({"audio_bytes": fake_wav}))
    assert len(gemini_calls_off) == 1
    assert gemini_calls_off[0]["audio_bytes"] is not None, "Direct audio mode must pass audio_bytes to brain"
    assert gemini_calls_off[0]["prompt"] == "", "Direct audio mode must start with prompt=''"
    assert res_off["reply"] == "Simulated direct audio reply"

    # 3. Audio turn routing: pre_search_memory=True (upfront STT + Turn-0 memory pre-fetch)
    gemini_calls_on = []
    async def mock_run_gemini_on(prompt, image_bytes, burst_images, on_sentence=None, audio_bytes=None):
        gemini_calls_on.append({"prompt": prompt, "audio_bytes": audio_bytes})
        return "Simulated upfront STT reply", False, []

    adapter_on = GotchiZeroBrainAdapter(
        api_key="fake-key", dispatch_discord=False, pre_search_memory=True
    )
    adapter_on._run_gemini_with_tools = mock_run_gemini_on
    adapter_on._transcribe_audio_with_gemini = mock_transcribe

    res_on = asyncio.run(adapter_on.handle_turn({"audio_bytes": fake_wav}))
    assert len(gemini_calls_on) == 1
    assert gemini_calls_on[0]["audio_bytes"] is None, "Upfront STT mode must not pass audio_bytes to brain"
    assert gemini_calls_on[0]["prompt"] == "What is the status of the homelab?"
    assert res_on["transcript"] == "What is the status of the homelab?"
    assert res_on["reply"] == "Simulated upfront STT reply"

    # 4. App factory toggle
    app_default = create_zero_gotchi_app()
    assert app_default is not None


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
    assert select_thinking_level("How warm is the garage?", step=0) == "LOW"
    assert select_thinking_level("Summarize the memory lookup", step=1) == "LOW"
    assert select_thinking_budget("How warm is the garage?", step=0) == 0
    assert select_thinking_budget("Summarize the memory lookup", step=1) == 0

    assert select_thinking_level("Retry after error", step=1, tool_had_error=True) == "MEDIUM"
    assert select_thinking_level("Chained multi-tool step", step=2) == "MEDIUM"
    assert select_thinking_level("Burst analysis", step=0, burst_count=5) == "MEDIUM"
    assert select_thinking_budget("Retry after error", step=1, tool_had_error=True) == 512

    assert select_thinking_level("Deep math", step=0, model_requested_deep_thinking=True) == "HIGH"
    assert select_thinking_budget("Deep math", step=0, model_requested_deep_thinking=True) == 1024


def test_record_gotchi_turn_sandboxing(monkeypatch, tmp_path):
    """Verifies that dispatch_discord=False suppresses _append_voice_turn_to_history and ATTACHMENTS_DIR writes."""
    appended: List[Any] = []
    monkeypatch.setattr(
        "backend.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )
    test_attach_dir = tmp_path / "attachments"
    monkeypatch.setattr("backend.gotchi_zero_adapter.ATTACHMENTS_DIR", test_attach_dir)

    fake_img = b"\xff\xd8\xff\xe0" + b"\x00" * 32

    # 1. Turn with dispatch_discord=False (unit test mode)
    res_sandboxed = record_gotchi_turn_to_channels(
        node_id="gotchi-test",
        session_id="sess-sandbox",
        transcript="Test transcript",
        reply="Test reply",
        image_bytes=fake_img,
        dispatch_discord=False,
    )
    assert res_sandboxed["ok"] is True
    assert res_sandboxed["dispatched"] is False
    assert len(appended) == 0, "No turns should be appended when dispatch_discord=False"
    assert not test_attach_dir.exists(), "No attachment directory should be created when dispatch_discord=False"
    assert res_sandboxed["saved_image_path"] is None

    # 2. Turn with dispatch_discord=True (live mode)
    outbox_calls: List[Any] = []
    monkeypatch.setattr(
        "backend.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, msg, **kw: outbox_calls.append((ch, msg)),
    )
    monkeypatch.setattr("tools.deliver_image.deliver_image", lambda path, channel_input, caption: {"success": True})

    res_live = record_gotchi_turn_to_channels(
        node_id="gotchi-test",
        session_id="sess-live",
        transcript="Live transcript",
        reply="Live reply",
        image_bytes=fake_img,
        dispatch_discord=True,
    )
    assert res_live["ok"] is True
    assert res_live["dispatched"] is True
    assert len(appended) == 2, "Both Ryan and Zero turns should be recorded when dispatch_discord=True"
    assert test_attach_dir.exists(), "Attachment directory should be created when dispatch_discord=True"
    assert res_live["saved_image_path"] is not None


def test_fetch_web_search_results_and_modes(monkeypatch):
    """Verifies fetch_web_search_results and mode routing in execute_gotchi_tool_call."""
    # 1. Test fetch_web_search_results with empty query
    assert "Empty search query" in fetch_web_search_results("")

    # 2. Mock SerpApi query inside fetch_web_search_results
    def mock_urlopen(req, timeout=12.0):
        class MockResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def read(self):
                data = {
                    "answer_box": {"answer": "42 is the answer."},
                    "organic_results": [
                        {
                            "title": "FastAPI Lifespan Events",
                            "link": "https://fastapi.tiangolo.com/advanced/events/",
                            "snippet": "Use asynccontextmanager for lifespan events.",
                        }
                    ],
                }
                return json.dumps(data).encode("utf-8")
        return MockResp()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    res_str = fetch_web_search_results("FastAPI lifespan")
    assert "Direct Answer: 42 is the answer." in res_str
    assert "FastAPI Lifespan Events" in res_str
    assert "https://fastapi.tiangolo.com" in res_str

    # 3. Test mode='web' in execute_gotchi_tool_call
    web_res = asyncio.run(
        execute_gotchi_tool_call("web_and_reddit_search", {"query": "FastAPI lifespan", "mode": "web"})
    )
    assert "FastAPI Lifespan Events" in web_res.get("result", "")

    # 4. Test mode='reddit' empty-results fallback to web search
    class MockSubprocessRun:
        def __init__(self, *a, **kw):
            self.stdout = json.dumps({"ok": True, "count": 0, "threads": []})
            self.stderr = ""
            self.returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MockSubprocessRun())
    reddit_fallback_res = asyncio.run(
        execute_gotchi_tool_call("web_and_reddit_search", {"query": "FastAPI lifespan", "mode": "reddit"})
    )
    assert "falling back to web search" in reddit_fallback_res.get("result", "")
    assert "FastAPI Lifespan Events" in reddit_fallback_res.get("result", "")


def test_silent_and_filler_camera_snapshot_grounding():
    """Verifies that silent or filler-only camera snapshots ground the prompt to describe the photo directly."""
    # 1. is_filler_or_silent checks
    assert is_filler_or_silent("") is True
    assert is_filler_or_silent("   ") is True
    assert is_filler_or_silent("Ah") is True
    assert is_filler_or_silent("um") is True
    assert is_filler_or_silent("uh") is True
    assert is_filler_or_silent("Uh...") is True
    assert is_filler_or_silent("Ah, um") is True
    assert is_filler_or_silent("What is the printer doing?") is False
    assert is_filler_or_silent("Describe this component") is False

    # 2. System prompt includes silent vision grounding note when is_silent_vision=True
    sys_prompt = build_gotchi_zero_system_prompt("", has_vision=True, is_silent_vision=True)
    assert "Do NOT attempt to answer or resume past conversation topics from chat history" in sys_prompt
    assert "Describe what you see in the photo directly" in sys_prompt

    # 3. handle_turn with filler prompt and image normalizes transcript and describes photo directly
    received_turn: Dict[str, Any] = {}
    def mock_llm_caller(turn_payload):
        received_turn.update(turn_payload)
        return {"reply": "I see a 3D printer hotend with blue filament loaded."}

    adapter = GotchiZeroBrainAdapter(
        api_key="fake-key",
        dispatch_discord=False,
        llm_caller=mock_llm_caller,
    )

    fake_img = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-test",
                "session_id": "sess-photo-1",
                "prompt": "Um...",
                "image_bytes": fake_img,
            }
        )
    )
    assert res["transcript"] == "Analyze this camera snapshot from Gotchi."
    assert received_turn["prompt"] == "Analyze this camera snapshot from Gotchi."
    assert "3D printer hotend" in res["reply"]

    # 4. Burst mode filler prompt
    res_burst = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-test",
                "session_id": "sess-burst-1",
                "prompt": "Ah",
                "burst_images": [fake_img, fake_img],
            }
        )
    )
    assert res_burst["transcript"] == "Analyze this 5-frame camera burst from Gotchi."


def test_voice_security_gate_and_realpath_read_allowlist(monkeypatch, tmp_path):
    staged_msgs: List[Any] = []
    monkeypatch.setattr(
        "backend.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: staged_msgs.append((int(ch), content)),
    )

    find_res = asyncio.run(
        execute_gotchi_tool_call(
            "run_shell_command",
            {"command": "find /workspace/tools -maxdepth 1 -name gotchi_zero_adapter.py"},
        )
    )
    assert find_res.get("confirmation_required") is not True
    assert "exit_code" in find_res

    destructive_cmds = [
        "rm -rf /workspace/tmp_test",
        "git reset --hard HEAD~1",
        "docker compose down",
    ]
    for d_cmd in destructive_cmds:
        d_res = asyncio.run(
            execute_gotchi_tool_call("run_shell_command", {"command": d_cmd})
        )
        assert d_res["confirmation_required"] is True


def test_is_destructive_voice_command_and_write(tmp_path):
    for safe_cmd in [
        "git status -s",
        "git log -n 5",
        "git diff HEAD~1",
        "find /workspace/tools -maxdepth 1 -name gotchi_zero_adapter.py",
        "ls -la /workspace",
        "cat /workspace/README.md",
        "grep -rn 'Zero' /workspace/tools",
        "python3 -c 'print(1+1)'",
        "curl https://198.51.100.10:8123/api/states",
    ]:
        is_destr, _ = is_destructive_voice_command(safe_cmd)
        assert is_destr is False, f"Expected safe: {safe_cmd}"

    for bad_cmd in [
        "rm -rf /workspace/tmp_test",
        "rm file.txt",
        "rmdir old_dir",
        "unlink test.link",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git push --force origin main",
        "docker compose down",
        "systemctl stop homeassistant",
        "reboot",
    ]:
        is_destr, _ = is_destructive_voice_command(bad_cmd)
        assert is_destr is True, f"Expected destructive: {bad_cmd}"


def test_extract_and_forward_artifacts_to_zero_chat(monkeypatch, tmp_path):
    dispatched: List[Any] = []
    monkeypatch.setattr(
        "backend.gotchi_zero_adapter.send_gotchi_artifact_to_zero_chat",
        lambda message, file_path=None, screenshot_url=None, mirror_gotchi_chat=True: dispatched.append(
            {"message": message, "file": file_path, "shot": screenshot_url}
        ),
    )

    raw1 = "Here is the documentation: https://example.com/spec."
    spoken1 = "Here is the documentation."
    new_spk1, sent1 = extract_and_forward_artifacts_to_zero_chat(raw1, spoken1)
    assert sent1 is True
    assert "I sent the link to Zero-Chat." in new_spk1
    assert len(dispatched) == 1
