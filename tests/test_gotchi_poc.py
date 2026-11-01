import base64
import struct
from poc.gotchi_poc import (
    GotchiUIState,
    RefreshTracker,
    build_multipart_wav,
    count_dirty_pixels,
    parse_sse_stream,
    render_sprite_200x200,
    wrap_pcm16_to_wav,
)


def test_wrap_pcm16_to_wav_header():
    pcm = b"\x00\x00" * 16000  # 1.0s at 16 kHz mono 16-bit
    wav = wrap_pcm16_to_wav(pcm, sample_rate=16000, channels=1)
    assert len(wav) == 44 + len(pcm)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    sr, byte_rate = struct.unpack("<II", wav[24:32])
    assert sr == 16000
    assert byte_rate == 32000


def test_multipart_payload():
    wav = wrap_pcm16_to_wav(b"\x01\x02" * 100)
    body, ctype = build_multipart_wav(wav, node_id="gotchi-pi0", session_id="s-1")
    assert "multipart/form-data; boundary=" in ctype
    assert b'name="audio"; filename="ptt.wav"' in body
    assert b"gotchi-pi0" in body


def test_multipart_single_image_and_prompt():
    wav = wrap_pcm16_to_wav(b"\x00\x00" * 50)
    dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIFdummy"
    body, ctype = build_multipart_wav(
        wav,
        node_id="gotchi-cam-1",
        session_id="sess-snap-1",
        image_jpeg=dummy_jpeg,
        prompt="Analyze this object",
        audio_out_mode="speaker",
    )
    assert b'name="audio"; filename="ptt.wav"' in body
    assert b'name="image"; filename="snap.jpg"' in body
    assert b"JFIFdummy" in body
    assert b'name="prompt"' in body
    assert b"Analyze this object" in body
    assert b'name="audio_out_mode"' in body
    assert b"speaker" in body


def test_multipart_five_photo_burst():
    wav = wrap_pcm16_to_wav(b"\x00\x00" * 50)
    burst = [f"frame_{i}".encode() for i in range(5)]
    body, ctype = build_multipart_wav(
        wav,
        node_id="gotchi-burst-1",
        session_id="sess-burst-1",
        burst_images=burst,
    )
    for i in range(5):
        assert f'name="image_{i}"; filename="snap_{i}.jpg"'.encode() in body
        assert f"frame_{i}".encode() in body


def test_multipart_standalone_snapshot_without_audio():
    dummy_jpeg = b"\xff\xd8\xff\xe0standalone"
    body, ctype = build_multipart_wav(
        wav_bytes=None,
        node_id="gotchi-standalone",
        session_id="sess-stand-1",
        image_jpeg=dummy_jpeg,
        prompt="What is this item?",
    )
    assert b'name="audio"' not in body
    assert b'name="image"; filename="snap.jpg"' in body
    assert b"standalone" in body
    assert b"What is this item?" in body


def test_refresh_tracker_policy():
    rt = RefreshTracker(max_consecutive_partials=3, idle_scrub_defer_s=75.0, full_refresh_minutes=15.0)
    assert rt.select_refresh_mode(now=100.0) == "full"
    rt.record_write("full", now=100.0)
    assert rt.select_refresh_mode(now=101.0) == "partial"
    rt.record_write("partial", now=101.0)
    rt.record_write("partial", now=102.0)
    rt.record_write("partial", now=103.0)
    # Hit max_consecutive_partials (3), but active/mid-turn (idle_duration_s < 75s) -> deferred to partial
    assert rt.select_refresh_mode(now=104.0, idle_duration_s=10.0) == "partial"
    # Idle for >= 75s -> scrub permitted as full refresh
    assert rt.select_refresh_mode(now=104.0, idle_duration_s=75.0) == "full"
    # Failed partial recovers with full
    assert rt.record_write("partial", succeeded=False, now=105.0) == "full"


def test_refresh_tracker_pixel_budget_and_hard_ceiling():
    rt = RefreshTracker(
        max_consecutive_partials=35,
        max_partials_hard_ceiling=80,
        dirty_pixel_budget=140000,
        idle_scrub_defer_s=75.0,
    )
    # Initial frame requires full refresh
    assert rt.select_refresh_mode() == "full"
    rt.record_write("full", now=0.0)

    # Accumulate dirty pixels under budget
    rt.record_write("partial", dirty_pixels=50000, now=1.0)
    assert rt.cumulative_dirty_pixels == 50000
    assert rt.consecutive_partials == 1
    assert rt.select_refresh_mode(now=2.0, idle_duration_s=0.0) == "partial"

    # Exceed dirty-pixel budget (150000 >= 140000)
    rt.record_write("partial", dirty_pixels=100000, now=2.0)
    assert rt.cumulative_dirty_pixels == 150000
    assert rt.needs_scrub(now=2.0) is True

    # Active / mid-turn (idle < 75s): scrub is deferred!
    assert rt.select_refresh_mode(now=3.0, idle_duration_s=10.0) == "partial"
    # Sat in IDLE for >= 75s: scrub executes as full
    assert rt.select_refresh_mode(now=76.0, idle_duration_s=75.0) == "full"

    # Record full write resets both consecutive partials and cumulative dirty pixels
    rt.record_write("full", now=76.0)
    assert rt.consecutive_partials == 0
    assert rt.cumulative_dirty_pixels == 0

    # Hard ceiling (80 partials) forces full refresh even when active (idle_duration_s = 0.0)
    for _ in range(80):
        rt.record_write("partial", dirty_pixels=100)
    assert rt.consecutive_partials == 80
    assert rt.select_refresh_mode(now=100.0, idle_duration_s=0.0) == "full"


def test_blink_rendering_gotchi_and_zero_skins():
    # Gotchi skin: open eyes vs closed horizontal eyelid lines
    ui_open = GotchiUIState(state="idle", skin="gotchi", blink=False)
    ui_blink = GotchiUIState(state="idle", skin="gotchi", blink=True)
    im_open = render_sprite_200x200(ui_open)
    im_blink = render_sprite_200x200(ui_blink)

    diff = count_dirty_pixels(im_open, im_blink)
    assert diff > 0, "Blink state must alter rendered eye pixels"

    # Zero skin: blink rendering verification
    ui_zero_open = GotchiUIState(state="listening", skin="zero", blink=False)
    ui_zero_blink = GotchiUIState(state="listening", skin="zero", blink=True)
    im_z_open = render_sprite_200x200(ui_zero_open)
    im_z_blink = render_sprite_200x200(ui_zero_blink)

    diff_z = count_dirty_pixels(im_z_open, im_z_blink)
    assert diff_z > 0, "Zero skin blink state must alter rendered eye pixels"
    assert im_z_blink.getpixel((0, 0)) == 255
    assert im_z_blink.getpixel((199, 0)) == 255


def test_head_posture_shifts():
    # LISTENING lean-in (dy=-2)
    ui_idle = GotchiUIState(state="idle", skin="gotchi", anim_frame=0)
    ui_listen = GotchiUIState(state="listening", skin="gotchi", anim_frame=0)
    im_idle = render_sprite_200x200(ui_idle)
    im_listen = render_sprite_200x200(ui_listen)
    assert count_dirty_pixels(im_idle, im_listen) > 0

    # THINKING tilt (dx=-2, dy=-2)
    ui_think = GotchiUIState(state="thinking", skin="gotchi", anim_frame=0)
    im_think = render_sprite_200x200(ui_think)
    assert count_dirty_pixels(im_idle, im_think) > 0

    # SPEAKING bob alternating dy (-1 / +1)
    ui_spk_f0 = GotchiUIState(state="speaking", skin="gotchi", anim_frame=0)
    ui_spk_f1 = GotchiUIState(state="speaking", skin="gotchi", anim_frame=1)
    im_spk_f0 = render_sprite_200x200(ui_spk_f0)
    im_spk_f1 = render_sprite_200x200(ui_spk_f1)
    # Head and equalizer move between frame 0 (dy=-1) and frame 1 (dy=1)
    assert count_dirty_pixels(im_spk_f0, im_spk_f1) > 0


def test_three_frame_animation_loops():
    # 1. LISTENING: pulsing soundwave arcs across 3 frames
    f0 = render_sprite_200x200(GotchiUIState(state="listening", anim_frame=0))
    f1 = render_sprite_200x200(GotchiUIState(state="listening", anim_frame=1))
    f2 = render_sprite_200x200(GotchiUIState(state="listening", anim_frame=2))
    assert count_dirty_pixels(f0, f1) > 0
    assert count_dirty_pixels(f1, f2) > 0
    assert count_dirty_pixels(f0, f2) > 0

    # 2. THINKING: 1, 2, 3 ellipsis dots
    t0 = render_sprite_200x200(GotchiUIState(state="thinking", anim_frame=0))
    t1 = render_sprite_200x200(GotchiUIState(state="thinking", anim_frame=1))
    t2 = render_sprite_200x200(GotchiUIState(state="thinking", anim_frame=2))
    assert count_dirty_pixels(t0, t1) > 0
    assert count_dirty_pixels(t1, t2) > 0

    # 3. SPEAKING: 3 mouth shapes + bouncing 5-bar equalizer
    s0 = render_sprite_200x200(GotchiUIState(state="speaking", anim_frame=0))
    s1 = render_sprite_200x200(GotchiUIState(state="speaking", anim_frame=1))
    s2 = render_sprite_200x200(GotchiUIState(state="speaking", anim_frame=2))
    assert count_dirty_pixels(s0, s1) > 0
    assert count_dirty_pixels(s1, s2) > 0


def test_count_dirty_pixels():
    from PIL import Image
    im1 = Image.new("1", (200, 200), 255)
    im2 = Image.new("1", (200, 200), 255)
    assert count_dirty_pixels(im1, im2) == 0

    # Flip 10 pixels
    for x in range(10):
        im2.putpixel((x, 0), 0)
    assert count_dirty_pixels(im1, im2) == 10


def test_parse_sse_stream_and_render_sprite():
    raw_pcm = b"\x10\x00" * 240
    b64_pcm = base64.b64encode(raw_pcm).decode()
    sse_lines = [
        "event: state",
        'data: {"state": "transcribing"}',
        "",
        "event: transcript",
        'data: {"transcript": "Hello Gotchi", "speaker": "ryan"}',
        "",
        "event: thinking",
        'data: {"elapsed_seconds": 1}',
        "",
        "event: reply",
        'data: {"reply": "Loud and clear.", "tts_engine": "kokoro"}',
        "",
        "event: audio_chunk",
        f'data: {{"chunk_index": 0, "format": "pcm", "sample_rate": 24000, "channels": 1, "is_final": true, "data": "{b64_pcm}"}}',
        "",
        "event: done",
        'data: {"duration_ms": 840}',
        "",
    ]
    states_seen = []
    pcm_chunks = []
    ui = parse_sse_stream(
        sse_lines,
        on_state_change=lambda s: states_seen.append(s.state),
        on_audio_pcm=lambda pcm, sr, final: pcm_chunks.append((pcm, sr, final)),
    )
    assert ui.state == "idle"
    assert ui.transcript == "Hello Gotchi"
    assert ui.reply == "Loud and clear."
    assert len(pcm_chunks) == 1
    assert pcm_chunks[0][0] == raw_pcm
    assert pcm_chunks[0][1] == 24000
    assert pcm_chunks[0][2] is True

    im = render_sprite_200x200(ui, refresh_mode="partial")
    assert im.size == (200, 200)
    assert im.mode == "1"


def test_parse_sse_live_sentence_stream():
    # Live gateway shape (2026-10-18): turn, state, transcript, sentence x2, done.
    pcm_a = b"\x10\x00" * 300  # 600 bytes
    pcm_b = b"\x20\x00" * 50   # 100 bytes
    sse_lines = [
        "event: turn",
        'data: {"turn_id": "t-1"}',
        "",
        "event: state",
        'data: {"state": "transcribing"}',
        "",
        "event: transcript",
        'data: {"text": "hello", "transcript": "hello", "speaker": ""}',
        "",
        "event: state",
        'data: {"state": "speaking"}',
        "",
        "event: sentence",
        'data: {"text": "one", "engine": "kokoro", "format": "pcm", "sample_rate": 24000, '
        '"channels": 1, "audio_b64": "' + base64.b64encode(pcm_a).decode() + '"}',
        "",
        "event: sentence",
        'data: {"text": "two", "engine": "kokoro", "format": "pcm", "sample_rate": 24000, '
        '"channels": 1, "audio_b64": "' + base64.b64encode(pcm_b).decode() + '"}',
        "",
        "event: done",
        'data: {"duration_ms": 1234}',
        "",
    ]
    pcm_chunks = []
    ui = parse_sse_stream(sse_lines, on_audio_pcm=lambda pcm, sr, final: pcm_chunks.append((pcm, sr, final)))
    assert ui.audio_bytes_received == len(pcm_a) + len(pcm_b) == 700
    assert len(pcm_chunks) == 2
    assert pcm_chunks[0][0] == pcm_a and pcm_chunks[1][0] == pcm_b
    assert all(sr == 24000 and final is False for _, sr, final in pcm_chunks)
    assert ui.transcript == "hello"
    assert ui.state == "idle"  # `done` is the last state change
    assert ui.status_text == "DONE (1234ms)"


def test_render_zero_skin_all_9_states():
    from poc.gotchi_poc import VALID_STATES
    for st in VALID_STATES:
        ui = GotchiUIState(state=st, skin="zero", status_text=f"TEST {st.upper()}")
        im = render_sprite_200x200(ui, refresh_mode="partial")
        assert im.size == (200, 200)
        assert im.mode == "1"
        assert im.getpixel((0, 0)) == 255
        assert im.getpixel((199, 0)) == 255


def test_multipart_payload_with_snapshot_and_prompt():
    wav = wrap_pcm16_to_wav(b"\x00\x00" * 4000)
    fake_jpeg = b"\xff\xd8\xff\xe0JFIF\x00\xff\xd9"
    body, _ = build_multipart_wav(
        wav,
        node_id="gotchi-v1-01",
        session_id="s-snap",
        image_jpeg=fake_jpeg,
        prompt="Describe what you see in this snapshot briefly.",
    )
    assert b'name="image"; filename="snap.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert fake_jpeg in body
    assert b'name="prompt"' in body
    assert b"Describe what you see in this snapshot briefly." in body


def test_is_whole_utterance_go_live():
    from poc.gotchi_poc import is_whole_utterance_go_live
    assert is_whole_utterance_go_live("Go Live") is True
    assert is_whole_utterance_go_live("  go   live! ") is True
    assert is_whole_utterance_go_live("Go live.") is True
    assert is_whole_utterance_go_live("let us go live now") is False
    assert is_whole_utterance_go_live("go live mode") is False
    assert is_whole_utterance_go_live("") is False


def test_firmware_dynamic_cpu_clock_switch_and_ota_wifi_sleep():
    """Verify ESP32-S3 dynamic CPU clock scaling (80 MHz idle / 240 MHz active via ScopedCpuBoost)

    pairs with coexistence-safe WIFI_PS_MIN_MODEM during active turns and OTA uploads.
    """
    from pathlib import Path
    main_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")

    assert "setCpuFrequencyMhz(80);" in main_cpp
    assert "setCpuFrequencyMhz(240);" in main_cpp
    assert "struct ScopedCpuBoost" in main_cpp
    assert "WiFi.setSleep(WIFI_PS_MIN_MODEM);" in main_cpp
    assert "WiFi.setSleep(WIFI_PS_MAX_MODEM);" in main_cpp

    # OTA UPLOAD_FILE_START boosts to 240 MHz and sets WIFI_PS_MIN_MODEM
    assert "if (up.status == UPLOAD_FILE_START) {" in main_cpp
    assert "if (WiFi.status() == WL_CONNECTED) WiFi.setSleep(WIFI_PS_MIN_MODEM);" in main_cpp


def test_firmware_epd_panel_constants_and_idle_scrub_methods():
    """Verify ESP32 epd_panel.h and epd_panel.cpp match RefreshTracker policy:

    35 partials, 80 hard ceiling, 140k dirty pixel budget, 75s idle scrub deferral,
    340ms partial settle, bit-diff dirty pixel tracking, and blink-masked full refresh.
    """
    from pathlib import Path
    epd_h = (Path(__file__).resolve().parent.parent / "firmware" / "include" / "epd_panel.h").read_text(encoding="utf-8")
    epd_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "epd_panel.cpp").read_text(encoding="utf-8")

    # Header constants
    assert "MAX_CONSECUTIVE_PARTIALS      = 35;" in epd_h or "MAX_CONSECUTIVE_PARTIALS = 35;" in epd_h
    assert "MAX_PARTIALS_HARD_CEILING     = 80;" in epd_h or "MAX_PARTIALS_HARD_CEILING = 80;" in epd_h
    assert "MAX_DIRTY_PIXELS_BEFORE_SCRUB = 140000UL;" in epd_h
    assert "IDLE_SCRUB_DEFER_MS          = 75000UL;" in epd_h or "IDLE_SCRUB_DEFER_MS = 75000UL;" in epd_h

    # Header methods
    assert "bool needsIdleScrub() const;" in epd_h
    assert "uint32_t cumulativeDirtyPixels() const;" in epd_h
    assert "bool drawBlinkMaskedFull(const GotchiUIState& ui);" in epd_h

    # Implementation: waveform settle times (2650ms full / 340ms partial)
    assert "min_waveform_ms = full ? 2650UL : 340UL;" in epd_cpp

    # Implementation: dirty pixel tracking using __builtin_popcount bit differences
    assert "__builtin_popcount" in epd_cpp
    assert "s_prev_frame[i] ^ raw[i]" in epd_cpp
    assert "rtc_dirty_pixels += " in epd_cpp or "rtc_dirty_pixels +=" in epd_cpp

    # Implementation: full refresh resets rtc_dirty_pixels to 0
    assert "rtc_dirty_pixels = 0;" in epd_cpp

    # Implementation: needsIdleScrub logic
    assert "EpdPanel::needsIdleScrub() const" in epd_cpp
    assert "MAX_CONSECUTIVE_PARTIALS" in epd_cpp
    assert "MAX_DIRTY_PIXELS_BEFORE_SCRUB" in epd_cpp

    # Implementation: blink-masked full refresh
    assert "EpdPanel::drawBlinkMaskedFull(const GotchiUIState& ui)" in epd_cpp
    assert "blink = true" in epd_cpp

    # Implementation: periodic full refresh gated on unattended idle time
    assert "IDLE_SCRUB_DEFER_MS" in epd_cpp

    # Implementation: non-blocking ~10ms partial refresh, warm HV rails & direct GPIO
    assert "bool isPanelBusy() const;" in epd_h
    assert "rails_warm_" in epd_h
    assert "async_refresh_active_" in epd_h
    assert "bool EpdPanel::isPanelBusy() const" in epd_cpp
    assert "rails_warm_" in epd_cpp
    assert "async_refresh_active_" in epd_cpp
    assert "0xFC" in epd_cpp
    assert "GPIO.out_w1ts" in epd_cpp


def test_firmware_sprite_render_and_main_idle_scrub():
    """Verify ESP32 sprite_render.cpp and main.cpp implementation:

    - Head posture shifts (dx, dy offsets for LISTENING, THINKING, SPEAKING)
    - Closed horizontal eyelids when ui.blink is true
    - 3-frame animation loops (anim_frame % 3) for LISTENING, THINKING, SPEAKING
    - Idle scrub timer tracking (s_idle_entered_ts) and blink-masked full scrub in loop()
    - Stepping anim_frame during active voice states
    """
    from pathlib import Path
    sprite_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "sprite_render.cpp").read_text(encoding="utf-8")
    main_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")

    # sprite_render.cpp: Head posture shifts
    assert "ui.state == UiState::LISTENING" in sprite_cpp
    assert "dy = -2;" in sprite_cpp
    assert "ui.state == UiState::THINKING || ui.state == UiState::SYNTHESIZING" in sprite_cpp
    assert "dx = -2;" in sprite_cpp
    assert "ui.state == UiState::SPEAKING" in sprite_cpp
    assert "(ui.anim_frame % 2 == 0) ? -1 : 1" in sprite_cpp

    # sprite_render.cpp: 3-frame animation loop index
    assert "const uint8_t frame = ui.anim_frame % 3;" in sprite_cpp

    # sprite_render.cpp: Closed horizontal eyelid rendering when ui.blink is true
    assert "ui.blink" in sprite_cpp
    # Zero skin blink: black box over eyes, white horizontal lines
    assert "thickLine(d, 45 + dx, 82 + dy, 68 + dx, 82 + dy, 3, GxEPD_WHITE);" in sprite_cpp
    assert "thickLine(d, 87 + dx, 78 + dy, 114 + dx, 78 + dy, 3, GxEPD_WHITE);" in sprite_cpp
    # Gotchi skin blink: black horizontal lines
    assert "thickLine(d, 64 + dx, 70 + dy, 84 + dx, 70 + dy, 3, GxEPD_BLACK);" in sprite_cpp
    assert "thickLine(d, 116 + dx, 70 + dy, 136 + dx, 70 + dy, 3, GxEPD_BLACK);" in sprite_cpp

    # sprite_render.cpp: Multi-frame animations for LISTENING, THINKING, SPEAKING
    # LISTENING soundwave arcs
    assert "arcs_left" in sprite_cpp
    assert "arcs_right" in sprite_cpp
    assert "arcs_l" in sprite_cpp
    assert "arcs_r" in sprite_cpp

    # THINKING ellipsis dots
    assert "dot_xs" in sprite_cpp

    # SPEAKING mouth shapes and equalizer patterns
    assert "eq_patterns" in sprite_cpp
    assert "fillLowerHalfEllipse" in sprite_cpp

    # Paper-white backdrop: black side-boxes removed from LISTENING and SPEAKING
    assert "d.fillRect(2, 50, 29, 81, GxEPD_BLACK);" not in sprite_cpp
    assert "d.fillRect(170, 50, 28, 81, GxEPD_BLACK);" not in sprite_cpp
    assert "d.fillRect(5, 108, 31, 27, GxEPD_BLACK);" not in sprite_cpp

    # main.cpp: Idle scrub timer tracking
    assert "static uint32_t s_idle_entered_ts = 0;" in main_cpp
    assert "s_idle_entered_ts = millis();" in main_cpp

    # main.cpp: Blink-masked scrub in loop() when unattended idle >= IDLE_SCRUB_DEFER_MS
    assert "panel.needsIdleScrub()" in main_cpp
    assert "IDLE_SCRUB_DEFER_MS" in main_cpp
    assert "panel.drawBlinkMaskedFull(idle_ui);" in main_cpp

    # main.cpp: Stepping anim_frame during active voice states
    assert "s_voice_anim_frame" in main_cpp
    assert "enriched.anim_frame = s_voice_anim_frame;" in main_cpp


def test_firmware_voice_animation_frame_stepping():
    """Verify live animation frame stepping (Listening/Thinking/Speaking):

    1. main.cpp:
       - dispatchVoiceOrSnap calls onVoiceState(ui) upon entering THINKING
       - dispatchVoiceOrSnap avoids showIdle() screen wipe if s_reopen_mic_pending is true
       - runPttTurn calls onVoiceState(ui) upon entering LISTENING and stamps last_anim_step_ms post-draw
       - runPttTurn recording loop steps onVoiceState(ui) every 650ms while Core 0 drains I2S
       - s_ptt_release_watch / s_ptt_tap_stop_watch latch button changes during screen refreshes
       - released_once && s_ptt_tap_stop_ts != 0 finishes recording on tap
    2. voice_client.cpp:
       - Tracks s_active_ui, s_active_cb, s_sse_can_step_thinking, and s_last_anim_step_ms
       - Steps s_active_cb(*s_active_ui) every 550ms on Core 1 inside checkAbortFlag() during THINKING and SPEAKING
       - Only executes abort polling and animation stepping on Core 1 (xPortGetCoreID() == 1)
       - THINKING stepping is gated on !hasBufferedBytes() (s_sse_can_step_thinking)
       - s_last_anim_step_ms is stamped AFTER s_active_cb(*s_active_ui) and cb(ui) post-draw
       - Calls commitTurnSentenceForPlayback() before playTurnPcm() so audio plays on Core 0
    """
    from pathlib import Path

    main_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
    voice_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "voice_client.cpp").read_text(encoding="utf-8")

    # 1. main.cpp: LISTENING and THINKING animation entry & periodic stepping
    assert "ui.state = UiState::THINKING;\n    ui.status_text = is_standalone_snap ? \"SENDING SNAP...\" : \"THINKING...\";\n    onVoiceState(ui);" in main_cpp
    assert "ui.state = UiState::LISTENING;\n    ui.status_text = s_staged_jpeg_active ? \"LISTENING (+SNAP)...\" : \"LISTENING...\";\n    onVoiceState(ui);" in main_cpp

    # Reopen mic pending bypasses showIdle() screen wipe
    assert "if (s_reopen_mic_pending) {\n        setVoiceUiState(UiState::IDLE);\n    } else {\n        showIdle();\n    }" in main_cpp

    # Periodic 650ms stepping in recording loop with s_ptt_release_watch and s_ptt_tap_stop_watch latches
    assert "now - last_anim_step_ms >= 650" in main_cpp
    assert "s_ptt_release_watch = true;" in main_cpp
    assert "s_ptt_release_watch = false;" in main_cpp
    assert "s_ptt_tap_stop_watch = true;" in main_cpp
    assert "s_ptt_tap_stop_watch = false;" in main_cpp
    assert "last_anim_step_ms = millis();" in main_cpp
    assert "s_ptt_release_ts != 0 || btnStable(PIN_BTN_TOP_PTT, false)" in main_cpp
    assert "released_once && s_ptt_tap_stop_ts != 0" in main_cpp

    # 2. voice_client.cpp: Active UI/CB tracking, Core 1 isolation, and post-draw frame stepping in checkAbortFlag()
    assert "s_active_ui" in voice_cpp
    assert "s_active_cb" in voice_cpp
    assert "s_last_anim_step_ms" in voice_cpp
    assert "s_sse_can_step_thinking" in voice_cpp
    assert "hasBufferedBytes" in voice_cpp
    assert "if (xPortGetCoreID() == 1)" in voice_cpp
    assert "s_active_ui->state != UiState::THINKING || s_sse_can_step_thinking" in voice_cpp
    assert "s_active_ui->state == UiState::THINKING || s_active_ui->state == UiState::SPEAKING" in voice_cpp
    assert "now - s_last_anim_step_ms >= 550" in voice_cpp
    assert "s_active_cb(*s_active_ui);\n                        s_last_anim_step_ms = millis();" in voice_cpp

    # State events in THINKING invoking cb(ui) and stamping timer post-draw
    assert "(next != ui.state || next == UiState::THINKING) && next != UiState::SPEAKING" in voice_cpp
    assert "if (cb) cb(ui);\n            s_last_anim_step_ms = millis();" in voice_cpp

    # commitTurnSentenceForPlayback() called before playTurnPcm()
    assert "gotchi_audio::commitTurnSentenceForPlayback();\n            gotchi_audio::playTurnPcm();" in voice_cpp


def test_battery_idle_redraw_and_plug_unplug_contracts():
    """5-min idle polling redraw, plug/unplug redraw, and charging %+ badge contracts."""
    from pathlib import Path

    main_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
    sprite_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "sprite_render.cpp").read_text(encoding="utf-8")

    # Track drawn battery pct and wall power in showIdle
    assert "s_last_drawn_battery_pct" in main_cpp
    assert "s_last_drawn_wall_power" in main_cpp
    assert "s_last_drawn_battery_pct = s_battery_pct;" in main_cpp
    assert "s_last_drawn_wall_power = isExternalPowerConnected();" in main_cpp

    # Immediate plug/unplug redraw in IDLE
    assert "isExternalPowerConnected() != s_last_drawn_wall_power" in main_cpp

    # Unplug settle completion redraw in IDLE
    assert "unplug_settle_done" in main_cpp

    # 5-minute idle redraw when battery pct changes (on battery or wall power)
    assert "IDLE_BAT_LOG_INTERVAL_MS" in main_cpp
    assert "if (s_battery_pct != s_last_drawn_battery_pct)" in main_cpp

    # Charging %+ badge formatted as "%d%%+" when plugged in with valid battery percentage
    assert 'snprintf(bat_badge, sizeof(bat_badge), "%d%%+", (int)ui.battery_pct);' in sprite_cpp
    assert 'snprintf(bat_badge, sizeof(bat_badge), "CHG");' in sprite_cpp
    assert 'snprintf(bat_badge, sizeof(bat_badge), "%d%%", (int)ui.battery_pct);' in sprite_cpp

    # Top-right badge placement on Zero skin (y=6)
    assert "badge_x = 195 - (int)strlen(bat_badge) * 6;" in sprite_cpp
    assert "d.fillRect(badge_x - 2, 4, 198 - (badge_x - 2), 11, GxEPD_WHITE);" in sprite_cpp
    assert "d.setCursor(badge_x, 6);" in sprite_cpp

    # Verify poc/gotchi_poc.py renders %d%%+ badge when wall_power is True and battery_pct >= 0
    ui_chg = GotchiUIState(state="idle", battery_pct=42, wall_power=True, skin="zero")
    im_chg = render_sprite_200x200(ui_chg)
    assert im_chg.size == (200, 200)

    ui_bat = GotchiUIState(state="idle", battery_pct=75, wall_power=False, skin="zero")
    im_bat = render_sprite_200x200(ui_bat)
    assert im_bat.size == (200, 200)

    ui_unwired = GotchiUIState(state="idle", battery_pct=-1, wall_power=True, skin="zero")
    im_unwired = render_sprite_200x200(ui_unwired)
    assert im_unwired.size == (200, 200)


def test_epd_panel_ghosting_fix_and_core_link():
    """Verify EpdPanel preserves un-clamped partial refresh for mid-turn animations
    while re-driving solid blacks on synchronous return-to-idle partial refresh,
    verifies clean line-art Zero portrait bitmap header, and links gotchi_core.cpp at EOF.
    """
    from pathlib import Path
    fw_dir = Path(__file__).resolve().parent.parent / "firmware"
    epd_cpp = (fw_dir / "src" / "epd_panel.cpp").read_text(encoding="utf-8")
    zero_skin_h = (fw_dir / "include" / "zero_skin_bitmap.h").read_text(encoding="utf-8")

    # 1. Non-idle async partial refresh branch must be UN-CLAMPED (no ghosting clamp)
    assert "if (!full && ui.state != UiState::IDLE) {" in epd_cpp
    non_idle_branch = epd_cpp.split("if (!full && ui.state != UiState::IDLE) {")[1].split("return true;")[0]
    assert "s_prev_frame[i] |=" not in non_idle_branch

    # 2. Synchronous return-to-idle partial refresh branch MUST re-drive solid blacks
    assert "s_prev_frame[i] |= static_cast<uint8_t>(~raw[i]);" in epd_cpp
    return_to_idle_branch = epd_cpp.split("if (full || !init_done_ || hibernating_) {")[1]
    assert "s_prev_frame[i] |= static_cast<uint8_t>(~raw[i]);" in return_to_idle_branch

    # 3. Clean line-art Zero portrait header and symbol
    assert "Clean Line-Art Paper-White Zero portrait" in zero_skin_h
    assert "ZERO_BASE_PORTRAIT_196x134" in zero_skin_h
    assert "ZERO_IDLE_PORTRAIT_196x134" in zero_skin_h

    # 4. Shared core link
    assert '#include "../../core/src/gotchi_core.cpp"' in epd_cpp


def test_sprite_render_a1_hand_and_scribbling_pencil():
    """Verify firmware/src/sprite_render.cpp implementation:
    - Clamped fillRect with max(0, paste_y) and min(177, paste_y + 177)
    - dx = 0 on THINKING/SYNTHESIZING for Zero skin
    - Expanded blink boxes (40+dx, 64+dy, 32, 41) and (82+dx, 60+dy, 38, 43) gated on IDLE, LISTENING, SPEAKING, TRANSCRIBING
    - SPEAKING frame 0 teeth bar y = 128 + dy (h=3)
    - SPEAKING frame 2 rounded slot (63+dx, 127+dy, 29, 7, 3) with inner white bar
    - Equalizer shifted to 4 + i * 5
    - A1 1-bit humanoid hand bitmap drawing (ZERO_HAND_A1_MASK_88x65 and ZERO_HAND_A1_WHITE_88x65)
    - TRANSCRIBING visor eyes (45+frame, 84, 17, 17, 5) and (88+frame, 81, 20, 18, 6)
    - TRANSCRIBING mouth line (65, 131) to (90, 129)
    - Wave points and scribbling pencil
    """
    from pathlib import Path
    sprite_cpp = (Path(__file__).resolve().parent.parent / "firmware" / "src" / "sprite_render.cpp").read_text(encoding="utf-8")

    # Include header
    assert '#include "zero_thinking_hand_1bit.h"' in sprite_cpp

    # Clamped fillRect
    assert "d.fillRect(paste_x, max(0, paste_y), 196, min(177, paste_y + 177) - max(0, paste_y), GxEPD_BLACK);" in sprite_cpp

    # Expanded blink
    assert "d.fillRect(40 + dx, 64 + dy, 32, 41, GxEPD_BLACK);" in sprite_cpp
    assert "d.fillRect(82 + dx, 60 + dy, 38, 43, GxEPD_BLACK);" in sprite_cpp
    assert "ui.state == UiState::TRANSCRIBING" in sprite_cpp

    # Speaking mouth & equalizer
    assert "d.fillRect(65 + dx, 128 + dy, 28, 3, GxEPD_WHITE);" in sprite_cpp
    assert "d.fillRoundRect(63 + dx, 127 + dy, 29, 7, 3, GxEPD_BLACK);" in sprite_cpp
    assert "d.fillRect(4 + i * 5, 121 - bh / 2, 3, bh, GxEPD_BLACK);" in sprite_cpp

    # A1 Thinking Hand
    assert "ZERO_HAND_A1_MASK_88x65" in sprite_cpp
    assert "ZERO_HAND_A1_WHITE_88x65" in sprite_cpp
    assert "18 + hx, 112 + hy" in sprite_cpp

    # Transcribing visor eyes, mouth, wave & pencil
    assert "d.fillRoundRect(45 + frame, 84, 17, 17, 5, GxEPD_WHITE);" in sprite_cpp
    assert "d.fillRoundRect(88 + frame, 81, 20, 18, 6, GxEPD_WHITE);" in sprite_cpp
    assert "thickLine(d, 65, 131, 90, 129, 3, GxEPD_BLACK);" in sprite_cpp
    assert "wave_pts" in sprite_cpp
    assert "end_idx = 2 + frame * 2" in sprite_cpp


def test_zero_skin_thinking_and_transcribing_animation_frames():
    """Verify poc/gotchi_poc.py multi-frame animations for Zero skin:
    - Thinking A1 hand strokes chin across frames 0, 1, 2
    - Transcribing wave and pencil progress across frames 0, 1, 2
    - Blink closed eyelids across states
    """
    # 1. Thinking A1 hand movement
    th0 = render_sprite_200x200(GotchiUIState(state="thinking", skin="zero", anim_frame=0))
    th1 = render_sprite_200x200(GotchiUIState(state="thinking", skin="zero", anim_frame=1))
    th2 = render_sprite_200x200(GotchiUIState(state="thinking", skin="zero", anim_frame=2))
    assert count_dirty_pixels(th0, th1) > 0
    assert count_dirty_pixels(th1, th2) > 0
    assert count_dirty_pixels(th0, th2) > 0

    # 2. Transcribing wave & pencil progression
    tr0 = render_sprite_200x200(GotchiUIState(state="transcribing", skin="zero", anim_frame=0))
    tr1 = render_sprite_200x200(GotchiUIState(state="transcribing", skin="zero", anim_frame=1))
    tr2 = render_sprite_200x200(GotchiUIState(state="transcribing", skin="zero", anim_frame=2))
    assert count_dirty_pixels(tr0, tr1) > 0
    assert count_dirty_pixels(tr1, tr2) > 0
    assert count_dirty_pixels(tr0, tr2) > 0

    # 3. Transcribing blink vs open
    tr_blink = render_sprite_200x200(GotchiUIState(state="transcribing", skin="zero", anim_frame=0, blink=True))
    assert count_dirty_pixels(tr0, tr_blink) > 0

    # 4. Speaking mouth animation
    sp0 = render_sprite_200x200(GotchiUIState(state="speaking", skin="zero", anim_frame=0))
    sp1 = render_sprite_200x200(GotchiUIState(state="speaking", skin="zero", anim_frame=1))
    sp2 = render_sprite_200x200(GotchiUIState(state="speaking", skin="zero", anim_frame=2))
    assert count_dirty_pixels(sp0, sp1) > 0
    assert count_dirty_pixels(sp1, sp2) > 0






