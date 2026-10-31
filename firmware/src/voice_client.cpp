// ============================================================================
// GOTCHI Voice Gateway Client (T3.1) — see include/voice_client.h.
// ============================================================================

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include "tls_root_ca.h"
#include <ArduinoJson.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "voice_client.h"
#include "ble_transport.h"
#include "log_buffer.h"

namespace gotchi_voice {
namespace {

constexpr const char* BOUNDARY                 = "----GotchiBoundary7MA4YWxkTrZu0gW";
constexpr size_t      SSE_BUF_BYTES            = 768 * 1024;
constexpr uint32_t    CONNECT_TIMEOUT_MS       = 5000;
constexpr uint32_t    WRITE_STALL_TIMEOUT_MS   = 5000;
constexpr uint32_t    SSE_READ_IDLE_TIMEOUT_MS = 15000;
constexpr uint32_t    IDLE_TIMEOUT_MS          = 60000;
constexpr size_t      UPLOAD_CHUNK             = 1460;
constexpr uint32_t    DEFAULT_RATE_HZ          = 24000;

static AbortPollFn             s_active_abort_poll = nullptr;
static volatile InteractResult s_active_abort_kind = InteractResult::DONE;
static uint32_t       s_turn_start_ms     = 0;
static int32_t        s_stt_ms            = -1;
static int32_t        s_first_text_ms     = -1;
static int32_t        s_first_audio_ms    = -1;

static void resetTurnTimings() {
    s_turn_start_ms  = millis();
    s_stt_ms         = -1;
    s_first_text_ms  = -1;
    s_first_audio_ms = -1;
}

static void logTurnTimings() {
    uint32_t total_ms = millis() - s_turn_start_ms;
    char msg[128];
    snprintf(msg, sizeof(msg),
             "turn_timing stt_ms=%ld first_text_ms=%ld first_audio_ms=%ld total_ms=%lu",
             (long)s_stt_ms, (long)s_first_text_ms, (long)s_first_audio_ms, (unsigned long)total_ms);
    gotchi_log::RingLogger.info("voice", msg);
}

static uint32_t s_last_abort_poll_ms = 0;

bool checkAbortFlag() {
    if (s_active_abort_kind != InteractResult::DONE) return true;
    // Only poll hardware buttons, HTTP server, and Serial JSON-RPC on Core 1 (Arduino loop core).
    // Core 0 background audio tasks read only the latched s_active_abort_kind flag.
    if (xPortGetCoreID() != 1) return false;
    uint32_t now = millis();
    if (now - s_last_abort_poll_ms < 25) return false;
    s_last_abort_poll_ms = now;
    if (s_active_abort_poll) {
        InteractResult r = s_active_abort_poll();
        if (r != InteractResult::DONE) {
            s_active_abort_kind = r;
            return true;
        }
    }
    return false;
}

// Shared static 4 KB socket read buffer for Reader on loopTask so interact() and
// uploadJournalSegment() do not allocate 4 KB on the FreeRTOS task stack.
static uint8_t s_reader_buf[4096];

// Buffered socket reader. When `chunked` is set it strips HTTP chunk framing
// (size line + CRLF after each chunk), so an SSE line that spans a chunk
// boundary stays intact.
class Reader {
public:
    explicit Reader(WiFiClient& c) : c_(c) {}

    void setChunked(bool on) { chunked_ = on; }

    // Next body byte, or -1 on EOF / idle timeout / terminating chunk / user abort.
    int next() {
        if (checkAbortFlag()) return -1;
        if (!chunked_) return rawByte();
        while (chunk_left_ == 0) {
            char line[32];
            int n = readLine(line, sizeof(line));
            if (n < 0) return -1;
            if (n == 0) continue;           // CRLF that follows a chunk's data
            unsigned long size = strtoul(line, nullptr, 16);
            if (size == 0) return -1;       // last-chunk
            chunk_left_ = size;
        }
        int b = rawByte();
        if (b >= 0) chunk_left_--;
        return b;
    }

    // Reads one raw line (CR stripped) into out, NUL-terminated, truncated to cap-1.
    // Returns stored length, or -1 on EOF / idle timeout / abort.
    int readLine(char* out, size_t cap) {
        size_t len = 0;
        while (true) {
            int b = rawByte();
            if (b < 0) return -1;
            if (b == '\n') break;
            if (b == '\r') continue;
            if (len + 1 < cap) out[len++] = (char)b;
        }
        out[len] = '\0';
        return (int)len;
    }

private:
    int rawByte() {
        if (pos_ == len_ && !fill()) return -1;
        return s_reader_buf[pos_++];
    }

    bool fill() {
        uint32_t t0 = millis();
        while (true) {
            if (checkAbortFlag()) return false;
            int avail = c_.available();
            if (avail > 0) {
                size_t want = (size_t)avail < sizeof(s_reader_buf) ? (size_t)avail : sizeof(s_reader_buf);
                int got = c_.read(s_reader_buf, want);
                if (got <= 0) return false;
                len_ = (size_t)got;
                pos_ = 0;
                return true;
            }
            if (!c_.connected()) return false;
            if (millis() - t0 > SSE_READ_IDLE_TIMEOUT_MS) return false;
            delay(1);
        }
    }

    WiFiClient& c_;
    bool chunked_ = false;
    size_t chunk_left_ = 0;
    size_t pos_ = 0;
    size_t len_ = 0;
};

// Splits "http(s)://host[:port]/path". Supports https:// (443 default for Tailscale-free
// mobile roaming over Cloudflare Tunnel / reverse proxy) and http:// (80/8765 on LAN).
bool parseUrl(const String& url, String& host, uint16_t& port, String& path, bool* out_tls = nullptr) {
    bool is_tls = false;
    String rest;
    if (url.startsWith("https://")) {
        is_tls = true;
        rest = url.substring(8);
    } else if (url.startsWith("http://")) {
        is_tls = false;
        rest = url.substring(7);
    } else {
        return false;
    }
    if (out_tls) *out_tls = is_tls;
    int slash = rest.indexOf('/');
    String hostport = slash < 0 ? rest : rest.substring(0, slash);
    path = slash < 0 ? String("/") : rest.substring(slash);
    int colon = hostport.indexOf(':');
    if (colon < 0) {
        host = hostport;
        port = is_tls ? 443 : 80;
    } else {
        host = hostport.substring(0, colon);
        port = (uint16_t)hostport.substring(colon + 1).toInt();
    }
    return host.length() > 0 && port > 0;
}

// Blocking write of n bytes; gives up if user aborts, peer disconnects, or stalls for WRITE_STALL_TIMEOUT_MS.
bool writeAll(WiFiClient& c, const uint8_t* p, size_t n) {
    size_t sent = 0;
    uint32_t t0 = millis();
    while (sent < n) {
        if (checkAbortFlag()) return false;
        size_t w = c.write(p + sent, n - sent);
        if (w == 0) {
            if (checkAbortFlag() || !c.connected() || millis() - t0 > WRITE_STALL_TIMEOUT_MS) return false;
            delay(1);
            continue;
        }
        sent += w;
        t0 = millis();
    }
    return true;
}

bool containsIgnoreCase(const char* hay, const char* needle) {
    size_t n = strlen(needle);
    for (; *hay; hay++) {
        if (strncasecmp(hay, needle, n) == 0) return true;
    }
    return false;
}

UiState stateForHttpStatus(int code) {
    switch (code) {
        case 409: return UiState::BUSY;     // another interaction in flight
        case 503: return UiState::OFFLINE;  // hub disabled / inference worker down
        case 400:                           // bad or missing audio
        case 500:                           // pipeline error
        default:  return UiState::ERROR;
    }
}

void setFailure(GotchiUIState& ui, UiState s, const char* code, const String& text, StateCb cb) {
    ui.state = s;
    ui.error_code = code;
    ui.status_text = text;
    if (cb) cb(ui);
}

// Locates the base64 value of "<key>":"...". Works on the raw line so the big
// string is never copied. Value ends at the next '"' (base64 has no escapes).
const char* findB64(const char* line, const char* key, size_t& out_len) {
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

// Handles one `data:` line. Returns true when the event is `done` or aborted.
bool handleEvent(const char* event, const char* line, const JsonDocument& filter,
                 GotchiUIState& ui, StateCb cb, bool mute_local_speaker = false) {
    if (checkAbortFlag()) return true;
    const bool is_sentence = strcmp(event, "sentence") == 0;
    const bool is_legacy   = strcmp(event, "audio_chunk") == 0;

    // Fast-path for audio events: extract base64 directly without scanning 200KB JSON through ArduinoJson
    size_t b64_len = 0;
    const char* b64 = nullptr;
    if (is_sentence)    b64 = findB64(line, "audio_b64", b64_len);
    else if (is_legacy) b64 = findB64(line, "data", b64_len);

    if (is_sentence || is_legacy) {
        if (!b64 || b64_len < 4) {
            Serial.printf("voice: '%s' event without audio payload\n", event);
            return false;
        }
        const bool is_filler = is_sentence && (
            (strstr(line, "\"filler\":true") != nullptr) ||
            (strstr(line, "\"filler\": true") != nullptr)
        );
        const bool is_async_handoff = is_sentence && (
            (strstr(line, "\"async_handoff\":true") != nullptr) ||
            (strstr(line, "\"async_handoff\": true") != nullptr)
        );
        (void)is_filler;
        (void)is_async_handoff;
        if (s_first_audio_ms < 0) s_first_audio_ms = (int32_t)(millis() - s_turn_start_ms);
        uint32_t rate = DEFAULT_RATE_HZ;
        const bool is_adpcm = (strstr(line, "\"format\":\"adpcm\"") != nullptr) ||
                              (strstr(line, "\"format\": \"adpcm\"") != nullptr);
        size_t pad = (b64[b64_len - 1] == '=') + (b64[b64_len >= 2 ? b64_len - 2 : 0] == '=');
        size_t raw_decoded_bytes = (b64_len / 4) * 3 - pad;
        ui.audio_bytes_received += (uint32_t)(is_adpcm ? (raw_decoded_bytes * 4) : raw_decoded_bytes);
        ui.last_sample_rate = rate;
        if (!mute_local_speaker) {
            gotchi_audio::appendBase64PcmToTurn(b64, b64_len, rate, is_adpcm);
            const bool sentence_done = is_sentence && (
                (strstr(line, "\"sentence_done\":true") != nullptr) ||
                (strstr(line, "\"sentence_done\": true") != nullptr)
            );
            if (sentence_done && gotchi_audio::getTurnPcmSamples() > 0) {
                gotchi_audio::commitTurnSentenceForPlayback();
            }
        }
        if (checkAbortFlag()) return true;
        return false;
    }

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, line, DeserializationOption::Filter(filter));
    if (err) {
        Serial.printf("voice: bad JSON in '%s' event: %s\n", event, err.c_str());
        return false;
    }

    if (strcmp(event, "state") == 0) {
        UiState next = uiStateFromString(doc["state"] | "");
        if (ui.skin == "zero" && (next == UiState::TRANSCRIBING || next == UiState::SYNTHESIZING)) {
            next = UiState::THINKING;
        }
        const char* name = UI_STATE_NAMES[static_cast<uint8_t>(next)];
        String text = name;
        text.toUpperCase();
        ui.status_text = text;
        // Defer SPEAKING e-paper refresh until the SSE stream finishes and Wi-Fi closes
        if (next != ui.state && next != UiState::SPEAKING) {
            ui.state = next;
            if (cb) cb(ui);
        }
    } else if (strcmp(event, "transcript") == 0) {
        if (s_stt_ms < 0) s_stt_ms = (int32_t)(millis() - s_turn_start_ms);
        const char* t = doc["transcript"] | "";
        if (!*t) t = doc["text"] | "";
        ui.transcript = t;
        ui.status_text = "HEARD";
        if (ui.skin != "zero" && cb) cb(ui);
    } else if (strcmp(event, "status") == 0) {
        const char* s = doc["status"] | "";
        if (*s) ui.status_text = s;
    } else if (strcmp(event, "reply") == 0) {
        if (s_first_text_ms < 0) s_first_text_ms = (int32_t)(millis() - s_turn_start_ms);
        ui.reply = doc["reply"] | "";
        if (ui.skin != "zero" && cb) cb(ui);
    } else if (is_sentence || is_legacy) {
        if (!b64 || b64_len < 4) {
            Serial.printf("voice: '%s' event without audio payload\n", event);
            return false;
        }
        if (s_first_audio_ms < 0) s_first_audio_ms = (int32_t)(millis() - s_turn_start_ms);
        uint32_t rate = doc["sample_rate"] | DEFAULT_RATE_HZ;
        const char* fmt = doc["format"] | "pcm";
        const bool is_adpcm = (strcmp(fmt, "adpcm") == 0);
        size_t pad = (b64[b64_len - 1] == '=') + (b64[b64_len >= 2 ? b64_len - 2 : 0] == '=');
        size_t raw_decoded_bytes = (b64_len / 4) * 3 - pad;
        ui.audio_bytes_received += (uint32_t)(is_adpcm ? (raw_decoded_bytes * 4) : raw_decoded_bytes);
        ui.last_sample_rate = rate;
        if (ui.state != UiState::SPEAKING) {
            ui.state = UiState::SPEAKING;
            if (cb) cb(ui);
        }
        if (!mute_local_speaker) {
            bool played = is_adpcm
                ? gotchi_audio::playBase64AdpcmChunk(b64, b64_len, rate)
                : gotchi_audio::playBase64PcmChunk(b64, b64_len, rate);
            if (!played && checkAbortFlag()) {
                return true;
            }
        } else {
            // Mute local speaker for discord_voice / ble_phone while keeping UI in sync (F8/T3.3)
            if (checkAbortFlag()) return true;
        }
    } else if (strcmp(event, "error") == 0) {
        const char* code = doc["error"] | "error";
        const char* msg  = doc["message"] | "";
        ui.state = UiState::ERROR;
        ui.error_code = code;
        if (*msg) ui.status_text = String(msg).substring(0, 22);
        if (cb) cb(ui);
    } else if (strcmp(event, "done") == 0) {
        logTurnTimings();
        return true;
    }
    return false;
}

// Reads SSE lines from rd into buf. Returns true if `done` was seen or user aborted.
bool streamEvents(Reader& rd, char* buf, GotchiUIState& ui, StateCb cb, bool mute_local_speaker = false) {
    JsonDocument filter;
    filter["state"] = true;
    filter["transcript"] = true;
    filter["text"] = true;
    filter["status"] = true;
    filter["reply"] = true;
    filter["sample_rate"] = true;
    filter["format"] = true;
    filter["error"] = true;
    filter["message"] = true;

    char event[24] = "";
    size_t len = 0;
    bool overflow = false;
    while (true) {
        if (checkAbortFlag()) return true;
        int b = rd.next();
        if (b < 0) return checkAbortFlag();
        if (b == '\r') continue;
        if (b != '\n') {
            if (len < SSE_BUF_BYTES - 1) buf[len++] = (char)b;
            else overflow = true;
            continue;
        }

        buf[len] = '\0';
        if (overflow) {
            Serial.printf("voice: dropped SSE line over %u bytes (event '%s')\n",
                          (unsigned)SSE_BUF_BYTES, event);
        } else if (len == 0) {
            event[0] = '\0';
        } else if (strncmp(buf, "event:", 6) == 0) {
            const char* name = buf + 6;
            while (*name == ' ') name++;
            strncpy(event, name, sizeof(event) - 1);
            event[sizeof(event) - 1] = '\0';
        } else if (strncmp(buf, "data:", 5) == 0) {
            if (handleEvent(event, buf + 5, filter, ui, cb, mute_local_speaker)) return true;
        }
        len = 0;
        overflow = false;
    }
}

static bool sendBleAudioCompressedOrRaw(const gotchi_audio::WavView& wav) {
    if (!wav.data || wav.length == 0) return true;
    size_t adpcm_cap = gotchi_ble::ADPCM_HEADER_SIZE + (wav.length / 4) + 16;
    uint8_t* adpcm_buf = (uint8_t*)(psramFound() ? ps_malloc(adpcm_cap) : malloc(adpcm_cap));
    if (adpcm_buf) {
        size_t adpcm_len = gotchi_ble::encodeWavToAdpcm(wav.data, wav.length, adpcm_buf, adpcm_cap);
        if (adpcm_len > 0) {
            bool ok = gotchi_ble::sendFramedPayload(
                gotchi_ble::FrameType::AUDIO_CHUNK,
                adpcm_buf,
                adpcm_len,
                0,
                false,
                gotchi_ble::FrameFlags::ADPCM
            );
            free(adpcm_buf);
            return ok;
        }
        free(adpcm_buf);
    }
    return gotchi_ble::sendFramedPayload(gotchi_ble::FrameType::AUDIO_CHUNK, wav.data, wav.length, 0);
}

bool interactOverBle(const gotchi_audio::WavView& wav, const String& url, const String& node_id,
                     GotchiUIState& ui, StateCb cb, const ImageAttachment* image,
                     InteractResult* out_result, const String& audio_out_mode) {
    const bool has_audio = (wav.data != nullptr && wav.length > 0);
    const bool has_burst = (image != nullptr && image->burst_count > 0);
    const bool has_single_image = (!has_burst && image != nullptr && image->data != nullptr && image->length > 0);

    gotchi_ble::clearIncomingQueue();

    if (has_audio) {
        if (!sendBleAudioCompressedOrRaw(wav)) {
            setFailure(ui, UiState::ERROR, "ble_tx", "BLE TX FAIL", cb);
            return false;
        }
    }

    if (has_burst) {
        for (size_t i = 0; i < image->burst_count && i < ImageAttachment::MAX_BURST_FRAMES; ++i) {
            if (image->burst_data[i] && image->burst_lengths[i] > 0) {
                if (!gotchi_ble::sendFramedPayload(
                        gotchi_ble::FrameType::IMAGE_FRAME,
                        image->burst_data[i],
                        image->burst_lengths[i],
                        (uint8_t)i)) {
                    setFailure(ui, UiState::ERROR, "ble_tx", "BLE SNAP FAIL", cb);
                    return false;
                }
            }
        }
    } else if (has_single_image) {
        if (!gotchi_ble::sendFramedPayload(
                gotchi_ble::FrameType::IMAGE_FRAME,
                image->data,
                image->length,
                0)) {
            setFailure(ui, UiState::ERROR, "ble_tx", "BLE SNAP FAIL", cb);
            return false;
        }
    }

    JsonDocument commitDoc;
    commitDoc["node_id"] = node_id;
    commitDoc["hub_url"] = url;
    commitDoc["audio_out_mode"] = audio_out_mode.length() > 0 ? audio_out_mode : String("speaker");
    if (image != nullptr && image->default_prompt.length() > 0) {
        commitDoc["prompt"] = image->default_prompt;
    }
    String commitJson;
    serializeJson(commitDoc, commitJson);
    if (!gotchi_ble::sendFramedPayload(
            gotchi_ble::FrameType::TURN_COMMIT,
            (const uint8_t*)commitJson.c_str(),
            commitJson.length(),
            0)) {
        setFailure(ui, UiState::ERROR, "ble_tx", "BLE COMMIT FAIL", cb);
        return false;
    }

    constexpr size_t BLE_SSE_CAP = 16 * 1024;
    uint8_t* rx_buf = (uint8_t*)ps_malloc(BLE_SSE_CAP);
    if (!rx_buf) rx_buf = (uint8_t*)malloc(2048);
    const size_t rx_cap = psramFound() ? BLE_SSE_CAP : 2048;
    if (!rx_buf) {
        setFailure(ui, UiState::ERROR, "oom", "NO BLE BUF", cb);
        return false;
    }

    JsonDocument filter;
    filter["state"] = true;
    filter["transcript"] = true;
    filter["text"] = true;
    filter["status"] = true;
    filter["reply"] = true;
    filter["sample_rate"] = true;
    filter["format"] = true;
    filter["error"] = true;
    filter["message"] = true;
    filter["event"] = true;
    filter["detail"] = true;

    const bool mute_local = (audio_out_mode == "discord_voice" || audio_out_mode == "ble_phone");
    gotchi_audio::setPlaybackAbortCheck(checkAbortFlag);

    uint32_t last_rx_ts = millis();
    bool done = false;

    while (millis() - last_rx_ts <= IDLE_TIMEOUT_MS) {
        if (checkAbortFlag()) {
            gotchi_ble::sendFramedPayload(gotchi_ble::FrameType::CANCEL, nullptr, 0);
            done = true;
            break;
        }
        size_t rx_len = 0;
        gotchi_ble::FrameType rx_type = gotchi_ble::FrameType::SSE_EVENT;
        if (!gotchi_ble::pollIncomingFrame(rx_buf, rx_cap - 1, rx_len, rx_type)) {
            delay(2);
            continue;
        }
        last_rx_ts = millis();
        rx_buf[rx_len] = '\0';

        if (rx_type != gotchi_ble::FrameType::SSE_EVENT) {
            continue;
        }

        char* text = (char*)rx_buf;
        if (text[0] == '{') {
            if (handleEvent("error", text, filter, ui, cb, mute_local)) {
                done = true;
                break;
            }
            continue;
        }

        char event_name[24] = "message";
        char* saveptr = nullptr;
        for (char* line = strtok_r(text, "\r\n", &saveptr); line != nullptr; line = strtok_r(nullptr, "\r\n", &saveptr)) {
            if (strncmp(line, "event:", 6) == 0) {
                const char* p = line + 6;
                while (*p == ' ') p++;
                strncpy(event_name, p, sizeof(event_name) - 1);
                event_name[sizeof(event_name) - 1] = '\0';
            } else if (strncmp(line, "data:", 5) == 0) {
                const char* d = line + 5;
                while (*d == ' ') d++;
                if (handleEvent(event_name, d, filter, ui, cb, mute_local)) {
                    done = true;
                    break;
                }
            }
        }
        if (done) break;
    }

    free(rx_buf);
    if (!checkAbortFlag() && gotchi_audio::getTurnPcmSamples() > 0) {
        char hwm_msg[64];
        snprintf(hwm_msg, sizeof(hwm_msg), "pre-play stack_hwm=%uB pcm_samples=%u",
                 (unsigned)uxTaskGetStackHighWaterMark(NULL),
                 (unsigned)gotchi_audio::getTurnPcmSamples());
        gotchi_log::RingLogger.info("voice", hwm_msg);
        if (ui.state != UiState::SPEAKING) {
            ui.state = UiState::SPEAKING;
            ui.status_text = "SPEAKING";
            if (cb) cb(ui);
        }
        if (!mute_local) {
            gotchi_audio::playTurnPcm();
        } else {
            gotchi_audio::clearTurnPcm();
        }
    }
    gotchi_audio::setPlaybackAbortCheck(nullptr);
    s_active_abort_poll = nullptr;
    gotchi_audio::muteSpeaker();

    if (s_active_abort_kind != InteractResult::DONE) {
        if (out_result) *out_result = s_active_abort_kind;
        return true;
    }
    if (out_result) *out_result = done ? InteractResult::DONE : InteractResult::ERROR;
    return done;
}

} // namespace

bool interact(const gotchi_audio::WavView& wav, const String& url, const String& node_id,
              GotchiUIState& ui, StateCb cb, const String& bearer_token,
              const ImageAttachment* image, AbortPollFn abort_poll,
              InteractResult* out_result, const String& audio_out_mode) {
    resetTurnTimings();
    gotchi_audio::clearTurnPcm();
    s_active_abort_poll = abort_poll;
    s_active_abort_kind = InteractResult::DONE;
    if (out_result) *out_result = InteractResult::ERROR;

    String host, path;
    uint16_t port = 80;
    bool is_tls = false;
    const bool has_audio = (wav.data != nullptr && wav.length > 0);
    const bool has_burst = (image != nullptr && image->burst_count > 0);
    const bool has_single_image = (!has_burst && image != nullptr && image->data != nullptr && image->length > 0);

    if (!parseUrl(url, host, port, path, &is_tls)) {
        setFailure(ui, UiState::ERROR, "bad_url", "BAD VOICE URL", cb);
        return false;
    }
    const bool is_pending_fetch = (path.indexOf("/pending_answer") >= 0);
    if (!has_audio && !has_burst && !has_single_image && !is_pending_fetch) {
        setFailure(ui, UiState::ERROR, "bad_url", "BAD VOICE URL", cb);
        return false;
    }
    if (!is_pending_fetch && (WiFi.status() != WL_CONNECTED || audio_out_mode == "ble_phone") && gotchi_ble::isBlePeerConnected()) {
        if (interactOverBle(wav, url, node_id, ui, cb, image, out_result, audio_out_mode)) {
            return true;
        }
        // Clean failover: if BLE dropped mid-turn (e.g. phone walked out of range) and Wi-Fi is connected,
        // fall through to direct Wi-Fi HTTP upload and route audio to the onboard speaker.
        if (WiFi.status() != WL_CONNECTED || s_active_abort_kind != InteractResult::DONE) {
            return false;
        }
        gotchi_log::RingLogger.info("ble", "BLE turn failed; failing over to direct Wi-Fi + onboard speaker");
    }
    if (WiFi.status() != WL_CONNECTED) {
        setFailure(ui, UiState::OFFLINE, "wifi", "NO WIFI", cb);
        return false;
    }

    // If audio_out_mode was configured for `ble_phone` but the phone is out of range / disconnected,
    // automatically fail over audio playback to Gotchi's onboard `speaker` so the reply is audible.
    const String effective_audio_out_mode = (audio_out_mode == "ble_phone" && !gotchi_ble::isBlePeerConnected())
        ? String("speaker")
        : audio_out_mode;

    char session[24];
    snprintf(session, sizeof(session), "gotchi-%lu", (unsigned long)millis());

    String head;
    String pre;
    String post;
    String img_pre[ImageAttachment::MAX_BURST_FRAMES];
    size_t burst_total_len = 0;

    if (is_pending_fetch) {
        head = "GET " + path + " HTTP/1.1\r\n";
        head += "Host: " + host;
        if ((!is_tls && port != 80) || (is_tls && port != 443)) head += ":" + String(port);
        head += "\r\n";
        head += "Accept: text/event-stream\r\n";
        head += "X-Gotchi-Node: " + node_id + "\r\n";
        if (bearer_token.length() > 0) {
            head += "Authorization: Bearer " + bearer_token + "\r\n";
        }
        head += "Connection: close\r\n\r\n";
    } else {
        if (has_audio) {
            pre += "--";
            pre += BOUNDARY;
            pre += "\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"ptt.wav\"\r\n";
            pre += "Content-Type: audio/wav\r\n\r\n";
        }

        bool wrote_part = has_audio;

        if (has_burst) {
            for (size_t i = 0; i < image->burst_count && i < ImageAttachment::MAX_BURST_FRAMES; ++i) {
                if (image->burst_data[i] && image->burst_lengths[i] > 0) {
                    if (wrote_part) img_pre[i] += "\r\n";
                    wrote_part = true;
                    img_pre[i] += "--";
                    img_pre[i] += BOUNDARY;
                    img_pre[i] += "\r\nContent-Disposition: form-data; name=\"image_" + String(i) + "\"; filename=\"snap_" + String(i) + ".jpg\"\r\n";
                    img_pre[i] += "Content-Type: image/jpeg\r\n\r\n";
                    burst_total_len += img_pre[i].length() + image->burst_lengths[i];
                }
            }
        } else if (has_single_image) {
            if (wrote_part) img_pre[0] += "\r\n";
            wrote_part = true;
            img_pre[0] += "--";
            img_pre[0] += BOUNDARY;
            img_pre[0] += "\r\nContent-Disposition: form-data; name=\"image\"; filename=\"snap.jpg\"\r\n";
            img_pre[0] += "Content-Type: image/jpeg\r\n\r\n";
            burst_total_len += img_pre[0].length() + image->length;
        }

        if (!wrote_part) {
            setFailure(ui, UiState::ERROR, "bad_url", "BAD VOICE URL", cb);
            return false;
        }

        post = "\r\n--";
        post += BOUNDARY;
        post += "\r\nContent-Disposition: form-data; name=\"node_id\"\r\n\r\n";
        post += node_id;
        post += "\r\n--";
        post += BOUNDARY;
        post += "\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n";
        post += session;
        if (image != nullptr && image->default_prompt.length() > 0) {
            post += "\r\n--";
            post += BOUNDARY;
            post += "\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n";
            post += image->default_prompt;
        }
        if (effective_audio_out_mode.length() > 0) {
            post += "\r\n--";
            post += BOUNDARY;
            post += "\r\nContent-Disposition: form-data; name=\"audio_out_mode\"\r\n\r\n";
            post += effective_audio_out_mode;
        }
        post += "\r\n--";
        post += BOUNDARY;
        post += "--\r\n";

        size_t content_len = (has_audio ? (pre.length() + wav.length) : 0) + post.length() + burst_total_len;

        head = "POST " + path + " HTTP/1.1\r\n";
        head += "Host: " + host;
        if ((!is_tls && port != 80) || (is_tls && port != 443)) head += ":" + String(port);
        head += "\r\n";
        head += "Content-Type: multipart/form-data; boundary=";
        head += BOUNDARY;
        head += "\r\n";
        head += "Content-Length: " + String((unsigned long)content_len) + "\r\n";
        head += "Accept: text/event-stream\r\n";
        head += "X-Gotchi-Node: " + node_id + "\r\n";
        if (bearer_token.length() > 0) {
            head += "Authorization: Bearer " + bearer_token + "\r\n";
        }
        head += "Connection: close\r\n\r\n";
    }

    WiFiClient plain_client;
    WiFiClientSecure tls_client;
    if (is_tls) tls_client.setCACert(GOTCHI_TLS_ROOT_CA_PEM);
    WiFiClient& client = is_tls ? static_cast<WiFiClient&>(tls_client) : plain_client;
    if (!client.connect(host.c_str(), port, CONNECT_TIMEOUT_MS)) {
        if (s_active_abort_kind != InteractResult::DONE) {
            if (out_result) *out_result = s_active_abort_kind;
            return true;
        }
        if (gotchi_ble::isBlePeerConnected()) {
            return interactOverBle(wav, url, node_id, ui, cb, image, out_result, audio_out_mode);
        }
        setFailure(ui, UiState::OFFLINE, "connect", "NO HUB", cb);
        return false;
    }

    bool sent = writeAll(client, (const uint8_t*)head.c_str(), head.length());
    if (!is_pending_fetch) {
        if (sent && has_audio) {
            sent = writeAll(client, (const uint8_t*)pre.c_str(), pre.length());
            for (size_t off = 0; sent && off < wav.length; off += UPLOAD_CHUNK) {
                size_t n = wav.length - off < UPLOAD_CHUNK ? wav.length - off : UPLOAD_CHUNK;
                sent = writeAll(client, wav.data + off, n);
            }
        }
        if (sent && has_burst) {
            for (size_t i = 0; sent && i < image->burst_count && i < ImageAttachment::MAX_BURST_FRAMES; ++i) {
                if (image->burst_data[i] && image->burst_lengths[i] > 0) {
                    sent = writeAll(client, (const uint8_t*)img_pre[i].c_str(), img_pre[i].length());
                    for (size_t off = 0; sent && off < image->burst_lengths[i]; off += UPLOAD_CHUNK) {
                        size_t n = image->burst_lengths[i] - off < UPLOAD_CHUNK ? image->burst_lengths[i] - off : UPLOAD_CHUNK;
                        sent = writeAll(client, image->burst_data[i] + off, n);
                    }
                }
            }
        } else if (sent && has_single_image) {
            sent = writeAll(client, (const uint8_t*)img_pre[0].c_str(), img_pre[0].length());
            for (size_t off = 0; sent && off < image->length; off += UPLOAD_CHUNK) {
                size_t n = image->length - off < UPLOAD_CHUNK ? image->length - off : UPLOAD_CHUNK;
                sent = writeAll(client, image->data + off, n);
            }
        }
        sent = sent && writeAll(client, (const uint8_t*)post.c_str(), post.length());
    }
    if (!sent) {
        client.stop();
        if (s_active_abort_kind != InteractResult::DONE) {
            if (out_result) *out_result = s_active_abort_kind;
            return true;
        }
        if (!is_pending_fetch && gotchi_ble::isBlePeerConnected()) {
            return interactOverBle(wav, url, node_id, ui, cb, image, out_result, audio_out_mode);
        }
        setFailure(ui, UiState::ERROR, "send", is_pending_fetch ? "FETCH FAILED" : "UPLOAD FAILED", cb);
        return false;
    }

    Reader rd(client);
    char line[256];
    int n = rd.readLine(line, sizeof(line));
    if (n < 0) {
        client.stop();
        if (s_active_abort_kind != InteractResult::DONE) {
            if (out_result) *out_result = s_active_abort_kind;
            return true;
        }
        setFailure(ui, UiState::ERROR, "no_response", "NO RESPONSE", cb);
        return false;
    }
    int status = 0;
    if (strncmp(line, "HTTP/", 5) == 0) {
        const char* sp = strchr(line, ' ');
        if (sp) status = atoi(sp + 1);
    }
    if (status != 200) {
        client.stop();
        if (is_pending_fetch && status == 404) {
            setFailure(ui, UiState::IDLE, "no_pending", "NO ANSWER", cb);
            return false;
        }
        char code[8];
        snprintf(code, sizeof(code), "%d", status);
        setFailure(ui, stateForHttpStatus(status), code, String("HTTP ") + code, cb);
        return false;
    }

    bool chunked = false;
    while (true) {
        n = rd.readLine(line, sizeof(line));
        if (n < 0) {
            client.stop();
            setFailure(ui, UiState::ERROR, "headers", "BAD RESPONSE", cb);
            return false;
        }
        if (n == 0) break;
        if (strncasecmp(line, "Transfer-Encoding:", 18) == 0 && containsIgnoreCase(line, "chunked")) {
            chunked = true;
        }
    }
    rd.setChunked(chunked);

    char* buf = (char*)ps_malloc(SSE_BUF_BYTES);
    if (!buf) {
        client.stop();
        setFailure(ui, UiState::ERROR, "oom", "NO PSRAM", cb);
        return false;
    }

    const bool mute_local = (effective_audio_out_mode == "discord_voice" || effective_audio_out_mode == "ble_phone");
    gotchi_audio::setPlaybackAbortCheck(checkAbortFlag);
    bool done = streamEvents(rd, buf, ui, cb, mute_local);
    free(buf);
    client.stop();

    if (!checkAbortFlag() && gotchi_audio::getTurnPcmSamples() > 0) {
        char hwm_msg[64];
        snprintf(hwm_msg, sizeof(hwm_msg), "pre-play stack_hwm=%uB pcm_samples=%u",
                 (unsigned)uxTaskGetStackHighWaterMark(NULL),
                 (unsigned)gotchi_audio::getTurnPcmSamples());
        gotchi_log::RingLogger.info("voice", hwm_msg);
        if (ui.state != UiState::SPEAKING) {
            ui.state = UiState::SPEAKING;
            ui.status_text = "SPEAKING";
            if (cb) cb(ui);
        }
        if (!mute_local) {
            gotchi_audio::playTurnPcm();
        } else {
            gotchi_audio::clearTurnPcm();
        }
    }
    gotchi_audio::setPlaybackAbortCheck(nullptr);
    s_active_abort_poll = nullptr;
    gotchi_audio::muteSpeaker();

    if (s_active_abort_kind != InteractResult::DONE) {
        if (out_result) *out_result = s_active_abort_kind;
        return true;
    }
    if (out_result) *out_result = done ? InteractResult::DONE : InteractResult::ERROR;
    return done;
}

bool uploadJournalSegment(const gotchi_audio::WavView& wav, const String& url,
                          const String& node_id, const String& bearer_token) {
    if (!wav.data || wav.length == 0) return false;

    // Priority 1: BLE companion link (allows Wi-Fi radio to stay powered off all day)
    if (gotchi_ble::isBlePeerConnected()) {
        if (sendBleAudioCompressedOrRaw(wav)) {
            JsonDocument commitDoc;
            commitDoc["node_id"] = node_id;
            commitDoc["hub_url"] = url;
            commitDoc["mode"] = "journal";
            commitDoc["audio_out_mode"] = "ble_phone";
            String commitJson;
            serializeJson(commitDoc, commitJson);
            bool ok = gotchi_ble::sendFramedPayload(
                gotchi_ble::FrameType::TURN_COMMIT,
                (const uint8_t*)commitJson.c_str(),
                commitJson.length(),
                0
            );
            if (ok) {
                gotchi_log::RingLogger.info("journal", "Dispatched journal segment over BLE");
                return true;
            }
        }
        gotchi_log::RingLogger.warn("journal", "BLE journal send failed; falling back to Wi-Fi if connected");
    }

    // Priority 2: Direct Wi-Fi HTTP upload when connected (or on BLE failover)
    if (WiFi.status() != WL_CONNECTED) {
        return false;
    }

    String host, path;
    uint16_t port = 80;
    bool is_tls = false;
    if (!parseUrl(url, host, port, path, &is_tls)) {
        return false;
    }

    char session[28];
    snprintf(session, sizeof(session), "jseg-%lu", (unsigned long)millis());

    String pre = "--";
    pre += BOUNDARY;
    pre += "\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"journal.wav\"\r\n";
    pre += "Content-Type: audio/wav\r\n\r\n";

    String post = "\r\n--";
    post += BOUNDARY;
    post += "\r\nContent-Disposition: form-data; name=\"node_id\"\r\n\r\n";
    post += node_id;
    post += "\r\n--";
    post += BOUNDARY;
    post += "\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n";
    post += session;
    post += "\r\n--";
    post += BOUNDARY;
    post += "\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\njournal\r\n--";
    post += BOUNDARY;
    post += "--\r\n";

    size_t content_len = pre.length() + wav.length + post.length();

    String head = "POST " + path + " HTTP/1.1\r\n";
    head += "Host: " + host;
    if ((!is_tls && port != 80) || (is_tls && port != 443)) head += ":" + String(port);
    head += "\r\nContent-Type: multipart/form-data; boundary=";
    head += BOUNDARY;
    head += "\r\nContent-Length: " + String((unsigned long)content_len) + "\r\n";
    head += "X-Gotchi-Node: " + node_id + "\r\n";
    if (bearer_token.length() > 0) {
        head += "Authorization: Bearer " + bearer_token + "\r\n";
    }
    head += "Connection: close\r\n\r\n";

    WiFiClient plain_client;
    WiFiClientSecure tls_client;
    if (is_tls) tls_client.setCACert(GOTCHI_TLS_ROOT_CA_PEM);
    WiFiClient& client = is_tls ? static_cast<WiFiClient&>(tls_client) : plain_client;
    if (!client.connect(host.c_str(), port, CONNECT_TIMEOUT_MS)) {
        return false;
    }
    bool sent = writeAll(client, (const uint8_t*)head.c_str(), head.length()) &&
                writeAll(client, (const uint8_t*)pre.c_str(), pre.length());
    for (size_t off = 0; sent && off < wav.length; off += UPLOAD_CHUNK) {
        size_t n = wav.length - off < UPLOAD_CHUNK ? wav.length - off : UPLOAD_CHUNK;
        sent = writeAll(client, wav.data + off, n);
    }
    sent = sent && writeAll(client, (const uint8_t*)post.c_str(), post.length());
    if (sent) {
        Reader rd(client);
        char line[128];
        rd.readLine(line, sizeof(line));
        gotchi_log::RingLogger.info("journal", "Uploaded journal segment over Wi-Fi");
    }
    client.stop();
    return sent;
}

String buildPendingAnswerUrl(const String& voice_url, const String& node_id, const String& answer_id) {
    String host, path;
    uint16_t port = 80;
    bool is_tls = false;
    if (!parseUrl(voice_url, host, port, path, &is_tls)) {
        return "";
    }
    String url = (is_tls ? "https://" : "http://") + host;
    if ((!is_tls && port != 80) || (is_tls && port != 443)) {
        url += ":" + String(port);
    }
    url += "/api/gotchi/pending_answer?node_id=" + node_id;
    if (answer_id.length() > 0) {
        url += "&answer_id=" + answer_id;
    }
    return url;
}

bool ackPendingAnswer(const String& voice_url, const String& node_id,
                      const String& answer_id, const String& bearer_token) {
    if (WiFi.status() != WL_CONNECTED) return false;

    String host, path;
    uint16_t port = 80;
    bool is_tls = false;
    if (!parseUrl(voice_url, host, port, path, &is_tls)) return false;

    JsonDocument doc;
    doc["node_id"] = node_id;
    if (answer_id.length() > 0) {
        doc["answer_id"] = answer_id;
    }
    String body;
    serializeJson(doc, body);

    String req = "POST /api/gotchi/pending_answer/ack HTTP/1.1\r\n";
    req += "Host: " + host;
    if ((!is_tls && port != 80) || (is_tls && port != 443)) req += ":" + String(port);
    req += "\r\n";
    req += "Content-Type: application/json\r\n";
    req += "Content-Length: " + String((unsigned long)body.length()) + "\r\n";
    req += "X-Gotchi-Node: " + node_id + "\r\n";
    if (bearer_token.length() > 0) {
        req += "Authorization: Bearer " + bearer_token + "\r\n";
    }
    req += "Connection: close\r\n\r\n";
    req += body;

    WiFiClient plain_client;
    WiFiClientSecure tls_client;
    if (is_tls) tls_client.setCACert(GOTCHI_TLS_ROOT_CA_PEM);
    WiFiClient& client = is_tls ? static_cast<WiFiClient&>(tls_client) : plain_client;
    if (!client.connect(host.c_str(), port, CONNECT_TIMEOUT_MS)) {
        return false;
    }
    writeAll(client, (const uint8_t*)req.c_str(), req.length());
    Reader rd(client);
    char line[128];
    rd.readLine(line, sizeof(line));
    client.stop();
    return true;
}

bool fetchPendingAnswer(const String& voice_url, const String& node_id,
                        GotchiUIState& ui, StateCb cb, const String& bearer_token,
                        const String& answer_id, AbortPollFn abort_poll,
                        InteractResult* out_result, const String& audio_out_mode) {
    String pending_url = buildPendingAnswerUrl(voice_url, node_id, answer_id);
    if (pending_url.length() == 0) return false;

    gotchi_audio::WavView empty_wav{nullptr, 0, 0.0f};
    bool ok = interact(
        empty_wav, pending_url, node_id, ui, cb, bearer_token,
        nullptr, abort_poll, out_result, audio_out_mode
    );

    // Acknowledge if completed or cancelled by user abort so it doesn't replay on next PTT
    if (ok || (out_result && *out_result == InteractResult::ABORT_CANCEL)) {
        ackPendingAnswer(voice_url, node_id, answer_id, bearer_token);
    }
    return ok;
}

} // namespace gotchi_voice
