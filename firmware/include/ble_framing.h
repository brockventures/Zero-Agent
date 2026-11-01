#pragma once
// ============================================================================
// GOTCHI BLE 5.0 Packet Framing & 4-Bit IMA ADPCM Codec (T4.1 / F18)
//   - High-throughput binary framing over BLE 5.0 2M PHY (GATT / L2CAP CoC).
//   - Frame types:
//       AUDIO_CHUNK (0x01): 16 kHz mono audio (4-bit IMA ADPCM GAD1 or 16-bit PCM WAV).
//       IMAGE_FRAME (0x02): JPEG snapshot chunks (with burst frame_index 0..4).
//       TURN_COMMIT (0x03): Marks end of user input / interaction dispatch.
//       SSE_EVENT   (0x04): Return events from gateway (state, audio, reply).
//       ACK         (0x05): Acknowledgment of packet / sequence receipt.
//       CANCEL      (0x06): Interaction abort / cancellation.
//   - 4-bit IMA ADPCM Smart Pipe compression (4:1 ratio; 64 kbps @ 16 kHz uplink,
//     96 kbps @ 24 kHz downlink) with 12-byte `GAD1` header (`magic`, `sample_rate`, `num_samples`).
//   - Sequence numbers, burst frame_index, & chunk indices for reliable reassembly.
//   - CRC16-CCITT header check (14 bytes) and CRC32 payload check for link integrity.
// ============================================================================

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

namespace gotchi_ble {

constexpr uint16_t BLE_MAGIC = 0x4742; // "GB" (Gotchi BLE)
constexpr size_t BLE_HEADER_SIZE = 16;
constexpr size_t BLE_TRAILER_SIZE = 4; // CRC32 (4 bytes)
constexpr size_t BLE_MAX_PAYLOAD = 512 - BLE_HEADER_SIZE - BLE_TRAILER_SIZE; // 492 bytes for 512 MTU
constexpr size_t BLE_L2CAP_MAX_PAYLOAD = 2048; // Supported for L2CAP CoC

constexpr uint32_t ADPCM_MAGIC = 0x31444147; // "GAD1" in little-endian
constexpr size_t ADPCM_HEADER_SIZE = 16;     // magic (4B) + sample_rate (4B) + num_samples (4B) + predictor (i16) + step_index (u8) + reserved (u8)

enum class FrameType : uint8_t {
    AUDIO_CHUNK = 0x01,
    IMAGE_FRAME = 0x02,
    TURN_COMMIT = 0x03,
    SSE_EVENT   = 0x04,
    ACK         = 0x05,
    CANCEL      = 0x06,
    CONTROL     = 0x07,
};

enum class FrameFlags : uint8_t {
    NONE         = 0x00,
    FIRST_CHUNK  = 0x01,
    LAST_CHUNK   = 0x02,
    SINGLE_CHUNK = 0x03, // FIRST | LAST
    REQUIRES_ACK = 0x04,
    ADPCM        = 0x08, // 4-bit IMA ADPCM compressed audio stream (GAD1)
};

inline FrameFlags operator|(FrameFlags a, FrameFlags b) {
    return static_cast<FrameFlags>(static_cast<uint8_t>(a) | static_cast<uint8_t>(b));
}
inline bool operator&(FrameFlags a, FrameFlags b) {
    return (static_cast<uint8_t>(a) & static_cast<uint8_t>(b)) != 0;
}

#pragma pack(push, 1)
struct PacketHeader {
    uint16_t magic;         // 0x4742 (Little-Endian)
    uint8_t  frame_type;    // FrameType
    uint8_t  flags;         // FrameFlags
    uint8_t  frame_index;   // Burst / stream frame index (0..4 for 5-photo burst)
    uint8_t  reserved;      // Reserved (0x00) for 16-bit alignment
    uint16_t seq_num;       // Monotonically increasing link sequence number
    uint16_t chunk_idx;     // Chunk index within this frame/turn (0..total_chunks-1)
    uint16_t total_chunks;  // Total chunks for this frame (1 if single chunk)
    uint16_t payload_len;   // Payload length in bytes (excluding header & trailer)
    uint16_t header_crc16;  // CRC16-CCITT of preceding 14 bytes of header
};
#pragma pack(pop)

static_assert(sizeof(PacketHeader) == BLE_HEADER_SIZE, "PacketHeader must be exactly 16 bytes");

// CRC calculation helpers
uint16_t calcCrc16(const uint8_t* data, size_t length);
uint32_t calcCrc32(const uint8_t* data, size_t length);

// 4-bit IMA ADPCM Codec (4:1 audio compression over BLE)
size_t encodeImaAdpcm(
    const int16_t* pcm,
    size_t num_samples,
    uint8_t* out_adpcm,
    size_t out_cap,
    int32_t initial_predictor = 0,
    int32_t initial_step_index = 0
);
size_t decodeImaAdpcm(
    const uint8_t* adpcm,
    size_t adpcm_len,
    size_t num_samples,
    int16_t* out_pcm,
    size_t out_max_samples,
    int32_t initial_predictor = 0,
    int32_t initial_step_index = 0
);

// Encodes a 16-bit mono RIFF WAV buffer into a 16-byte `GAD1` header + 4-bit IMA ADPCM nibbles.
// Seeds initial_predictor to the first sample to prevent start-of-chunk transient pops.
// Returns total encoded bytes written to `out_buf`, or 0 on error.
size_t encodeWavToAdpcm(const uint8_t* wav_data, size_t wav_len, uint8_t* out_buf, size_t out_cap);

// Decodes a `GAD1` container (or raw 4-bit IMA ADPCM buffer) into 16-bit signed mono PCM samples.
// Returns number of decoded PCM samples written to `out_pcm`.
size_t decodeAdpcmToPcm(
    const uint8_t* adpcm_buf,
    size_t adpcm_len,
    int16_t* out_pcm,
    size_t out_max_samples,
    uint32_t* out_sample_rate = nullptr
);

struct Packet {
    PacketHeader header;
    const uint8_t* payload = nullptr;
    uint32_t payload_crc32 = 0;
};

// Encodes a packet into a preallocated buffer.
// Buffer must have at least BLE_HEADER_SIZE + payload_len + BLE_TRAILER_SIZE bytes.
// Returns total encoded bytes, or 0 on failure.
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
    uint8_t frame_index = 0
);

// Decodes and validates a raw packet from a buffer.
// Validates magic, header CRC16, length bounds, and payload CRC32.
// Returns true on success.
bool decodePacket(
    const uint8_t* in_buf,
    size_t in_len,
    Packet& out_packet
);

// Sequence tracker and PSRAM-backed defragmenter for incoming multi-chunk payloads
class PacketReassembler {
public:
    explicit PacketReassembler(size_t max_payload_size = 128 * 1024);
    ~PacketReassembler();

    bool valid() const { return buffer_ != nullptr; }
    bool inPsram() const { return in_psram_; }

    void reset();
    void resetSequenceTracking();

    // Ingests a decoded packet, enforcing both chunk_idx and seq_num continuity.
    // Returns true if packet is accepted.
    // If this packet completes a fragmented turn/frame, `is_complete` is set to true.
    bool pushPacket(const Packet& packet, bool& is_complete);

    const uint8_t* getPayload() const { return buffer_; }
    size_t getPayloadLength() const { return current_len_; }
    FrameType getFrameType() const { return current_type_; }
    uint8_t getFrameIndex() const { return current_frame_index_; }
    uint16_t getLastSeqNum() const { return last_seq_num_; }

private:
    size_t max_size_;
    uint8_t* buffer_ = nullptr;
    bool in_psram_ = false;
    size_t current_len_ = 0;
    FrameType current_type_ = FrameType::AUDIO_CHUNK;
    uint8_t current_frame_index_ = 0;
    uint16_t expected_chunk_ = 0;
    uint16_t total_chunks_ = 0;
    uint16_t last_seq_num_ = 0;
    bool has_last_seq_ = false;
    bool in_progress_ = false;
};

} // namespace gotchi_ble
