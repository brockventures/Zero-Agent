#pragma once
// ============================================================================
// GOTCHI Portable Client Core (core/include/gotchi_core.h)
// Hardware-agnostic C++17 client core shared across ESP32-S3 firmware,
// Android NDK / JNI client engines, and desktop runners.
// Zero <Arduino.h> dependencies.
// ============================================================================

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>

#ifdef __cplusplus
namespace gotchi_core {

// Scoped: unscoped BUSY/ERROR collide with ESP-IDF ROM declarations (ets_sys.h).
enum class UiState : uint8_t {
    IDLE = 0,
    LISTENING = 1,
    TRANSCRIBING = 2,
    THINKING = 3,
    SYNTHESIZING = 4,
    SPEAKING = 5,
    ERROR = 6,
    BUSY = 7,
    OFFLINE = 8,
};

static constexpr uint8_t UI_STATE_COUNT = 9;

// Lowercase wire names, indexed by UiState (same spelling as Python VALID_STATES).
static const char* const UI_STATE_NAMES[UI_STATE_COUNT] = {
    "idle", "listening", "transcribing", "thinking", "synthesizing",
    "speaking", "error", "busy", "offline",
};

// 3-frame animation step result
#pragma pack(push, 1)
struct AnimStep {
    uint8_t frame; // 0, 1, 2
    bool blink;    // true if eyelids should be closed
};
#pragma pack(pop)

// ADPCM constants & container magic
constexpr uint32_t GAD1_MAGIC = 0x31444147; // "GAD1" in little-endian
constexpr size_t GAD1_HEADER_SIZE = 16;     // magic (4B) + sample_rate (4B) + num_samples (4B) + predictor (i16) + step_index (u8) + reserved (u8)

// Map string state name to enum. Unknown or null names map to IDLE.
extern "C" UiState uiStateFromString(const char* name);

// Deterministic 3-frame expression & blink cadence stepper.
// - state: current UiState enum
// - current_frame: previous frame index (0..2 or raw counter)
// - elapsed_ms: elapsed milliseconds (in state or delta tick; 200ms per frame, 3000ms blink cycle)
extern "C" AnimStep stepAnimationFrame(UiState state, uint8_t current_frame, uint32_t elapsed_ms);

// 4-bit IMA ADPCM Codec helpers (4:1 audio compression)
extern "C" size_t encodeImaAdpcm(
    const int16_t* pcm,
    size_t num_samples,
    uint8_t* out_adpcm,
    size_t out_cap,
    int32_t initial_predictor = 0,
    int32_t initial_step_index = 0
);

extern "C" size_t decodeImaAdpcm(
    const uint8_t* adpcm,
    size_t adpcm_len,
    size_t num_samples,
    int16_t* out_pcm,
    size_t out_max_samples,
    int32_t initial_predictor = 0,
    int32_t initial_step_index = 0
);

// Encodes a 16-bit mono RIFF WAV buffer (or raw PCM) into a 16-byte GAD1 container.
extern "C" size_t encodeWavToAdpcm(
    const uint8_t* wav_data,
    size_t wav_len,
    uint8_t* out_buf,
    size_t out_cap
);

// Decodes a GAD1 container (or raw 4-bit IMA ADPCM buffer) into 16-bit signed mono PCM.
extern "C" size_t decodeAdpcmToPcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate = nullptr
);

} // namespace gotchi_core

// Global aliases for drop-in ESP32 and C++ compatibility
using gotchi_core::UiState;
using gotchi_core::UI_STATE_COUNT;
using gotchi_core::UI_STATE_NAMES;
using gotchi_core::AnimStep;
using gotchi_core::uiStateFromString;
using gotchi_core::stepAnimationFrame;
using gotchi_core::encodeImaAdpcm;
using gotchi_core::decodeImaAdpcm;
using gotchi_core::encodeWavToAdpcm;
using gotchi_core::decodeAdpcmToPcm;
using gotchi_core::GAD1_MAGIC;
using gotchi_core::GAD1_HEADER_SIZE;

#endif // __cplusplus

// C / ctypes compatible interface with explicit gotchi_* prefixes
#ifdef __cplusplus
extern "C" {
#endif

#pragma pack(push, 1)
typedef struct GotchiAnimStepC {
    uint8_t frame;
    uint8_t blink;
} GotchiAnimStepC;
#pragma pack(pop)

uint8_t gotchi_ui_state_from_string(const char* name);
GotchiAnimStepC gotchi_step_animation_frame(uint8_t state, uint8_t current_frame, uint32_t elapsed_ms);

size_t gotchi_encode_ima_adpcm(
    const int16_t* pcm,
    size_t num_samples,
    uint8_t* out_adpcm,
    size_t out_cap,
    int32_t initial_predictor,
    int32_t initial_step_index
);

size_t gotchi_decode_ima_adpcm(
    const uint8_t* adpcm,
    size_t adpcm_len,
    size_t num_samples,
    int16_t* out_pcm,
    size_t out_max_samples,
    int32_t initial_predictor,
    int32_t initial_step_index
);

size_t gotchi_encode_wav_to_adpcm(
    const uint8_t* wav_data,
    size_t wav_len,
    uint8_t* out_buf,
    size_t out_cap
);

size_t gotchi_decode_adpcm_to_pcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate
);

#ifdef __cplusplus
}
#endif
