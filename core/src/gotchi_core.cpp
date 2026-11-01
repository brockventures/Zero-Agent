// ============================================================================
// GOTCHI Portable Client Core (core/src/gotchi_core.cpp)
// Hardware-agnostic C++17 client core implementation.
// Zero <Arduino.h> dependencies.
// ============================================================================

#include "gotchi_core.h"
#include <string.h>
#include <stdlib.h>

namespace gotchi_core {

namespace {

static const int8_t kImaIndexTable[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8
};

static const int16_t kImaStepTable[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};

static inline uint8_t encodeImaSample(int16_t sample, int32_t& predictor, int32_t& step_index) {
    int32_t step = kImaStepTable[step_index];
    int32_t diff = static_cast<int32_t>(sample) - predictor;
    uint8_t nibble = 0;
    if (diff < 0) {
        nibble = 8;
        diff = -diff;
    }
    if (diff >= step) {
        nibble |= 4;
        diff -= step;
    }
    if (diff >= (step >> 1)) {
        nibble |= 2;
        diff -= (step >> 1);
    }
    if (diff >= (step >> 2)) {
        nibble |= 1;
    }

    int32_t vpdiff = step >> 3;
    if (nibble & 4) vpdiff += step;
    if (nibble & 2) vpdiff += (step >> 1);
    if (nibble & 1) vpdiff += (step >> 2);

    if (nibble & 8) predictor -= vpdiff;
    else predictor += vpdiff;

    if (predictor > 32767) predictor = 32767;
    else if (predictor < -32768) predictor = -32768;

    step_index += kImaIndexTable[nibble & 0x0F];
    if (step_index < 0) step_index = 0;
    else if (step_index > 88) step_index = 88;

    return nibble & 0x0F;
}

static inline int16_t decodeImaNibble(uint8_t nibble, int32_t& predictor, int32_t& step_index) {
    int32_t step = kImaStepTable[step_index];
    int32_t vpdiff = step >> 3;
    if (nibble & 4) vpdiff += step;
    if (nibble & 2) vpdiff += (step >> 1);
    if (nibble & 1) vpdiff += (step >> 2);

    if (nibble & 8) predictor -= vpdiff;
    else predictor += vpdiff;

    if (predictor > 32767) predictor = 32767;
    else if (predictor < -32768) predictor = -32768;

    step_index += kImaIndexTable[nibble & 0x0F];
    if (step_index < 0) step_index = 0;
    else if (step_index > 88) step_index = 88;

    return static_cast<int16_t>(predictor);
}

} // namespace

extern "C" UiState uiStateFromString(const char* name) {
    if (!name) return UiState::IDLE;
    for (uint8_t i = 0; i < UI_STATE_COUNT; i++) {
        if (strcmp(name, UI_STATE_NAMES[i]) == 0) return static_cast<UiState>(i);
    }
    return UiState::IDLE;
}

extern "C" AnimStep stepAnimationFrame(UiState state, uint8_t current_frame, uint32_t elapsed_ms) {
    AnimStep result;
    uint8_t base_frame = current_frame % 3;
    if (elapsed_ms == 0) {
        result.frame = static_cast<uint8_t>((base_frame + 1) % 3);
    } else {
        uint32_t step = elapsed_ms / 200;
        result.frame = static_cast<uint8_t>((base_frame + step) % 3);
    }

    bool can_blink = (state == UiState::IDLE ||
                      state == UiState::LISTENING ||
                      state == UiState::SPEAKING ||
                      state == UiState::TRANSCRIBING);

    if (!can_blink) {
        result.blink = false;
    } else if (elapsed_ms > 0) {
        uint32_t blink_phase = elapsed_ms % 3000;
        result.blink = (blink_phase < 150);
    } else {
        result.blink = (current_frame % 15 == 0);
    }

    return result;
}

extern "C" size_t encodeImaAdpcm(
    const int16_t* pcm,
    size_t num_samples,
    uint8_t* out_adpcm,
    size_t out_cap,
    int32_t initial_predictor,
    int32_t initial_step_index
) {
    if (!pcm || num_samples == 0 || !out_adpcm) return 0;
    size_t needed = (num_samples + 1) / 2;
    if (out_cap < needed) return 0;

    int32_t predictor = initial_predictor;
    if (predictor > 32767) predictor = 32767;
    else if (predictor < -32768) predictor = -32768;
    int32_t step_index = initial_step_index;
    if (step_index < 0) step_index = 0;
    else if (step_index > 88) step_index = 88;

    for (size_t i = 0; i < num_samples; i += 2) {
        uint8_t lo = encodeImaSample(pcm[i], predictor, step_index);
        uint8_t hi = (i + 1 < num_samples) ? encodeImaSample(pcm[i + 1], predictor, step_index) : 0;
        out_adpcm[i / 2] = static_cast<uint8_t>((lo & 0x0F) | ((hi & 0x0F) << 4));
    }
    return needed;
}

extern "C" size_t decodeImaAdpcm(
    const uint8_t* adpcm,
    size_t adpcm_len,
    size_t num_samples,
    int16_t* out_pcm,
    size_t out_max_samples,
    int32_t initial_predictor,
    int32_t initial_step_index
) {
    if (!adpcm || adpcm_len == 0 || !out_pcm || out_max_samples == 0) return 0;
    size_t max_possible = adpcm_len * 2;
    size_t target = (num_samples > 0 && num_samples < max_possible) ? num_samples : max_possible;
    if (target > out_max_samples) target = out_max_samples;

    int32_t predictor = initial_predictor;
    if (predictor > 32767) predictor = 32767;
    else if (predictor < -32768) predictor = -32768;
    int32_t step_index = initial_step_index;
    if (step_index < 0) step_index = 0;
    else if (step_index > 88) step_index = 88;

    size_t written = 0;
    for (size_t i = 0; i < adpcm_len && written < target; ++i) {
        uint8_t b = adpcm[i];
        out_pcm[written++] = decodeImaNibble(b & 0x0F, predictor, step_index);
        if (written >= target) break;
        out_pcm[written++] = decodeImaNibble((b >> 4) & 0x0F, predictor, step_index);
    }
    return written;
}

extern "C" size_t encodeWavToAdpcm(const uint8_t* wav_data, size_t wav_len, uint8_t* out_buf, size_t out_cap) {
    if (!wav_data || wav_len == 0 || !out_buf) return 0;
    uint32_t sample_rate = 16000;
    const uint8_t* pcm_ptr = wav_data;
    size_t pcm_bytes = wav_len;

    if (wav_len >= 44 && memcmp(wav_data, "RIFF", 4) == 0 && memcmp(wav_data + 8, "WAVE", 4) == 0) {
        memcpy(&sample_rate, wav_data + 24, sizeof(uint32_t));
        if (sample_rate < 4000 || sample_rate > 96000) sample_rate = 16000;
        pcm_ptr = wav_data + 44;
        pcm_bytes = wav_len - 44;
    }

    size_t num_samples = pcm_bytes / sizeof(int16_t);
    size_t adpcm_bytes = (num_samples + 1) / 2;
    if (out_cap < GAD1_HEADER_SIZE + adpcm_bytes) return 0;

    const int16_t* samples = reinterpret_cast<const int16_t*>(pcm_ptr);
    int16_t init_pred = (num_samples > 0) ? samples[0] : 0;
    uint8_t init_idx = 0;
    uint8_t reserved = 0;

    uint32_t magic = GAD1_MAGIC;
    uint32_t sr_u32 = sample_rate;
    uint32_t ns_u32 = static_cast<uint32_t>(num_samples);
    memcpy(out_buf + 0, &magic, 4);
    memcpy(out_buf + 4, &sr_u32, 4);
    memcpy(out_buf + 8, &ns_u32, 4);
    memcpy(out_buf + 12, &init_pred, 2);
    out_buf[14] = init_idx;
    out_buf[15] = reserved;

    size_t encoded = encodeImaAdpcm(
        samples,
        num_samples,
        out_buf + GAD1_HEADER_SIZE,
        out_cap - GAD1_HEADER_SIZE,
        init_pred,
        init_idx
    );
    return encoded > 0 ? (GAD1_HEADER_SIZE + encoded) : 0;
}

extern "C" size_t decodeAdpcmToPcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate
) {
    if (!adpcm_buf || adpcm_len == 0 || !out_pcm || out_max_samples == 0) return 0;
    uint32_t sr = 16000;
    size_t num_samples = 0;
    int32_t init_pred = 0;
    int32_t init_idx = 0;
    const uint8_t* raw_adpcm = adpcm_buf;
    size_t raw_len = adpcm_len;

    if (adpcm_len >= 12) {
        uint32_t magic = 0;
        memcpy(&magic, adpcm_buf, 4);
        if (magic == GAD1_MAGIC) {
            uint32_t parsed_sr = 0;
            uint32_t parsed_ns = 0;
            memcpy(&parsed_sr, adpcm_buf + 4, 4);
            memcpy(&parsed_ns, adpcm_buf + 8, 4);
            if (parsed_sr >= 4000 && parsed_sr <= 96000) sr = parsed_sr;
            num_samples = static_cast<size_t>(parsed_ns);
            size_t needed_bytes = (num_samples + 1) / 2;
            if (adpcm_len >= GAD1_HEADER_SIZE && (adpcm_len - GAD1_HEADER_SIZE) >= needed_bytes) {
                int16_t p16 = 0;
                memcpy(&p16, adpcm_buf + 12, 2);
                init_pred = p16;
                init_idx = adpcm_buf[14];
                raw_adpcm = adpcm_buf + GAD1_HEADER_SIZE;
                raw_len = adpcm_len - GAD1_HEADER_SIZE;
            } else {
                raw_adpcm = adpcm_buf + 12;
                raw_len = adpcm_len - 12;
            }
        }
    }
    if (out_sample_rate) *out_sample_rate = sr;
    return decodeImaAdpcm(raw_adpcm, raw_len, num_samples, out_pcm, out_max_samples, init_pred, init_idx);
}

} // namespace gotchi_core

// ============================================================================
// C Linkage Wrappers with gotchi_* prefix for ctypes / Android JNI
// ============================================================================
extern "C" {

uint8_t gotchi_ui_state_from_string(const char* name) {
    return static_cast<uint8_t>(gotchi_core::uiStateFromString(name));
}

GotchiAnimStepC gotchi_step_animation_frame(uint8_t state, uint8_t current_frame, uint32_t elapsed_ms) {
    gotchi_core::AnimStep step = gotchi_core::stepAnimationFrame(
        static_cast<gotchi_core::UiState>(state),
        current_frame,
        elapsed_ms
    );
    GotchiAnimStepC res;
    res.frame = step.frame;
    res.blink = step.blink ? 1 : 0;
    return res;
}

size_t gotchi_encode_ima_adpcm(
    const int16_t* pcm,
    size_t num_samples,
    uint8_t* out_adpcm,
    size_t out_cap,
    int32_t initial_predictor,
    int32_t initial_step_index
) {
    return gotchi_core::encodeImaAdpcm(pcm, num_samples, out_adpcm, out_cap, initial_predictor, initial_step_index);
}

size_t gotchi_decode_ima_adpcm(
    const uint8_t* adpcm,
    size_t adpcm_len,
    size_t num_samples,
    int16_t* out_pcm,
    size_t out_max_samples,
    int32_t initial_predictor,
    int32_t initial_step_index
) {
    return gotchi_core::decodeImaAdpcm(adpcm, adpcm_len, num_samples, out_pcm, out_max_samples, initial_predictor, initial_step_index);
}

size_t gotchi_encode_wav_to_adpcm(
    const uint8_t* wav_data,
    size_t wav_len,
    uint8_t* out_buf,
    size_t out_cap
) {
    return gotchi_core::encodeWavToAdpcm(wav_data, wav_len, out_buf, out_cap);
}

size_t gotchi_decode_adpcm_to_pcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate
) {
    return gotchi_core::decodeAdpcmToPcm(adpcm_buf, adpcm_len, out_pcm, out_max_samples, out_sample_rate);
}

} // extern "C"
