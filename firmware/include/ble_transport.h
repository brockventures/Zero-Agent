#pragma once
// ============================================================================
// GOTCHI V1 — ESP32-S3 BLE GATT UART Peripheral Transport (T4.1 / T4.2 / F18)
//   - Advertises GOTCHI BLE GATT UART service (6E400001-B5A3-F393-E0A9-E50E24DCCA9E)
//   - CHAR_RX (6E400002-...): Phone -> GOTCHI (Write / Write No Response)
//   - CHAR_TX (6E400003-...): GOTCHI -> Phone (Notify)
//   - Uses T4.1 binary framing (ble_framing.h) to fragment outgoing PTT audio,
//     camera snapshots (0..4), and TURN_COMMIT metadata over CHAR_TX, and
//     reassemble incoming SSE_EVENT frames written by GotchiBleRelayService.
// ============================================================================

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ble_framing.h"

namespace gotchi_ble {

constexpr const char* GATT_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char* GATT_CHAR_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char* GATT_CHAR_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";

// Inter-packet CHAR_TX notification pacing bounds (ms).
// Paces ~40 ADPCM uplink chunks + TURN_COMMIT at 10-12ms intervals instead of an
// unthrottled 2ms tight loop that overflows Android's BLE GATT queue and drops END_OF_TURN.
constexpr uint32_t BLE_TX_MIN_PACING_MS     = 10;
constexpr uint32_t BLE_TX_DEFAULT_PACING_MS = 12;
constexpr uint32_t BLE_TX_MAX_PACING_MS     = 50;

// Configures or queries the inter-packet CHAR_TX notification pacing interval (clamped to 10..50 ms).
void setTxNotifyPacingMs(uint32_t pacing_ms);
uint32_t getTxNotifyPacingMs();

// Initializes the ESP32-S3 BLE GATT server and starts advertising.
bool initBleTransport(const char* device_name = "GOTCHI-V1");

// Returns true when an Android BLE companion relay is actively connected.
bool isBlePeerConnected();

// Updates simulated/negotiated MTU payload cap (default 492 bytes for 512 MTU).
void setNegotiatedMtu(uint16_t att_mtu);
uint16_t getNegotiatedMaxPayload();

// Fragments `payload` into MTU-sized T4.1 packets and sends them via CHAR_TX Notify.
bool sendFramedPayload(
    FrameType type,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t frame_index = 0,
    bool requires_ack = false,
    FrameFlags extra_flags = FrameFlags::NONE
);

// Feeds a raw packet written to CHAR_RX into the transport reassembler.
// Exposed for both the GATT write callback and deterministic unit tests.
bool onRxRawPacket(const uint8_t* raw, size_t len);

// Pops the next completed downlink frame (e.g. SSE_EVENT or ACK) received on CHAR_RX.
// Returns true if a completed frame was copied into `out_buf`.
bool pollIncomingFrame(
    uint8_t* out_buf,
    size_t out_cap,
    size_t& out_len,
    FrameType& out_type
);

// Clears any queued incoming frames and resets the RX reassembler before a new turn.
void clearIncomingQueue();

} // namespace gotchi_ble
