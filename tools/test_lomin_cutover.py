#!/usr/bin/env python3
"""
test_lomin_cutover.py — Unit & Integration Tests for Lomin Port 8771 Cutover & Red-Team Fixes
==============================================================================================
Tests:
1. `load_lomin_env_file`: parses `/workspace/config/lomin.env` and custom env files (comments,
   quotes, `export ` prefixes, inline comments).
2. `sync_lomin_modules_to_gotchi`: vendors `/tmp/lomin-parity-repo` modules into
   `/workspace/gotchi/lomin/` and applies red-team patches to the vendored `gateway.py`.
3. `apply_lomin_gateway_redteam_patches`:
   - Direct-Audio Whisper bypass: when `MVG_BRAIN_DIRECT_AUDIO=1` is active, a failing or
     unreachable local Whisper `transcribe()` does not abort `/voice/turn` with `502` or
     `error:stt`; it returns a placeholder transcript (`"Voice prompt"`) and forwards
     `audio_b64` directly to the brain.
   - Multimodal `**kwargs` forwarding: `STREAM_BRAIN_CALL` / `plugin_brain_call_stream`
     forwards `image_b64`, `burst_images_b64`, and `audio_b64` into `_plugin_brain_call_stream`.
   - Heartbeat pending notification status: `/api/voice/heartbeat` returns `200 JSON` with
     `has_pending` from `get_pending_store()`.
4. `ensure_single_adapter_shim`: replaces `/workspace/gotchi/backend/gotchi_zero_adapter.py`
   with a clean single-source-of-truth re-export shim (`from tools.gotchi_zero_adapter import *`).
5. `ZeroHealthHandler._ensure_gotchi_gateway_up`: loads `/workspace/config/lomin.env` into `env`,
   includes `/workspace/gotchi/lomin` in `PYTHONPATH`, and invokes `ensure_single_adapter_shim()`.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import sys
import threading
import wave
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import lomin_runtime, zero_health_server  # noqa: E402


def _make_test_wav_bytes(duration_ms: int = 500, sample_rate: int = 24000) -> bytes:
    """Generates non-silent 16-bit mono PCM WAV bytes so VAD/speech gate passes."""
    buf = io.BytesIO()
    n_samples = int(sample_rate * (duration_ms / 1000.0))
    # Alternating non-zero amplitude samples
    pcm = b"\x80\x0c\x80\xf3" * (n_samples // 2)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def test_load_lomin_env_file_default_and_custom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Parse real /workspace/config/lomin.env if present
    if Path("/workspace/config/lomin.env").is_file():
        real_env = lomin_runtime.load_lomin_env_file("/workspace/config/lomin.env")
        assert real_env.get("MVG_PORT") == "8771"
        assert real_env.get("MVG_PLUGIN") == "gotchi_zero_adapter"
        assert real_env.get("MVG_BRAIN_DIRECT_AUDIO") == "1"
        assert real_env.get("GOTCHI_ZERO_MODEL") == "gemini-3.8-flash"
        assert real_env.get("MVG_GEMINI_VOICE") == "Schedar"

    # 2. Parse custom env file with comments, export prefix, quotes
    custom_env_file = tmp_path / "lomin.env"
    custom_env_file.write_text(
        "# Comment line\n"
        "\n"
        "MVG_PORT=8771\n"
        "export MVG_BRAIN_DIRECT_AUDIO='1'\n"
        'MVG_GEMINI_VOICE="Schedar" # inline comment\n'
        "GOTCHI_CONTEXT_CACHE_ENABLED=1 # cache flag\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MVG_PORT", raising=False)
    parsed = lomin_runtime.load_lomin_env_file(custom_env_file, apply_to_environ=True)
    assert parsed["MVG_PORT"] == "8771"
    assert parsed["MVG_BRAIN_DIRECT_AUDIO"] == "1"
    assert parsed["MVG_GEMINI_VOICE"] == "Schedar"
    assert parsed["GOTCHI_CONTEXT_CACHE_ENABLED"] == "1"
    assert os.environ.get("MVG_PORT") == "8771"


def test_sync_lomin_modules_and_single_adapter_shim(tmp_path: Path) -> None:
    dest_lomin = tmp_path / "gotchi_lomin"
    synced = lomin_runtime.sync_lomin_modules_to_gotchi(
        src_dir="/tmp/lomin-parity-repo",
        dest_dir=dest_lomin,
    )
    assert "gateway.py" in synced
    assert "pending_and_auth.py" in synced
    assert (dest_lomin / "gateway.py").is_file()

    # Also verify syncing to canonical /workspace/gotchi/lomin and updating shim
    lomin_runtime.sync_lomin_modules_to_gotchi()
    shim_path = lomin_runtime.ensure_single_adapter_shim()
    assert shim_path.is_file()
    shim_text = shim_path.read_text(encoding="utf-8")
    assert "from tools.gotchi_zero_adapter import *" in shim_text
    assert len(shim_text.splitlines()) < 60

    # Verify importing backend.gotchi_zero_adapter resolves identical symbols as tools.gotchi_zero_adapter
    import backend.gotchi_zero_adapter as backend_adapter
    import importlib
    import tools.gotchi_zero_adapter as canonical_adapter

    importlib.reload(backend_adapter)
    assert backend_adapter.GotchiZeroBrainAdapter is canonical_adapter.GotchiZeroBrainAdapter
    assert backend_adapter.create_zero_gotchi_app is canonical_adapter.create_zero_gotchi_app


def test_redteam_patches_direct_audio_whisper_bypass_and_kwargs_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lomin_runtime.sync_lomin_modules_to_gotchi()
    import gateway

    lomin_runtime.apply_lomin_gateway_redteam_patches(gateway)

    # 1. Direct-Audio Whisper bypass unit check on gateway.transcribe
    def _failing_whisper(_wav: bytes) -> str:
        raise ConnectionError("Whisper server unreachable on port 8090")

    monkeypatch.setattr(gateway, "_orig_transcribe", _failing_whisper)
    monkeypatch.setattr(gateway, "BRAIN_DIRECT_AUDIO", True)
    monkeypatch.setenv("MVG_BRAIN_DIRECT_AUDIO", "1")

    wav_bytes = _make_test_wav_bytes()
    tx = gateway.transcribe(wav_bytes)
    assert tx == lomin_runtime.DIRECT_AUDIO_PLACEHOLDER_TRANSCRIPT

    # When MVG_BRAIN_DIRECT_AUDIO=0, Whisper failure still raises normally
    monkeypatch.setattr(gateway, "BRAIN_DIRECT_AUDIO", False)
    monkeypatch.setenv("MVG_BRAIN_DIRECT_AUDIO", "0")
    monkeypatch.setenv("MVG_DIRECT_AUDIO", "0")
    with pytest.raises(ConnectionError):
        gateway.transcribe(wav_bytes)

    # 2. kwargs forwarding through STREAM_BRAIN_CALL -> _plugin_brain_call_stream
    monkeypatch.setattr(gateway, "BRAIN_DIRECT_AUDIO", True)
    monkeypatch.setenv("MVG_BRAIN_DIRECT_AUDIO", "1")
    monkeypatch.setenv("MVG_ENABLE_VISION", "1")

    captured_calls: List[Dict[str, Any]] = []

    def _mock_plugin_stream(
        target: str,
        prompt: str,
        timeout: int,
        on_partial: Any = None,
        stages_out: Any = None,
        **kwargs: Any,
    ) -> str:
        captured_calls.append(
            {
                "target": target,
                "prompt": prompt,
                "timeout": timeout,
                "kwargs": dict(kwargs),
            }
        )
        if callable(on_partial):
            on_partial("Multimodal reply ready.")
        return "Multimodal reply ready."

    monkeypatch.setattr(gateway, "_plugin_brain_call_stream", _mock_plugin_stream)
    monkeypatch.setattr(gateway, "STREAM_BRAIN_CALL", gateway.plugin_brain_call_stream)

    partials: List[str] = []
    reply = gateway.STREAM_BRAIN_CALL(
        "What is in this frame?",
        "sess-mm-1",
        "gotchi-ryan-01",
        "ryan",
        0.95,
        30,
        partials.append,
        image_b64="dGVzdF9pbWFnZQ==",
        burst_images_b64=["YnVyc3RfMQ==", "YnVyc3RfMg=="],
        audio_b64="dGVzdF9hdWRpbw==",
    )
    assert reply == "Multimodal reply ready."
    assert len(captured_calls) == 1
    assert captured_calls[0]["kwargs"]["image_b64"] == "dGVzdF9pbWFnZQ=="
    assert captured_calls[0]["kwargs"]["burst_images_b64"] == ["YnVyc3RfMQ==", "YnVyc3RfMg=="]
    assert captured_calls[0]["kwargs"]["audio_b64"] == "dGVzdF9hdWRpbw=="


def test_redteam_voice_turn_end_to_end_and_heartbeat_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lomin_runtime.sync_lomin_modules_to_gotchi()
    import gateway
    from pending_and_auth import get_pending_store

    lomin_runtime.apply_lomin_gateway_redteam_patches(gateway)

    store = get_pending_store()
    store.clear()
    monkeypatch.setattr(gateway, "NODES_PATH", tmp_path / "nodes.json")
    monkeypatch.setattr(gateway, "NODE_TARGETS_PATH", tmp_path / "node-targets.json")
    monkeypatch.setattr(gateway, "WATCH_TOKENS_PATH", tmp_path / "watch-tokens.json")
    monkeypatch.delenv("MVG_DEVICE_TOKENS", raising=False)
    monkeypatch.setenv("MVG_BRAIN_DIRECT_AUDIO", "1")
    monkeypatch.setenv("MVG_ENABLE_VISION", "1")
    monkeypatch.setattr(gateway, "BRAIN_DIRECT_AUDIO", True)

    # Simulate broken/missing local Whisper server
    def _whisper_down(_wav: bytes) -> str:
        raise RuntimeError("Whisper connection refused")

    monkeypatch.setattr(gateway, "_orig_transcribe", _whisper_down)
    monkeypatch.setattr(gateway, "speech_gate", lambda _w: ("passed", 450, 1.0))
    monkeypatch.setattr(
        gateway,
        "synthesize",
        lambda _text: (_make_test_wav_bytes(duration_ms=100), "gemini"),
    )

    seen_plugin_kwargs: List[Dict[str, Any]] = []

    def _mock_plugin_stream(
        target: str,
        prompt: str,
        timeout: int,
        on_partial: Any = None,
        stages_out: Any = None,
        **kwargs: Any,
    ) -> str:
        seen_plugin_kwargs.append(dict(kwargs))
        ans = "Direct audio processed without Whisper."
        if callable(on_partial):
            on_partial(ans)
        return ans

    monkeypatch.setattr(gateway, "_plugin_brain_call_stream", _mock_plugin_stream)
    monkeypatch.setattr(gateway, "STREAM_BRAIN_CALL", gateway.plugin_brain_call_stream)

    srv = gateway.Server(("127.0.0.1", 0), gateway.GatewayHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    try:
        # 1. Test /voice/turn with failing Whisper + MVG_BRAIN_DIRECT_AUDIO=1
        wav_bytes = _make_test_wav_bytes(duration_ms=400)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/voice/turn?node_id=gotchi-ryan-01&tts=none",
            body=wav_bytes,
            headers={"Content-Type": "audio/wav", "X-Node-Id": "gotchi-ryan-01"},
        )
        resp = conn.getresponse()
        body_text = resp.read().decode("utf-8")
        conn.close()

        assert resp.status == 200
        assert "Direct audio processed without Whisper." in body_text
        assert "error:stt" not in body_text
        assert len(seen_plugin_kwargs) == 1
        assert seen_plugin_kwargs[0].get("audio_b64") == base64.b64encode(wav_bytes).decode("ascii")

        # 2. Test /api/voice/heartbeat when store is empty -> 200 JSON with has_pending=False
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/voice/heartbeat?node=gotchi-ryan-01", body=b"{}")
        hb_resp = conn.getresponse()
        hb_data = json.loads(hb_resp.read().decode("utf-8"))
        conn.close()

        assert hb_resp.status == 200
        assert hb_data["status"] == "ok"
        assert hb_data["has_pending"] is False
        assert hb_data["pending"] is None

        # 3. Store a pending notification and verify /api/voice/heartbeat returns has_pending=True
        store.store(
            node_id="gotchi-ryan-01",
            prompt="check build status",
            answer="Your FORGE build finished cleanly.",
            topic="forge",
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/voice/heartbeat?node=gotchi-ryan-01")
        hb_resp2 = conn.getresponse()
        hb_data2 = json.loads(hb_resp2.read().decode("utf-8"))
        conn.close()

        assert hb_resp2.status == 200
        assert hb_data2["has_pending"] is True
        assert hb_data2["pending"]["answer"] == "Your FORGE build finished cleanly."
    finally:
        srv.shutdown()
        store.clear()


def test_ensure_gotchi_gateway_up_loads_lomin_env_and_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = zero_health_server.ZeroHealthHandler.__new__(zero_health_server.ZeroHealthHandler)

    popen_calls: List[Dict[str, Any]] = []
    shim_called: List[bool] = []
    sync_called: List[bool] = []

    class DummySocket:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.call_idx = len(popen_calls)

        def __enter__(self) -> "DummySocket":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def settimeout(self, _t: float) -> None:
            pass

        def connect_ex(self, _addr: Any) -> int:
            # First check returns 1 (down); after Popen returns 0 (up)
            return 0 if popen_calls else 1

    def _fake_popen(cmd: List[str], **kwargs: Any) -> MagicMock:
        popen_calls.append({"cmd": cmd, "kwargs": kwargs})
        return MagicMock()

    monkeypatch.setattr("socket.socket", DummySocket)
    monkeypatch.setattr(zero_health_server.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        lomin_runtime,
        "ensure_single_adapter_shim",
        lambda *a, **k: shim_called.append(True) or Path(lomin_runtime.DEFAULT_GOTCHI_ADAPTER_SHIM_PATH),
    )
    monkeypatch.setattr(
        lomin_runtime,
        "sync_lomin_modules_to_gotchi",
        lambda *a, **k: sync_called.append(True) or ["gateway.py"],
    )

    with patch("builtins.open", MagicMock()):
        handler._ensure_gotchi_gateway_up()

    assert len(shim_called) == 1
    assert len(sync_called) == 1
    assert len(popen_calls) == 1
    spawn_env = popen_calls[0]["kwargs"]["env"]
    assert "/workspace/gotchi/lomin" in spawn_env["PYTHONPATH"]
    assert spawn_env.get("MVG_PORT") == "8771"
    assert spawn_env.get("MVG_BRAIN_DIRECT_AUDIO") == "1"
    assert spawn_env.get("MVG_PLUGIN") == "gotchi_zero_adapter"
    assert spawn_env.get("GOTCHI_ZERO_MODEL") == "gemini-3.8-flash"
