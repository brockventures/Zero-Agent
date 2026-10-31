// ============================================================================
// GOTCHI V1 Edge Firmware — Seeed Studio XIAO ESP32S3 Sense (8MB Flash / 8MB PSRAM)
// Implements Epic 2 (T2.1-T2.7), Epic 3 (T3.1-T3.2) & PRD F13/F14/F19/F20/F21:
//   - Dual-transport JSON-RPC (`USB-C Serial 115200` + `POST /api/rpc` w/ Bearer auth)
//   - Strict separation: Top button (`BTN_TOP`) = speaking only; Bottom (`BTN_BOT`) = camera only
//   - Hybrid / Tap-to-Talk / Hold-to-Talk input with 120s PSRAM WAV cap & VAD silence stop
//   - Configurable 500ms circular pre-roll (`usb_host`/`usb_only` | `always` | `off`)
//   - Reply Cancel/Mute on `BTN_TOP` within 42ms (barge-in dropped per F13)
//   - Camera snapshot on `BTN_BOT`: short press on release stages 1 photo (or attaches to
//     in-progress recording per F14); long press (>=700ms) captures 5-photo burst over 3s (F20)
//   - Go Live mode (`live_mode`, default false): entered via `BTN_TOP` double-tap within
//     `double_tap_ms` (400ms) or whole-utterance `"go live"` transcript (F19/F21); ends on
//     `BTN_TOP` press or `live_silence_ms` (15s) silence
// ============================================================================

#include <Arduino.h>
#if defined(ARDUINO)
SET_LOOP_TASK_STACK_SIZE(16 * 1024);
#endif
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <ArduinoJson.h>
#include <driver/gpio.h>
#include <driver/i2s.h>
#include "esp_camera.h"
#include "pins.h"
#include "epd_panel.h"
#include "ui_state.h"
#include "audio_hal.h"
#include "camera_hal.h"
#include "onboarding_portal.h"
#include "voice_client.h"
#include "log_buffer.h"
#include "live_client.h"
#include "ble_framing.h"
#include "ble_transport.h"

static Preferences prefs;
static WebServer httpServer(80);

struct GotchiConfig {
    String hub_url = "http://gotchi.local:8765";
    // Voice gateway URL (supports https:// over 443 for Tailscale-free mobile roaming or http:// on LAN port 8765).
    String voice_url = "http://gotchi.local:8765/api/voice/interact";
    String live_url = "";             // Optional dedicated Go Live WS URL (falls back to voice_url when empty)
    String node_id = "gotchi-v1-01";
    String wake_mode = "ptt";         // "ptt" | "wake_word"
    String input_mode = "hybrid";     // "hybrid" | "tap" | "hold"
    String preroll_mode = "usb_host"; // "usb_host" (or "usb_only") | "always" | "off"
    String audio_out_mode = "speaker";// "speaker" | "discord_voice" | "ble_phone"
    String skin = "zero";             // "zero" | "gotchi"
    String bearer_token = "";         // Per-device Bearer token for off-LAN / tunnel auth (T3.2)
    String wifi_ssid = "";
    String wifi_psk = "";
    uint32_t ptt_max_ms = 120000;     // 120s max recording cap in 8MB PSRAM (3.84 MB)
    uint32_t ptt_min_ms = 250;        // 250ms minimum floor
    uint32_t ptt_min_rms_lsb = 50;    // 50 LSB minimum RMS energy
    uint32_t tap_latch_ms = 500;      // Press released < 500ms latches into hands-free Tap-to-Talk
    uint32_t double_tap_ms = 400;     // Second BTN_TOP tap within 400ms of first release -> Go Live (F19)
    uint32_t vad_silence_ms = 1500;   // 1.5s trailing silence ends Tap-to-Talk turn
    uint32_t vad_rms_lsb = 150;       // 150 LSB speech detection threshold for VAD silence timer
    uint32_t snap_window_ms = 3000;   // 3.0s debounce window to attach snapshot to next voice prompt
    uint32_t cam_long_press_ms = 700; // Holding BTN_BOT >= 700ms triggers 5-photo burst over 3s (F20)
    bool     live_mode = false;       // F19: Go Live mode off by default (configurable via set_config)
    bool     live_barge_in = false;   // T6.1: Go Live full-duplex AEC + speech barge-in off by default until bench delay calibration
    uint32_t live_silence_ms = 15000; // F19: 15s silence ends Go Live session
    bool     journal_mode = false;    // F24: Passive All-Day Journal mode off by default (configurable via set_config)
    bool     ble_disable_wifi = true; // F18/F24: Default true — turn off Wi-Fi radio whenever connected to phone over BLE
    uint32_t journal_seg_max_ms = 30000;   // F24: Max 30s per passive journal speech segment
    uint32_t journal_silence_ms = 2000;    // F24: 2.0s trailing silence closes a journal segment
    uint32_t journal_min_voiced_ms = 1000; // F24: Minimum 1.0s voiced duration to upload a journal segment
    String default_image_prompt = "Describe what you see in this snapshot briefly.";
} cfg;

static bool constantTimeEquals(const String& a, const String& b) {
    if (a.length() != b.length()) return false;
    uint8_t diff = 0;
    for (size_t i = 0; i < a.length(); ++i) {
        diff |= (uint8_t)(a[i] ^ b[i]);
    }
    return diff == 0;
}

static constexpr uint32_t SAMPLE_RATE_REC = 16000;
static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
static constexpr uint32_t BTN_DEBOUNCE_MS = 30;
static constexpr uint32_t VAD_INITIAL_TIMEOUT_MS = 5000; // Max wait for speech to start in tap mode
static constexpr size_t   MAX_STAGED_JPEG_BYTES = 128 * 1024;
static constexpr size_t   BURST_FRAME_COUNT = 5;
static constexpr uint32_t BURST_TOTAL_DURATION_MS = 3000; // 5 frames over 3.0s (0, 750, 1500, 2250, 3000ms)

// Staged camera snapshot / 5-frame burst state (BTN_BOT, allocated in PSRAM)
static uint8_t* s_staged_jpeg_bufs[BURST_FRAME_COUNT] = {nullptr};
static size_t   s_staged_jpeg_lens[BURST_FRAME_COUNT] = {0};
static size_t   s_staged_jpeg_count = 0;
static bool     s_staged_jpeg_active = false;
static uint32_t s_staged_jpeg_ts = 0;
static constexpr uint32_t NOTIFY_STATUS_TTL_MS = 60000;
static String   s_notify_status_text = "";
static uint32_t s_notify_expire_ts = 0;

static constexpr uint32_t PENDING_ANSWER_TTL_MS = 2UL * 3600UL * 1000UL; // 2 hours
static bool     s_pending_answer_ready = false;
static uint32_t s_pending_answer_expire_ts = 0;
static String   s_pending_answer_id = "";

static bool isPendingAnswerValid() {
    if (!s_pending_answer_ready) return false;
    if ((int32_t)(millis() - s_pending_answer_expire_ts) >= 0) {
        s_pending_answer_ready = false;
        s_pending_answer_id = "";
        return false;
    }
    return true;
}
static UiState s_last_drawn_voice_state = UiState::IDLE;
static void showIdle();
static bool     s_enter_live_pending = false;
static bool     s_wifi_disabled_for_ble_lp = false;
static uint32_t s_last_journal_sniff_ts    = 0;
static bool     s_btn_top_armed            = false;
static bool     s_btn_bot_armed            = false;

// Dynamically boosts CPU clock to 240 MHz during active voice, camera, live, or journal-upload
// bursts so JPEG DMA, base64 audio decoding, and NLMS AEC run at full speed, then drops back to
// 80 MHz as soon as the device returns to idle in BLE low-power mode.
struct ScopedCpuBoost {
    ScopedCpuBoost() {
        if (s_wifi_disabled_for_ble_lp) {
            setCpuFrequencyMhz(240);
        }
    }
    ~ScopedCpuBoost() {
        if (s_wifi_disabled_for_ble_lp) {
            setCpuFrequencyMhz(80);
        }
    }
};

// E-paper panel + refresh policy live in epd_panel.{h,cpp}.
EpdPanel panel;

// ============================================================================
// V1 Power & Battery Telemetry (3x 220k Multiplexed ADC Sense on D4 & D2)
//   - D4 (PIN_BTN_BOT_MOD / GPIO5 / ADC1_CH4): 220k to switched BAT+, 220k to GND
//   - D2 (PIN_BTN_TOP_PTT / GPIO3 / ADC1_CH2): single 220k series resistor to 5V (VBUS)
// Normal operation keeps INPUT_PULLUP (45k to 3.3V) active on both pins so even if
// 5V (VBUS) sits at 0V on battery, D2 idles at >= 2.74V and D4 at >= 2.82V (well above
// ESP32-S3 VIH_min = 0.75 * VDD = 2.475V). Once per second when idle, a 250us pulsed
// ADC read measures VBAT/2 on D4 (high-Z) and VBUS on D2 (against internal 45k INPUT_PULLDOWN).
// ============================================================================
static constexpr uint32_t POWER_TELEMETRY_INTERVAL_MS = 1000;
static uint32_t s_last_power_sample_ts = 0;
static uint16_t s_battery_mv = 0;
static uint16_t s_last_d4_mv = 0;
static uint16_t s_last_d2_mv = 0;
static int8_t   s_battery_pct = -1;
static bool     s_wall_brick_power = false;

// Piecewise-linear 1S LiPo state-of-charge curve (3300 mV = 0%, 4150 mV+ = 100%).
static int8_t lipoMvToPercent(uint16_t mv) {
    if (mv == 0) return -1;
    if (mv <= 3300) return 0;
    if (mv >= 4150) return 100;
    if (mv < 3500)  return (int8_t)(((uint32_t)(mv - 3300) * 10) / 200);          // 3.30V..3.50V -> 0..10%
    if (mv < 3680)  return (int8_t)(10 + ((uint32_t)(mv - 3500) * 20) / 180);     // 3.50V..3.68V -> 10..30%
    if (mv < 3800)  return (int8_t)(30 + ((uint32_t)(mv - 3680) * 30) / 120);     // 3.68V..3.80V -> 30..60%
    if (mv < 3950)  return (int8_t)(60 + ((uint32_t)(mv - 3800) * 25) / 150);     // 3.80V..3.95V -> 60..85%
    return (int8_t)(85 + ((uint32_t)(mv - 3950) * 15) / 200);                     // 3.95V..4.15V -> 85..100%
}

static bool isUsbDataHostConnected() {
    return (bool)Serial;
}

static void samplePowerAndBatteryTelemetry(bool force = false) {
    uint32_t now = millis();
    if (!force && s_last_power_sample_ts != 0 && (now - s_last_power_sample_ts < POWER_TELEMETRY_INTERVAL_MS)) {
        return;
    }
    // Only sample when neither tactile button is actively held LOW.
    if (digitalRead(PIN_BTN_TOP_PTT) == LOW || digitalRead(PIN_BTN_BOT_MOD) == LOW) {
        return;
    }
    s_last_power_sample_ts = now;

    // 1. Sample VBAT/2 divider on D4 (PIN_BTN_BOT_MOD):
    //    2000us pull-down discharges pin/amp cap when switch is OFF; 400us high-Z settles at VBAT/2 (~1650..2100 mV) when ON.
    pinMode(PIN_BTN_BOT_MOD, INPUT_PULLDOWN);
    delayMicroseconds(2000);
    pinMode(PIN_BTN_BOT_MOD, INPUT);
    delayMicroseconds(400);
    uint32_t vbat_div_mv = analogReadMilliVolts(PIN_BTN_BOT_MOD);
    pinMode(PIN_BTN_BOT_MOD, INPUT_PULLUP);
    s_last_d4_mv = (uint16_t)vbat_div_mv;

    if (vbat_div_mv >= PIN_VBAT_DIV_MIN_MV && vbat_div_mv <= PIN_VBAT_DIV_MAX_MV) {
        uint16_t raw_mv = (uint16_t)(vbat_div_mv * 2);
        // 4:1 EMA smoothing to prevent percentage flicker
        s_battery_mv = (s_battery_mv == 0) ? raw_mv : (uint16_t)((s_battery_mv * 3 + raw_mv) / 4);
        s_battery_pct = lipoMvToPercent(s_battery_mv);
    } else if (vbat_div_mv < PIN_VBAT_DIV_MIN_MV) {
        s_battery_mv = 0;
        s_battery_pct = -1;
    }

    // 2. Sample 5V VBUS via single 220k series resistor on D2 (PIN_BTN_TOP_PTT) against internal 45k INPUT_PULLDOWN:
    //    Reads ~849 mV (>= 650 mV) when 5V wall brick or USB host is present, < 550 mV on battery or unwired.
    pinMode(PIN_BTN_TOP_PTT, INPUT_PULLDOWN);
    delayMicroseconds(1000);
    uint32_t vbus_div_mv = analogReadMilliVolts(PIN_BTN_TOP_PTT);
    pinMode(PIN_BTN_TOP_PTT, INPUT_PULLUP);
    s_last_d2_mv = (uint16_t)vbus_div_mv;

    s_wall_brick_power = (vbus_div_mv >= PIN_VBUS_WALL_MIN_MV);
}

static bool isWallBrickPowerConnected() {
    return s_wall_brick_power;
}

static bool isExternalPowerConnected() {
    return isUsbDataHostConnected() || isWallBrickPowerConnected();
}

static void attachPowerToUi(GotchiUIState& ui) {
    ui.battery_pct = s_battery_pct;
    ui.battery_mv  = s_battery_mv;
    ui.wall_power  = isExternalPowerConnected();
}

static void applyPrerollPolicy() {
    if (cfg.preroll_mode == "always") {
        gotchi_audio::setPrerollEnabled(true);
    } else if (cfg.preroll_mode == "off") {
        gotchi_audio::setPrerollEnabled(false);
    } else {
        // "usb_host" or "usb_only": active on either USB data host OR 5V USB-C wall brick
        gotchi_audio::setPrerollEnabled(isExternalPowerConnected());
    }
}

// Normalizes a transcript (lowercase, strips punctuation/whitespace) and returns
// true iff the entire utterance is "go live" (PRD F21).
static bool isWholeUtteranceGoLive(const String& transcript) {
    String cleaned;
    cleaned.reserve(transcript.length());
    bool last_space = true;
    for (size_t i = 0; i < transcript.length(); ++i) {
        char c = transcript.charAt(i);
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
            cleaned += c;
            last_space = false;
        } else if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            if (!last_space) {
                cleaned += ' ';
                last_space = true;
            }
        }
    }
    cleaned.trim();
    return cleaned == "go live";
}

static void clearStagedSnapshots() {
    s_staged_jpeg_active = false;
    s_staged_jpeg_count = 0;
    for (size_t i = 0; i < BURST_FRAME_COUNT; ++i) {
        s_staged_jpeg_lens[i] = 0;
    }
}

void loadConfig() {
    prefs.begin("gotchi", true);
    cfg.hub_url              = prefs.getString("hub_url", cfg.hub_url);
    cfg.voice_url            = prefs.getString("voice_url", cfg.voice_url);
    cfg.live_url             = prefs.getString("live_url", cfg.live_url);
    cfg.node_id              = prefs.getString("node_id", cfg.node_id);
    cfg.wake_mode            = prefs.getString("wake_mode", cfg.wake_mode);
    cfg.input_mode           = prefs.getString("input_mode", cfg.input_mode);
    cfg.preroll_mode         = prefs.getString("preroll_mode", cfg.preroll_mode);
    cfg.audio_out_mode       = prefs.getString("audio_out_mode", cfg.audio_out_mode);
    if (cfg.audio_out_mode != "speaker" && cfg.audio_out_mode != "discord_voice" && cfg.audio_out_mode != "ble_phone") {
        cfg.audio_out_mode = "speaker";
    }
    cfg.skin                 = prefs.getString("skin", cfg.skin);
    cfg.bearer_token         = prefs.getString("bearer_token", "");
    cfg.wifi_ssid            = prefs.getString("wifi_ssid", "");
    cfg.wifi_ssid.trim();
    cfg.wifi_psk             = prefs.getString("wifi_psk", "");
    cfg.ptt_max_ms           = prefs.getUInt("ptt_max_ms", cfg.ptt_max_ms);
    cfg.ptt_min_ms           = prefs.getUInt("ptt_min_ms", cfg.ptt_min_ms);
    cfg.ptt_min_rms_lsb      = prefs.getUInt("ptt_min_rms", cfg.ptt_min_rms_lsb);
    cfg.tap_latch_ms         = prefs.getUInt("tap_latch_ms", cfg.tap_latch_ms);
    cfg.double_tap_ms        = prefs.getUInt("dbl_tap_ms", cfg.double_tap_ms);
    cfg.vad_silence_ms       = prefs.getUInt("vad_sil_ms", cfg.vad_silence_ms);
    cfg.vad_rms_lsb          = prefs.getUInt("vad_rms_lsb", cfg.vad_rms_lsb);
    cfg.snap_window_ms       = prefs.getUInt("snap_win_ms", cfg.snap_window_ms);
    cfg.cam_long_press_ms    = prefs.getUInt("cam_long_ms", cfg.cam_long_press_ms);
    cfg.live_mode            = prefs.getBool("live_mode", cfg.live_mode);
    cfg.live_barge_in        = prefs.getBool("live_barge_in", cfg.live_barge_in);
    cfg.live_silence_ms      = prefs.getUInt("live_sil_ms", cfg.live_silence_ms);
    cfg.journal_mode         = prefs.getBool("journal_mode", cfg.journal_mode);
    cfg.ble_disable_wifi     = prefs.getBool("ble_no_wifi", cfg.ble_disable_wifi);
    cfg.journal_seg_max_ms   = prefs.getUInt("jrn_seg_ms", cfg.journal_seg_max_ms);
    cfg.journal_silence_ms   = prefs.getUInt("jrn_sil_ms", cfg.journal_silence_ms);
    cfg.journal_min_voiced_ms= prefs.getUInt("jrn_min_ms", cfg.journal_min_voiced_ms);
    cfg.default_image_prompt = prefs.getString("img_prompt", cfg.default_image_prompt);
    prefs.end();
    if (cfg.ptt_max_ms > 120000) cfg.ptt_max_ms = 120000;
    if (cfg.ptt_max_ms < 1000)   cfg.ptt_max_ms = 1000;
}

void saveConfig() {
    prefs.begin("gotchi", false);
    prefs.putString("hub_url", cfg.hub_url);
    prefs.putString("voice_url", cfg.voice_url);
    prefs.putString("live_url", cfg.live_url);
    prefs.putString("node_id", cfg.node_id);
    prefs.putString("wake_mode", cfg.wake_mode);
    prefs.putString("input_mode", cfg.input_mode);
    prefs.putString("preroll_mode", cfg.preroll_mode);
    prefs.putString("audio_out_mode", cfg.audio_out_mode);
    prefs.putString("skin", cfg.skin);
    prefs.putString("bearer_token", cfg.bearer_token);
    prefs.putString("wifi_ssid", cfg.wifi_ssid);
    prefs.putString("wifi_psk", cfg.wifi_psk);
    prefs.putUInt("ptt_max_ms", cfg.ptt_max_ms);
    prefs.putUInt("ptt_min_ms", cfg.ptt_min_ms);
    prefs.putUInt("ptt_min_rms", cfg.ptt_min_rms_lsb);
    prefs.putUInt("tap_latch_ms", cfg.tap_latch_ms);
    prefs.putUInt("dbl_tap_ms", cfg.double_tap_ms);
    prefs.putUInt("vad_sil_ms", cfg.vad_silence_ms);
    prefs.putUInt("vad_rms_lsb", cfg.vad_rms_lsb);
    prefs.putUInt("snap_win_ms", cfg.snap_window_ms);
    prefs.putUInt("cam_long_ms", cfg.cam_long_press_ms);
    prefs.putBool("live_mode", cfg.live_mode);
    prefs.putBool("live_barge_in", cfg.live_barge_in);
    prefs.putUInt("live_sil_ms", cfg.live_silence_ms);
    prefs.putBool("journal_mode", cfg.journal_mode);
    prefs.putBool("ble_no_wifi", cfg.ble_disable_wifi);
    prefs.putUInt("jrn_seg_ms", cfg.journal_seg_max_ms);
    prefs.putUInt("jrn_sil_ms", cfg.journal_silence_ms);
    prefs.putUInt("jrn_min_ms", cfg.journal_min_voiced_ms);
    prefs.putString("img_prompt", cfg.default_image_prompt);
    prefs.end();
    applyPrerollPolicy();
}

String handleJsonRpc(const String& jsonLine) {
    JsonDocument req;
    JsonDocument res;
    if (deserializeJson(req, jsonLine)) {
        res["ok"] = false;
        res["error"] = "invalid_json";
        String out;
        serializeJson(res, out);
        return out;
    }
    String cmd = req["cmd"] | "";
    res["cmd"] = cmd;
    res["ok"] = true;

    if (cmd == "get_config") {
        res["hub_url"] = cfg.hub_url;
        res["voice_url"] = cfg.voice_url;
        res["live_url"] = cfg.live_url;
        res["node_id"] = cfg.node_id;
        res["wake_mode"] = cfg.wake_mode;
        res["input_mode"] = cfg.input_mode;
        res["preroll_mode"] = cfg.preroll_mode;
        res["audio_out_mode"] = cfg.audio_out_mode;
        res["skin"] = cfg.skin;
        res["bearer_token_set"] = (cfg.bearer_token.length() > 0);
        res["wifi_ssid"] = cfg.wifi_ssid;
        res["ptt_max_ms"] = cfg.ptt_max_ms;
        res["ptt_min_ms"] = cfg.ptt_min_ms;
        res["ptt_min_rms_lsb"] = cfg.ptt_min_rms_lsb;
        res["tap_latch_ms"] = cfg.tap_latch_ms;
        res["double_tap_ms"] = cfg.double_tap_ms;
        res["vad_silence_ms"] = cfg.vad_silence_ms;
        res["vad_rms_lsb"] = cfg.vad_rms_lsb;
        res["snap_window_ms"] = cfg.snap_window_ms;
        res["cam_long_press_ms"] = cfg.cam_long_press_ms;
        res["live_mode"] = cfg.live_mode;
        res["live_barge_in"] = cfg.live_barge_in;
        res["live_silence_ms"] = cfg.live_silence_ms;
        res["journal_mode"] = cfg.journal_mode;
        res["ble_disable_wifi"] = cfg.ble_disable_wifi;
        res["journal_seg_max_ms"] = cfg.journal_seg_max_ms;
        res["journal_silence_ms"] = cfg.journal_silence_ms;
        res["journal_min_voiced_ms"] = cfg.journal_min_voiced_ms;
        res["default_image_prompt"] = cfg.default_image_prompt;
        samplePowerAndBatteryTelemetry(true);
        res["usb_host_connected"] = isUsbDataHostConnected();
        res["wall_power_connected"] = isWallBrickPowerConnected();
        res["external_power_connected"] = isExternalPowerConnected();
        res["battery_mv"] = s_battery_mv;
        res["battery_pct"] = (int)s_battery_pct;
        res["d4_mv"] = s_last_d4_mv;
        res["d2_mv"] = s_last_d2_mv;
        res["ble_connected"] = gotchi_ble::isBlePeerConnected();
        res["wifi_connected"] = (WiFi.status() == WL_CONNECTED);
        res["wifi_rssi_dbm"] = (WiFi.status() == WL_CONNECTED) ? (int)WiFi.RSSI() : 0;
        res["psram_free"] = ESP.getFreePsram();
        res["fw_version"] = GOTCHI_FW_VERSION;
    } else if (cmd == "set_config") {
        if (req["audio_out_mode"].is<const char*>()) {
            String m = req["audio_out_mode"].as<String>();
            if (m != "speaker" && m != "discord_voice" && m != "ble_phone") {
                res["ok"] = false;
                res["error"] = "invalid_audio_out_mode";
                String out;
                serializeJson(res, out);
                return out;
            }
            cfg.audio_out_mode = m;
        }
        if (req["hub_url"].is<const char*>()) cfg.hub_url = req["hub_url"].as<String>();
        if (req["voice_url"].is<const char*>()) cfg.voice_url = req["voice_url"].as<String>();
        if (req["live_url"].is<const char*>()) cfg.live_url = req["live_url"].as<String>();
        if (req["node_id"].is<const char*>()) {
            String nid = req["node_id"].as<String>();
            bool valid_nid = (nid.length() >= 1 && nid.length() <= 64);
            for (size_t i = 0; valid_nid && i < nid.length(); ++i) {
                char c = nid.charAt(i);
                bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                          (c >= '0' && c <= '9') || c == '-' || c == '_';
                if (!ok) valid_nid = false;
            }
            if (!valid_nid) {
                res["ok"] = false;
                res["error"] = "invalid_node_id";
                String out;
                serializeJson(res, out);
                return out;
            }
            cfg.node_id = nid;
        }
        if (req["wake_mode"].is<const char*>()) cfg.wake_mode = req["wake_mode"].as<String>();
        if (req["input_mode"].is<const char*>()) cfg.input_mode = req["input_mode"].as<String>();
        if (req["preroll_mode"].is<const char*>()) cfg.preroll_mode = req["preroll_mode"].as<String>();
        if (req["skin"].is<const char*>()) cfg.skin = req["skin"].as<String>();
        if (req["bearer_token"].is<const char*>()) cfg.bearer_token = req["bearer_token"].as<String>();
        if (req["ptt_max_ms"].is<uint32_t>()) {
            uint32_t v = req["ptt_max_ms"].as<uint32_t>();
            cfg.ptt_max_ms = v > 120000 ? 120000 : (v < 1000 ? 1000 : v);
        }
        if (req["ptt_min_ms"].is<uint32_t>()) cfg.ptt_min_ms = req["ptt_min_ms"].as<uint32_t>();
        if (req["ptt_min_rms_lsb"].is<uint32_t>()) cfg.ptt_min_rms_lsb = req["ptt_min_rms_lsb"].as<uint32_t>();
        if (req["tap_latch_ms"].is<uint32_t>()) cfg.tap_latch_ms = req["tap_latch_ms"].as<uint32_t>();
        if (req["double_tap_ms"].is<uint32_t>()) cfg.double_tap_ms = req["double_tap_ms"].as<uint32_t>();
        if (req["vad_silence_ms"].is<uint32_t>()) cfg.vad_silence_ms = req["vad_silence_ms"].as<uint32_t>();
        if (req["vad_rms_lsb"].is<uint32_t>()) cfg.vad_rms_lsb = req["vad_rms_lsb"].as<uint32_t>();
        if (req["snap_window_ms"].is<uint32_t>()) cfg.snap_window_ms = req["snap_window_ms"].as<uint32_t>();
        if (req["cam_long_press_ms"].is<uint32_t>()) cfg.cam_long_press_ms = req["cam_long_press_ms"].as<uint32_t>();
        if (req["live_mode"].is<bool>()) cfg.live_mode = req["live_mode"].as<bool>();
        if (req["live_barge_in"].is<bool>()) cfg.live_barge_in = req["live_barge_in"].as<bool>();
        if (req["live_silence_ms"].is<uint32_t>()) cfg.live_silence_ms = req["live_silence_ms"].as<uint32_t>();
        if (req["journal_mode"].is<bool>()) cfg.journal_mode = req["journal_mode"].as<bool>();
        if (req["ble_disable_wifi"].is<bool>()) cfg.ble_disable_wifi = req["ble_disable_wifi"].as<bool>();
        if (req["journal_seg_max_ms"].is<uint32_t>()) cfg.journal_seg_max_ms = req["journal_seg_max_ms"].as<uint32_t>();
        if (req["journal_silence_ms"].is<uint32_t>()) cfg.journal_silence_ms = req["journal_silence_ms"].as<uint32_t>();
        if (req["journal_min_voiced_ms"].is<uint32_t>()) cfg.journal_min_voiced_ms = req["journal_min_voiced_ms"].as<uint32_t>();
        if (req["default_image_prompt"].is<const char*>()) {
            cfg.default_image_prompt = req["default_image_prompt"].as<String>();
        }
        saveConfig();
    } else if (cmd == "set_wifi") {
        cfg.wifi_ssid = req["ssid"] | cfg.wifi_ssid;
        cfg.wifi_ssid.trim();
        cfg.wifi_psk  = req["psk"]  | cfg.wifi_psk;
        saveConfig();
    } else if (cmd == "self_test") {
        bool psram_ok = (psramFound() && ESP.getFreePsram() > 2000000);
        bool epd_ok = epdWaitBusy(500);
        int top_btn = digitalRead(PIN_BTN_TOP_PTT);
        int bot_btn = digitalRead(PIN_BTN_BOT_MOD);
        samplePowerAndBatteryTelemetry(true);
        sensor_t* s = esp_camera_sensor_get();
        uint16_t cam_pid = s ? s->id.PID : 0;

        // Baseline idle 2.4 GHz Wi-Fi RSSI before energizing Class-D amp + camera DVP bus
        bool wifi_conn = (WiFi.status() == WL_CONNECTED);
        int rssi_idle_dbm = wifi_conn ? (int)WiFi.RSSI() : 0;
        if (s_wifi_disabled_for_ble_lp) setCpuFrequencyMhz(240);

        // Drive MAX98357A Class-D speaker chime while capturing a test JPEG frame and
        // sampling active Wi-Fi RSSI to detect near-field coupling or Class-D EMI desense.
        const uint32_t sr = 24000;
        const size_t tone_samples = sr / 12; // 83 ms 880 Hz test note
        static int16_t tone_buf[2000];
        for (size_t i = 0; i < tone_samples; ++i) {
            float env = 1.0f - ((float)i / (float)tone_samples);
            tone_buf[i] = (int16_t)(sinf(2.0f * 3.14159265f * 880.0f * ((float)i / sr)) * 4800.0f * env);
        }
        gotchi_audio::playPcmChunk(tone_buf, tone_samples, sr);
        camera_fb_t* fb = gotchi_camera::captureJpeg();
        size_t cam_frame_bytes = (fb && fb->buf) ? fb->len : 0;
        int rssi_active_dbm = wifi_conn ? (int)WiFi.RSSI() : 0;
        if (fb) {
            esp_camera_fb_return(fb);
        }
        gotchi_camera::standby();
        gotchi_audio::muteSpeaker();
        if (s_wifi_disabled_for_ble_lp) setCpuFrequencyMhz(80);

        int rssi_drop_db = wifi_conn ? (rssi_idle_dbm - rssi_active_dbm) : 0;
        bool rf_desense_warn = wifi_conn && (rssi_drop_db > 2);

        res["psram_ok"] = psram_ok;
        res["epd_busy_clear"] = epd_ok;
        res["btn_top_state"] = top_btn;
        res["btn_bot_state"] = bot_btn;
        res["battery_mv"] = s_battery_mv;
        res["battery_pct"] = (int)s_battery_pct;
        res["wall_power_connected"] = isWallBrickPowerConnected();
        res["usb_host_connected"] = isUsbDataHostConnected();
        res["camera_pid"] = cam_pid;
        res["camera_frame_bytes"] = (uint32_t)cam_frame_bytes;
        res["wifi_connected"] = wifi_conn;
        res["wifi_rssi_dbm"] = rssi_idle_dbm;
        res["wifi_rssi_idle_dbm"] = rssi_idle_dbm;
        res["wifi_rssi_active_dbm"] = rssi_active_dbm;
        res["wifi_rssi_drop_db"] = rssi_drop_db;
        res["rf_desense_warn"] = rf_desense_warn;
        gotchi_log::RingLogger.info("self_test", psram_ok && epd_ok && cam_pid != 0 ? "Self-test passed" : "Self-test detected hardware faults");
    } else if (cmd == "factory_reset") {
        prefs.begin("gotchi", false);
        prefs.clear();
        prefs.end();
        res["status"] = "cleared_nvs";
    } else if (cmd == "draw_state") {
        if (panel.isDrawing()) {
            res["ok"] = false;
            res["error"] = "epd_busy";
            String out;
            serializeJson(res, out);
            return out;
        }
        GotchiUIState ui;
        attachPowerToUi(ui);
        ui.skin = req["skin"].is<const char*>() ? req["skin"].as<String>() : cfg.skin;
        ui.state = uiStateFromString(req["state"] | "idle");
        if (req["status_text"].is<const char*>()) ui.status_text = req["status_text"].as<String>();
        if (req["battery_pct"].is<int>()) ui.battery_pct = (int8_t)req["battery_pct"].as<int>();
        if (req["wall_power"].is<bool>()) ui.wall_power = req["wall_power"].as<bool>();
        bool force_full = req["force_full"] | false;
        uint32_t t_draw0 = millis();
        bool ok = panel.draw(ui, force_full);
        uint32_t draw_ms = millis() - t_draw0;
        res["ok"] = ok;
        res["mode"] = panel.lastMode();
        res["draw_ms"] = draw_ms;
        res["busy_high_ms"] = panel.lastBusyHighMs();
        res["busy_pin"] = digitalRead(PIN_EPD_BUSY);
        if (!ok) {
            gotchi_log::RingLogger.error("epd", "E-paper draw timed out (BUSY stuck)");
        }
    } else if (cmd == "get_logs") {
        size_t limit = 50;
        if (req["limit"].is<size_t>()) limit = req["limit"].as<size_t>();
        else if (req["lines"].is<size_t>()) limit = req["lines"].as<size_t>();

        const char* tag_filter = req["tag"].is<const char*>() ? req["tag"].as<const char*>() : nullptr;
        const char* lvl_str = req["level"].is<const char*>() ? req["level"].as<const char*>() : nullptr;
        gotchi_log::Level min_lvl = gotchi_log::Level::DEBUG;
        if (lvl_str) {
            if (strcasecmp(lvl_str, "ERROR") == 0) min_lvl = gotchi_log::Level::ERROR;
            else if (strcasecmp(lvl_str, "WARN") == 0 || strcasecmp(lvl_str, "WARNING") == 0) min_lvl = gotchi_log::Level::WARN;
            else if (strcasecmp(lvl_str, "INFO") == 0) min_lvl = gotchi_log::Level::INFO;
        }

        res["total_logged"] = gotchi_log::RingLogger.totalLogged();
        res["count"] = gotchi_log::RingLogger.count();
        res["capacity"] = gotchi_log::RingLogger.capacity();
        JsonArray logsArr = res["logs"].to<JsonArray>();
        gotchi_log::RingLogger.dumpJson(logsArr, limit, tag_filter, min_lvl);
    } else if (cmd == "clear_logs") {
        gotchi_log::RingLogger.clear();
        res["status"] = "cleared";
        gotchi_log::RingLogger.info("sys", "Logs cleared via RPC");
    } else if (cmd == "diag_epd") {
        // 1. Check BUSY pin idle state (pullup vs pulldown vs floating)
        pinMode(PIN_EPD_BUSY, INPUT_PULLUP);
        delayMicroseconds(100);
        int busy_pu = digitalRead(PIN_EPD_BUSY);
        pinMode(PIN_EPD_BUSY, INPUT_PULLDOWN);
        delayMicroseconds(100);
        int busy_pd = digitalRead(PIN_EPD_BUSY);
        pinMode(PIN_EPD_BUSY, INPUT);
        delayMicroseconds(100);
        int busy_in = digitalRead(PIN_EPD_BUSY);
        res["busy_pullup"] = busy_pu;
        res["busy_pulldown"] = busy_pd;
        res["busy_floating"] = (busy_pu == 1 && busy_pd == 0);
        res["busy_idle"] = busy_in;

        // 2. Test hardware reset pulse (RST = GPIO1) and poll BUSY (GPIO6) every 100us for 50ms
        pinMode(PIN_EPD_RST, OUTPUT);
        digitalWrite(PIN_EPD_RST, HIGH);
        delay(20);
        digitalWrite(PIN_EPD_RST, LOW);
        int busy_during_rst = digitalRead(PIN_EPD_BUSY);
        delay(10);
        digitalWrite(PIN_EPD_RST, HIGH);
        int busy_high_us = -1;
        uint32_t t_rst = micros();
        while (micros() - t_rst < 50000) {
            if (digitalRead(PIN_EPD_BUSY) == HIGH) {
                busy_high_us = (int)(micros() - t_rst);
                break;
            }
            delayMicroseconds(50);
        }
        res["busy_during_rst_low"] = busy_during_rst;
        res["busy_after_rst_high_us"] = busy_high_us;

        // 3. Test 3-wire SPI read of SSD1681 Register 0x2F (Status Bit) and 0x2E (User ID) via bit-bang on SCK=7, MOSI=9, CS=2, DC=4
        pinMode(PIN_EPD_CS, OUTPUT);
        pinMode(PIN_EPD_DC, OUTPUT);
        pinMode(PIN_EPD_SCK, OUTPUT);
        pinMode(PIN_EPD_MOSI, OUTPUT);
        digitalWrite(PIN_EPD_CS, HIGH);
        digitalWrite(PIN_EPD_SCK, LOW);

        auto bbWriteCmd = [](uint8_t c) {
            pinMode(PIN_EPD_MOSI, OUTPUT);
            digitalWrite(PIN_EPD_DC, LOW);
            digitalWrite(PIN_EPD_CS, LOW);
            for (int i = 7; i >= 0; --i) {
                digitalWrite(PIN_EPD_SCK, LOW);
                digitalWrite(PIN_EPD_MOSI, (c >> i) & 1);
                delayMicroseconds(2);
                digitalWrite(PIN_EPD_SCK, HIGH);
                delayMicroseconds(2);
            }
            digitalWrite(PIN_EPD_SCK, LOW);
            digitalWrite(PIN_EPD_CS, HIGH);
            digitalWrite(PIN_EPD_DC, HIGH);
        };
        auto bbReadReg = [](uint8_t reg) -> uint8_t {
            pinMode(PIN_EPD_MOSI, OUTPUT);
            digitalWrite(PIN_EPD_DC, LOW);
            digitalWrite(PIN_EPD_CS, LOW);
            for (int i = 7; i >= 0; --i) {
                digitalWrite(PIN_EPD_SCK, LOW);
                digitalWrite(PIN_EPD_MOSI, (reg >> i) & 1);
                delayMicroseconds(2);
                digitalWrite(PIN_EPD_SCK, HIGH);
                delayMicroseconds(2);
            }
            digitalWrite(PIN_EPD_SCK, LOW);
            digitalWrite(PIN_EPD_DC, HIGH);
            pinMode(PIN_EPD_MOSI, INPUT_PULLUP);
            delayMicroseconds(5);
            uint8_t val = 0;
            for (int i = 7; i >= 0; --i) {
                digitalWrite(PIN_EPD_SCK, HIGH);
                delayMicroseconds(2);
                val = (val << 1) | (digitalRead(PIN_EPD_MOSI) & 1);
                digitalWrite(PIN_EPD_SCK, LOW);
                delayMicroseconds(2);
            }
            digitalWrite(PIN_EPD_CS, HIGH);
            pinMode(PIN_EPD_MOSI, OUTPUT);
            return val;
        };

        // Send SW Reset (0x12) via bit-bang and check if BUSY goes HIGH!
        bbWriteCmd(0x12);
        int busy_sw_rst_us = -1;
        uint32_t t_sw = micros();
        while (micros() - t_sw < 50000) {
            if (digitalRead(PIN_EPD_BUSY) == HIGH) {
                busy_sw_rst_us = (int)(micros() - t_sw);
                break;
            }
            delayMicroseconds(50);
        }
        delay(20);
        uint8_t reg_2f_before = bbReadReg(0x2F);
        uint8_t reg_2d = bbReadReg(0x2D);
        res["busy_after_sw_rst_us"] = busy_sw_rst_us;
        res["reg_0x2f_before"] = reg_2f_before;
        res["reg_0x2d"] = reg_2d;

        // Re-init panel and draw static test pattern
        panel.begin();
        GotchiUIState test_ui;
        attachPowerToUi(test_ui);
        test_ui.skin = "zero";
        test_ui.state = UiState::SPEAKING;
        test_ui.reply = "STATIC TEST PATTERN OK!";
        test_ui.status_text = "BENCH DISPLAY CHECK";
        uint32_t td0 = millis();
        bool dok = panel.draw(test_ui, true);
        res["draw_ms"] = millis() - td0;
        res["busy_rise_us"] = panel.lastBusyRiseUs();
        res["busy_high_ms"] = panel.lastBusyHighMs();
        res["reg_0x2f_after"] = bbReadReg(0x2F);
        res["draw_ok"] = dok;
    } else if (cmd == "notify") {
        if (s_last_drawn_voice_state != UiState::IDLE) {
            res["ok"] = false;
            res["error"] = "busy";
            String out;
            serializeJson(res, out);
            return out;
        }
        String chime = req["chime"].is<const char*>() ? req["chime"].as<String>() : String("ready");
        if (!gotchi_audio::isValidChimeKind(chime.c_str())) {
            res["ok"] = false;
            res["error"] = "invalid_chime";
            String out;
            serializeJson(res, out);
            return out;
        }
        String msg = req["status_text"] | (req["message"] | "BUILD READY");
        msg.trim();
        if (msg.length() > 22) msg = msg.substring(0, 22);
        s_notify_status_text = msg;
        s_notify_expire_ts = millis() + NOTIFY_STATUS_TTL_MS;
        if (req["notify_id"].is<const char*>()) {
            res["notify_id"] = req["notify_id"].as<String>();
        }
        res["status_text"] = s_notify_status_text;

        bool has_pending_audio = req["has_pending_audio"] | false;
        String answer_id = req["answer_id"].is<const char*>() ? req["answer_id"].as<String>() : String("");
        if (has_pending_audio || answer_id.length() > 0) {
            s_pending_answer_ready = true;
            s_pending_answer_expire_ts = millis() + PENDING_ANSWER_TTL_MS;
            s_pending_answer_id = answer_id;
            res["has_pending_audio"] = true;
            if (answer_id.length() > 0) {
                res["answer_id"] = answer_id;
            }
            if (chime.length() == 0) {
                chime = "ready";
            }
        }
        res["chime"] = chime;

        if (s_wifi_disabled_for_ble_lp) setCpuFrequencyMhz(240);
        if (chime.length() > 0) {
            gotchi_audio::playChime(chime.c_str());
        }
        showIdle();
        if (s_wifi_disabled_for_ble_lp) setCpuFrequencyMhz(80);
    } else if (cmd == "led_test") {
        String g21 = req["gpio21"] | "high";
        gpio_hold_dis(GPIO_NUM_21);
        if (g21 == "low") {
            pinMode(21, OUTPUT);
            digitalWrite(21, LOW);
            gpio_hold_en(GPIO_NUM_21);
        } else if (g21 == "input") {
            pinMode(21, INPUT);
        } else {
            pinMode(21, OUTPUT);
            digitalWrite(21, HIGH);
            gpio_hold_en(GPIO_NUM_21);
        }
        uint32_t d2_float_ms = req["d2_float_ms"] | 0;
        if (d2_float_ms > 0) {
            if (d2_float_ms > 15000) d2_float_ms = 15000;
            pinMode(PIN_BTN_TOP_PTT, INPUT_PULLDOWN);
            delay(d2_float_ms);
            pinMode(PIN_BTN_TOP_PTT, INPUT_PULLUP);
            s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        }
        res["gpio21"] = g21;
        res["d2_float_ms"] = d2_float_ms;
    } else if (cmd == "reboot") {
        ESP.restart();
    } else {
        res["ok"] = false;
        res["error"] = "unknown_command";
    }

    String out;
    serializeJson(res, out);
    return out;
}

static bool btnStable(uint8_t pin, bool pressed) {
    const int want = pressed ? LOW : HIGH;
    if (digitalRead(pin) != want) return false;
    delay(BTN_DEBOUNCE_MS);
    return digitalRead(pin) == want;
}

// Edge-arming guards so a button held down at startup (or physically stuck LOW)
// never triggers an infinite PTT, camera burst, or self-cancel loop until it has
// first been observed stably HIGH (released).
static void updateButtonArming() {
    if (!s_btn_top_armed && btnStable(PIN_BTN_TOP_PTT, false)) {
        s_btn_top_armed = true;
    }
    if (!s_btn_bot_armed && btnStable(PIN_BTN_BOT_MOD, false)) {
        s_btn_bot_armed = true;
    }
}

static void waitForButtonRelease(uint8_t pin, uint32_t max_wait_ms = 2000) {
    uint32_t t0 = millis();
    while (digitalRead(pin) == LOW && (millis() - t0 < max_wait_ms)) {
        delay(5);
    }
}


static void showIdle() {
    s_last_drawn_voice_state = UiState::IDLE;
    samplePowerAndBatteryTelemetry();
    GotchiUIState idle_ui;
    attachPowerToUi(idle_ui);
    idle_ui.skin = cfg.skin;
    if (s_notify_status_text.length() > 0 && (int32_t)(millis() - s_notify_expire_ts) >= 0) {
        s_notify_status_text = "";
    }
    if (s_staged_jpeg_active && s_staged_jpeg_count > 0) {
        idle_ui.status_text = s_staged_jpeg_count > 1
            ? (String("BURST (") + String(s_staged_jpeg_count) + ") STAGED")
            : "SNAP STAGED";
    } else if (s_notify_status_text.length() > 0) {
        idle_ui.status_text = s_notify_status_text;
    }
    panel.draw(idle_ui, false);
}

static void onVoiceState(const GotchiUIState& ui) {
    GotchiUIState enriched = ui;
    if (enriched.state == UiState::TRANSCRIBING || enriched.state == UiState::SYNTHESIZING) {
        enriched.state = UiState::THINKING;
    }
    if (enriched.skin == "zero" && enriched.state == s_last_drawn_voice_state) {
        return;
    }
    s_last_drawn_voice_state = enriched.state;
    if (enriched.battery_pct < 0 && s_battery_pct >= 0) {
        attachPowerToUi(enriched);
    }
    panel.draw(enriched);
}

static void pollRpcAndHttp() {
    httpServer.handleClient();
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();
        if (line.length() > 0) {
            Serial.println(handleJsonRpc(line));
        }
    }
}

// Polled every ~42ms during TTS playback and SSE wait (PRD F13).
// BTN_TOP cancels/mutes the reply and returns to IDLE (barge-in is dropped).
// BTN_BOT is camera-only and never aborts voice playback.
static gotchi_voice::InteractResult pollVoiceAbort() {
    if (s_last_drawn_voice_state != UiState::SPEAKING) pollRpcAndHttp();
    if (!s_btn_top_armed && digitalRead(PIN_BTN_TOP_PTT) == HIGH) {
        s_btn_top_armed = true;
    }
    if (s_btn_top_armed && digitalRead(PIN_BTN_TOP_PTT) == LOW && btnStable(PIN_BTN_TOP_PTT, true)) {
        s_btn_top_armed = false;
        return gotchi_voice::InteractResult::ABORT_CANCEL;
    }
    return gotchi_voice::InteractResult::DONE;
}

// Captures one JPEG frame into slot `slot_idx` of PSRAM staging buffers.
static bool captureIntoSlot(size_t slot_idx) {
    if (slot_idx >= BURST_FRAME_COUNT || !s_staged_jpeg_bufs[slot_idx]) return false;
    camera_fb_t* fb = gotchi_camera::captureJpeg();
    if (!fb || !fb->buf || fb->len == 0 || fb->len > MAX_STAGED_JPEG_BYTES) {
        if (fb) esp_camera_fb_return(fb);
        gotchi_camera::standby();
        return false;
    }
    memcpy(s_staged_jpeg_bufs[slot_idx], fb->buf, fb->len);
    s_staged_jpeg_lens[slot_idx] = fb->len;
    esp_camera_fb_return(fb);
    gotchi_camera::standby();
    return true;
}

static size_t captureBurstFrames() {
    const uint32_t interval_ms = BURST_TOTAL_DURATION_MS / (BURST_FRAME_COUNT - 1);
    uint32_t burst_start = millis();
    size_t captured = 0;
    for (size_t i = 0; i < BURST_FRAME_COUNT; ++i) {
        uint32_t target_ts = burst_start + (uint32_t)(i * interval_ms);
        while ((int32_t)(target_ts - millis()) > 0) {
            pollRpcAndHttp();
            delay(5);
        }
        if (captureIntoSlot(captured)) {
            gotchi_audio::playChime("snap");
            captured++;
        }
    }
    return captured;
}

// Runs a Go Live continuous session (PRD F19 / F21 / Section 5b).
// Stays open until `BTN_TOP` is pressed or `cfg.live_silence_ms` of silence elapses.
static void runLiveSession() {
    GotchiUIState ui;
    ui.skin = cfg.skin;

    if (!cfg.live_mode) {
        gotchi_log::RingLogger.info("live", "Go Live ignored: live_mode is off");
        showIdle();
        return;
    }

    gotchi_live::LiveSessionConfig lcfg;
    lcfg.voice_url = (cfg.live_url.length() > 0) ? cfg.live_url : cfg.voice_url;
    lcfg.node_id = cfg.node_id;
    lcfg.bearer_token = cfg.bearer_token;
    lcfg.audio_out_mode = cfg.audio_out_mode;
    lcfg.live_silence_ms = cfg.live_silence_ms;
    lcfg.vad_rms_lsb = cfg.vad_rms_lsb;
    lcfg.cam_long_press_ms = cfg.cam_long_press_ms;
    lcfg.enabled = cfg.live_mode;
    lcfg.live_barge_in = cfg.live_barge_in;

    gotchi_live::runSession(
        lcfg,
        ui,
        onVoiceState,
        [](size_t slot, gotchi_live::CapturedFrameView* out_frame) -> bool {
            if (!captureIntoSlot(slot)) return false;
            if (out_frame) {
                out_frame->data = s_staged_jpeg_bufs[slot];
                out_frame->length = s_staged_jpeg_lens[slot];
            }
            return true;
        },
        [](gotchi_live::CapturedFrameView* out_frames, size_t max_frames) -> size_t {
            size_t count = captureBurstFrames();
            if (count > max_frames) count = max_frames;
            if (out_frames) {
                for (size_t i = 0; i < count; ++i) {
                    out_frames[i].data = s_staged_jpeg_bufs[i];
                    out_frames[i].length = s_staged_jpeg_lens[i];
                }
            }
            return count;
        },
        pollRpcAndHttp
    );

    showIdle();
}

static void dispatchVoiceOrSnap(const gotchi_audio::WavView& wav, bool include_staged_snap, bool is_standalone_snap) {
    GotchiUIState ui;
    attachPowerToUi(ui);
    ui.skin = cfg.skin;

    if (WiFi.status() != WL_CONNECTED && !gotchi_ble::isBlePeerConnected()) {
        ui.state = UiState::OFFLINE;
        ui.status_text = "NO WIFI";
        panel.draw(ui);
        delay(2000);
        showIdle();
        return;
    }

    ui.state = UiState::THINKING;
    ui.status_text = is_standalone_snap ? "SENDING SNAP..." : "THINKING...";
    s_last_drawn_voice_state = UiState::THINKING;
    panel.draw(ui);
    gotchi_log::RingLogger.info("http", is_standalone_snap ? "Uploading standalone snapshot" : "Uploading voice turn WAV", cfg.wifi_psk.c_str(), cfg.bearer_token.c_str());

    gotchi_voice::ImageAttachment img;
    const gotchi_voice::ImageAttachment* img_ptr = nullptr;
    if (include_staged_snap && s_staged_jpeg_active && s_staged_jpeg_count > 0) {
        if (s_staged_jpeg_count > 1) {
            img.burst_count = s_staged_jpeg_count < gotchi_voice::ImageAttachment::MAX_BURST_FRAMES
                            ? s_staged_jpeg_count
                            : gotchi_voice::ImageAttachment::MAX_BURST_FRAMES;
            for (size_t i = 0; i < img.burst_count; ++i) {
                img.burst_data[i] = s_staged_jpeg_bufs[i];
                img.burst_lengths[i] = s_staged_jpeg_lens[i];
            }
        } else if (s_staged_jpeg_bufs[0] && s_staged_jpeg_lens[0] > 0) {
            img.data = s_staged_jpeg_bufs[0];
            img.length = s_staged_jpeg_lens[0];
        }
        if (is_standalone_snap) {
            img.default_prompt = cfg.default_image_prompt;
        }
        img_ptr = &img;
        clearStagedSnapshots();
    }

    gotchi_voice::InteractResult result = gotchi_voice::InteractResult::DONE;
    bool ok = gotchi_voice::interact(
        wav, cfg.voice_url, cfg.node_id, ui, onVoiceState, cfg.bearer_token,
        img_ptr, pollVoiceAbort, &result, cfg.audio_out_mode
    );
    if (ok) {
        gotchi_log::RingLogger.info("http", "Interact stream completed (done)");
    } else if (result == gotchi_voice::InteractResult::ABORT_CANCEL) {
        gotchi_log::RingLogger.info("audio", "Interact cancelled by user (BTN_TOP)");
    } else {
        gotchi_log::RingLogger.warn("http", "Interact stream terminated with error or disconnect");
    }

    if (result == gotchi_voice::InteractResult::ABORT_CANCEL) {
        waitForButtonRelease(PIN_BTN_TOP_PTT);
        showIdle();
        return;
    }

    // PRD F21: Saying "Go Live" as the whole utterance launches a Live session (when live_mode is enabled)
    if (cfg.live_mode && isWholeUtteranceGoLive(ui.transcript)) {
        s_enter_live_pending = true;
        return;
    }

    if (ok && ui.audio_bytes_received == 0 && ui.reply.isEmpty() && cfg.audio_out_mode == "speaker") {
        gotchi_log::RingLogger.info("voice", "Turn ghost-filtered by gateway (0 audio bytes)");
        ui.state = UiState::IDLE;
        ui.status_text = "TRY AGAIN";
        panel.draw(ui);
        delay(1200);
        showIdle();
        return;
    }

    if (!ok) {
        delay(2000);
    }
    showIdle();
}

// Handles bottom camera button (`BTN_BOT`) in IDLE (PRD F14 & F20):
//   - Short press (< `cam_long_press_ms`, default 700ms): captures 1 snapshot on release,
//     immediately enters hands-free listening (`LISTENING (+SNAP)...`), and auto-describes
//     the photo if no speech is heard within 2 seconds.
//   - Long press (>= `cam_long_press_ms`): captures a burst of 5 photos over 3 seconds,
//     then enters the same 2-second auto-listen / auto-describe flow.
static void runPttTurn(bool auto_listen_for_snap = false);

static void handleCameraButtonIdle() {
    GotchiUIState ui;
    ui.skin = cfg.skin;
    uint32_t t0 = millis();
    bool is_long_press = false;

    while (digitalRead(PIN_BTN_BOT_MOD) == LOW) {
        if (millis() - t0 >= cfg.cam_long_press_ms) {
            is_long_press = true;
            break;
        }
        delay(5);
    }

    if (!s_staged_jpeg_bufs[0]) {
        waitForButtonRelease(PIN_BTN_BOT_MOD);
        ui.state = UiState::ERROR;
        ui.status_text = "NO SNAP BUF";
        panel.draw(ui);
        delay(1500);
        showIdle();
        return;
    }

    clearStagedSnapshots();

    if (!is_long_press) {
        // Short press: capture single snapshot on release (F14) and start listening immediately
        if (!captureIntoSlot(0)) {
            gotchi_audio::playChime("error");
            ui.state = UiState::ERROR;
            ui.status_text = "CAM ERROR";
            panel.draw(ui);
            delay(1500);
            showIdle();
            return;
        }
        s_staged_jpeg_count = 1;
        s_staged_jpeg_active = true;
        s_staged_jpeg_ts = millis();
        gotchi_audio::playChime("snap");
        gotchi_log::RingLogger.info("camera", "Captured single snapshot JPEG");
        runPttTurn(true);
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
        return;
    }

    // Long press: capture burst of 5 photos over 3 seconds (F20: 0ms, 750ms, 1500ms, 2250ms, 3000ms)
    size_t captured = captureBurstFrames();
    waitForButtonRelease(PIN_BTN_BOT_MOD);

    if (captured == 0) {
        gotchi_audio::playChime("error");
        ui.state = UiState::ERROR;
        ui.status_text = "BURST ERROR";
        panel.draw(ui);
        delay(1500);
        showIdle();
        return;
    }

    s_staged_jpeg_count = captured;
    s_staged_jpeg_active = true;
    s_staged_jpeg_ts = millis();
    char bmsg[64];
    snprintf(bmsg, sizeof(bmsg), "Captured burst of %u frames", (unsigned)captured);
    gotchi_log::RingLogger.info("camera", bmsg);
    runPttTurn(true);
    s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
    s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
}

// One voice turn on `BTN_TOP` (or auto-listen after `BTN_BOT` camera capture, PRD F13/F14/F19):
//   - "hold" mode: hold BTN_TOP to record, release to send.
//   - "tap" mode: tap BTN_TOP to open mic, auto-stops after `cfg.vad_silence_ms` (1.5s)
//     of trailing silence once speech is heard (or tap BTN_TOP again to send immediately).
//   - "hybrid" mode (default): releasing BTN_TOP within `cfg.tap_latch_ms` (500ms) latches
//     into hands-free Tap-to-Talk; holding longer than `cfg.tap_latch_ms` stops on release.
//   - Double-tap `BTN_TOP` within `cfg.double_tap_ms` (400ms) of first release: when `cfg.live_mode`
//     is enabled, discards the partial clip and starts a Go Live session (F19); when `cfg.live_mode`
//     is off, does nothing special.
//   - Pressing `BTN_BOT` during recording captures a photo on release and attaches it to the
//     voice turn in progress (F14; bottom button no longer cancels recording).
static void runPttTurn(bool auto_listen_for_snap) {
    if (!auto_listen_for_snap && isPendingAnswerValid()) {
        waitForButtonRelease(PIN_BTN_TOP_PTT);
        s_pending_answer_ready = false;
        String ans_id = s_pending_answer_id;
        s_pending_answer_id = "";
        s_notify_status_text = "";

        GotchiUIState ui;
        attachPowerToUi(ui);
        ui.skin = cfg.skin;
        ui.state = UiState::THINKING;
        ui.status_text = "ANSWER...";
        s_last_drawn_voice_state = UiState::THINKING;
        panel.draw(ui);

        gotchi_voice::InteractResult result = gotchi_voice::InteractResult::DONE;
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        bool ok = gotchi_voice::fetchPendingAnswer(
            cfg.voice_url, cfg.node_id, ui, onVoiceState, cfg.bearer_token,
            ans_id, pollVoiceAbort, &result, cfg.audio_out_mode
        );
        if (ok) {
            gotchi_log::RingLogger.info("voice", "Pending async answer played to completion");
        } else if (result == gotchi_voice::InteractResult::ABORT_CANCEL) {
            gotchi_log::RingLogger.info("voice", "Pending async answer cancelled by user (BTN_TOP)");
            waitForButtonRelease(PIN_BTN_TOP_PTT);
        } else {
            gotchi_log::RingLogger.warn("voice", "Failed to stream pending async answer");
        }
        showIdle();
        return;
    }

    GotchiUIState ui;
    attachPowerToUi(ui);
    ui.skin = cfg.skin;

    static volatile bool     s_ptt_release_watch = false;
    static volatile uint32_t s_ptt_release_ts    = 0;
    static uint8_t           s_ptt_high_streak   = 0;

    panel.setBusyPollHook([]() {
        if (s_ptt_release_watch && s_ptt_release_ts == 0) {
            if (digitalRead(PIN_BTN_TOP_PTT) == HIGH) {
                if (++s_ptt_high_streak >= 3) {
                    s_ptt_release_ts = millis();
                }
            } else {
                s_ptt_high_streak = 0;
            }
        }
    });

    uint32_t press_start_ts = millis();
    s_ptt_release_ts    = 0;
    s_ptt_high_streak   = 0;
    s_ptt_release_watch = !auto_listen_for_snap;

    // Play start chime BEFORE opening the mic buffer so speaker output is never recorded into WAV
    if (!auto_listen_for_snap) {
        gotchi_audio::playChime("ptt_start");
        if (s_ptt_release_ts == 0 && btnStable(PIN_BTN_TOP_PTT, false)) {
            s_ptt_release_ts = millis();
        }
    }

    if (!gotchi_audio::beginPttRecording()) {
        s_ptt_release_watch = false;
        ui.state = UiState::ERROR;
        ui.error_code = "mic";
        ui.status_text = "MIC ERROR";
        panel.draw(ui);
        delay(1500);
        showIdle();
        return;
    }
    size_t vad_cursor_samples = gotchi_audio::getRecordedSamples();
    size_t last_voiced_sample = vad_cursor_samples;
    uint32_t voiced_frames = 0;

    // Start Core 0 DMA drain task during LISTENING screen refresh so zero speech is dropped
    bool async_running = gotchi_audio::startAsyncPttTask();
    ui.state = UiState::LISTENING;
    ui.status_text = s_staged_jpeg_active ? "LISTENING (+SNAP)..." : "LISTENING...";
    s_last_drawn_voice_state = UiState::LISTENING;
    panel.draw(ui);
    size_t draw_end_samples = gotchi_audio::getRecordedSamples();
    s_ptt_release_watch = false;
    if (!auto_listen_for_snap && s_ptt_release_ts == 0 && btnStable(PIN_BTN_TOP_PTT, false)) {
        s_ptt_release_ts = millis();
    }
    gotchi_log::RingLogger.info("audio", "PTT recording started");

    const float vad_thresh = (float)cfg.vad_rms_lsb / 32768.0f;
    const float min_rms    = (float)cfg.ptt_min_rms_lsb / 32768.0f;
    const size_t min_bytes = 44 + (SAMPLE_RATE_REC * sizeof(int16_t) * cfg.ptt_min_ms) / 1000;

    uint32_t t0 = millis();
    uint32_t first_release_ts = s_ptt_release_ts;
    bool speech_heard = false;
    bool released_once = auto_listen_for_snap || (first_release_ts != 0);
    // In hybrid mode, any button release during the chime/screen draw or within tap_latch_ms (500ms)
    // after LISTENING appears latches into hands-free Tap-to-Talk.
    bool latched_tap_mode = auto_listen_for_snap ||
                            (cfg.input_mode == "tap") ||
                            (cfg.input_mode == "hybrid" && released_once &&
                             ((int32_t)(first_release_ts - (t0 + cfg.tap_latch_ms)) <= 0));
    bool start_live_session = false;
    const uint32_t initial_timeout_ms = auto_listen_for_snap ? 2000UL : VAD_INITIAL_TIMEOUT_MS;

    // In explicit "hold" mode, if user released during screen refresh, capture brief tail and send
    if (released_once && !latched_tap_mode) {
        uint32_t tail_t0 = millis();
        while (millis() - tail_t0 < (cfg.input_mode == "hold" ? 800UL : 350UL)) {
            if (!async_running) gotchi_audio::pollPttRecording();
            httpServer.handleClient();
            delay(5);
        }
    }

    while (!(released_once && !latched_tap_mode) && (millis() - press_start_ts < cfg.ptt_max_ms)) {
        uint32_t elapsed = millis() - t0;
        if (!async_running) gotchi_audio::pollPttRecording();
        httpServer.handleClient();

        // PRD F14: Pressing BTN_BOT during a recording captures a snapshot on release
        // and attaches it to the voice turn in progress (does not cancel the turn).
        if (!s_btn_bot_armed && digitalRead(PIN_BTN_BOT_MOD) == HIGH) {
            s_btn_bot_armed = true;
        }
        if (s_btn_bot_armed && digitalRead(PIN_BTN_BOT_MOD) == LOW && btnStable(PIN_BTN_BOT_MOD, true)) {
            s_btn_bot_armed = false;
            waitForButtonRelease(PIN_BTN_BOT_MOD);
            s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
            if (captureIntoSlot(0)) {
                s_staged_jpeg_count = 1;
                s_staged_jpeg_active = true;
                s_staged_jpeg_ts = millis();
                gotchi_audio::playChime("snap");
            }
        }

        if (!released_once) {
            if (btnStable(PIN_BTN_TOP_PTT, false)) {
                released_once = true;
                first_release_ts = millis();
                if (cfg.input_mode == "hold") {
                    break;
                } else if (cfg.input_mode == "hybrid") {
                    if (first_release_ts - t0 <= cfg.tap_latch_ms) {
                        latched_tap_mode = true; // Quick tap -> latch into hands-free VAD listening
                    } else {
                        break; // Held past tap_latch_ms -> classic PTT release-to-send
                    }
                }
            }
        } else if (latched_tap_mode) {
            // Second tap of BTN_TOP after first release:
            //   - Within `double_tap_ms` (400ms): if `live_mode` is enabled, discard clip & start Go Live (F19);
            //     if `live_mode` is off, do nothing special (keep listening).
            //   - After `double_tap_ms`: finish recording and send immediately.
            if (digitalRead(PIN_BTN_TOP_PTT) == LOW && btnStable(PIN_BTN_TOP_PTT, true)) {
                uint32_t since_release = auto_listen_for_snap ? (cfg.double_tap_ms + 1) : (millis() - first_release_ts);
                waitForButtonRelease(PIN_BTN_TOP_PTT);
                if (since_release <= cfg.double_tap_ms) {
                    if (cfg.live_mode) {
                        start_live_session = true;
                        break;
                    }
                    // When live_mode is off, a double-tap within 400ms does nothing special
                } else {
                    break;
                }
            }
        }

        // Scan non-overlapping 20ms (320-sample) frames from the start of recording (including
        // speech captured on Core 0 while the LISTENING screen was drawing). Rejects sharp
        // button-release clicks via crest factor <= 4.5 and latches speech_heard once 4 voiced
        // frames (80ms cumulative, allowing consonant gaps) are observed.
        gotchi_audio::scanSpeechFrames(vad_cursor_samples, vad_thresh, voiced_frames, last_voiced_sample);
        if (voiced_frames >= 4) {
            speech_heard = true;
        }

        // Start the trailing-silence timer only after the LISTENING screen draw has finished
        // (whichever sample mark is later: last_voiced_sample or draw_end_samples).
        size_t cur_samples = gotchi_audio::getRecordedSamples();
        size_t silence_anchor = (last_voiced_sample > draw_end_samples) ? last_voiced_sample : draw_end_samples;
        uint32_t silence_ms = (cur_samples > silence_anchor)
            ? (uint32_t)(((cur_samples - silence_anchor) * 1000ULL) / SAMPLE_RATE_REC)
            : 0;

        if (latched_tap_mode && released_once) {
            if (speech_heard && (silence_ms >= cfg.vad_silence_ms)) {
                break; // User finished speaking and paused for vad_silence_ms
            }
            if (!speech_heard && (elapsed >= initial_timeout_ms)) {
                break; // No speech detected within initial window (2s for snap, 5s for voice)
            }
        }

        delay(5);
    }

    gotchi_audio::stopAsyncPttTask();
    gotchi_audio::scanSpeechFrames(vad_cursor_samples, vad_thresh, voiced_frames, last_voiced_sample);
    if (voiced_frames >= 2) {
        speech_heard = true;
    }
    gotchi_audio::WavView wav = gotchi_audio::finishPttRecording();
    char fin_msg[96];
    snprintf(fin_msg, sizeof(fin_msg),
             "PTT recording finished (samples=%u voiced_frames=%u speech=%d)",
             (unsigned)gotchi_audio::getRecordedSamples(),
             (unsigned)voiced_frames,
             (int)speech_heard);
    gotchi_log::RingLogger.info("audio", fin_msg);

    if (start_live_session) {
        s_enter_live_pending = true;
        return;
    }

    if (auto_listen_for_snap && !speech_heard) {
        gotchi_audio::WavView no_audio_wav{nullptr, 0, 0.0f};
        dispatchVoiceOrSnap(no_audio_wav, true, true);
        return;
    }

    gotchi_audio::playChime("ptt_stop");

    if ((latched_tap_mode && !speech_heard && !s_staged_jpeg_active) || wav.length < min_bytes) {
        if (s_staged_jpeg_active) {
            gotchi_audio::WavView no_audio_wav{nullptr, 0, 0.0f};
            dispatchVoiceOrSnap(no_audio_wav, true, true);
            return;
        }
        ui.state = UiState::IDLE;
        ui.status_text = "TOO SHORT";
        panel.draw(ui);
        delay(1500);
        showIdle();
        return;
    }

    dispatchVoiceOrSnap(wav, s_staged_jpeg_active, false);
}

// ============================================================================
// V1 All-Day Journal Mode (`journal_mode`, default false — PRD F24 / T3.5)
//   - BLE-Only Low-Power State: When bonded to the phone over BLE on battery power
//     (`gotchi_ble::isBlePeerConnected() && !isExternalPowerConnected()`), shuts down the
//     ESP32-S3 Wi-Fi radio completely (`WiFi.mode(WIFI_OFF)`), drops CPU frequency to 80 MHz,
//     and keeps the e-paper display & MAX98357A speaker in 0 uA / <1 uA deep sleep.
//   - Duty-Cycled Acoustic Sniff: Probes a 16 ms (256-sample) PDM slice in internal SRAM every
//     120 ms while silent. When ambient speech crosses `cfg.vad_rms_lsb`, transitions into
//     silent segment capture (`up to journal_seg_max_ms = 30s`, closing after `journal_silence_ms = 2s`
//     of trailing silence) and uploads via BLE (`mode: "journal"`) or Wi-Fi without waking the
//     speaker or e-paper screen. Any button press (`BTN_TOP` / `BTN_BOT`) immediately preempts
//     passive journal capture with zero user latency.
// ============================================================================
static void manageBleLowPowerWiFi() {
    // Default V1 behavior (`ble_disable_wifi == true`): whenever bonded & connected to the phone
    // over BLE (`gotchi_ble::isBlePeerConnected()`), turn off the Wi-Fi radio completely (`WIFI_OFF`)
    // and drop idle CPU clock to 80 MHz. If the phone disconnects from BLE, automatically restore
    // 240 MHz and reconnect Wi-Fi STA as fallback.
    bool want_ble_lp = cfg.ble_disable_wifi && gotchi_ble::isBlePeerConnected();
    if (want_ble_lp && !s_wifi_disabled_for_ble_lp) {
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
        setCpuFrequencyMhz(80);
        s_wifi_disabled_for_ble_lp = true;
        gotchi_log::RingLogger.info("power", "BLE connected: WiFi_OFF + 80 MHz low-power mode active");
    } else if (!want_ble_lp && s_wifi_disabled_for_ble_lp) {
        setCpuFrequencyMhz(240);
        s_wifi_disabled_for_ble_lp = false;
        if (cfg.wifi_ssid.length() > 0) {
            WiFi.mode(WIFI_STA);
            WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_psk.c_str());
        }
        gotchi_log::RingLogger.info("power", "BLE disconnected or WiFi re-enabled: restored 240 MHz + Wi-Fi STA");
    }
}

static void pollJournalMode() {
    if (!cfg.journal_mode) return;
    uint32_t now = millis();
    if (now - s_last_journal_sniff_ts < 120) return;
    s_last_journal_sniff_ts = now;

    // 16 ms acoustic probe in internal SRAM
    int16_t probe[256];
    float probe_rms = 0.0f;
    size_t n = gotchi_audio::readLivePcmChunk(probe, 256, &probe_rms);
    const float vad_thresh = (float)cfg.vad_rms_lsb / 32768.0f;
    if (n == 0 || probe_rms < vad_thresh) {
        return;
    }

    // Speech detected -> boost CPU to 240 MHz while recording & uploading passive journal segment,
    // then automatically drop back to 80 MHz via ScopedCpuBoost when returning to idle sniff.
    ScopedCpuBoost boost;
    if (!gotchi_audio::beginPttRecording()) return;
    uint32_t seg_start = millis();
    uint32_t last_voice_ts = seg_start;
    uint32_t voiced_ms = 20;

    while (millis() - seg_start < cfg.journal_seg_max_ms) {
        // Preempt immediately if user presses Top (PTT) or Bottom (Camera) button
        if (digitalRead(PIN_BTN_TOP_PTT) == LOW || digitalRead(PIN_BTN_BOT_MOD) == LOW) {
            gotchi_log::RingLogger.info("journal", "Passive journal segment preempted by button press");
            return;
        }
        gotchi_audio::pollPttRecording();
        httpServer.handleClient();

        float recent_rms = gotchi_audio::getRecentRms(1600);
        if (recent_rms >= vad_thresh) {
            last_voice_ts = millis();
            voiced_ms += 10;
        } else if (millis() - last_voice_ts >= cfg.journal_silence_ms) {
            break;
        }
        delay(10);
    }

    gotchi_audio::WavView wav = gotchi_audio::finishPttRecording();
    if (voiced_ms >= cfg.journal_min_voiced_ms && wav.length > 44) {
        gotchi_voice::uploadJournalSegment(wav, cfg.voice_url, cfg.node_id, cfg.bearer_token);
    }
}

static void latchUserLedOff() {
    // XIAO ESP32S3 onboard USR LED on GPIO 21 is active-LOW (tied to 3.3V).
    // Drive GPIO 21 HIGH and latch pad hold across deep sleep so it never glows.
    gpio_hold_dis(GPIO_NUM_21);
    pinMode(21, OUTPUT);
    digitalWrite(21, HIGH);
    gpio_hold_en(GPIO_NUM_21);
    gpio_deep_sleep_hold_en();
}

void setup() {
    // Hardware brownout detector left enabled to distinguish battery rail brownout (reset_reason=9)
    // from software panic (reset_reason=4) during speaker playback tests.
    // WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);
    latchUserLedOff();
    pinMode(PIN_BTN_TOP_PTT, INPUT_PULLUP);
    pinMode(PIN_BTN_BOT_MOD, INPUT_PULLUP);
    pinMode(PIN_EPD_BUSY, INPUT);

    // Fast wake check when slide switch (Switch Leg 1 -> D4) is OFF:
    // stay in ultra-low-power deep sleep and re-check every 1.0s until switch turns ON.
    samplePowerAndBatteryTelemetry(true);
    if (s_battery_pct < 0 && !isUsbDataHostConnected()) {
        esp_sleep_enable_timer_wakeup(1000000ULL);
        esp_deep_sleep_start();
    }

    Serial.begin(115200);
    gotchi_log::RingLogger.init(true);
    char boot_msg[144];
    snprintf(boot_msg, sizeof(boot_msg),
             "GOTCHI V1 FW %s booting (reset_reason=%d bat_mv=%u bat_pct=%d wall=%d usb=%d stack_hwm=%u)",
             GOTCHI_FW_VERSION, (int)esp_reset_reason(),
             (unsigned)s_battery_mv, (int)s_battery_pct,
             (int)isWallBrickPowerConnected(), (int)isUsbDataHostConnected(),
             (unsigned)uxTaskGetStackHighWaterMark(NULL));
    gotchi_log::RingLogger.info("boot", boot_msg);

    if (psramFound()) {
        gotchi_log::RingLogger.info("boot", "PSRAM detected and initialized");
        for (size_t i = 0; i < BURST_FRAME_COUNT; ++i) {
            s_staged_jpeg_bufs[i] = (uint8_t*)ps_malloc(MAX_STAGED_JPEG_BYTES);
        }
    } else {
        gotchi_log::RingLogger.warn("boot", "No PSRAM detected; operating in internal SRAM");
    }
    loadConfig();

    bool wifi_configured = cfg.wifi_ssid.length() > 0;
    bool wifi_up = false;
    if (wifi_configured) {
        WiFi.mode(WIFI_STA);
        char connect_msg[64];
        snprintf(connect_msg, sizeof(connect_msg), "Connecting to SSID: %.32s", cfg.wifi_ssid.c_str());
        gotchi_log::RingLogger.info("wifi", connect_msg, cfg.wifi_psk.c_str(), cfg.bearer_token.c_str());
        WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_psk.c_str());
        uint32_t t0 = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_CONNECT_TIMEOUT_MS) {
            delay(100);
        }
        wifi_up = (WiFi.status() == WL_CONNECTED);
        if (wifi_up) {
            gotchi_log::RingLogger.info("wifi", "WiFi connected successfully", cfg.wifi_psk.c_str(), cfg.bearer_token.c_str());
        } else {
            gotchi_log::RingLogger.warn("wifi", "WiFi connection timed out", cfg.wifi_psk.c_str(), cfg.bearer_token.c_str());
        }
    } else {
        gotchi_log::RingLogger.info("wifi", "WiFi unconfigured; starting AP setup portal");
    }

    gotchi_audio::init();
    samplePowerAndBatteryTelemetry(true);
    applyPrerollPolicy();
    gotchi_camera::init();
    gotchi_ble::initBleTransport(cfg.node_id.c_str());
    if (!wifi_configured || !wifi_up) {
        gotchi_portal::startSoftApPortal(httpServer);
    }

    panel.begin();
    GotchiUIState boot_ui;
    attachPowerToUi(boot_ui);
    boot_ui.skin = cfg.skin;
    if (wifi_configured && !wifi_up && !gotchi_ble::isBlePeerConnected()) {
        boot_ui.state = UiState::OFFLINE;
        boot_ui.status_text = "NO WIFI";
    }
    panel.draw(boot_ui);

    httpServer.on("/api/rpc", HTTP_POST, []() {
        if (cfg.bearer_token.length() > 0) {
            String auth = httpServer.header("Authorization");
            String expected = "Bearer " + cfg.bearer_token;
            if (!constantTimeEquals(auth, expected)) {
                httpServer.send(401, "application/json", "{\"ok\":false,\"error\":\"unauthorized\"}");
                return;
            }
        }
        String resp = handleJsonRpc(httpServer.arg("plain"));
        httpServer.send(200, "application/json", resp);
    });
    httpServer.on("/api/ota", HTTP_POST, []() {
        bool ok = !Update.hasError();
        httpServer.send(ok ? 200 : 500, "application/json", ok ? "{\"ok\":true,\"status\":\"rebooting\"}" : "{\"ok\":false}");
        delay(200);
        if (ok) ESP.restart();
    }, []() {
        if (cfg.bearer_token.length() > 0) {
            String auth = httpServer.header("Authorization");
            String expected = "Bearer " + cfg.bearer_token;
            if (!constantTimeEquals(auth, expected)) return;
        }
        HTTPUpload& up = httpServer.upload();
        if (up.status == UPLOAD_FILE_START) {
            Update.abort();
            Update.begin(UPDATE_SIZE_UNKNOWN);
        } else if (up.status == UPLOAD_FILE_WRITE) {
            Update.write(up.buf, up.currentSize);
        } else if (up.status == UPLOAD_FILE_END) {
            Update.end(true);
        } else if (up.status == UPLOAD_FILE_ABORTED) {
            Update.abort();
        }
    });
    static const char* kCollectedHeaders[] = {"Authorization"};
    httpServer.collectHeaders(kCollectedHeaders, 1);
    httpServer.begin();
    latchUserLedOff();
    s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
    s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
    if (!s_btn_top_armed || !s_btn_bot_armed) {
        char btn_warn[96];
        snprintf(btn_warn, sizeof(btn_warn),
                 "Button held LOW at startup (top_armed=%d bot_armed=%d); disarmed until released HIGH",
                 (int)s_btn_top_armed, (int)s_btn_bot_armed);
        gotchi_log::RingLogger.warn("boot", btn_warn);
    }
}

void loop() {
    httpServer.handleClient();
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();
        if (line.length() > 0) {
            Serial.println(handleJsonRpc(line));
        }
    }

    updateButtonArming();

    // Launch Go Live session if triggered by double-tap or whole-utterance "Go Live" command (F19/F21)
    if (s_enter_live_pending) {
        s_enter_live_pending = false;
        ScopedCpuBoost boost;
        runLiveSession();
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
        return;
    }

    // Poll incoming BLE CONTROL (0x07) frames while IDLE so notifications work in BLE low-power mode
    if (s_last_drawn_voice_state == UiState::IDLE && gotchi_ble::isBlePeerConnected()) {
        uint8_t ctrl_buf[512];
        size_t ctrl_len = 0;
        gotchi_ble::FrameType ctrl_type = gotchi_ble::FrameType::CONTROL;
        if (gotchi_ble::pollIncomingFrame(ctrl_buf, sizeof(ctrl_buf) - 1, ctrl_len, ctrl_type)) {
            if (ctrl_type == gotchi_ble::FrameType::CONTROL && ctrl_len > 0) {
                ctrl_buf[ctrl_len] = 0;
                String resp = handleJsonRpc(String((const char*)ctrl_buf));
                gotchi_ble::sendFramedPayload(
                    gotchi_ble::FrameType::CONTROL,
                    (const uint8_t*)resp.c_str(),
                    resp.length(),
                    0
                );
            }
        }
    }

    // Expire 60-second notification banner cleanly back to IDLE
    if (s_last_drawn_voice_state == UiState::IDLE && s_notify_status_text.length() > 0 &&
        (int32_t)(millis() - s_notify_expire_ts) >= 0) {
        s_notify_status_text = "";
        showIdle();
    }

    // Top button (speaking only, F13): start Hybrid / Tap-to-Talk / Hold-to-Talk / Double-Tap Live
    if (s_btn_top_armed && btnStable(PIN_BTN_TOP_PTT, true)) {
        s_btn_top_armed = false;
        s_notify_status_text = "";
        ScopedCpuBoost boost;
        runPttTurn();
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        return;
    }

    // Bottom button (camera only, F14/F20): short press = 1 photo on release; long press = 5-photo burst over 3s
    if (s_btn_bot_armed && btnStable(PIN_BTN_BOT_MOD, true)) {
        s_btn_bot_armed = false;
        ScopedCpuBoost boost;
        handleCameraButtonIdle();
        s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
        return;
    }

    // If staged camera snapshot(s) weren't paired with a voice prompt within `snap_window_ms` (3s),
    // dispatch standalone with `default_image_prompt` and no `audio` part.
    if (s_staged_jpeg_active && (millis() - s_staged_jpeg_ts >= cfg.snap_window_ms)) {
        ScopedCpuBoost boost;
        gotchi_audio::WavView no_audio_wav{nullptr, 0};
        dispatchVoiceOrSnap(no_audio_wav, true, true);
        return;
    }

    samplePowerAndBatteryTelemetry();
    static uint8_t s_switch_off_streak = 0;
    if (s_battery_pct < 0) {
        if (s_switch_off_streak < 10) s_switch_off_streak++;
        if (s_switch_off_streak >= 3 && !isUsbDataHostConnected()) {
            gotchi_audio::muteSpeaker();
            gotchi_camera::standby();
            panel.prepareDeepSleep();
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
            latchUserLedOff();
            esp_sleep_enable_timer_wakeup(1000000ULL);
            esp_deep_sleep_start();
        }
    } else {
        s_switch_off_streak = 0;
    }
    manageBleLowPowerWiFi();
    applyPrerollPolicy();
    if (cfg.journal_mode) {
        pollJournalMode();
    } else {
        gotchi_audio::pumpPreroll();
    }
    delay(2);
}
