// ============================================================================
// GOTCHI BLE 5.0 Packet Framing & 4-Bit IMA ADPCM Implementation (T4.1 / F18)
// ============================================================================

#include "ble_framing.h"
#include "log_buffer.h"
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <string.h>
#include <stdlib.h>

namespace gotchi_ble {

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

size_t encodeImaAdpcm(
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

size_t decodeImaAdpcm(
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

size_t encodeWavToAdpcm(const uint8_t* wav_data, size_t wav_len, uint8_t* out_buf, size_t out_cap) {
    if (!wav_data || wav_len < 44 || !out_buf) return 0;
    uint32_t sample_rate = 16000;
    const uint8_t* pcm_ptr = wav_data;
    size_t pcm_bytes = wav_len;

    if (memcmp(wav_data, "RIFF", 4) == 0 && memcmp(wav_data + 8, "WAVE", 4) == 0) {
        memcpy(&sample_rate, wav_data + 24, sizeof(uint32_t));
        if (sample_rate < 4000 || sample_rate > 96000) sample_rate = 16000;
        pcm_ptr = wav_data + 44;
        pcm_bytes = wav_len - 44;
    }

    size_t num_samples = pcm_bytes / sizeof(int16_t);
    size_t adpcm_bytes = (num_samples + 1) / 2;
    if (out_cap < ADPCM_HEADER_SIZE + adpcm_bytes) return 0;

    const int16_t* samples = reinterpret_cast<const int16_t*>(pcm_ptr);
    int16_t init_pred = (num_samples > 0) ? samples[0] : 0;
    uint8_t init_idx = 0;
    uint8_t reserved = 0;

    uint32_t magic = ADPCM_MAGIC;
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
        out_buf + ADPCM_HEADER_SIZE,
        out_cap - ADPCM_HEADER_SIZE,
        init_pred,
        init_idx
    );
    return encoded > 0 ? (ADPCM_HEADER_SIZE + encoded) : 0;
}

size_t decodeAdpcmToPcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate
) {
    if (!adpcm_buf || adpcm_len == 0 || !out_pcm || out_max_samples == 0) return 0;
    uint32_t sr = 24000;
    size_t num_samples = 0;
    int32_t init_pred = 0;
    int32_t init_idx = 0;
    const uint8_t* raw_adpcm = adpcm_buf;
    size_t raw_len = adpcm_len;

    if (adpcm_len >= 12) {
        uint32_t magic = 0;
        memcpy(&magic, adpcm_buf, 4);
        if (magic == ADPCM_MAGIC) {
            uint32_t parsed_sr = 0;
            uint32_t parsed_ns = 0;
            memcpy(&parsed_sr, adpcm_buf + 4, 4);
            memcpy(&parsed_ns, adpcm_buf + 8, 4);
            if (parsed_sr >= 4000 && parsed_sr <= 96000) sr = parsed_sr;
            num_samples = static_cast<size_t>(parsed_ns);
            size_t needed_bytes = (num_samples + 1) / 2;
            if (adpcm_len >= ADPCM_HEADER_SIZE && (adpcm_len - ADPCM_HEADER_SIZE) >= needed_bytes) {
                int16_t p16 = 0;
                memcpy(&p16, adpcm_buf + 12, 2);
                init_pred = p16;
                init_idx = adpcm_buf[14];
                raw_adpcm = adpcm_buf + ADPCM_HEADER_SIZE;
                raw_len = adpcm_len - ADPCM_HEADER_SIZE;
            } else {
                raw_adpcm = adpcm_buf + 12;
                raw_len = adpcm_len - 12;
            }
        }
    }
    if (out_sample_rate) *out_sample_rate = sr;
    return decodeImaAdpcm(raw_adpcm, raw_len, num_samples, out_pcm, out_max_samples, init_pred, init_idx);
}

// CRC16-CCITT (Polynomial 0x1021, init 0xFFFF)
uint16_t calcCrc16(const uint8_t* data, size_t length) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < length; ++i) {
        crc ^= static_cast<uint16_t>(data[i]) << 8;
        for (int bit = 0; bit < 8; ++bit) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

// CRC32 (Standard IEEE 802.3, Polynomial 0xEDB88320)
uint32_t calcCrc32(const uint8_t* data, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; ++i) {
        uint8_t byte = data[i];
        crc ^= byte;
        for (int bit = 0; bit < 8; ++bit) {
            uint32_t mask = -(crc & 1);
            crc = (crc >> 1) ^ (0xEDB88320 & mask);
        }
    }
    return ~crc;
}

size_t encodePacket(
    FrameType type,
    uint16_t seq_num,
    uint16_t chunk_idx,
    uint16_t total_chunks,
    FrameFlags flags,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t* out_buf,
    size_t out_cap,
    uint8_t frame_index
) {
    size_t total_needed = BLE_HEADER_SIZE + payload_len + BLE_TRAILER_SIZE;
    if (!out_buf || out_cap < total_needed) {
        return 0;
    }

    PacketHeader hdr;
    hdr.magic = BLE_MAGIC;
    hdr.frame_type = static_cast<uint8_t>(type);
    hdr.flags = static_cast<uint8_t>(flags);
    hdr.frame_index = frame_index;
    hdr.reserved = 0;
    hdr.seq_num = seq_num;
    hdr.chunk_idx = chunk_idx;
    hdr.total_chunks = total_chunks;
    hdr.payload_len = static_cast<uint16_t>(payload_len);
    // Header CRC16 covers first 14 bytes (excluding header_crc16 field itself)
    hdr.header_crc16 = calcCrc16(reinterpret_cast<const uint8_t*>(&hdr), 14);

    memcpy(out_buf, &hdr, BLE_HEADER_SIZE);

    if (payload && payload_len > 0) {
        memcpy(out_buf + BLE_HEADER_SIZE, payload, payload_len);
    }

    uint32_t crc32 = (payload && payload_len > 0) ? calcCrc32(payload, payload_len) : 0;
    uint8_t* trailer = out_buf + BLE_HEADER_SIZE + payload_len;
    trailer[0] = static_cast<uint8_t>(crc32 & 0xFF);
    trailer[1] = static_cast<uint8_t>((crc32 >> 8) & 0xFF);
    trailer[2] = static_cast<uint8_t>((crc32 >> 16) & 0xFF);
    trailer[3] = static_cast<uint8_t>((crc32 >> 24) & 0xFF);

    return total_needed;
}

bool decodePacket(
    const uint8_t* in_buf,
    size_t in_len,
    Packet& out_packet
) {
    if (!in_buf || in_len < (BLE_HEADER_SIZE + BLE_TRAILER_SIZE)) {
        return false;
    }

    PacketHeader hdr;
    memcpy(&hdr, in_buf, BLE_HEADER_SIZE);

    if (hdr.magic != BLE_MAGIC) {
        return false;
    }

    uint16_t computed_crc16 = calcCrc16(in_buf, 14);
    if (computed_crc16 != hdr.header_crc16) {
        return false;
    }

    size_t expected_total = BLE_HEADER_SIZE + hdr.payload_len + BLE_TRAILER_SIZE;
    if (in_len < expected_total) {
        return false;
    }

    const uint8_t* payload = in_buf + BLE_HEADER_SIZE;
    const uint8_t* trailer = payload + hdr.payload_len;

    uint32_t payload_crc32 = static_cast<uint32_t>(trailer[0]) |
                             (static_cast<uint32_t>(trailer[1]) << 8) |
                             (static_cast<uint32_t>(trailer[2]) << 16) |
                             (static_cast<uint32_t>(trailer[3]) << 24);

    uint32_t computed_crc32 = (hdr.payload_len > 0) ? calcCrc32(payload, hdr.payload_len) : 0;
    if (computed_crc32 != payload_crc32) {
        return false;
    }

    out_packet.header = hdr;
    out_packet.payload = (hdr.payload_len > 0) ? payload : nullptr;
    out_packet.payload_crc32 = payload_crc32;
    return true;
}

PacketReassembler::PacketReassembler(size_t max_payload_size)
    : max_size_(max_payload_size) {
    if (psramFound()) {
        buffer_ = static_cast<uint8_t*>(ps_malloc(max_size_));
        if (buffer_) {
            in_psram_ = true;
        }
    }
    if (!buffer_) {
        // Fallback to smaller internal SRAM buffer only if PSRAM is absent
        size_t fallback_size = max_size_ > 16384 ? 16384 : max_size_;
        buffer_ = static_cast<uint8_t*>(malloc(fallback_size));
        if (buffer_) {
            max_size_ = fallback_size;
            in_psram_ = false;
        } else {
            gotchi_log::RingLogger.error("ble", "PacketReassembler buffer allocation failed");
        }
    }
}

PacketReassembler::~PacketReassembler() {
    if (buffer_) {
        free(buffer_);
        buffer_ = nullptr;
    }
}

void PacketReassembler::reset() {
    current_len_ = 0;
    expected_chunk_ = 0;
    total_chunks_ = 0;
    in_progress_ = false;
}

void PacketReassembler::resetSequenceTracking() {
    reset();
    has_last_seq_ = false;
    last_seq_num_ = 0;
}

bool PacketReassembler::pushPacket(const Packet& packet, bool& is_complete) {
    is_complete = false;
    if (!buffer_) {
        gotchi_log::RingLogger.error("ble", "pushPacket rejected: reassembler has no buffer");
        return false;
    }

    const PacketHeader& hdr = packet.header;

    // Enforce monotonic link sequence number continuity across packets
    if (has_last_seq_) {
        uint16_t expected_seq = static_cast<uint16_t>(last_seq_num_ + 1);
        if (hdr.seq_num != expected_seq) {
            gotchi_log::RingLogger.warn("ble", "BLE packet sequence gap detected; resetting reassembler");
            reset();
            has_last_seq_ = false;
            return false;
        }
    }

    bool is_first = (hdr.flags & static_cast<uint8_t>(FrameFlags::FIRST_CHUNK)) || (hdr.chunk_idx == 0);
    bool is_last = (hdr.flags & static_cast<uint8_t>(FrameFlags::LAST_CHUNK)) || (hdr.chunk_idx + 1 == hdr.total_chunks);

    if (is_first) {
        if (in_progress_) {
            // Unfinished previous multi-chunk frame was interrupted by a new FIRST_CHUNK
            gotchi_log::RingLogger.warn("ble", "Incomplete BLE frame interrupted by new FIRST_CHUNK");
            reset();
            return false;
        }
        reset();
        current_type_ = static_cast<FrameType>(hdr.frame_type);
        current_frame_index_ = hdr.frame_index;
        total_chunks_ = hdr.total_chunks;
        expected_chunk_ = 0;
        in_progress_ = true;
    } else if (!in_progress_) {
        // Received mid-frame packet without first chunk
        return false;
    }

    // Verify chunk sequence continuity
    if (hdr.chunk_idx != expected_chunk_) {
        reset();
        return false;
    }

    // Verify frame type and burst frame_index match across all chunks of the frame
    if (static_cast<FrameType>(hdr.frame_type) != current_type_ || hdr.frame_index != current_frame_index_) {
        reset();
        return false;
    }

    // Check capacity
    if (current_len_ + hdr.payload_len > max_size_) {
        reset();
        return false;
    }

    if (hdr.payload_len > 0 && packet.payload) {
        memcpy(buffer_ + current_len_, packet.payload, hdr.payload_len);
        current_len_ += hdr.payload_len;
    }

    last_seq_num_ = hdr.seq_num;
    has_last_seq_ = true;
    expected_chunk_++;

    if (is_last || (total_chunks_ > 0 && expected_chunk_ >= total_chunks_)) {
        is_complete = true;
        in_progress_ = false;
    }

    return true;
}

} // namespace gotchi_ble
