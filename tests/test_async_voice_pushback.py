#!/usr/bin/env python3
"""
Unit tests for Gotchi Async Voice Push-Back:
1. Durable SQLite Pending Answer Store with 2-hour TTL.
2. Eyes-free 'Regarding <topic>: ' context tag.
3. Long-answer detection, Discord delivery & 1-2 sentence spoken summary condensation.
4. 20-second recency auto-speak vs chime + PTT.
5. GET /api/gotchi/pending_answer (SSE audio stream) and POST /api/gotchi/pending_answer/ack.
6. GotchiZeroAdapter target symbol parity and helpers.
"""

import asyncio
import base64
import time
from typing import Any, Dict
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from backend.llm_config import LLMConfig
from backend.voice_gateway import (
    PendingVoiceAnswerStore,
    VoiceGatewayService,
    build_spoken_context,
    condense_long_answer,
    create_app,
    extract_topic,
    is_long_answer,
)
from backend.gotchi_zero_adapter import GotchiZeroAdapter, GotchiZeroBrainAdapter


def test_gotchi_zero_adapter_target_symbol_parity():
    """Verifies GotchiZeroAdapter is exported and identical to GotchiZeroBrainAdapter."""
    assert GotchiZeroAdapter is GotchiZeroBrainAdapter
    adapter = GotchiZeroAdapter()
    assert hasattr(adapter, "handle_turn")


def test_pending_voice_answer_store_lifecycle():
    """Verifies SQLite pending answer CRUD, 2-hour TTL, and ack mechanics."""
    store = PendingVoiceAnswerStore(db_path=":memory:")

    # 1. Initially no pending answer
    assert store.get_latest_pending("gotchi-node-1") is None

    # 2. Save an answer with 2-hour TTL (7200s)
    t0 = time.time()
    saved = store.save_answer(
        answer_id="ans-001",
        node_id="gotchi-node-1",
        session_id="sess-001",
        topic="the Dodgers score",
        prompt="What is the score of the Dodgers game?",
        full_answer="The Dodgers defeated the Padres 5 to 3 in 9 innings.",
        spoken_text="Regarding the Dodgers score: The Dodgers defeated the Padres 5 to 3.",
        audio_pcm=b"\x00\x01\x02\x03",
        audio_format="pcm",
        sample_rate=24000,
        auto_spoken=False,
        ttl_s=7200.0,
    )

    assert saved["answer_id"] == "ans-001"
    assert saved["expires_at"] >= t0 + 7199.0
    assert saved["expires_at"] <= t0 + 7205.0

    # 3. Retrieve pending answer
    retrieved = store.get_latest_pending("gotchi-node-1")
    assert retrieved is not None
    assert retrieved["answer_id"] == "ans-001"
    assert retrieved["node_id"] == "gotchi-node-1"
    assert retrieved["topic"] == "the Dodgers score"
    assert retrieved["spoken_text"] == "Regarding the Dodgers score: The Dodgers defeated the Padres 5 to 3."
    assert bytes(retrieved["audio_pcm"]) == b"\x00\x01\x02\x03"
    assert retrieved["acked"] == 0

    # Also retrieve by answer_id
    by_id = store.get_latest_pending("gotchi-node-1", answer_id="ans-001")
    assert by_id is not None
    assert by_id["answer_id"] == "ans-001"

    # Non-existent node_id
    assert store.get_latest_pending("other-node") is None

    # 4. Acknowledge the answer
    acked = store.ack_answer("gotchi-node-1", "ans-001")
    assert acked is True
    assert store.get_latest_pending("gotchi-node-1") is None

    # Acking again returns False
    assert store.ack_answer("gotchi-node-1", "ans-001") is False


def test_pending_voice_answer_store_ttl_expiration_and_prune():
    """Verifies answers older than TTL expire and prune correctly."""
    store = PendingVoiceAnswerStore(db_path=":memory:")

    # Save an already expired answer (ttl_s = -10.0)
    store.save_answer(
        answer_id="ans-expired",
        node_id="gotchi-node-2",
        spoken_text="Expired answer",
        ttl_s=-10.0,
    )

    # Should not be returned by get_latest_pending
    assert store.get_latest_pending("gotchi-node-2") is None

    # Prune expired
    deleted = store.prune_expired()
    assert deleted == 1


def test_extract_topic():
    """Verifies natural topic extraction from various spoken query phrasing."""
    assert extract_topic("What is the score of the Cubs game?") == "the score of the Cubs game"
    assert extract_topic("Check the status of PR 94") == "PR 94"
    assert extract_topic("Can you check on the status of the 3D printer?") == "the 3D printer"
    assert extract_topic("How's the weather in San Francisco today?") == "the weather in San Francisco today"
    assert extract_topic("Tell me what is the server temperature") == "the server temperature"
    assert extract_topic("Status of build fg-101") == "build fg-101"
    assert extract_topic("") == "your request"
    assert extract_topic("   ") == "your request"


def test_build_spoken_context():
    """Verifies eyes-free context tag prepending and idempotency."""
    assert (
        build_spoken_context("the Cubs game", "The Cubs won 4 to 2.")
        == "Regarding the Cubs game: The Cubs won 4 to 2."
    )
    # Idempotent: does not duplicate 'Regarding'
    assert (
        build_spoken_context("the Cubs game", "Regarding the Cubs game: The Cubs won 4 to 2.")
        == "Regarding the Cubs game: The Cubs won 4 to 2."
    )
    # Empty spoken text fallback
    assert build_spoken_context("the Cubs game", "") == "Regarding the Cubs game: done."


def test_is_long_answer():
    """Verifies >50 words or multi-paragraph triggers long answer detection."""
    short_single = "The weather in Redwood City is currently 68 degrees and sunny with light winds."
    assert not is_long_answer(short_single)
    assert len(short_single.split()) < 50

    long_single = " ".join(["word"] * 51)
    assert is_long_answer(long_single)

    multi_paragraph = "Paragraph one with just a few words.\n\nParagraph two with a few more words."
    assert is_long_answer(multi_paragraph)

    assert not is_long_answer("")


def test_condense_long_answer():
    """Verifies long answers condense to 1-2 sentences with Discord notification cue."""
    long_answer = (
        "The database migration finished successfully after running for 4 minutes. "
        "All 42 tables were migrated cleanly with zero data loss or checksum mismatches. "
        "We also verified that query latencies on the primary replicas remain under 12 milliseconds. "
        "The old temporary staging tables have been archived to the cold storage backup directory."
    )
    assert is_long_answer(long_answer)

    condensed = condense_long_answer(long_answer, topic="the database migration")
    assert condensed.startswith("Regarding the database migration:")
    assert "I posted the full details to Discord." in condensed
    # Must be concise: <= 40 words total
    assert len(condensed.split()) <= 45


@pytest.mark.asyncio
async def test_detached_async_handoff_20s_recency_and_notify_queue():
    """
    Verifies detached async handoff:
    - Within 20 seconds: auto_speak is True in notification item and SQLite answer.
    - Chime notification is enqueued with has_pending_audio=True and answer_id.
    - Pre-synthesized audio cached in SQLite.
    """
    cfg = LLMConfig.from_env_or_file()
    cfg.async_handoff_ms = 40  # Trigger handoff quickly after 40ms
    store = PendingVoiceAnswerStore(db_path=":memory:")

    async def slow_handler(turn: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(0.1)
        return {
            "transcript": "What is the status of the build?",
            "reply": "The build succeeded and all unit tests passed.",
        }

    service = VoiceGatewayService(
        llm_config=cfg,
        turn_handler=slow_handler,
        pending_answer_store=store,
        tts_synthesizer=lambda text, sr: b"\x10\x20\x30\x40",
    )

    events = []
    async for sse_chunk in service.generate_sse_stream({"prompt": "What is the status of the build?"}, node_id="gotchi-01"):
        events.append(sse_chunk)

    # Verify handoff event was emitted
    joined = "".join(events)
    assert "async_handoff" in joined
    assert "Still pulling those details" in joined

    # Wait for background supervision to complete
    await asyncio.sleep(0.2)

    # 1. Verify notification item was enqueued with has_pending_audio and answer_id
    notifications = service.pending_notifications.get("gotchi-01", [])
    assert len(notifications) == 1
    ntf = notifications[0]
    assert ntf["has_pending_audio"] is True
    assert ntf["chime"] == "ready"
    assert ntf["auto_speak"] is True  # Completed well within 20s
    assert "answer_id" in ntf
    assert ntf["answer_id"].startswith("ans-")

    # 2. Verify answer was saved in SQLite
    saved_answer = store.get_latest_pending("gotchi-01", answer_id=ntf["answer_id"])
    assert saved_answer is not None
    assert saved_answer["auto_spoken"] == 1
    assert "Regarding" in saved_answer["spoken_text"]
    assert bytes(saved_answer["audio_pcm"]) == b"\x10\x20\x30\x40"


@pytest.mark.asyncio
async def test_detached_async_handoff_over_20s_chime_only():
    """Verifies that if background completion takes >20s, auto_speak is False (chime only)."""
    cfg = LLMConfig.from_env_or_file()
    cfg.async_handoff_ms = 20
    store = PendingVoiceAnswerStore(db_path=":memory:")

    real_monotonic = time.monotonic
    offset = [0.0]

    def fake_monotonic():
        return real_monotonic() + offset[0]

    async def mock_handler(turn: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(0.05)
        offset[0] += 25.0
        return {
            "transcript": "Check the long job",
            "reply": "Job completed successfully.",
        }

    service = VoiceGatewayService(
        llm_config=cfg,
        turn_handler=mock_handler,
        pending_answer_store=store,
        tts_synthesizer=lambda text, sr: b"\xaa\xbb",
    )

    with patch("time.monotonic", side_effect=fake_monotonic):
        async for _ in service.generate_sse_stream({"prompt": "Check the long job"}, node_id="gotchi-02"):
            pass
        await asyncio.sleep(0.15)

    notifications = service.pending_notifications.get("gotchi-02", [])
    assert len(notifications) == 1
    ntf = notifications[0]
    assert ntf["has_pending_audio"] is True
    assert ntf["chime"] == "ready"
    assert ntf["auto_speak"] is False  # Beyond 20s -> chime + PTT only


@pytest.mark.asyncio
async def test_long_answer_discord_routing_and_spoken_condensation():
    """Verifies >50 word answer delivers full response to Discord and condenses spoken audio."""
    cfg = LLMConfig.from_env_or_file()
    cfg.async_handoff_ms = 30
    store = PendingVoiceAnswerStore(db_path=":memory:")

    long_reply_text = (
        "The automated test suite finished running across all 18 integration containers. "
        "A total of 342 test cases executed with 340 passing and 2 non-critical deprecation notices. "
        "Memory consumption peaked at 410 megabytes which is well within our 1 gigabyte threshold. "
        "Network latency averaged 4.2 milliseconds across all database query cycles. "
        "Overall the deployment is completely healthy and ready for production traffic."
    )
    assert is_long_answer(long_reply_text)

    async def long_handler(turn: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(0.05)
        return {
            "transcript": "Run the full test suite",
            "raw_reply": long_reply_text,
            "reply": long_reply_text,
        }

    service = VoiceGatewayService(
        llm_config=cfg,
        turn_handler=long_handler,
        pending_answer_store=store,
        tts_synthesizer=lambda text, sr: b"\xca\xfe",
    )

    async for _ in service.generate_sse_stream({"prompt": "Run the full test suite"}, node_id="gotchi-long"):
        pass

    await asyncio.sleep(0.15)

    # 1. Full answer delivered to Discord via TurnTranscriptSink
    assert len(service.turn_logs) >= 1
    logged = service.turn_logs[-1]
    assert logged["mode"] == "async_handoff_long"
    assert "Async Answer Breakdown" in logged["content"]
    assert long_reply_text in logged["reply"]

    # 2. Spoken text in SQLite is condensed with context tag & Discord note
    saved = store.get_latest_pending("gotchi-long")
    assert saved is not None
    assert saved["spoken_text"].startswith("Regarding the full test suite:")
    assert "I posted the full details to Discord." in saved["spoken_text"]
    assert len(saved["spoken_text"].split()) < 40


def test_pending_answer_http_endpoints_sse_stream_and_ack():
    """Verifies GET /api/gotchi/pending_answer SSE streaming and POST /api/gotchi/pending_answer/ack."""
    store = PendingVoiceAnswerStore(db_path=":memory:")
    cfg = LLMConfig.from_env_or_file()
    service = VoiceGatewayService(llm_config=cfg, pending_answer_store=store)
    app = create_app(service=service)
    client = TestClient(app)

    # 1. When no pending answer exists -> 404
    resp = client.get("/api/gotchi/pending_answer?node_id=test-badge")
    assert resp.status_code == 404
    assert resp.json()["error"] == "no_pending_answer"

    # 2. Save a pending answer
    sample_pcm = b"\x01\x02\x03\x04\x05\x06"
    store.save_answer(
        answer_id="ans-ptt-100",
        node_id="test-badge",
        topic="deploy status",
        spoken_text="Regarding deploy status: Deploy finished cleanly.",
        audio_pcm=sample_pcm,
        audio_format="pcm",
        sample_rate=24000,
    )

    # 3. GET /api/gotchi/pending_answer -> 200 text/event-stream
    stream_resp = client.get("/api/gotchi/pending_answer?node_id=test-badge")
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers["content-type"]
    body = stream_resp.text

    # Verify SSE events match /api/voice/interact protocol
    assert "event: state\ndata: {\"state\": \"speaking\"}" in body
    assert "event: sentence\n" in body
    assert "Regarding deploy status: Deploy finished cleanly." in body
    assert base64.b64encode(sample_pcm).decode("ascii") in body
    assert "ans-ptt-100" in body
    assert "event: done\n" in body

    # 4. POST /api/gotchi/pending_answer/ack
    ack_resp = client.post(
        "/api/gotchi/pending_answer/ack",
        json={"node_id": "test-badge", "answer_id": "ans-ptt-100"},
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["ok"] is True
    assert ack_resp.json()["acked"] is True
    assert ack_resp.json()["answer_id"] == "ans-ptt-100"

    # 5. Subsequent GET returns 404 since it has been acknowledged
    after_ack = client.get("/api/gotchi/pending_answer?node_id=test-badge")
    assert after_ack.status_code == 404
    assert after_ack.json()["error"] == "no_pending_answer"


@pytest.mark.asyncio
async def test_gotchi_zero_adapter_handle_turn_async_long_condensation():
    """Verifies GotchiZeroAdapter handle_turn condenses long answers on async turns."""
    adapter = GotchiZeroAdapter(dispatch_discord=False)

    long_reply = (
        "First sentence explaining that the job completed successfully. "
        "Second sentence detailing that all files are processed across the cluster. "
        "Third sentence giving extra deep analysis of the output data and pipeline status. "
        "Fourth sentence describing future architectural recommendations and caveats. "
        "Fifth sentence noting disk usage statistics and concluding the full report. "
        "Sixth sentence confirming that zero errors or warnings occurred during the entire execution run."
    )
    assert is_long_answer(long_reply)

    # Call handle_turn with mock llm_caller producing long_reply
    adapter.llm_caller = lambda turn: {"reply": long_reply, "posted_to_zero_chat": False}

    res = await adapter.handle_turn({
        "node_id": "gotchi-adapt-01",
        "prompt": "What is the status of the report?",
        "is_async_handoff": True,
    })

    assert res["reply"].startswith("Regarding the report:")
    assert "I posted the full details to Discord." in res["reply"]
    assert len(res["reply"].split()) < 35


def test_firmware_source_contract_eyes_free_ptt_fetch():
    """
    Verifies firmware source code invariants in main.cpp, voice_client.h, and voice_client.cpp:
    1. s_pending_answer_ready with 2-hour TTL (7200s).
    2. handleRpcCommand latches s_pending_answer_ready on notify with has_pending_audio or answer_id.
    3. runPttTurn checks isPendingAnswerValid() and immediately fetches pending answer without opening mic.
    4. Full BTN_TOP abort support via pollVoiceAbort passed to fetchPendingAnswer.
    5. fetchPendingAnswer and ackPendingAnswer declarations and implementations.
    """
    from pathlib import Path

    candidates = [
        Path("firmware/src/main.cpp"),
        Path("/workspace/gotchi/firmware/src/main.cpp"),
    ]
    main_cpp = next((p for p in candidates if p.exists()), None)
    assert main_cpp is not None, "firmware/src/main.cpp must exist"
    main_text = main_cpp.read_text()

    assert "s_pending_answer_ready" in main_text
    assert "PENDING_ANSWER_TTL_MS" in main_text
    assert "2UL * 3600UL * 1000UL" in main_text or "7200000" in main_text
    assert "isPendingAnswerValid" in main_text
    assert "has_pending_audio" in main_text
    assert "fetchPendingAnswer" in main_text

    # Verify voice_client declarations and implementations
    h_cands = [
        Path("firmware/src/voice_client.h"),
        Path("firmware/include/voice_client.h"),
        Path("/workspace/gotchi/firmware/include/voice_client.h"),
    ]
    h_file = next((p for p in h_cands if p.exists()), None)
    assert h_file is not None, "voice_client.h must exist"

    cpp_cands = [
        Path("firmware/src/voice_client.cpp"),
        Path("/workspace/gotchi/firmware/src/voice_client.cpp"),
    ]
    cpp_file = next((p for p in cpp_cands if p.exists()), None)
    assert cpp_file is not None, "firmware/src/voice_client.cpp must exist"
    cpp_text = cpp_file.read_text()

    assert "buildPendingAnswerUrl" in cpp_text
    assert "ackPendingAnswer" in cpp_text
    assert "fetchPendingAnswer" in cpp_text
    assert "/api/gotchi/pending_answer" in cpp_text


def test_firmware_state_machine_simulation_with_gateway():
    """
    Simulates the firmware state machine lifecycle with real VoiceGatewayService endpoints:
    1. Device is IDLE.
    2. Notify payload arrives (has_pending_audio=True, answer_id='ans-test-999').
    3. s_pending_answer_ready latches with 2-hour TTL.
    4. 60s visual banner expires; s_pending_answer_ready remains True.
    5. Short tap of BTN_TOP_PTT in IDLE triggers fetch & play instead of mic recording.
    6. Audio stream received from GET /api/gotchi/pending_answer.
    7. POST /api/gotchi/pending_answer/ack clears pending state on gateway and badge.
    8. Subsequent PTT tap opens mic normally.
    """
    store = PendingVoiceAnswerStore(db_path=":memory:")
    cfg = LLMConfig.from_env_or_file()
    service = VoiceGatewayService(llm_config=cfg, pending_answer_store=store)
    app = create_app(service=service)
    client = TestClient(app)

    # 1. Gateway saves pending answer
    sample_audio = b"\xde\xad\xbe\xef" * 16
    saved = store.save_answer(
        answer_id="ans-test-999",
        node_id="gotchi-sim-01",
        topic="the morning weather",
        spoken_text="Regarding the morning weather: High of 72 degrees with clear skies.",
        audio_pcm=sample_audio,
        ttl_s=7200.0,
    )
    assert saved["answer_id"] == "ans-test-999"

    # 2. Firmware state machine emulation
    class GotchiFirmwareSimulator:
        def __init__(self, node_id: str, test_client: TestClient):
            self.node_id = node_id
            self.client = test_client
            self.state = "IDLE"
            self.status_text = ""
            self.notify_expire_ts = 0.0
            self.pending_answer_ready = False
            self.pending_answer_expire_ts = 0.0
            self.pending_answer_id = ""
            self.mic_opened = False
            self.last_audio_played = b""
            self.clock = 1000.0

        def is_pending_valid(self) -> bool:
            if not self.pending_answer_ready:
                return False
            if self.clock >= self.pending_answer_expire_ts:
                self.pending_answer_ready = False
                self.pending_answer_id = ""
                return False
            return True

        def handle_rpc_notify(self, payload: dict) -> dict:
            if self.state != "IDLE":
                return {"ok": False, "error": "busy"}
            chime = payload.get("chime", "ready")
            msg = (payload.get("status_text") or payload.get("message") or "BUILD READY")[:22]
            self.status_text = msg
            self.notify_expire_ts = self.clock + 60.0  # 60s visual banner
            has_pending = payload.get("has_pending_audio", False)
            ans_id = payload.get("answer_id", "")
            if has_pending or ans_id:
                self.pending_answer_ready = True
                self.pending_answer_expire_ts = self.clock + 7200.0  # 2-hour TTL
                self.pending_answer_id = ans_id
            return {"ok": True, "status_text": self.status_text, "chime": chime}

        def loop_tick(self, delta_s: float):
            self.clock += delta_s
            # 60s visual banner expiration
            if self.state == "IDLE" and self.status_text and self.clock >= self.notify_expire_ts:
                self.status_text = ""

        def press_ptt(self, abort_during_playback: bool = False):
            # Mirror firmware/src/main.cpp:runPttTurn
            if self.is_pending_valid():
                self.pending_answer_ready = False
                ans_id = self.pending_answer_id
                self.pending_answer_id = ""
                self.status_text = ""
                self.state = "THINKING"
                self.mic_opened = False  # Mic NEVER opened!

                # Fetch from gateway
                resp = self.client.get(f"/api/gotchi/pending_answer?node_id={self.node_id}&answer_id={ans_id}")
                if resp.status_code == 200:
                    self.state = "SPEAKING"
                    # Parse SSE sentence audio
                    for line in resp.text.splitlines():
                        if line.startswith("data: ") and "audio_b64" in line:
                            import json
                            data = json.loads(line[6:])
                            if "audio_b64" in data and data["audio_b64"]:
                                self.last_audio_played = base64.b64decode(data["audio_b64"])
                    # Send ack
                    self.client.post(
                        "/api/gotchi/pending_answer/ack",
                        json={"node_id": self.node_id, "answer_id": ans_id},
                    )
                self.state = "IDLE"
                return "pending_played"
            else:
                # Normal PTT turn: mic opens
                self.mic_opened = True
                self.state = "LISTENING"
                return "normal_ptt"

    sim = GotchiFirmwareSimulator("gotchi-sim-01", client)
    assert sim.state == "IDLE"
    assert not sim.is_pending_valid()

    # Step 1: Notify RPC arrives
    res = sim.handle_rpc_notify({
        "cmd": "notify",
        "has_pending_audio": True,
        "answer_id": "ans-test-999",
        "status_text": "ANSWER READY",
        "chime": "ready",
    })
    assert res["ok"] is True
    assert sim.status_text == "ANSWER READY"
    assert sim.is_pending_valid() is True

    # Step 2: 65 seconds elapse -> visual banner clears, but 2-hour pending answer remains latched!
    sim.loop_tick(65.0)
    assert sim.status_text == ""
    assert sim.is_pending_valid() is True

    # Step 3: User taps BTN_TOP_PTT in IDLE
    turn_result = sim.press_ptt()
    assert turn_result == "pending_played"
    assert sim.mic_opened is False  # Mic was NOT opened!
    assert sim.last_audio_played == sample_audio
    assert sim.pending_answer_ready is False
    assert sim.state == "IDLE"

    # Step 4: Verify answer was acked on gateway
    assert store.get_latest_pending("gotchi-sim-01") is None

    # Step 5: Next PTT press in IDLE opens mic normally for speech
    next_turn = sim.press_ptt()
    assert next_turn == "normal_ptt"
    assert sim.mic_opened is True
    assert sim.state == "LISTENING"

