// ============================================================================
// GOTCHI Go Live Bidirectional Streaming Client Implementation (F19 / F21)
// ============================================================================

#include "live_client.h"
#include "pins.h"
#include "log_buffer.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include "tls_root_ca.h"
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <string.h>
#include <strings.h>
#include <stdio.h>

namespace gotchi_live {
namespace {

constexpr uint32_t CONNECT_TIMEOUT_MS = 5000;
constexpr uint32_t HANDSHAKE_TIMEOUT_MS = 5000;
constexpr size_t   MAX_WS_RX_BYTES = 256 * 1024;
constexpr size_t   PCM_UPLINK_SAMPLES = 320; // 20ms at 16 kHz = 640 bytes

static bool isButtonPressed(uint8_t pin) {
    if (digitalRead(pin) != LOW) return false;
    delay(20);
    return digitalRead(pin) == LOW;
}

static void waitForRelease(uint8_t pin, uint32_t max_wait_ms = 2000) {
    uint32_t t0 = millis();
    while (digitalRead(pin) == LOW && (millis() - t0 < max_wait_ms)) {
        delay(5);
    }
}

static bool writeAll(WiFiClient& c, const uint8_t* p, size_t n, uint32_t timeout_ms = 5000) {
    size_t sent = 0;
    uint32_t t0 = millis();
    while (sent < n) {
        size_t w = c.write(p + sent, n - sent);
        if (w == 0) {
            if (!c.connected() || (millis() - t0 > timeout_ms)) return false;
            delay(1);
            continue;
        }
        sent += w;
        t0 = millis();
    }
    return true;
}

static bool readExact(WiFiClient& c, uint8_t* dst, size_t n, uint32_t timeout_ms = 2000) {
    size_t got_total = 0;
    uint32_t t0 = millis();
    while (got_total < n) {
        int avail = c.available();
        if (avail > 0) {
            size_t want = n - got_total;
            int r = c.read(dst + got_total, want);
            if (r > 0) {
                got_total += (size_t)r;
                t0 = millis();
                continue;
            }
        }
        if (!c.connected() || (millis() - t0 > timeout_ms)) return false;
        delay(1);
    }
    return true;
}

// Client-to-server RFC 6455 masked frame writer
static bool sendWsFrame(WiFiClient& c, uint8_t opcode, const uint8_t* payload, size_t len) {
    uint8_t hdr[14];
    size_t hdr_len = 0;
    hdr[hdr_len++] = static_cast<uint8_t>(0x80 | (opcode & 0x0F)); // FIN + opcode

    if (len <= 125) {
        hdr[hdr_len++] = static_cast<uint8_t>(0x80 | len); // MASK=1 + 7-bit len
    } else if (len <= 65535) {
        hdr[hdr_len++] = static_cast<uint8_t>(0x80 | 126);
        hdr[hdr_len++] = static_cast<uint8_t>((len >> 8) & 0xFF);
        hdr[hdr_len++] = static_cast<uint8_t>(len & 0xFF);
    } else {
        hdr[hdr_len++] = static_cast<uint8_t>(0x80 | 127);
        for (int i = 7; i >= 0; --i) {
            hdr[hdr_len++] = static_cast<uint8_t>((len >> (i * 8)) & 0xFF);
        }
    }

    uint32_t mask_val = (uint32_t)millis() ^ 0xA55A3C69UL;
    uint8_t mask[4] = {
        static_cast<uint8_t>((mask_val >> 24) & 0xFF),
        static_cast<uint8_t>((mask_val >> 16) & 0xFF),
        static_cast<uint8_t>((mask_val >> 8) & 0xFF),
        static_cast<uint8_t>(mask_val & 0xFF),
    };
    memcpy(hdr + hdr_len, mask, 4);
    hdr_len += 4;

    if (!writeAll(c, hdr, hdr_len)) return false;
    if (len == 0 || !payload) return true;

    // Mask and write in 1024-byte chunks so we never allocate a second copy of a large JPEG
    uint8_t chunk[1024];
    size_t offset = 0;
    while (offset < len) {
        size_t n = (len - offset < sizeof(chunk)) ? (len - offset) : sizeof(chunk);
        for (size_t i = 0; i < n; ++i) {
            chunk[i] = payload[offset + i] ^ mask[(offset + i) & 3];
        }
        if (!writeAll(c, chunk, n)) return false;
        offset += n;
    }
    return true;
}

static bool sendWsImageFrame(WiFiClient& c, const uint8_t* jpeg, size_t jpeg_len, size_t frame_idx) {
    if (!jpeg || jpeg_len == 0) return false;
    size_t b64_max = ((jpeg_len + 2) / 3) * 4 + 4;
    char* b64 = static_cast<char*>(psramFound() ? ps_malloc(b64_max) : malloc(b64_max));
    if (!b64) return false;

    size_t b64_len = 0;
    if (mbedtls_base64_encode(reinterpret_cast<unsigned char*>(b64), b64_max, &b64_len, jpeg, jpeg_len) != 0) {
        free(b64);
        return false;
    }
    b64[b64_len] = '\0';

    size_t json_max = b64_len + 96;
    char* json_buf = static_cast<char*>(psramFound() ? ps_malloc(json_max) : malloc(json_max));
    if (!json_buf) {
        free(b64);
        return false;
    }
    int n = snprintf(json_buf, json_max, "{\"type\":\"image\",\"frame_index\":%u,\"image_b64\":\"%s\"}",
                     (unsigned)frame_idx, b64);
    free(b64);
    if (n <= 0 || (size_t)n >= json_max) {
        free(json_buf);
        return false;
    }

    bool ok = sendWsFrame(c, 0x01, reinterpret_cast<const uint8_t*>(json_buf), (size_t)n);
    free(json_buf);
    return ok;
}

static const char* findB64Value(const char* line, const char* key, size_t& out_len) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char* p = strstr(line, pat);
    if (!p) {
        snprintf(pat, sizeof(pat), "\"%s\": \"", key);
        p = strstr(line, pat);
    }
    if (!p) return nullptr;
    p += strlen(pat);
    const char* end = strchr(p, '"');
    if (!end) return nullptr;
    out_len = (size_t)(end - p);
    return p;
}

static void handleWsTextMessage(WiFiClient& c, const char* text, size_t len, bool mute_speaker,
                                bool live_barge_in, uint32_t& last_activity_ts, GotchiUIState& ui,
                                gotchi_voice::StateCb cb) {
    if (!text || len == 0) return;

    size_t b64_len = 0;
    const char* b64 = findB64Value(text, "audio_b64", b64_len);

    JsonDocument filter;
    filter["type"] = true;
    filter["sample_rate"] = true;
    filter["text"] = true;
    filter["reply"] = true;
    filter["error"] = true;
    filter["message"] = true;

    JsonDocument doc;
    if (deserializeJson(doc, text, DeserializationOption::Filter(filter))) {
        return;
    }

    const char* type = doc["type"] | "";
    if (strcmp(type, "audio") == 0 && b64 && b64_len >= 4) {
        uint32_t sr = doc["sample_rate"] | 24000;
        size_t pad = (b64[b64_len - 1] == '=') + (b64_len >= 2 && b64[b64_len - 2] == '=');
        ui.audio_bytes_received += (uint32_t)((b64_len / 4) * 3 - pad);
        ui.last_sample_rate = sr;
        if (ui.state != UiState::SPEAKING) {
            ui.state = UiState::SPEAKING;
            ui.status_text = "LIVE (REPLY)";
            if (cb) cb(ui);
        }
        if (!mute_speaker) {
            gotchi_audio::playBase64PcmChunk(b64, b64_len, sr);
            if (live_barge_in && gotchi_audio::consumeLiveBargeInFlag()) {
                const char* barge_json = "{\"type\":\"barge_in\"}";
                sendWsFrame(c, 0x01, reinterpret_cast<const uint8_t*>(barge_json), strlen(barge_json));
                ui.state = UiState::LISTENING;
                ui.status_text = "LIVE (BARGE-IN)";
                if (cb) cb(ui);
            } else {
                gotchi_audio::flushRxDmaBuffer();
            }
        }
        last_activity_ts = millis();
    } else if (strcmp(type, "text") == 0) {
        const char* t = doc["text"] | (doc["reply"] | "");
        if (*t) {
            ui.reply = t;
            if (cb) cb(ui);
        }
        last_activity_ts = millis();
    } else if (strcmp(type, "error") == 0) {
        const char* code = doc["error"] | "error";
        const char* msg  = doc["message"] | "";
        ui.state = UiState::ERROR;
        ui.error_code = code;
        if (*msg) ui.status_text = String(msg).substring(0, 22);
        if (cb) cb(ui);
    }
}

// Non-blocking poll of incoming server-to-client WebSocket frames.
// Returns false if a Close frame (`0x08`) was received or socket broke.
static bool pollWsDownlink(WiFiClient& c, uint8_t* rx_buf, size_t rx_cap, bool mute_speaker,
                           bool live_barge_in, uint32_t& last_activity_ts, GotchiUIState& ui,
                           gotchi_voice::StateCb cb) {
    while (c.available() >= 2) {
        uint8_t h2[2];
        if (!readExact(c, h2, 2)) return false;
        uint8_t opcode = h2[0] & 0x0F;
        bool masked = (h2[1] & 0x80) != 0;
        size_t payload_len = h2[1] & 0x7F;

        if (payload_len == 126) {
            uint8_t ext[2];
            if (!readExact(c, ext, 2)) return false;
            payload_len = ((size_t)ext[0] << 8) | ext[1];
        } else if (payload_len == 127) {
            uint8_t ext[8];
            if (!readExact(c, ext, 8)) return false;
            payload_len = 0;
            for (int i = 0; i < 8; ++i) {
                payload_len = (payload_len << 8) | ext[i];
            }
        }

        uint8_t mask[4] = {0};
        if (masked) {
            if (!readExact(c, mask, 4)) return false;
        }

        if (opcode == 0x08) {
            // Close frame
            return false;
        }

        if (payload_len >= rx_cap) {
            // Drain oversized frame without overflowing buffer
            uint8_t discard[256];
            size_t left = payload_len;
            while (left > 0) {
                size_t chunk = left < sizeof(discard) ? left : sizeof(discard);
                if (!readExact(c, discard, chunk)) return false;
                left -= chunk;
            }
            continue;
        }

        if (payload_len > 0) {
            if (!readExact(c, rx_buf, payload_len)) return false;
            if (masked) {
                for (size_t i = 0; i < payload_len; ++i) {
                    rx_buf[i] ^= mask[i & 3];
                }
            }
        }
        rx_buf[payload_len] = '\0';

        if (opcode == 0x09) {
            // Ping -> Pong
            sendWsFrame(c, 0x0A, rx_buf, payload_len);
        } else if (opcode == 0x01) {
            // Text JSON frame
            handleWsTextMessage(c, reinterpret_cast<const char*>(rx_buf), payload_len,
                                mute_speaker, live_barge_in, last_activity_ts, ui, cb);
        } else if (opcode == 0x02 && payload_len >= 2) {
            // Raw binary 24 kHz 16-bit PCM downlink frame
            ui.audio_bytes_received += (uint32_t)payload_len;
            ui.last_sample_rate = 24000;
            if (ui.state != UiState::SPEAKING) {
                ui.state = UiState::SPEAKING;
                ui.status_text = "LIVE (REPLY)";
                if (cb) cb(ui);
            }
            if (!mute_speaker) {
                gotchi_audio::playPcmChunk(reinterpret_cast<const int16_t*>(rx_buf),
                                           payload_len / sizeof(int16_t), 24000);
                if (live_barge_in && gotchi_audio::consumeLiveBargeInFlag()) {
                    const char* barge_json = "{\"type\":\"barge_in\"}";
                    sendWsFrame(c, 0x01, reinterpret_cast<const uint8_t*>(barge_json), strlen(barge_json));
                    ui.state = UiState::LISTENING;
                    ui.status_text = "LIVE (BARGE-IN)";
                    if (cb) cb(ui);
                } else {
                    gotchi_audio::flushRxDmaBuffer();
                }
            }
            last_activity_ts = millis();
        }
    }
    return true;
}

static bool performWsHandshake(WiFiClient& c, const String& host, uint16_t port,
                               const String& ws_path, const String& node_id,
                               const String& bearer_token, const String& audio_out_mode) {
    String req = "GET " + ws_path + " HTTP/1.1\r\n";
    req += "Host: " + host;
    if (port != 80) req += ":" + String(port);
    req += "\r\n";
    req += "Upgrade: websocket\r\n";
    req += "Connection: Upgrade\r\n";
    req += "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n";
    req += "Sec-WebSocket-Version: 13\r\n";
    req += "X-Gotchi-Node: " + node_id + "\r\n";
    if (audio_out_mode.length() > 0) {
        req += "X-Gotchi-Audio-Out: " + audio_out_mode + "\r\n";
    }
    if (bearer_token.length() > 0) {
        req += "Authorization: Bearer " + bearer_token + "\r\n";
    }
    req += "\r\n";

    if (!writeAll(c, reinterpret_cast<const uint8_t*>(req.c_str()), req.length())) {
        return false;
    }

    // Read HTTP status line and headers until CRLFCRLF
    uint32_t t0 = millis();
    String line;
    bool status_101 = false;
    bool got_status = false;
    while (millis() - t0 < HANDSHAKE_TIMEOUT_MS) {
        if (!c.available()) {
            if (!c.connected()) return false;
            delay(2);
            continue;
        }
        char ch = (char)c.read();
        if (ch == '\r') continue;
        if (ch == '\n') {
            if (!got_status) {
                got_status = true;
                status_101 = (line.startsWith("HTTP/1.1 101") || line.startsWith("HTTP/1.0 101"));
            } else if (line.length() == 0) {
                return status_101;
            }
            line = "";
        } else if (line.length() < 256) {
            line += ch;
        }
    }
    return false;
}

} // namespace

bool parseLiveUrl(const String& url, String& host, uint16_t& port, String& ws_path, bool* out_tls) {
    bool is_tls = false;
    String rest;
    if (url.startsWith("https://")) { is_tls = true; rest = url.substring(8); }
    else if (url.startsWith("wss://")) { is_tls = true; rest = url.substring(6); }
    else if (url.startsWith("http://")) { is_tls = false; rest = url.substring(7); }
    else if (url.startsWith("ws://")) { is_tls = false; rest = url.substring(5); }
    else return false;
    if (out_tls) *out_tls = is_tls;

    int slash = rest.indexOf('/');
    String hostport = slash < 0 ? rest : rest.substring(0, slash);
    String orig_path = slash < 0 ? String("/") : rest.substring(slash);

    int colon = hostport.indexOf(':');
    if (colon < 0) {
        host = hostport;
        port = is_tls ? 443 : 80;
    } else {
        host = hostport.substring(0, colon);
        port = (uint16_t)hostport.substring(colon + 1).toInt();
    }
    if (host.length() == 0 || port == 0) return false;

    if (orig_path.indexOf("/api/live/ws") >= 0) {
        ws_path = orig_path;
    } else {
        ws_path = "/api/live/ws";
    }
    return true;
}

SessionEndReason runSession(
    const LiveSessionConfig& config,
    GotchiUIState& ui,
    gotchi_voice::StateCb cb,
    SnapshotCallback snap_cb,
    BurstCallback burst_cb,
    PollTickCallback poll_tick_cb
) {
    if (!config.enabled) {
        gotchi_log::RingLogger.info("live", "Go Live session rejected: live_mode is disabled");
        return SessionEndReason::LIVE_DISABLED;
    }

    if (WiFi.status() != WL_CONNECTED) {
        gotchi_log::RingLogger.warn("live", "Go Live session rejected: no WiFi connection");
        ui.state = UiState::OFFLINE;
        ui.status_text = "NO WIFI";
        if (cb) cb(ui);
        return SessionEndReason::NETWORK_ERROR;
    }

    String host, ws_path;
    uint16_t port = 80;
    bool is_tls = false;
    if (!parseLiveUrl(config.voice_url, host, port, ws_path, &is_tls)) {
        gotchi_log::RingLogger.error("live", "Go Live invalid voice_url");
        ui.state = UiState::ERROR;
        ui.error_code = "bad_url";
        ui.status_text = "BAD LIVE URL";
        if (cb) cb(ui);
        return SessionEndReason::NETWORK_ERROR;
    }

    waitForRelease(PIN_BTN_TOP_PTT);
    gotchi_log::RingLogger.info("live", "Connecting Go Live WebSocket", nullptr, config.bearer_token.c_str());

    WiFiClient plain_ws;
    WiFiClientSecure tls_ws;
    if (is_tls) tls_ws.setCACert(GOTCHI_TLS_ROOT_CA_PEM);
    WiFiClient& ws_client = is_tls ? static_cast<WiFiClient&>(tls_ws) : plain_ws;
    if (!ws_client.connect(host.c_str(), port, CONNECT_TIMEOUT_MS) ||
        !performWsHandshake(ws_client, host, port, ws_path, config.node_id, config.bearer_token, config.audio_out_mode)) {
        ws_client.stop();
        gotchi_log::RingLogger.warn("live", "Go Live WebSocket handshake failed");
        ui.state = UiState::OFFLINE;
        ui.error_code = "live_connect";
        ui.status_text = "LIVE NO HUB";
        if (cb) cb(ui);
        return SessionEndReason::NETWORK_ERROR;
    }

    uint8_t* rx_buf = static_cast<uint8_t*>(psramFound() ? ps_malloc(MAX_WS_RX_BYTES) : malloc(32768));
    size_t rx_cap = rx_buf ? (psramFound() ? MAX_WS_RX_BYTES : 32768) : 0;
    if (!rx_buf) {
        ws_client.stop();
        ui.state = UiState::ERROR;
        ui.error_code = "oom";
        ui.status_text = "NO PSRAM";
        if (cb) cb(ui);
        return SessionEndReason::NETWORK_ERROR;
    }

    gotchi_audio::playChime("ptt_start");
    ui.state = UiState::LISTENING;
    ui.status_text = "LIVE ACTIVE";
    if (cb) cb(ui);

    const float vad_thresh = (float)config.vad_rms_lsb / 32768.0f;
    const bool mute_speaker = (config.audio_out_mode == "discord_voice" || config.audio_out_mode == "ble_phone");
    // Enable on-device NLMS Acoustic Echo Cancellation + 300ms full-duplex barge-in ONLY when Go Live AND live_barge_in are active (T6.1)
    if (config.live_barge_in) {
        gotchi_audio::setLiveAecEnabled(true, vad_thresh > 0.002f ? vad_thresh * 1.5f : 0.008f);
    } else {
        gotchi_audio::setLiveAecEnabled(false);
    }

    uint32_t last_speech_ts = millis();
    int16_t pcm_chunk[PCM_UPLINK_SAMPLES];

    SessionEndReason exit_reason = SessionEndReason::USER_EXIT;

    while (true) {
        if (poll_tick_cb) poll_tick_cb();

        // 1. Exit Check: BTN_TOP single press immediately ends session (F19)
        if (isButtonPressed(PIN_BTN_TOP_PTT)) {
            waitForRelease(PIN_BTN_TOP_PTT);
            gotchi_log::RingLogger.info("live", "User ended Go Live session via BTN_TOP");
            exit_reason = SessionEndReason::USER_EXIT;
            break;
        }

        // 2. Mid-Session Camera Snapshot / Burst via BTN_BOT (F19 / F20)
        if (isButtonPressed(PIN_BTN_BOT_MOD)) {
            uint32_t bot_press_ts = millis();
            bool is_burst = false;
            while (digitalRead(PIN_BTN_BOT_MOD) == LOW) {
                if (poll_tick_cb) poll_tick_cb();
                if (millis() - bot_press_ts >= config.cam_long_press_ms) {
                    is_burst = true;
                    break;
                }
                delay(5);
            }

            if (is_burst) {
                gotchi_log::RingLogger.info("live", "Injecting 5-photo burst into Live session");
                if (burst_cb) {
                    CapturedFrameView frames[gotchi_voice::ImageAttachment::MAX_BURST_FRAMES];
                    size_t count = burst_cb(frames, gotchi_voice::ImageAttachment::MAX_BURST_FRAMES);
                    for (size_t i = 0; i < count; ++i) {
                        if (frames[i].data && frames[i].length > 0) {
                            sendWsImageFrame(ws_client, frames[i].data, frames[i].length, i);
                        }
                    }
                    if (count > 0) {
                        ui.status_text = "LIVE (+BURST)";
                        if (cb) cb(ui);
                    }
                }
                waitForRelease(PIN_BTN_BOT_MOD);
            } else {
                waitForRelease(PIN_BTN_BOT_MOD);
                gotchi_log::RingLogger.info("live", "Injecting single snapshot into Live session");
                CapturedFrameView frame;
                if (snap_cb && snap_cb(0, &frame) && frame.data && frame.length > 0) {
                    gotchi_audio::playChime("snap");
                    sendWsImageFrame(ws_client, frame.data, frame.length, 0);
                    ui.status_text = "LIVE (+SNAP)";
                    if (cb) cb(ui);
                }
            }
            last_speech_ts = millis();
        }

        // 3. Uplink 16 kHz 16-bit mono PCM chunks (AEC-cleaned) & VAD check
        float chunk_rms = 0.0f;
        size_t samples_read = gotchi_audio::readLivePcmChunk(pcm_chunk, PCM_UPLINK_SAMPLES, &chunk_rms);
        if (samples_read > 0) {
            if (!sendWsFrame(ws_client, 0x02, reinterpret_cast<const uint8_t*>(pcm_chunk),
                             samples_read * sizeof(int16_t))) {
                gotchi_log::RingLogger.warn("live", "Go Live WebSocket uplink write failed");
                exit_reason = SessionEndReason::NETWORK_ERROR;
                break;
            }
            if (chunk_rms >= vad_thresh) {
                last_speech_ts = millis();
                if (ui.state != UiState::LISTENING) {
                    ui.state = UiState::LISTENING;
                    ui.status_text = "LIVE (SPEAKING)";
                    if (cb) cb(ui);
                }
            }
        }

        // 4. Downlink 24 kHz PCM / JSON event polling
        if (!pollWsDownlink(ws_client, rx_buf, rx_cap, mute_speaker, config.live_barge_in, last_speech_ts, ui, cb)) {
            gotchi_log::RingLogger.info("live", "Go Live WebSocket closed by server");
            exit_reason = SessionEndReason::NETWORK_ERROR;
            break;
        }

        // 5. VAD silence auto-timeout check
        if (millis() - last_speech_ts >= config.live_silence_ms) {
            gotchi_log::RingLogger.info("live", "Go Live session ended: silence timeout reached");
            exit_reason = SessionEndReason::SILENCE_TIMEOUT;
            break;
        }

        if (!ws_client.connected()) {
            exit_reason = SessionEndReason::NETWORK_ERROR;
            break;
        }

        delay(2);
    }

    // Turn off Go Live AEC immediately when session exits so normal PTT incurs zero overhead
    gotchi_audio::setLiveAecEnabled(false);

    // Send polite RFC 6455 Close frame (0x08) before closing TCP socket
    if (ws_client.connected()) {
        sendWsFrame(ws_client, 0x08, nullptr, 0);
        ws_client.stop();
    }
    free(rx_buf);

    gotchi_audio::playChime("ptt_stop");
    gotchi_audio::muteSpeaker();
    gotchi_log::RingLogger.info("live", "Go Live session finished");

    return exit_reason;
}

} // namespace gotchi_live
