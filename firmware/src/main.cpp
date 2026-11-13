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
#include <esp_wifi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <ArduinoJson.h>
#include <driver/gpio.h>
#include <driver/i2s.h>
#include <esp_sleep.h>
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
    bool     reply_reopen_mic = true;        // Reply ending in '?' reopens the mic after speaker playback (no PTT press)
    uint32_t reply_reopen_window_ms = 4000;  // Listening window for that follow-up capture; VAD closes it on silence
    uint32_t cam_long_press_ms = 700; // Holding BTN_BOT >= 700ms triggers 5-photo burst over 3s (F20)
    bool     live_mode = false;       // F19: Go Live mode off by default (configurable via set_config)
    bool     live_barge_in = false;   // T6.1: Go Live full-duplex AEC + speech barge-in off by default until bench delay calibration
    uint32_t live_silence_ms = 15000; // F19: 15s silence ends Go Live session
    bool     journal_mode = false;    // F24: Passive All-Day Journal mode off by default (configurable via set_config)
    bool     ble_disable_wifi = true; // F18/F24: Default true — turn off Wi-Fi radio whenever connected to phone over BLE
    uint32_t ble_wifi_idle_off_ms = 300000; // When bonded over BLE with Wi-Fi enabled, power down Wi-Fi after 5 min of idle
    uint32_t battery_wifi_idle_off_ms = 120000; // 2 minutes default unplugged battery idle sleep
    uint32_t screen_sleep_ms = 600000;     // 10 minutes default idle screen sleep (0 = disabled)
    uint32_t journal_seg_max_ms = 30000;   // F24: Max 30s per passive journal speech segment
    uint32_t journal_silence_ms = 2000;    // F24: 2.0s trailing silence closes a journal segment
    uint32_t journal_min_voiced_ms = 1000; // F24: Minimum 1.0s voiced duration to upload a journal segment
    String default_image_prompt = "Describe what you see in this snapshot briefly.";
    uint8_t chime_volume_pct = 35;
    uint8_t ble_tx_pacing_ms = gotchi_ble::BLE_TX_DEFAULT_PACING_MS; // 12ms CHAR_TX notification pacing (10..50ms)
    uint32_t ble_thinking_timeout_ms = 25000;                        // 25s THINKING recovery guard in BLE relay / ble_phone mode
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
static bool     s_auto_speak_pending = false;
static bool     s_reopen_mic_pending = false; // set after a '?' reply finishes playing; loop() reopens the mic
static bool     s_voice_busy = false;
static constexpr uint32_t DEFERRED_WAIT_MAX_MS = 660000UL; // 11 minutes
static bool     s_deferred_wait_active = false;
static uint32_t s_deferred_wait_start_ts = 0;
static uint32_t s_deferred_next_poll_ms = 30000;
static uint32_t s_last_deferred_poll_ts = 0;

struct GotchiUIStateWithAsync : public GotchiUIState {
    bool async_handoff = false;
};
// millis() when the last voice/camera/live turn ended. The fuel gauge skips samples
// for BATTERY_LOAD_SETTLE_MS after it, because VBAT sags under speaker and Wi-Fi TX load.
static uint32_t s_last_load_end_ts = 0;
static uint32_t s_last_activity_ts = 0;

struct ScopedVoiceBusy {
    ScopedVoiceBusy() { s_voice_busy = true; s_last_activity_ts = millis(); }
    ~ScopedVoiceBusy() { s_voice_busy = false; s_last_load_end_ts = millis(); s_last_activity_ts = millis(); }
};

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
static uint32_t s_idle_entered_ts = 0;
static int8_t   s_last_drawn_battery_pct = -1;
static bool     s_last_drawn_wall_power = false;
static bool     s_last_drawn_wifi_active = false;
static bool     s_last_drawn_ble_active = false;
static bool     s_screen_sleeping = false;
static constexpr uint32_t BLE_THINKING_MIN_TIMEOUT_MS = 5000;
static constexpr uint32_t BLE_THINKING_MAX_TIMEOUT_MS = 60000;
static uint32_t s_thinking_entered_ts = 0;
static bool     s_ble_thinking_timed_out = false;
static void showIdle();
static bool     s_enter_live_pending = false;
static bool     s_wifi_disabled_for_ble_lp = false;
static uint32_t s_last_journal_sniff_ts    = 0;
static bool     s_btn_top_armed            = false;
static bool     s_btn_bot_armed            = false;

static TaskHandle_t s_rgb_task_handle = nullptr;
static volatile bool s_led_test_override = false;
static volatile bool s_rgb_force_refresh = false;

static bool isBleRelayOrPhoneAudioMode() {
    if (cfg.audio_out_mode == "ble_phone") {
        return true;
    }
    return gotchi_ble::isBlePeerConnected() &&
           (s_wifi_disabled_for_ble_lp || WiFi.status() != WL_CONNECTED);
}

static void notifyRgbTask() {
    if (s_rgb_task_handle) {
        xTaskNotifyGive(s_rgb_task_handle);
    }
}

static void setVoiceUiState(UiState state) {
    if (state != UiState::IDLE) {
        s_screen_sleeping = false;
        s_last_activity_ts = millis();
    }
    if (state == UiState::THINKING || state == UiState::TRANSCRIBING || state == UiState::SYNTHESIZING) {
        if (s_thinking_entered_ts == 0) {
            s_thinking_entered_ts = millis();
            if (s_thinking_entered_ts == 0) s_thinking_entered_ts = 1;
        }
    } else {
        s_thinking_entered_ts = 0;
    }
    s_last_drawn_voice_state = state;
    notifyRgbTask();
}

static void standbyCameraAndNotifyRgb() {
    gotchi_camera::standby();
    s_rgb_force_refresh = true;
    notifyRgbTask();
}

static void setRgbOffAndLatchGpio21() {
    gotchi_camera::writeStandbyRgbLed(0, 0, 0);
    gpio_hold_dis(GPIO_NUM_21);
    pinMode(21, OUTPUT);
    digitalWrite(21, LOW);
    gpio_hold_en(GPIO_NUM_21);
}

static void rgbStatusTaskFn(void* param) {
    (void)param;
    bool was_active = false;
    int last_r = -1;
    int last_g = -1;
    int last_b = -1;
    TickType_t wait_ticks = portMAX_DELAY;

    while (true) {
        ulTaskNotifyTake(pdTRUE, wait_ticks);
        wait_ticks = portMAX_DELAY;

        if (s_rgb_force_refresh) {
            s_rgb_force_refresh = false;
            was_active = false;
            last_r = -1;
            last_g = -1;
            last_b = -1;
        }

        // If manual led_test RPC is currently commanding the LED, sleep until next state change
        if (s_led_test_override) {
            was_active = false;
            last_r = -1;
            last_g = -1;
            last_b = -1;
            continue;
        }

        if (!gotchi_camera::isInStandby()) {
            if (was_active) {
                setRgbOffAndLatchGpio21();
                was_active = false;
            }
            last_r = -1;
            last_g = -1;
            last_b = -1;
            continue;
        }

        UiState st = s_last_drawn_voice_state;
        bool want_active = false;
        uint8_t target_r = 0;
        uint8_t target_g = 0;
        uint8_t target_b = 0;

        if (st == UiState::LISTENING) {
            // Steady Cyan (0, 48, 64) — write once, then block indefinitely until state changes
            want_active = true;
            target_r = 0;
            target_g = 48;
            target_b = 64;
            wait_ticks = portMAX_DELAY;
        } else if (st == UiState::THINKING || st == UiState::TRANSCRIBING || st == UiState::SYNTHESIZING) {
            // 1 Hz Violet breathing wave updated at 20 Hz (50 ms) only while thinking
            uint32_t t = millis() % 1000;
            float phase = (float)t * (2.0f * (float)M_PI / 1000.0f);
            float factor = 0.5f * (1.0f - cosf(phase)); // 0.0 -> 1.0 -> 0.0
            float brightness = 0.15f + 0.85f * factor;
            want_active = true;
            target_r = (uint8_t)(48.0f * brightness);
            target_g = 0;
            target_b = (uint8_t)(64.0f * brightness);
            wait_ticks = pdMS_TO_TICKS(50);
        } else if (st == UiState::SPEAKING) {
            // Steady Emerald Green (0, 56, 20) — write once, then block indefinitely until state changes
            want_active = true;
            target_r = 0;
            target_g = 56;
            target_b = 20;
            wait_ticks = portMAX_DELAY;
        } else if (st == UiState::IDLE && s_notify_status_text.length() > 0) {
            int32_t rem_ms = (int32_t)(s_notify_expire_ts - millis());
            if (rem_ms > 0) {
                // Amber (64, 32, 0) double-blink: 4 exact wakes per 4,000 ms cycle (0, 120, 260, 380 ms)
                uint32_t t = millis() % 4000;
                uint32_t next_edge_ms = 4000 - t;
                if (t < 120) {
                    want_active = true;
                    target_r = 64;
                    target_g = 32;
                    target_b = 0;
                    next_edge_ms = 120 - t;
                } else if (t < 260) {
                    want_active = false;
                    next_edge_ms = 260 - t;
                } else if (t < 380) {
                    want_active = true;
                    target_r = 64;
                    target_g = 32;
                    target_b = 0;
                    next_edge_ms = 380 - t;
                }
                uint32_t sleep_ms = (next_edge_ms < (uint32_t)rem_ms) ? next_edge_ms : (uint32_t)rem_ms;
                if (sleep_ms < 1) sleep_ms = 1;
                wait_ticks = pdMS_TO_TICKS(sleep_ms);
            }
        }

        if (want_active) {
            if (!was_active || target_r != last_r || target_g != last_g || target_b != last_b) {
                if (gotchi_camera::writeStandbyRgbLed(target_r, target_g, target_b)) {
                    was_active = true;
                    last_r = target_r;
                    last_g = target_g;
                    last_b = target_b;
                }
            }
        } else if (was_active) {
            setRgbOffAndLatchGpio21();
            was_active = false;
            last_r = 0;
            last_g = 0;
            last_b = 0;
        }
    }
}

// Dynamically switches Wi-Fi to WIFI_PS_MIN_MODEM during active voice,
// camera, live, or journal-upload bursts, and drops back to WIFI_PS_MAX_MODEM whenever idle.
// Dynamic CPU frequency switching (80/240 MHz) is removed to avoid ESP32-S3 OPI PSRAM + Wi-Fi/BLE panics.
static int s_cpu_boost_depth = 0;

struct ScopedCpuBoost {
    ScopedCpuBoost() {
        if (s_cpu_boost_depth++ == 0) {
            if (!s_wifi_disabled_for_ble_lp && WiFi.status() == WL_CONNECTED) {
                WiFi.setSleep(WIFI_PS_MIN_MODEM);
            }
        }
    }
    ~ScopedCpuBoost() {
        if (--s_cpu_boost_depth <= 0) {
            s_cpu_boost_depth = 0;
            if (!s_wifi_disabled_for_ble_lp && WiFi.status() == WL_CONNECTED) {
                WiFi.setSleep(WIFI_PS_MAX_MODEM);
            }
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
static constexpr uint32_t POWER_TELEMETRY_IDLE_INTERVAL_MS = 60000; // 60s relaxed telemetry on battery idle
static uint32_t s_last_power_sample_ts = 0;
static uint16_t s_battery_mv = 0;
static uint16_t s_last_d4_mv = 0;
static uint16_t s_last_d2_mv = 0;
static int8_t   s_battery_pct = -1;
static bool     s_wall_brick_power = false;

// Fuel-gauge steadiness (2026-10-31):
//  - no samples while a turn is running or for BATTERY_LOAD_SETTLE_MS after it (load sag);
//  - on battery the shown percent never rises (it only resets after an unplug settles);
//  - after external power is removed, samples wait BATTERY_UNPLUG_SETTLE_MS for the
//    charger-held voltage to relax, then the EMA and the percent restart from a fresh read.
static constexpr uint32_t BATTERY_LOAD_SETTLE_MS   = 5000;
static constexpr uint32_t BATTERY_UNPLUG_SETTLE_MS = 60000;
static bool     s_prev_external_power = false;
static uint32_t s_unplug_ts = 0;
static bool     s_battery_reseed = false;

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

static bool isExternalPowerConnected();

static void samplePowerAndBatteryTelemetry(bool force = false) {
    uint32_t now = millis();
    bool idle_battery = !isExternalPowerConnected() && (s_last_drawn_voice_state == UiState::IDLE) &&
                        (s_wifi_disabled_for_ble_lp || WiFi.getMode() == WIFI_MODE_NULL);
    uint32_t interval = idle_battery ? POWER_TELEMETRY_IDLE_INTERVAL_MS : POWER_TELEMETRY_INTERVAL_MS;
    if (!force && s_last_power_sample_ts != 0 && (now - s_last_power_sample_ts < interval)) {
        return;
    }
    // Only sample when neither tactile button is actively held LOW.
    if (digitalRead(PIN_BTN_TOP_PTT) == LOW || digitalRead(PIN_BTN_BOT_MOD) == LOW) {
        return;
    }
    // Skip while a turn is loading the battery, and briefly after it, so sag does not
    // pull the gauge down. The first sample (boot switch-off check) always runs.
    if (s_battery_mv != 0 &&
        (s_voice_busy || (s_last_load_end_ts != 0 && now - s_last_load_end_ts < BATTERY_LOAD_SETTLE_MS))) {
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

    // 2. Decide charging state from USB host plus the previous VBUS read (step 3 below
    //    refreshes it, so an unplug is seen within one sample interval), then apply VBAT.
    bool external_now = isUsbDataHostConnected() || s_wall_brick_power;
    if (s_prev_external_power && !external_now) {
        s_unplug_ts = now;
        s_battery_reseed = true;
    }
    s_prev_external_power = external_now;
    bool unplug_settling = s_battery_reseed && (now - s_unplug_ts < BATTERY_UNPLUG_SETTLE_MS);

    if (vbat_div_mv >= PIN_VBAT_DIV_MIN_MV && vbat_div_mv <= PIN_VBAT_DIV_MAX_MV) {
        uint16_t raw_mv = (uint16_t)(vbat_div_mv * 2);
        if (unplug_settling) {
            // Charger-held voltage is still relaxing: keep the last values until it settles.
        } else if (s_battery_reseed || s_battery_mv == 0) {
            s_battery_reseed = false;
            s_battery_mv = raw_mv;
            s_battery_pct = lipoMvToPercent(s_battery_mv);
        } else {
            // 4:1 EMA smoothing to prevent percentage flicker
            s_battery_mv = (uint16_t)((s_battery_mv * 3 + raw_mv) / 4);
            int8_t pct = lipoMvToPercent(s_battery_mv);
            // On battery the shown percent only goes down; on external power it may rise.
            if (!external_now && s_battery_pct >= 0 && pct > s_battery_pct) {
                pct = s_battery_pct;
            }
            s_battery_pct = pct;
        }
    } else if (vbat_div_mv < PIN_VBAT_DIV_MIN_MV) {
        s_battery_mv = 0;
        s_battery_pct = -1;
    }

    // 3. Sample 5V VBUS via single 220k series resistor on D2 (PIN_BTN_TOP_PTT) against internal 45k INPUT_PULLDOWN:
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
    ui.wifi_active = !s_wifi_disabled_for_ble_lp && (WiFi.status() == WL_CONNECTED);
    ui.ble_active  = gotchi_ble::isBlePeerConnected();
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
    cfg.reply_reopen_mic     = prefs.getBool("reopen_q", cfg.reply_reopen_mic);
    cfg.reply_reopen_window_ms = prefs.getUInt("reopen_win_ms", cfg.reply_reopen_window_ms);
    cfg.cam_long_press_ms    = prefs.getUInt("cam_long_ms", cfg.cam_long_press_ms);
    cfg.live_mode            = prefs.getBool("live_mode", cfg.live_mode);
    cfg.live_barge_in        = prefs.getBool("live_barge_in", cfg.live_barge_in);
    cfg.live_silence_ms      = prefs.getUInt("live_sil_ms", cfg.live_silence_ms);
    cfg.journal_mode         = prefs.getBool("journal_mode", cfg.journal_mode);
    cfg.ble_disable_wifi     = prefs.getBool("ble_no_wifi", cfg.ble_disable_wifi);
    cfg.ble_wifi_idle_off_ms = prefs.getUInt("ble_wifi_idl", cfg.ble_wifi_idle_off_ms);
    cfg.battery_wifi_idle_off_ms = prefs.getUInt("bat_wifi_idl", cfg.battery_wifi_idle_off_ms);
    cfg.screen_sleep_ms      = prefs.getUInt("scr_sleep_ms", cfg.screen_sleep_ms);
    cfg.journal_seg_max_ms   = prefs.getUInt("jrn_seg_ms", cfg.journal_seg_max_ms);
    cfg.journal_silence_ms   = prefs.getUInt("jrn_sil_ms", cfg.journal_silence_ms);
    cfg.journal_min_voiced_ms= prefs.getUInt("jrn_min_ms", cfg.journal_min_voiced_ms);
    cfg.default_image_prompt = prefs.getString("img_prompt", cfg.default_image_prompt);
    cfg.chime_volume_pct     = (uint8_t)prefs.getUInt("chime_vol", cfg.chime_volume_pct);
    cfg.ble_tx_pacing_ms     = (uint8_t)prefs.getUInt("ble_pace_ms", cfg.ble_tx_pacing_ms);
    cfg.ble_thinking_timeout_ms = prefs.getUInt("ble_thk_ms", cfg.ble_thinking_timeout_ms);
    if (!prefs.getBool("bat_idl_v1", false) && cfg.battery_wifi_idle_off_ms == 0) {
        cfg.battery_wifi_idle_off_ms = 120000;
        prefs.end();
        prefs.begin("gotchi", false);
        prefs.putUInt("bat_wifi_idl", 120000);
        prefs.putBool("bat_idl_v1", true);
    }
    prefs.end();
    if (cfg.chime_volume_pct > 100) cfg.chime_volume_pct = 100;
    gotchi_audio::setChimeVolumePct(cfg.chime_volume_pct);
    if (cfg.ble_tx_pacing_ms < gotchi_ble::BLE_TX_MIN_PACING_MS) {
        cfg.ble_tx_pacing_ms = (uint8_t)gotchi_ble::BLE_TX_MIN_PACING_MS;
    } else if (cfg.ble_tx_pacing_ms > gotchi_ble::BLE_TX_MAX_PACING_MS) {
        cfg.ble_tx_pacing_ms = (uint8_t)gotchi_ble::BLE_TX_MAX_PACING_MS;
    }
    gotchi_ble::setTxNotifyPacingMs(cfg.ble_tx_pacing_ms);
    if (cfg.ble_thinking_timeout_ms < BLE_THINKING_MIN_TIMEOUT_MS) {
        cfg.ble_thinking_timeout_ms = BLE_THINKING_MIN_TIMEOUT_MS;
    } else if (cfg.ble_thinking_timeout_ms > BLE_THINKING_MAX_TIMEOUT_MS) {
        cfg.ble_thinking_timeout_ms = BLE_THINKING_MAX_TIMEOUT_MS;
    }
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
    prefs.putBool("reopen_q", cfg.reply_reopen_mic);
    prefs.putUInt("reopen_win_ms", cfg.reply_reopen_window_ms);
    prefs.putUInt("cam_long_ms", cfg.cam_long_press_ms);
    prefs.putBool("live_mode", cfg.live_mode);
    prefs.putBool("live_barge_in", cfg.live_barge_in);
    prefs.putUInt("live_sil_ms", cfg.live_silence_ms);
    prefs.putBool("journal_mode", cfg.journal_mode);
    prefs.putBool("ble_no_wifi", cfg.ble_disable_wifi);
    prefs.putUInt("ble_wifi_idl", cfg.ble_wifi_idle_off_ms);
    prefs.putUInt("bat_wifi_idl", cfg.battery_wifi_idle_off_ms);
    prefs.putUInt("scr_sleep_ms", cfg.screen_sleep_ms);
    prefs.putBool("bat_idl_v1", true);
    prefs.putUInt("jrn_seg_ms", cfg.journal_seg_max_ms);
    prefs.putUInt("jrn_sil_ms", cfg.journal_silence_ms);
    prefs.putUInt("jrn_min_ms", cfg.journal_min_voiced_ms);
    prefs.putString("img_prompt", cfg.default_image_prompt);
    prefs.putUInt("chime_vol", cfg.chime_volume_pct);
    prefs.putUInt("ble_pace_ms", cfg.ble_tx_pacing_ms);
    prefs.putUInt("ble_thk_ms", cfg.ble_thinking_timeout_ms);
    prefs.end();
    gotchi_audio::setChimeVolumePct(cfg.chime_volume_pct);
    gotchi_ble::setTxNotifyPacingMs(cfg.ble_tx_pacing_ms);
    applyPrerollPolicy();
}

String handleJsonRpc(const String& jsonLine) {
    JsonDocument req;
    JsonDocument res;
    String trimmed = jsonLine;
    trimmed.trim();
    if (trimmed.equalsIgnoreCase("STATE:idle")) {
        gotchi_ble::clearIncomingQueue();
        if (!s_voice_busy && s_last_drawn_voice_state != UiState::IDLE) {
            showIdle();
        }
        res["cmd"] = "state";
        res["ok"] = true;
        res["state"] = "idle";
        String out;
        serializeJson(res, out);
        return out;
    }
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
        res["reply_reopen_mic"] = cfg.reply_reopen_mic;
        res["reply_reopen_window_ms"] = cfg.reply_reopen_window_ms;
        res["cam_long_press_ms"] = cfg.cam_long_press_ms;
        res["live_mode"] = cfg.live_mode;
        res["live_barge_in"] = cfg.live_barge_in;
        res["live_silence_ms"] = cfg.live_silence_ms;
        res["journal_mode"] = cfg.journal_mode;
        res["ble_disable_wifi"] = cfg.ble_disable_wifi;
        res["ble_wifi_idle_off_ms"] = cfg.ble_wifi_idle_off_ms;
        res["battery_wifi_idle_off_ms"] = cfg.battery_wifi_idle_off_ms;
        res["screen_sleep_ms"] = cfg.screen_sleep_ms;
        res["journal_seg_max_ms"] = cfg.journal_seg_max_ms;
        res["journal_silence_ms"] = cfg.journal_silence_ms;
        res["journal_min_voiced_ms"] = cfg.journal_min_voiced_ms;
        res["default_image_prompt"] = cfg.default_image_prompt;
        res["chime_volume_pct"] = cfg.chime_volume_pct;
        res["ble_tx_pacing_ms"] = cfg.ble_tx_pacing_ms;
        res["ble_thinking_timeout_ms"] = cfg.ble_thinking_timeout_ms;
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
        res["cpu_mhz"] = getCpuFrequencyMhz();
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
        if (req["reply_reopen_mic"].is<bool>()) cfg.reply_reopen_mic = req["reply_reopen_mic"].as<bool>();
        if (req["reply_reopen_window_ms"].is<uint32_t>()) {
            uint32_t v = req["reply_reopen_window_ms"].as<uint32_t>();
            cfg.reply_reopen_window_ms = v < 500 ? 500 : (v > 15000 ? 15000 : v);
        }
        if (req["cam_long_press_ms"].is<uint32_t>()) cfg.cam_long_press_ms = req["cam_long_press_ms"].as<uint32_t>();
        if (req["live_mode"].is<bool>()) cfg.live_mode = req["live_mode"].as<bool>();
        if (req["live_barge_in"].is<bool>()) cfg.live_barge_in = req["live_barge_in"].as<bool>();
        if (req["live_silence_ms"].is<uint32_t>()) cfg.live_silence_ms = req["live_silence_ms"].as<uint32_t>();
        if (req["journal_mode"].is<bool>()) cfg.journal_mode = req["journal_mode"].as<bool>();
        if (req["ble_disable_wifi"].is<bool>()) cfg.ble_disable_wifi = req["ble_disable_wifi"].as<bool>();
        if (req["ble_wifi_idle_off_ms"].is<uint32_t>()) cfg.ble_wifi_idle_off_ms = req["ble_wifi_idle_off_ms"].as<uint32_t>();
        if (req["battery_wifi_idle_off_ms"].is<uint32_t>()) cfg.battery_wifi_idle_off_ms = req["battery_wifi_idle_off_ms"].as<uint32_t>();
        if (req["screen_sleep_ms"].is<uint32_t>()) cfg.screen_sleep_ms = req["screen_sleep_ms"].as<uint32_t>();
        if (req["journal_seg_max_ms"].is<uint32_t>()) cfg.journal_seg_max_ms = req["journal_seg_max_ms"].as<uint32_t>();
        if (req["journal_silence_ms"].is<uint32_t>()) cfg.journal_silence_ms = req["journal_silence_ms"].as<uint32_t>();
        if (req["journal_min_voiced_ms"].is<uint32_t>()) cfg.journal_min_voiced_ms = req["journal_min_voiced_ms"].as<uint32_t>();
        if (req["default_image_prompt"].is<const char*>()) {
            cfg.default_image_prompt = req["default_image_prompt"].as<String>();
        }
        if (req["chime_volume_pct"].is<int>() || req["chime_volume_pct"].is<uint32_t>()) {
            int v = req["chime_volume_pct"].as<int>();
            if (v < 0) v = 0;
            if (v > 100) v = 100;
            cfg.chime_volume_pct = (uint8_t)v;
            gotchi_audio::setChimeVolumePct(cfg.chime_volume_pct);
        }
        if (req["ble_tx_pacing_ms"].is<int>() || req["ble_tx_pacing_ms"].is<uint32_t>()) {
            int v = req["ble_tx_pacing_ms"].as<int>();
            if (v < (int)gotchi_ble::BLE_TX_MIN_PACING_MS) v = (int)gotchi_ble::BLE_TX_MIN_PACING_MS;
            if (v > (int)gotchi_ble::BLE_TX_MAX_PACING_MS) v = (int)gotchi_ble::BLE_TX_MAX_PACING_MS;
            cfg.ble_tx_pacing_ms = (uint8_t)v;
            gotchi_ble::setTxNotifyPacingMs(cfg.ble_tx_pacing_ms);
        }
        if (req["ble_thinking_timeout_ms"].is<uint32_t>() || req["ble_thinking_timeout_ms"].is<int>()) {
            uint32_t v = req["ble_thinking_timeout_ms"].as<uint32_t>();
            if (v < BLE_THINKING_MIN_TIMEOUT_MS) v = BLE_THINKING_MIN_TIMEOUT_MS;
            if (v > BLE_THINKING_MAX_TIMEOUT_MS) v = BLE_THINKING_MAX_TIMEOUT_MS;
            cfg.ble_thinking_timeout_ms = v;
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
        size_t cam_frame_bytes = 0;
        int rssi_active_dbm = 0;
        {
            ScopedCpuBoost boost;
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
            cam_frame_bytes = (fb && fb->buf) ? fb->len : 0;
            rssi_active_dbm = wifi_conn ? (int)WiFi.RSSI() : 0;
            if (fb) {
                esp_camera_fb_return(fb);
            }
            standbyCameraAndNotifyRgb();
            gotchi_audio::muteSpeaker();
        }

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
        s_screen_sleeping = false;
        s_last_activity_ts = millis();
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

        bool auto_speak = req["auto_speak"] | false;
        if (auto_speak && (s_last_drawn_voice_state == UiState::IDLE) && isPendingAnswerValid()) {
            s_auto_speak_pending = true;
            res["auto_speak"] = true;
        }

        {
            ScopedCpuBoost boost;
            if (chime.length() > 0) {
                gotchi_audio::playChime(chime.c_str());
            }
            showIdle();
        }
    } else if (cmd == "led_test") {
        if (req["r"].is<int>()) {
            gotchi_camera::standby();
            int r = req["r"].as<int>();
            int g = req["g"] | 0;
            int b = req["b"] | 0;
            uint8_t rgb_pin = req["rgb_pin"] | 48;
            gpio_hold_dis(GPIO_NUM_21);
            pinMode(21, OUTPUT);
            digitalWrite(21, LOW);
            gpio_hold_en(GPIO_NUM_21);
            gotchi_camera::writeStandbyRgbLed((uint8_t)r, (uint8_t)g, (uint8_t)b, rgb_pin);
            s_led_test_override = !(r == 0 && g == 0 && b == 0);
            res["r"] = r;
            res["g"] = g;
            res["b"] = b;
            res["rgb_pin"] = rgb_pin;
        } else {
            String g21 = req["gpio21"] | "low";
            gpio_hold_dis(GPIO_NUM_21);
            pinMode(21, OUTPUT);
            digitalWrite(21, LOW);
            gpio_hold_en(GPIO_NUM_21);
            s_led_test_override = false;
            res["gpio21"] = g21;
        }
        uint32_t d2_float_ms = req["d2_float_ms"] | 0;
        if (d2_float_ms > 0) {
            if (d2_float_ms > 15000) d2_float_ms = 15000;
            pinMode(PIN_BTN_TOP_PTT, INPUT_PULLDOWN);
            delay(d2_float_ms);
            pinMode(PIN_BTN_TOP_PTT, INPUT_PULLUP);
            s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        }
        res["d2_float_ms"] = d2_float_ms;
        notifyRgbTask();
    } else if (cmd == "turn_abort") {
        String reason = req["error"] | "turn_abort";
        gotchi_log::RingLogger.warn("ble", (String("BLE relay turn_abort received: ") + reason).c_str());
        gotchi_ble::clearIncomingQueue();
        s_thinking_entered_ts = 0;
        if (!s_voice_busy && s_last_drawn_voice_state != UiState::IDLE) {
            showIdle();
        }
        res["state"] = "idle";
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
    if (s_last_drawn_voice_state != UiState::IDLE || s_idle_entered_ts == 0) {
        s_idle_entered_ts = millis();
    }
    s_screen_sleeping = false;
    setVoiceUiState(UiState::IDLE);
    samplePowerAndBatteryTelemetry();
    GotchiUIState idle_ui;
    attachPowerToUi(idle_ui);
    s_last_drawn_battery_pct = s_battery_pct;
    s_last_drawn_wall_power = isExternalPowerConnected();
    s_last_drawn_wifi_active = idle_ui.wifi_active;
    s_last_drawn_ble_active = idle_ui.ble_active;
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

static void enterScreenSleep() {
    GotchiUIState sleep_ui;
    sleep_ui.state = UiState::IDLE;
    sleep_ui.sleeping = true;
    sleep_ui.blink = true;
    sleep_ui.skin = cfg.skin;
    sleep_ui.status_text = "SLEEPING (PRESS PTT)";
    attachPowerToUi(sleep_ui);
    panel.draw(sleep_ui, false);
    s_screen_sleeping = true;
}

static uint8_t s_voice_anim_frame = 0;

static void onVoiceState(const GotchiUIState& ui) {
    s_led_test_override = false;
    GotchiUIState enriched = ui;
    if (enriched.state == UiState::TRANSCRIBING || enriched.state == UiState::SYNTHESIZING) {
        enriched.state = UiState::THINKING;
    }
    if (enriched.state != s_last_drawn_voice_state) {
        s_voice_anim_frame = 0;
    } else {
        s_voice_anim_frame++;
    }
    enriched.anim_frame = s_voice_anim_frame;
    setVoiceUiState(enriched.state);
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
    if ((s_last_drawn_voice_state == UiState::THINKING ||
         s_last_drawn_voice_state == UiState::TRANSCRIBING ||
         s_last_drawn_voice_state == UiState::SYNTHESIZING) &&
        s_thinking_entered_ts != 0 &&
        isBleRelayOrPhoneAudioMode() &&
        (uint32_t)(millis() - s_thinking_entered_ts) >= cfg.ble_thinking_timeout_ms) {
        s_ble_thinking_timed_out = true;
        s_thinking_entered_ts = 0;
        gotchi_log::RingLogger.warn("ble", "THINKING timeout in BLE relay / ble_phone mode; recovering to IDLE");
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
        standbyCameraAndNotifyRgb();
        return false;
    }
    memcpy(s_staged_jpeg_bufs[slot_idx], fb->buf, fb->len);
    s_staged_jpeg_lens[slot_idx] = fb->len;
    esp_camera_fb_return(fb);
    standbyCameraAndNotifyRgb();
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
    s_deferred_wait_active = false;
    ScopedVoiceBusy voice_busy_guard;
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

static void wakeWiFiFromLowPower() {
    if (s_wifi_disabled_for_ble_lp && cfg.wifi_ssid.length() > 0) {
        WiFi.mode(WIFI_STA);
        WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_psk.c_str());
        WiFi.setSleep(WIFI_PS_MAX_MODEM);
        s_wifi_disabled_for_ble_lp = false;
    }
}

static void dispatchVoiceOrSnap(const gotchi_audio::WavView& wav, bool include_staged_snap, bool is_standalone_snap) {
    GotchiUIStateWithAsync ui;
    attachPowerToUi(ui);
    ui.skin = cfg.skin;

    wakeWiFiFromLowPower();

    if (WiFi.status() != WL_CONNECTED && !gotchi_ble::isBlePeerConnected()) {
        uint32_t t_assoc0 = millis();
        while (WiFi.status() != WL_CONNECTED && (millis() - t_assoc0 < 6000)) {
            delay(20);
        }
    }

    if (WiFi.status() != WL_CONNECTED && !gotchi_ble::isBlePeerConnected()) {
        ui.state = UiState::OFFLINE;
        ui.status_text = "NO WIFI";
        setVoiceUiState(UiState::OFFLINE);
        panel.draw(ui);
        delay(2000);
        showIdle();
        return;
    }

    ui.state = UiState::THINKING;
    ui.status_text = is_standalone_snap ? "SENDING SNAP..." : "THINKING...";
    onVoiceState(ui);
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

    s_ble_thinking_timed_out = false;
    gotchi_voice::InteractResult result = gotchi_voice::InteractResult::DONE;
    bool ok = gotchi_voice::interact(
        wav, cfg.voice_url, cfg.node_id, ui, onVoiceState, cfg.bearer_token,
        img_ptr, pollVoiceAbort, &result, cfg.audio_out_mode
    );
    if (s_ble_thinking_timed_out) {
        s_ble_thinking_timed_out = false;
        gotchi_ble::clearIncomingQueue();
        gotchi_log::RingLogger.warn("ble", "Recovered from stuck BLE THINKING turn; returning to IDLE");
        ui.state = UiState::ERROR;
        ui.error_code = "ble_timeout";
        ui.status_text = "BLE TIMEOUT";
        setVoiceUiState(UiState::ERROR);
        panel.draw(ui);
        delay(1200);
        showIdle();
        return;
    }
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
        setVoiceUiState(UiState::IDLE);
        panel.draw(ui);
        delay(1200);
        showIdle();
        return;
    }

    // Reply ended in '?' and finished on the local speaker: loop() reopens the mic (reply_reopen_mic).
    if (ok && result == gotchi_voice::InteractResult::DONE && ui.expects_reply &&
        ui.audio_bytes_received > 0 && cfg.reply_reopen_mic && cfg.audio_out_mode == "speaker") {
        s_reopen_mic_pending = true;
    }

    if (ui.status_text.indexOf("DEFER") >= 0 ||
        ui.reply.indexOf("[[defer]]") >= 0 ||
        ui.reply.indexOf("looking into that") >= 0 ||
        ui.reply.indexOf("look into that") >= 0) {
        ui.async_handoff = true;
    }

    if (ui.async_handoff) {
        s_deferred_wait_active = true;
        s_deferred_wait_start_ts = millis();
        s_last_deferred_poll_ts = millis();
        s_deferred_next_poll_ms = 30000;
        gotchi_log::RingLogger.info("voice", "Async handoff: activated 11-min duty-cycled deferred wait window");
    }

    if (!ok) {
        delay(2000);
    }
    if (s_reopen_mic_pending) {
        setVoiceUiState(UiState::IDLE);
    } else {
        showIdle();
    }
}

// Handles bottom camera button (`BTN_BOT`) in IDLE (PRD F14 & F20):
//   - Short press (< `cam_long_press_ms`, default 700ms): captures 1 snapshot on release,
//     immediately enters hands-free listening (`LISTENING (+SNAP)...`), and auto-describes
//     the photo if no speech is heard within 2 seconds.
//   - Long press (>= `cam_long_press_ms`): captures a burst of 5 photos over 3 seconds,
//     then enters the same 2-second auto-listen / auto-describe flow.
static void runPttTurn(bool auto_listen_for_snap = false, bool reply_followup = false);
static void playPendingAnswer();

static void handleCameraButtonIdle() {
    s_deferred_wait_active = false;
    ScopedVoiceBusy voice_busy_guard;
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

static void playPendingAnswer() {
    if (!isPendingAnswerValid()) return;
    ScopedVoiceBusy voice_busy_guard;
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
    onVoiceState(ui);

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
static void runPttTurn(bool auto_listen_for_snap, bool reply_followup) {
    s_deferred_wait_active = false;
    if (!auto_listen_for_snap && !reply_followup && isPendingAnswerValid()) {
        uint32_t t0 = millis();
        bool released = false;
        while (millis() - t0 < 500) {
            if (digitalRead(PIN_BTN_TOP_PTT) == HIGH) {
                released = true;
                break;
            }
            delay(5);
        }
        if (released) {
            playPendingAnswer();
            return;
        }
        // Still held at >= 500ms: skip queued answer and fall through into recording a new voice turn
        s_pending_answer_ready = false;
        s_pending_answer_id = "";
        s_notify_status_text = "";
        gotchi_log::RingLogger.info("voice", "Hold-to-skip: skipped pending answer, opening mic");
    }

    ScopedVoiceBusy voice_busy_guard;
    GotchiUIState ui;
    attachPowerToUi(ui);
    ui.skin = cfg.skin;

    static volatile bool     s_ptt_release_watch = false;
    static volatile uint32_t s_ptt_release_ts    = 0;
    static volatile bool     s_ptt_tap_stop_watch = false;
    static volatile uint32_t s_ptt_tap_stop_ts   = 0;
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
        if (s_ptt_tap_stop_watch && s_ptt_tap_stop_ts == 0 && digitalRead(PIN_BTN_TOP_PTT) == LOW) {
            s_ptt_tap_stop_ts = millis() ? millis() : 1;
        }
    });

    uint32_t press_start_ts = millis();
    s_ptt_release_ts     = 0;
    s_ptt_high_streak    = 0;
    s_ptt_release_watch  = !auto_listen_for_snap;
    s_ptt_tap_stop_watch = false;
    s_ptt_tap_stop_ts    = 0;

    // Play start chime BEFORE opening the mic buffer so speaker output is never recorded into WAV
    if (!auto_listen_for_snap) {
        gotchi_audio::playChime("ptt_start");
        if (s_ptt_release_ts == 0 && btnStable(PIN_BTN_TOP_PTT, false)) {
            s_ptt_release_ts = millis();
        }
    }

    if (!gotchi_audio::beginPttRecording(!reply_followup)) {
        s_ptt_release_watch = false;
        panel.setBusyPollHook(nullptr);
        ui.state = UiState::ERROR;
        ui.error_code = "mic";
        ui.status_text = "MIC ERROR";
        panel.draw(ui);
        delay(1500);
        showIdle();
        return;
    }
    size_t pre_draw_cursor_samples = gotchi_audio::getRecordedSamples();

    // Start Core 0 DMA drain task during LISTENING screen refresh so zero speech is dropped
    bool async_running = gotchi_audio::startAsyncPttTask();
    ui.state = UiState::LISTENING;
    ui.status_text = s_staged_jpeg_active ? "LISTENING (+SNAP)..." : "LISTENING...";
    onVoiceState(ui);
    uint32_t last_anim_step_ms = millis();
    size_t draw_end_samples = gotchi_audio::getRecordedSamples();
    // Counter 1 (arming live silence cutoff): scan strictly after the LISTENING screen draw
    // so button-handling noise during the e-paper refresh never arms an early silence cutoff.
    size_t post_draw_cursor_samples = draw_end_samples;
    size_t last_voiced_sample = draw_end_samples;
    uint32_t post_draw_voiced_frames = 0;
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
    bool released_once = auto_listen_for_snap || reply_followup || (first_release_ts != 0);
    // In hybrid mode, any button release during the chime/screen draw or within tap_latch_ms (500ms)
    // after LISTENING appears latches into hands-free Tap-to-Talk.
    bool latched_tap_mode = auto_listen_for_snap || reply_followup ||
                            (cfg.input_mode == "tap") ||
                            (cfg.input_mode == "hybrid" && released_once &&
                             ((int32_t)(first_release_ts - (t0 + cfg.tap_latch_ms)) <= 0));
    bool start_live_session = false;
    const uint32_t initial_timeout_ms = reply_followup ? cfg.reply_reopen_window_ms
                                      : (auto_listen_for_snap ? 2000UL : VAD_INITIAL_TIMEOUT_MS);

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

        uint32_t now = millis();
        if (now - last_anim_step_ms >= 650) {
            if (!released_once) {
                s_ptt_release_watch = true;
            } else {
                s_ptt_tap_stop_watch = true;
            }
            onVoiceState(ui);
            s_ptt_release_watch = false;
            s_ptt_tap_stop_watch = false;
            last_anim_step_ms = millis();
        }

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
            if (s_ptt_release_ts != 0 || btnStable(PIN_BTN_TOP_PTT, false)) {
                released_once = true;
                first_release_ts = (s_ptt_release_ts != 0) ? s_ptt_release_ts : millis();
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
            if ((released_once && s_ptt_tap_stop_ts != 0) || (digitalRead(PIN_BTN_TOP_PTT) == LOW && btnStable(PIN_BTN_TOP_PTT, true))) {
                uint32_t tap_ts = s_ptt_tap_stop_ts != 0 ? s_ptt_tap_stop_ts : millis();
                s_ptt_tap_stop_ts = 0;
                uint32_t since_release = (auto_listen_for_snap || reply_followup) ? (cfg.double_tap_ms + 1)
                                                                                  : (tap_ts - first_release_ts);
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

        // Counter 1: Scan non-overlapping 20ms (320-sample) frames strictly after the LISTENING
        // screen draw to arm the live silence cutoff (8 post-draw frames = 160ms), keeping
        // pre-draw button handling noise from arming an early cutoff.
        gotchi_audio::scanSpeechFrames(post_draw_cursor_samples, vad_thresh, post_draw_voiced_frames, last_voiced_sample);
        bool cutoff_armed = (post_draw_voiced_frames >= 8);
        if (cutoff_armed) {
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
            if (cutoff_armed && (silence_ms >= cfg.vad_silence_ms)) {
                break; // User finished speaking after the draw and paused for vad_silence_ms
            }
            if (!cutoff_armed && (elapsed >= initial_timeout_ms)) {
                break; // No post-draw cutoff armed within initial window; post-loop full scan decides upload
            }
        }

        delay(5);
    }

    gotchi_audio::stopAsyncPttTask();
    panel.setBusyPollHook(nullptr);
    gotchi_audio::scanSpeechFrames(post_draw_cursor_samples, vad_thresh, post_draw_voiced_frames, last_voiced_sample);
    // Counter 2 (drop-or-upload decision): Scan the full recording from pre_draw_cursor_samples
    // so a quick "yes" spoken during the LISTENING screen refresh still uploads in both Tap and Hold modes.
    uint32_t full_voiced_frames = 0;
    size_t full_last_voiced = pre_draw_cursor_samples;
    gotchi_audio::scanSpeechFrames(pre_draw_cursor_samples, vad_thresh, full_voiced_frames, full_last_voiced);
    if (post_draw_voiced_frames >= 8 || full_voiced_frames >= 8) {
        speech_heard = true;
    }
    gotchi_audio::WavView wav = gotchi_audio::finishPttRecording();
    char fin_msg[112];
    snprintf(fin_msg, sizeof(fin_msg),
             "PTT recording finished (samples=%u post_draw_frames=%u full_frames=%u speech=%d)",
             (unsigned)gotchi_audio::getRecordedSamples(),
             (unsigned)post_draw_voiced_frames,
             (unsigned)full_voiced_frames,
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

    // Follow-up window after a '?' reply timed out with no speech: close quietly, one refresh.
    if (reply_followup && !speech_heard && !s_staged_jpeg_active) {
        showIdle();
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
        setVoiceUiState(UiState::IDLE);
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
//     ESP32-S3 Wi-Fi radio completely (`WiFi.mode(WIFI_OFF)`),
//     and keeps the e-paper display & MAX98357A speaker in 0 uA / <1 uA deep sleep.
//   - Duty-Cycled Acoustic Sniff: Probes a 16 ms (256-sample) PDM slice in internal SRAM every
//     120 ms while silent. When ambient speech crosses `cfg.vad_rms_lsb`, transitions into
//     silent segment capture (`up to journal_seg_max_ms = 30s`, closing after `journal_silence_ms = 2s`
//     of trailing silence) and uploads via BLE (`mode: "journal"`) or Wi-Fi without waking the
//     speaker or e-paper screen. Any button press (`BTN_TOP` / `BTN_BOT`) immediately preempts
//     passive journal capture with zero user latency.
// ============================================================================
static bool checkGatewayPendingOnBootAndIdle(bool force);

static void manageBleLowPowerWiFi() {
    uint32_t now = millis();

    // 1. Duty-Cycled Deferred Burst Polling
    if (s_deferred_wait_active && !s_voice_busy && (s_cpu_boost_depth <= 0)) {
        if ((uint32_t)(now - s_deferred_wait_start_ts) >= DEFERRED_WAIT_MAX_MS) {
            s_deferred_wait_active = false;
        } else {
            uint32_t interval_ms = (s_deferred_next_poll_ms > 0)
                ? s_deferred_next_poll_ms
                : (((uint32_t)(now - s_deferred_wait_start_ts) < 120000UL) ? 30000UL : 120000UL);

            if ((uint32_t)(now - s_last_deferred_poll_ts) >= interval_ms) {
                s_last_deferred_poll_ts = now;
                if (s_wifi_disabled_for_ble_lp) {
                    if (cfg.wifi_ssid.length() > 0) {
                        WiFi.mode(WIFI_STA);
                        WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_psk.c_str());
                        uint32_t t0 = millis();
                        while (WiFi.status() != WL_CONNECTED && (millis() - t0 < 3500)) {
                            delay(20);
                        }
                    }
                    bool answered = false;
                    if (WiFi.status() == WL_CONNECTED) {
                        answered = checkGatewayPendingOnBootAndIdle(true);
                    }
                    if (!answered) {
                        WiFi.disconnect(true);
                        WiFi.mode(WIFI_OFF);
                        s_wifi_disabled_for_ble_lp = true;
                    } else {
                        s_deferred_wait_active = false;
                        s_last_activity_ts = millis();
                        s_wifi_disabled_for_ble_lp = false;
                        WiFi.setSleep(WIFI_PS_MAX_MODEM);
                    }
                } else if (WiFi.status() == WL_CONNECTED) {
                    bool answered = checkGatewayPendingOnBootAndIdle(true);
                    if (answered) {
                        s_deferred_wait_active = false;
                        s_last_activity_ts = millis();
                    }
                }
            }
        }
    }

    // Turn off the Wi-Fi radio (`WIFI_OFF`) when connected over BLE if either:
    //   1. `cfg.ble_disable_wifi` is true (immediate BLE-only mode), OR
    //   2. `cfg.ble_wifi_idle_off_ms > 0` and the badge has been idle with no button/turn/RPC activity
    //      for >= `cfg.ble_wifi_idle_off_ms` (default 5 minutes).
    //   3. Running on battery (!isExternalPowerConnected()) and idle for >= cfg.battery_wifi_idle_off_ms (default 2 minutes).
    // Any button press or BLE disconnect automatically wakes Wi-Fi STA back up in WIFI_PS_MAX_MODEM mode.
    bool ble_idle_expired = (cfg.ble_wifi_idle_off_ms > 0) &&
                            !s_voice_busy &&
                            (s_cpu_boost_depth <= 0) &&
                            ((uint32_t)(millis() - s_last_activity_ts) >= cfg.ble_wifi_idle_off_ms);
    bool want_ble_lp = gotchi_ble::isBlePeerConnected() && (cfg.ble_disable_wifi || ble_idle_expired);

    bool bat_idle_expired = !isExternalPowerConnected() &&
                            (cfg.battery_wifi_idle_off_ms > 0) &&
                            !s_voice_busy &&
                            (s_cpu_boost_depth <= 0) &&
                            ((uint32_t)(millis() - s_last_activity_ts) >= cfg.battery_wifi_idle_off_ms);
    bool want_wifi_off = want_ble_lp || bat_idle_expired;

    if (want_wifi_off && !s_wifi_disabled_for_ble_lp) {
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
        s_wifi_disabled_for_ble_lp = true;
        gotchi_log::RingLogger.info("power", want_ble_lp ? "BLE connected: WiFi_OFF low-power mode active"
                                                        : "Battery idle: WiFi_OFF low-power mode active");
    } else if (!want_wifi_off && s_wifi_disabled_for_ble_lp && !s_deferred_wait_active) {
        s_wifi_disabled_for_ble_lp = false;
        if (cfg.wifi_ssid.length() > 0) {
            WiFi.mode(WIFI_STA);
            WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_psk.c_str());
            WiFi.setSleep(s_cpu_boost_depth > 0 ? WIFI_PS_MIN_MODEM : WIFI_PS_MAX_MODEM);
        }
        gotchi_log::RingLogger.info("power", "BLE disconnected or WiFi re-enabled: restored Wi-Fi STA");
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

static uint32_t s_last_gateway_poll_ts = 0;
static constexpr uint32_t GATEWAY_POLL_INTERVAL_MS = 180000; // 3 minutes

static bool checkGatewayPendingOnBootAndIdle(bool force = false) {
    if (WiFi.status() != WL_CONNECTED) return false;
    uint32_t now = millis();
    if (!force && (now - s_last_gateway_poll_ts < GATEWAY_POLL_INTERVAL_MS)) {
        return false;
    }
    s_last_gateway_poll_ts = now;

    String notify_json;
    if (!gotchi_voice::pollPendingNotification(cfg.voice_url, cfg.node_id, cfg.bearer_token, &notify_json)) {
        return false;
    }
    if (notify_json.length() == 0) {
        return false;
    }

    JsonDocument req;
    DeserializationError err = deserializeJson(req, notify_json);
    if (err) {
        return false;
    }

    if (req["poll_after_s"].is<uint32_t>()) {
        uint32_t pas = req["poll_after_s"].as<uint32_t>();
        if (pas < 10) pas = 10;
        if (pas > 180) pas = 180;
        s_deferred_next_poll_ms = pas * 1000UL;
    }

    String chime = req["chime"] | "ready";
    String msg = req["status_text"] | (req["message"] | "BUILD READY");
    msg.trim();
    if (msg.length() > 22) msg = msg.substring(0, 22);
    s_notify_status_text = msg;
    s_notify_expire_ts = millis() + NOTIFY_STATUS_TTL_MS;
    s_screen_sleeping = false;
    s_last_activity_ts = millis();

    bool has_pending_audio = req["has_pending_audio"] | false;
    String answer_id = req["answer_id"].is<const char*>() ? req["answer_id"].as<String>() : String("");
    if (has_pending_audio || answer_id.length() > 0) {
        s_pending_answer_ready = true;
        s_pending_answer_expire_ts = millis() + PENDING_ANSWER_TTL_MS;
        s_pending_answer_id = answer_id;
        s_deferred_wait_active = false;
        if (chime.length() == 0) {
            chime = "ready";
        }
    }

    bool auto_speak = req["auto_speak"] | false;
    if (auto_speak && (s_last_drawn_voice_state == UiState::IDLE) && isPendingAnswerValid()) {
        s_auto_speak_pending = true;
    }

    if (chime.length() > 0) {
        gotchi_audio::playChime(chime.c_str());
    }
    showIdle();

    return true;
}

static void latchUserLedOff() {
    // Driving GPIO 21 LOW turns the onboard user LED off on the V1 hardware.
    // Drive GPIO 21 LOW and latch pad hold across deep sleep so it stays completely off.
    gotchi_camera::writeStandbyRgbLed(0, 0, 0);
    gpio_hold_dis(GPIO_NUM_21);
    pinMode(21, OUTPUT);
    digitalWrite(21, LOW);
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
    s_last_drawn_battery_pct = s_battery_pct;
    s_last_drawn_wall_power = isExternalPowerConnected();
    s_last_drawn_wifi_active = boot_ui.wifi_active;
    s_last_drawn_ble_active = boot_ui.ble_active;
    s_last_activity_ts = millis();
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
        s_last_activity_ts = millis();
        String resp = handleJsonRpc(httpServer.arg("plain"));
        httpServer.send(200, "application/json", resp);
    });
    httpServer.on("/api/ota", HTTP_POST, []() {
        bool ok = !Update.hasError();
        httpServer.send(ok ? 200 : 500, "application/json", ok ? "{\"ok\":true,\"status\":\"rebooting\"}" : "{\"ok\":false}");
        delay(200);
        if (ok) {
            ESP.restart();
        } else {
            if (!s_wifi_disabled_for_ble_lp && WiFi.status() == WL_CONNECTED) WiFi.setSleep(WIFI_PS_MAX_MODEM);
        }
    }, []() {
        if (cfg.bearer_token.length() > 0) {
            String auth = httpServer.header("Authorization");
            String expected = "Bearer " + cfg.bearer_token;
            if (!constantTimeEquals(auth, expected)) return;
        }
        HTTPUpload& up = httpServer.upload();
        if (up.status == UPLOAD_FILE_START) {
            if (WiFi.status() == WL_CONNECTED) WiFi.setSleep(WIFI_PS_MIN_MODEM);
            Update.abort();
            Update.begin(UPDATE_SIZE_UNKNOWN);
        } else if (up.status == UPLOAD_FILE_WRITE) {
            Update.write(up.buf, up.currentSize);
        } else if (up.status == UPLOAD_FILE_END) {
            Update.end(true);
        } else if (up.status == UPLOAD_FILE_ABORTED) {
            Update.abort();
            if (!s_wifi_disabled_for_ble_lp && WiFi.status() == WL_CONNECTED) WiFi.setSleep(WIFI_PS_MAX_MODEM);
        }
    });
    static const char* kCollectedHeaders[] = {"Authorization"};
    httpServer.collectHeaders(kCollectedHeaders, 1);
    httpServer.begin();
    latchUserLedOff();
    xTaskCreatePinnedToCore(
        rgbStatusTaskFn,
        "gotchi_rgb_status",
        3072,
        nullptr,
        tskIDLE_PRIORITY + 1,
        &s_rgb_task_handle,
        1
    );
    s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
    s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
    if (!s_btn_top_armed || !s_btn_bot_armed) {
        char btn_warn[96];
        snprintf(btn_warn, sizeof(btn_warn),
                 "Button held LOW at startup (top_armed=%d bot_armed=%d); disarmed until released HIGH",
                 (int)s_btn_top_armed, (int)s_btn_bot_armed);
        gotchi_log::RingLogger.warn("boot", btn_warn);
    }
    if (wifi_up || WiFi.status() == WL_CONNECTED) {
        checkGatewayPendingOnBootAndIdle(true);
        WiFi.setSleep(WIFI_PS_MAX_MODEM);
    }
}

static uint32_t s_last_idle_bat_log_ts = 0;
static constexpr uint32_t IDLE_BAT_LOG_INTERVAL_MS = 300000; // 5 minutes

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
        s_last_activity_ts = millis();
        s_deferred_wait_active = false;
        wakeWiFiFromLowPower();
        ScopedCpuBoost boost;
        runLiveSession();
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
        return;
    }

    // Reply ended in '?': reopen the mic once its playback has finished, in the same session.
    // Skipped when an async answer or auto-speak is queued, so pending answers keep their order.
    if (s_reopen_mic_pending) {
        s_reopen_mic_pending = false;
        if (!s_voice_busy && s_last_drawn_voice_state == UiState::IDLE &&
            !s_auto_speak_pending && !isPendingAnswerValid()) {
            ScopedCpuBoost boost;
            gotchi_voice::setContinueSession(true);
            runPttTurn(false, true);
            gotchi_voice::setContinueSession(false);
            s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        }
        return;
    }

    // Auto-speak hands-free answer when idle
    if (!s_voice_busy && s_last_drawn_voice_state == UiState::IDLE && s_auto_speak_pending) {
        s_auto_speak_pending = false;
        ScopedCpuBoost boost;
        playPendingAnswer();
        return;
    }

    // Periodically poll gateway for pending answers/notifications when idle on Wi-Fi
    if (!s_voice_busy && s_last_drawn_voice_state == UiState::IDLE && WiFi.status() == WL_CONNECTED) {
        checkGatewayPendingOnBootAndIdle(false);
    }

    // Guard against stuck THINKING state in loop() when no voice turn is active (BLE relay / ble_phone recovery)
    if (!s_voice_busy &&
        (s_last_drawn_voice_state == UiState::THINKING ||
         s_last_drawn_voice_state == UiState::TRANSCRIBING ||
         s_last_drawn_voice_state == UiState::SYNTHESIZING) &&
        s_thinking_entered_ts != 0 &&
        isBleRelayOrPhoneAudioMode() &&
        (uint32_t)(millis() - s_thinking_entered_ts) >= cfg.ble_thinking_timeout_ms) {
        gotchi_log::RingLogger.warn("ble", "Loop BLE THINKING guard triggered; resetting stuck state to IDLE");
        gotchi_ble::clearIncomingQueue();
        showIdle();
    }

    // Poll incoming BLE CONTROL (0x07) frames when not in an active turn so notifications & turn_abort recovery work
    if (!s_voice_busy && gotchi_ble::isBlePeerConnected()) {
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
        s_last_activity_ts = millis();
        s_notify_status_text = "";
        s_deferred_wait_active = false;
        wakeWiFiFromLowPower();
        ScopedCpuBoost boost;
        runPttTurn();
        s_btn_top_armed = (digitalRead(PIN_BTN_TOP_PTT) == HIGH);
        return;
    }

    // Bottom button (camera only, F14/F20): short press = 1 photo on release; long press = 5-photo burst over 3s
    if (s_btn_bot_armed && btnStable(PIN_BTN_BOT_MOD, true)) {
        s_btn_bot_armed = false;
        s_last_activity_ts = millis();
        s_deferred_wait_active = false;
        wakeWiFiFromLowPower();
        ScopedCpuBoost boost;
        handleCameraButtonIdle();
        s_btn_bot_armed = (digitalRead(PIN_BTN_BOT_MOD) == HIGH);
        return;
    }

    // If staged camera snapshot(s) weren't paired with a voice prompt within `snap_window_ms` (3s),
    // dispatch standalone with `default_image_prompt` and no `audio` part.
    if (s_staged_jpeg_active && (millis() - s_staged_jpeg_ts >= cfg.snap_window_ms)) {
        s_last_activity_ts = millis();
        s_deferred_wait_active = false;
        wakeWiFiFromLowPower();
        ScopedCpuBoost boost;
        gotchi_audio::WavView no_audio_wav{nullptr, 0};
        dispatchVoiceOrSnap(no_audio_wav, true, true);
        return;
    }

    bool had_unplug_reseed = s_battery_reseed;
    samplePowerAndBatteryTelemetry();
    bool unplug_settle_done = had_unplug_reseed && !s_battery_reseed;

    manageBleLowPowerWiFi();

    if (s_last_drawn_voice_state == UiState::IDLE) {
        if (cfg.screen_sleep_ms > 0 && !s_screen_sleeping &&
            (uint32_t)(millis() - s_last_activity_ts) >= cfg.screen_sleep_ms) {
            enterScreenSleep();
        }
        bool wifi_active = !s_wifi_disabled_for_ble_lp && (WiFi.status() == WL_CONNECTED);
        bool ble_active = gotchi_ble::isBlePeerConnected();
        if (!s_screen_sleeping) {
            if (isExternalPowerConnected() != s_last_drawn_wall_power) {
                showIdle();
            } else if (unplug_settle_done) {
                showIdle();
            } else if (wifi_active != s_last_drawn_wifi_active || ble_active != s_last_drawn_ble_active) {
                showIdle();
            }
        }
    }

    uint32_t now_idle_log = millis();
    if (s_last_drawn_voice_state == UiState::IDLE &&
        (s_last_idle_bat_log_ts == 0 || (now_idle_log - s_last_idle_bat_log_ts >= IDLE_BAT_LOG_INTERVAL_MS))) {
        s_last_idle_bat_log_ts = now_idle_log;
        char pwr_msg[144];
        snprintf(pwr_msg, sizeof(pwr_msg),
                 "idle_power bat_mv=%u raw_mv=%u d4_mv=%u d2_mv=%u bat_pct=%d cpu_mhz=%u rssi=%d wifi=%s ble=%s",
                 (unsigned)s_battery_mv, (unsigned)(s_last_d4_mv * 2),
                 (unsigned)s_last_d4_mv, (unsigned)s_last_d2_mv,
                 (int)s_battery_pct, (unsigned)getCpuFrequencyMhz(),
                 (WiFi.status() == WL_CONNECTED) ? (int)WiFi.RSSI() : 0,
                 (s_wifi_disabled_for_ble_lp || WiFi.getMode() == WIFI_MODE_NULL) ? "off" : "on",
                 gotchi_ble::isBlePeerConnected() ? "connected" : "adv");
        gotchi_log::RingLogger.info("power", pwr_msg);
        if (!s_screen_sleeping && s_battery_pct != s_last_drawn_battery_pct) {
            showIdle();
        }
    }
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
    applyPrerollPolicy();
    if (cfg.journal_mode) {
        pollJournalMode();
        delay(10);
    } else if (gotchi_audio::isPrerollEnabled()) {
        gotchi_audio::pumpPreroll();
        delay(2);
    } else {
        bool can_light_sleep = !isExternalPowerConnected() &&
                               !gotchi_ble::isBlePeerConnected() &&
                               (s_last_drawn_voice_state == UiState::IDLE) &&
                               !cfg.journal_mode &&
                               !gotchi_audio::isPrerollEnabled() &&
                               !s_staged_jpeg_active &&
                               !panel.isDrawing() &&
                               (s_wifi_disabled_for_ble_lp || WiFi.getMode() == WIFI_MODE_NULL) &&
                               !s_voice_busy &&
                               (s_cpu_boost_depth <= 0) &&
                               !s_enter_live_pending &&
                               !s_reopen_mic_pending &&
                               !s_auto_speak_pending &&
                               (s_battery_pct >= 0) &&
                               (digitalRead(PIN_BTN_TOP_PTT) == HIGH) &&
                               (digitalRead(PIN_BTN_BOT_MOD) == HIGH);

        if (can_light_sleep) {
            uint32_t now = millis();
            uint32_t sleep_ms = 60000; // 60s relaxed battery telemetry & sleep interval

            // Coalesced battery ADC telemetry deadline
            if (now - s_last_power_sample_ts < POWER_TELEMETRY_IDLE_INTERVAL_MS) {
                uint32_t rem_telemetry = POWER_TELEMETRY_IDLE_INTERVAL_MS - (now - s_last_power_sample_ts);
                if (rem_telemetry < sleep_ms) sleep_ms = rem_telemetry;
            }

            // Idle power log deadline
            if (now - s_last_idle_bat_log_ts < IDLE_BAT_LOG_INTERVAL_MS) {
                uint32_t rem_log = IDLE_BAT_LOG_INTERVAL_MS - (now - s_last_idle_bat_log_ts);
                if (rem_log < sleep_ms) sleep_ms = rem_log;
            }

            // Screen sleep deadline
            if (!s_screen_sleeping && cfg.screen_sleep_ms > 0) {
                uint32_t elapsed_active = now - s_last_activity_ts;
                uint32_t rem_screen_sleep = (elapsed_active < cfg.screen_sleep_ms)
                    ? (cfg.screen_sleep_ms - elapsed_active)
                    : 10;
                if (rem_screen_sleep < sleep_ms) sleep_ms = rem_screen_sleep;
            }

            // Deferred answer poll deadline
            if (s_deferred_wait_active) {
                uint32_t interval_ms = (s_deferred_next_poll_ms > 0)
                    ? s_deferred_next_poll_ms
                    : (((uint32_t)(now - s_deferred_wait_start_ts) < 120000UL) ? 30000UL : 120000UL);
                uint32_t elapsed_poll = now - s_last_deferred_poll_ts;
                uint32_t rem_poll = (elapsed_poll < interval_ms) ? (interval_ms - elapsed_poll) : 10;
                if (rem_poll < sleep_ms) sleep_ms = rem_poll;
            }

            // Notification status LED blink edge / expiry deadline
            if (s_notify_status_text.length() > 0) {
                int32_t rem_expire = (int32_t)(s_notify_expire_ts - now);
                if (rem_expire > 0) {
                    uint32_t t = now % 4000;
                    uint32_t next_edge_ms = 4000 - t;
                    if (t < 120) {
                        next_edge_ms = 120 - t;
                    } else if (t < 260) {
                        next_edge_ms = 260 - t;
                    } else if (t < 380) {
                        next_edge_ms = 380 - t;
                    }
                    if (next_edge_ms < (uint32_t)rem_expire && next_edge_ms < sleep_ms) {
                        sleep_ms = next_edge_ms;
                    } else if ((uint32_t)rem_expire < sleep_ms) {
                        sleep_ms = (uint32_t)rem_expire;
                    }
                } else {
                    sleep_ms = 10;
                }
            }

            if (sleep_ms < 10) sleep_ms = 10;

            gpio_wakeup_enable((gpio_num_t)PIN_BTN_TOP_PTT, GPIO_INTR_LOW_LEVEL);
            gpio_wakeup_enable((gpio_num_t)PIN_BTN_BOT_MOD, GPIO_INTR_LOW_LEVEL);
            esp_sleep_enable_gpio_wakeup();
            esp_sleep_enable_timer_wakeup((uint64_t)sleep_ms * 1000ULL);

            esp_light_sleep_start();

            gpio_wakeup_disable((gpio_num_t)PIN_BTN_TOP_PTT);
            gpio_wakeup_disable((gpio_num_t)PIN_BTN_BOT_MOD);
            esp_sleep_wakeup_cause_t wake_cause = esp_sleep_get_wakeup_cause();
            if (wake_cause == ESP_SLEEP_WAKEUP_GPIO) {
                s_last_activity_ts = millis();
            }
            updateButtonArming();
            samplePowerAndBatteryTelemetry();
        } else {
            delay(25);
        }
    }
}
