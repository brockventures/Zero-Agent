#pragma once
// ============================================================================
// GOTCHI Go Live Bidirectional Streaming Client (F19 / F21 / Section 5b)
//   - RFC 6455 WebSocket upgrade over WiFiClient to `/api/live/ws`:
//     - Uplink: 16 kHz 16-bit mono PCM binary frames (`opcode 0x02`).
//     - Downlink: 24 kHz 16-bit mono PCM JSON frames (`{"type":"audio","audio_b64":"..."}`)
//       played over MAX98357A I2S (or muted when `audio_out_mode` is `discord_voice` / `ble_phone`).
//   - Mid-session multimodal injection via BTN_BOT:
//     - Short press: single snapshot JPEG encoded as base64 JSON text frame (`{"type":"image",...}`).
//     - Long press (>= cam_long_press_ms): 5-photo burst over 3s injected sequentially.
//   - Exit triggers:
//     - BTN_TOP single press immediately ends session.
//     - VAD silence timeout (configurable live_silence_ms, default 15000 ms).
// ============================================================================

#include <Arduino.h>
#include "audio_hal.h"
#include "ui_state.h"
#include "voice_client.h"

namespace gotchi_live {

enum class SessionEndReason : uint8_t {
    USER_EXIT       = 0, // BTN_TOP pressed
    SILENCE_TIMEOUT = 1, // live_silence_ms elapsed without speech
    NETWORK_ERROR   = 2, // Connection dropped or failed
    LIVE_DISABLED   = 3, // live_mode disabled (avoids Arduino #define DISABLED 0x00 macro)
};

struct LiveSessionConfig {
    String voice_url = "";
    String node_id = "gotchi-v1-01";
    String bearer_token = "";
    String audio_out_mode = "speaker";
    uint32_t live_silence_ms = 15000;
    uint32_t vad_rms_lsb = 150;
    uint32_t cam_long_press_ms = 700;
    bool enabled = false;
    bool live_barge_in = false;
};

struct CapturedFrameView {
    const uint8_t* data = nullptr;
    size_t length = 0;
};

// Snapshot capture callback type for mid-session camera injection
using SnapshotCallback = bool (*)(size_t slot_idx, CapturedFrameView* out_frame);
using BurstCallback = size_t (*)(CapturedFrameView* out_frames, size_t max_frames);
using PollTickCallback = void (*)();

// Converts an HTTP(S)/WS(S) voice_url (e.g. "https://gotchi.example.com/api/voice/interact" or "http://host:8765/api/voice/interact")
// into host, port, "/api/live/ws" path, and TLS flag.
bool parseLiveUrl(const String& url, String& host, uint16_t& port, String& ws_path, bool* out_tls = nullptr);

// Runs the interactive bidirectional Go Live session loop until exit.
SessionEndReason runSession(
    const LiveSessionConfig& config,
    GotchiUIState& ui,
    gotchi_voice::StateCb cb,
    SnapshotCallback snap_cb = nullptr,
    BurstCallback burst_cb = nullptr,
    PollTickCallback poll_tick_cb = nullptr
);

} // namespace gotchi_live
