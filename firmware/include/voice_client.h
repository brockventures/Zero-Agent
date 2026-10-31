#pragma once
// ============================================================================
// GOTCHI Voice Gateway Client (T3.1) — POST /api/voice/interact.
//   - Multipart WAV (+ optional JPEG snapshot & prompt) upload over WiFiClient.
//   - Streaming SSE reply parser; `sentence` / `audio_chunk` PCM plays as it
//     arrives through gotchi_audio::playBase64PcmChunk with 42ms Barge-In / Cancel poll.
//   - Supports configurable `audio_out_mode`: "speaker" | "discord_voice" | "ble_phone" (F8/T3.3).
// Wire contract: docs/MIRRORMERE_VOICE_CLIENT.md & backend/voice_gateway.py.
// ============================================================================

#include <Arduino.h>
#include "audio_hal.h"
#include "ui_state.h"

namespace gotchi_voice {

using StateCb = void (*)(const GotchiUIState&);

enum class InteractResult : uint8_t {
    DONE           = 0,
    ERROR          = 1,
    ABORT_BARGE_IN = 2,
    ABORT_CANCEL   = 3,
};

struct ImageAttachment {
    static constexpr size_t MAX_BURST_FRAMES = 5;
    const uint8_t* data = nullptr;
    size_t length = 0;
    const uint8_t* burst_data[MAX_BURST_FRAMES] = {nullptr};
    size_t burst_lengths[MAX_BURST_FRAMES] = {0};
    size_t burst_count = 0;
    String default_prompt = "";
};

// Polled during SSE wait and 42ms speaker slices. Return DONE to continue,
// ABORT_BARGE_IN if BTN_TOP pressed, or ABORT_CANCEL if BTN_BOT pressed.
using AbortPollFn = InteractResult (*)();

// Uploads the WAV (+ optional JPEG attachment & prompt), parses the SSE reply,
// and plays audio as it arrives (unless audio_out_mode is discord_voice or ble_phone).
// Returns true if the stream reached `done`.
bool interact(const gotchi_audio::WavView& wav, const String& url, const String& node_id,
              GotchiUIState& ui, StateCb cb, const String& bearer_token = "",
              const ImageAttachment* image = nullptr, AbortPollFn abort_poll = nullptr,
              InteractResult* out_result = nullptr, const String& audio_out_mode = "speaker");

// Silently uploads a passive All-Day Journal speech segment (`mode="journal"`, F24 / T3.5)
// over BLE (when bonded to the phone relay with Wi-Fi off) or Wi-Fi HTTP without waking
// the speaker or refreshing the e-paper display.
bool uploadJournalSegment(const gotchi_audio::WavView& wav, const String& url,
                          const String& node_id, const String& bearer_token = "");

// Helper to construct the pending answer SSE endpoint URL from the configured voice_url.
String buildPendingAnswerUrl(const String& voice_url, const String& node_id,
                             const String& answer_id = "");

// Acknowledges receipt of a pending async voice answer (POST /api/gotchi/pending_answer/ack).
bool ackPendingAnswer(const String& voice_url, const String& node_id,
                      const String& answer_id = "", const String& bearer_token = "");

// Fetches and streams a pending async spoken answer from the gateway via SSE (GET /api/gotchi/pending_answer),
// playing audio chunks through gotchi_audio with full BTN_TOP abort support.
// Automatically acknowledges the answer on success or cancel via POST /api/gotchi/pending_answer/ack.
bool fetchPendingAnswer(const String& voice_url, const String& node_id,
                        GotchiUIState& ui, StateCb cb, const String& bearer_token = "",
                        const String& answer_id = "", AbortPollFn abort_poll = nullptr,
                        InteractResult* out_result = nullptr, const String& audio_out_mode = "speaker");

} // namespace gotchi_voice
