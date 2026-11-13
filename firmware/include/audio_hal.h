#pragma once
// ============================================================================
// GOTCHI Audio HAL — PDM Mic Capture (I2S0 RX) + MAX98357A Speaker (I2S1 TX)
// Hardware Pins (see include/pins.h):
//   - PDM Mic: CLK = GPIO 42, DATA = GPIO 41 (16 kHz, 16-bit mono)
//   - Speaker: BCLK = D7 (GPIO 44, silent at boot), LRCLK = D6 (GPIO 43), DIN = D9 (GPIO 8)
// ============================================================================

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace gotchi_audio {

struct WavView {
    const uint8_t* data;
    size_t length;
    float rms;
};

using AbortCheckFn = bool (*)();

// Initialize I2S0 (PDM RX at 16 kHz) and I2S1 (Standard I2S TX at 24 kHz)
// and allocate PSRAM circular pre-roll + 90-second WAV recording buffer.
bool init();

// Enable or disable continuous 500ms circular pre-roll sampling in idle loop.
void setPrerollEnabled(bool enabled);
bool isPrerollEnabled();

// Continuously sample PDM mic into the 500ms circular pre-roll ring buffer while idle.
void pumpPreroll();

// Enable or disable on-device NLMS Acoustic Echo Cancellation (AEC) and 300ms full-duplex
// speech barge-in detection. Scoped strictly to active Go Live sessions (off in normal PTT).
void setLiveAecEnabled(bool enabled, float barge_in_rms_thresh = 0.008f);
bool isLiveAecEnabled();

// Returns true (and clears the flag) if user speech triggered a >=300ms AEC barge-in
// during the most recent Go Live speaker playback chunk.
bool consumeLiveBargeInFlag();

// Drain any residual samples sitting in the I2S0 PDM RX DMA ring buffer after speaker playback.
void flushRxDmaBuffer();

// Apply 64-tap NLMS adaptive echo cancellation to a single 16 kHz mic sample against reference.
int16_t processLiveAecSample(int16_t mic_sample, int16_t ref_sample);

// Non-blocking read of 16 kHz 16-bit mono PCM samples from PDM mic into `dst` (up to `max_samples`).
// When Go Live AEC is enabled, subtracts speaker reference echo before computing `*out_rms`.
size_t readLivePcmChunk(int16_t* dst, size_t max_samples, float* out_rms = nullptr);

// Start a push-to-talk / tap-to-talk recording session: copies the 500ms pre-roll
// (if enabled and populated) into the start of the PSRAM WAV payload buffer.
// use_preroll=false skips the 500 ms pre-roll ring: the follow-up mic window after a reply
// passes false, because the ring was last filled before the previous turn and is stale.
bool beginPttRecording(bool use_preroll = true);

// Poll and append incoming PDM mic samples into the PSRAM WAV buffer.
// Returns total PCM samples recorded so far.
size_t pollPttRecording();

// Spawn a dedicated Core 0 FreeRTOS task that continuously drains I2S0 PDM RX
// while Core 1 plays chimes or refreshes the e-paper display over SPI.
bool startAsyncPttTask();

// Stop the background Core 0 PDM capture task before finalizing the WAV buffer.
void stopAsyncPttTask();

// Return total recorded PCM samples or elapsed recording duration in milliseconds.
size_t getRecordedSamples();
uint32_t getRecordedMs();

// Compute normalized RMS [0.0, 1.0] over the most recent `window_samples`
// (default 1600 samples = 100 ms at 16 kHz) for live Tap-to-Talk VAD silence detection.
float getRecentRms(size_t window_samples = 1600);

// Advance `cursor_samples` across newly recorded 20ms (320-sample) non-overlapping frames.
// Counts frames with RMS >= `rms_thresh` and crest factor (peak / RMS) <= 4.5 (rejecting
// sharp mechanical button clicks), updating `voiced_frames` and `last_voiced_sample`.
void scanSpeechFrames(size_t& cursor_samples, float rms_thresh, uint32_t& voiced_frames, size_t& last_voiced_sample);

// Finalize PTT recording: writes the 44-byte RIFF WAV header and returns buffer view.
WavView finishPttRecording();

// Build a minimal silent WAV in the PSRAM buffer (used when dispatching a standalone
// camera snapshot to gateways that require an `audio` part on /api/voice/interact).
WavView buildSilentWav(uint32_t duration_ms = 250);

// Register an optional callback checked every ~42 ms (1024 samples) during speaker playback.
// If the callback returns true, playback aborts and mutes immediately (for Barge-In / Cancel).
void setPlaybackAbortCheck(AbortCheckFn fn);

// Play raw 16-bit signed mono PCM chunk over MAX98357A I2S TX (honors dynamic sample_rate, default 24000 Hz)
bool playPcmChunk(const int16_t* samples, size_t sample_count, uint32_t sample_rate = 24000);

// Buffer turn audio in PSRAM during Wi-Fi SSE stream and stream completed sentences on Core 0
void clearTurnPcm();
bool appendBase64PcmToTurn(const char* b64_data, size_t b64_len, uint32_t sample_rate = 24000, bool is_adpcm = false);
void commitTurnSentenceForPlayback();
size_t getTurnPcmSamples();
bool playTurnPcm();

// Play base64-encoded PCM chunk directly from Mirrormere `sentence` / `audio_chunk` SSE event
bool playBase64PcmChunk(const char* b64_data, size_t b64_len, uint32_t sample_rate = 24000);

// Play base64-encoded 4-bit IMA ADPCM (`GAD1` or raw nibbles) chunk transcoded by GotchiBleRelayService
bool playBase64AdpcmChunk(const char* b64_data, size_t b64_len, uint32_t sample_rate = 24000);

// Validate whether `kind` is an allowed UI chime name ("ready", "ptt_start", "ptt_stop", "snap", "thinking", "error", "")
bool isValidChimeKind(const char* kind);

// Default UI chime playback volume percentage (35% of 24,000 LSB ceiling)
constexpr uint8_t DEFAULT_CHIME_VOLUME_PCT = 35;
constexpr uint8_t MAX_CHIME_VOLUME_PCT     = 100;
constexpr float   CHIME_PEAK_CEILING_LSB   = 24000.0f;

// Set / get UI chime playback volume percentage (0..100%, defaults to 35% of 24,000 LSB ceiling)
void setChimeVolumePct(uint8_t pct);
uint8_t getChimeVolumePct();

// Play 1C-2A-2 (Warm Studio Fade) tactile reed chime or 35mm SLR shutter ("ptt_start", "ptt_stop", "snap", "thinking", "ready", "error")
void playChime(const char* kind);

// Stop I2S TX clocks so MAX98357A automatically enters <1 uA hardware shutdown
void muteSpeaker();

} // namespace gotchi_audio
