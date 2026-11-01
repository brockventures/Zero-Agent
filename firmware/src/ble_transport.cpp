// ============================================================================
// GOTCHI V1 — ESP32-S3 BLE GATT UART Peripheral Transport Implementation
// ============================================================================

#include "ble_transport.h"
#include <string.h>

#if defined(ARDUINO)
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "log_buffer.h"
#endif

namespace gotchi_ble {
namespace {

constexpr size_t RX_QUEUE_DEPTH = 16;
constexpr size_t MAX_SSE_FRAME_BYTES = 16 * 1024;

struct QueuedFrame {
    bool occupied = false;
    FrameType type = FrameType::SSE_EVENT;
    size_t len = 0;
    uint8_t* data = nullptr;
};

static bool s_initialized = false;
static volatile bool s_peer_connected = false;
static uint16_t s_max_payload = (uint16_t)BLE_MAX_PAYLOAD;
static uint16_t s_tx_seq_num = 1;

static PacketReassembler s_rx_reassembler(MAX_SSE_FRAME_BYTES);
static QueuedFrame s_rx_queue[RX_QUEUE_DEPTH];
static size_t s_rx_head = 0;
static size_t s_rx_tail = 0;
static size_t s_rx_count = 0;

static void ensureRxQueueAllocated() {
    for (size_t i = 0; i < RX_QUEUE_DEPTH; ++i) {
        if (!s_rx_queue[i].data) {
#if defined(ARDUINO) && defined(BOARD_HAS_PSRAM)
            if (psramFound()) {
                s_rx_queue[i].data = (uint8_t*)ps_malloc(MAX_SSE_FRAME_BYTES);
            }
#endif
            if (!s_rx_queue[i].data) {
                s_rx_queue[i].data = new uint8_t[2048];
            }
        }
    }
}

static size_t slotCapacity() {
#if defined(ARDUINO) && defined(BOARD_HAS_PSRAM)
    if (psramFound()) return MAX_SSE_FRAME_BYTES;
#endif
    return 2048;
}

static bool enqueueCompletedFrame(FrameType type, const uint8_t* payload, size_t len) {
    ensureRxQueueAllocated();
    if (s_rx_count >= RX_QUEUE_DEPTH) {
        return false;
    }
    size_t cap = slotCapacity();
    if (len > cap) {
        return false;
    }
    QueuedFrame& slot = s_rx_queue[s_rx_tail];
    if (!slot.data) return false;
    if (len > 0 && payload != nullptr) {
        memcpy(slot.data, payload, len);
    }
    slot.len = len;
    slot.type = type;
    slot.occupied = true;
    s_rx_tail = (s_rx_tail + 1) % RX_QUEUE_DEPTH;
    s_rx_count++;
    return true;
}

#if defined(ARDUINO)
static BLEServer* s_server = nullptr;
static BLECharacteristic* s_tx_char = nullptr;
static BLECharacteristic* s_rx_char = nullptr;

class GotchiServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) override {
        (void)pServer;
        s_peer_connected = true;
        s_rx_reassembler.resetSequenceTracking();
        gotchi_log::RingLogger.info("ble", "BLE companion connected");
    }

    void onDisconnect(BLEServer* pServer) override {
        s_peer_connected = false;
        s_rx_reassembler.resetSequenceTracking();
        gotchi_log::RingLogger.info("ble", "BLE companion disconnected; restarting advertising");
        if (pServer) {
            pServer->startAdvertising();
        }
    }
};

class GotchiRxCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pCharacteristic) override {
        if (!pCharacteristic) return;
        const uint8_t* data = pCharacteristic->getData();
        size_t len = pCharacteristic->getLength();
        if (data && len > 0) {
            onRxRawPacket(data, len);
        }
    }
};
#endif

} // namespace

void setNegotiatedMtu(uint16_t att_mtu) {
    if (att_mtu > BLE_HEADER_SIZE + BLE_TRAILER_SIZE + 3) {
        uint16_t usable = att_mtu - 3 - (uint16_t)(BLE_HEADER_SIZE + BLE_TRAILER_SIZE);
        s_max_payload = usable < BLE_MAX_PAYLOAD ? usable : (uint16_t)BLE_MAX_PAYLOAD;
    } else {
        s_max_payload = (uint16_t)BLE_MAX_PAYLOAD;
    }
}

uint16_t getNegotiatedMaxPayload() {
    return s_max_payload;
}

bool isBlePeerConnected() {
    return s_peer_connected;
}

bool initBleTransport(const char* device_name) {
    if (s_initialized) return true;
    ensureRxQueueAllocated();
    s_rx_reassembler.resetSequenceTracking();

#if defined(ARDUINO)
    const char* name = (device_name && *device_name) ? device_name : "GOTCHI-V1";
    BLEDevice::init(name);
    BLEDevice::setMTU((uint16_t)512);

    s_server = BLEDevice::createServer();
    if (!s_server) return false;
    s_server->setCallbacks(new GotchiServerCallbacks());

    BLEService* service = s_server->createService(GATT_SERVICE_UUID);
    if (!service) return false;

    s_tx_char = service->createCharacteristic(
        GATT_CHAR_TX_UUID,
        BLECharacteristic::PROPERTY_NOTIFY
    );
    s_tx_char->addDescriptor(new BLE2902());

    s_rx_char = service->createCharacteristic(
        GATT_CHAR_RX_UUID,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
    );
    s_rx_char->setCallbacks(new GotchiRxCallbacks());

    service->start();

    BLEAdvertising* advertising = BLEDevice::getAdvertising();
    if (advertising) {
        advertising->addServiceUUID(GATT_SERVICE_UUID);
        advertising->setScanResponse(true);
        advertising->setMinPreferred(0x06);
        BLEDevice::startAdvertising();
    }
    gotchi_log::RingLogger.info("ble", "BLE GATT UART peripheral advertising started");
#else
    (void)device_name;
#endif

    s_initialized = true;
    return true;
}

bool onRxRawPacket(const uint8_t* raw, size_t len) {
    Packet pkt;
    if (!decodePacket(raw, len, pkt)) {
        return false;
    }
    bool complete = false;
    if (!s_rx_reassembler.pushPacket(pkt, complete)) {
        return false;
    }
    if (complete) {
        enqueueCompletedFrame(
            s_rx_reassembler.getFrameType(),
            s_rx_reassembler.getPayload(),
            s_rx_reassembler.getPayloadLength()
        );
        s_rx_reassembler.reset();
    }
    return true;
}

bool pollIncomingFrame(
    uint8_t* out_buf,
    size_t out_cap,
    size_t& out_len,
    FrameType& out_type
) {
    out_len = 0;
    if (s_rx_count == 0) return false;

    QueuedFrame& slot = s_rx_queue[s_rx_head];
    if (!slot.occupied) return false;

    size_t copy_len = slot.len < out_cap ? slot.len : out_cap;
    if (copy_len > 0 && out_buf != nullptr && slot.data != nullptr) {
        memcpy(out_buf, slot.data, copy_len);
    }
    out_len = copy_len;
    out_type = slot.type;

    slot.occupied = false;
    slot.len = 0;
    s_rx_head = (s_rx_head + 1) % RX_QUEUE_DEPTH;
    s_rx_count--;
    return true;
}

void clearIncomingQueue() {
    for (size_t i = 0; i < RX_QUEUE_DEPTH; ++i) {
        s_rx_queue[i].occupied = false;
        s_rx_queue[i].len = 0;
    }
    s_rx_head = 0;
    s_rx_tail = 0;
    s_rx_count = 0;
    s_rx_reassembler.reset();
}

bool sendFramedPayload(
    FrameType type,
    const uint8_t* payload,
    size_t payload_len,
    uint8_t frame_index,
    bool requires_ack,
    FrameFlags extra_flags
) {
#if defined(ARDUINO)
    if (!s_peer_connected || !s_tx_char) {
        return false;
    }
#endif
    uint16_t max_chunk = s_max_payload > 0 ? s_max_payload : (uint16_t)BLE_MAX_PAYLOAD;
    uint16_t total_chunks = payload_len == 0
        ? 1
        : (uint16_t)((payload_len + max_chunk - 1) / max_chunk);

    uint8_t pkt_buf[BLE_HEADER_SIZE + BLE_MAX_PAYLOAD + BLE_TRAILER_SIZE];

    for (uint16_t idx = 0; idx < total_chunks; ++idx) {
        size_t offset = (size_t)idx * max_chunk;
        size_t chunk_len = 0;
        if (payload_len > offset) {
            chunk_len = (payload_len - offset) < max_chunk ? (payload_len - offset) : max_chunk;
        }

        FrameFlags flags = extra_flags;
        if (idx == 0) flags = flags | FrameFlags::FIRST_CHUNK;
        if (idx + 1 == total_chunks) flags = flags | FrameFlags::LAST_CHUNK;
        if (requires_ack) flags = flags | FrameFlags::REQUIRES_ACK;

        size_t encoded_len = encodePacket(
            type,
            s_tx_seq_num++,
            idx,
            total_chunks,
            flags,
            (chunk_len > 0 && payload != nullptr) ? (payload + offset) : nullptr,
            chunk_len,
            pkt_buf,
            sizeof(pkt_buf),
            frame_index
        );
        if (encoded_len == 0) {
            return false;
        }

#if defined(ARDUINO)
        s_tx_char->setValue(pkt_buf, encoded_len);
        s_tx_char->notify();
        delay(2);
#endif
    }
    return true;
}

} // namespace gotchi_ble
