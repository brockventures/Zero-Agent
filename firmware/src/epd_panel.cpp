#include <string.h>
#include "epd_panel.h"
#ifdef ESP32
#include <soc/gpio_struct.h>
#endif

RTC_DATA_ATTR bool     rtc_first_frame_pending = true;
RTC_DATA_ATTR uint8_t  rtc_consecutive_partials = 0;
RTC_DATA_ATTR uint32_t rtc_dirty_pixels = 0;
RTC_DATA_ATTR uint32_t rtc_ms_since_full = 0;
static uint32_t last_full_ms = 0;
static uint32_t s_idle_start_ms = 0;
static uint8_t  s_prev_frame[5000];
static bool     s_has_prev_frame = false;

namespace {

constexpr uint16_t EPD_WIDTH  = 200;
constexpr uint16_t EPD_HEIGHT = 200;
constexpr uint32_t EPD_BUF_BYTES = (EPD_WIDTH * EPD_HEIGHT) / 8; // 5,000 bytes
constexpr uint32_t HEADER_BAR_BYTES = (21 * EPD_WIDTH) / 8;     // 525 bytes (y = 0..20)

inline void spiShiftByteSlow(uint8_t b) {
    for (int i = 7; i >= 0; --i) {
        digitalWrite(PIN_EPD_SCK, LOW);
        digitalWrite(PIN_EPD_MOSI, (b >> i) & 1);
        delayMicroseconds(5);
        digitalWrite(PIN_EPD_SCK, HIGH);
        delayMicroseconds(5);
    }
    digitalWrite(PIN_EPD_SCK, LOW);
}

inline void spiShiftByteFast(uint8_t b) {
#ifdef ESP32
    constexpr uint32_t sck_mask = (1UL << PIN_EPD_SCK);
    constexpr uint32_t mosi_mask = (1UL << PIN_EPD_MOSI);
    for (int i = 7; i >= 0; --i) {
        GPIO.out_w1tc = sck_mask;
        if ((b >> i) & 1) {
            GPIO.out_w1ts = mosi_mask;
        } else {
            GPIO.out_w1tc = mosi_mask;
        }
        GPIO.out_w1ts = sck_mask;
    }
    GPIO.out_w1tc = sck_mask;
#else
    for (int i = 7; i >= 0; --i) {
        digitalWrite(PIN_EPD_SCK, LOW);
        digitalWrite(PIN_EPD_MOSI, (b >> i) & 1);
        delayMicroseconds(1);
        digitalWrite(PIN_EPD_SCK, HIGH);
        delayMicroseconds(1);
    }
    digitalWrite(PIN_EPD_SCK, LOW);
#endif
}

void epdWriteCommand(uint8_t cmd) {
    digitalWrite(PIN_EPD_CS, HIGH);
    digitalWrite(PIN_EPD_DC, LOW);
    delayMicroseconds(100);
    digitalWrite(PIN_EPD_CS, LOW);
    delayMicroseconds(20);
    spiShiftByteSlow(cmd);
    delayMicroseconds(20);
    digitalWrite(PIN_EPD_CS, HIGH);
    delayMicroseconds(50);
    digitalWrite(PIN_EPD_DC, HIGH);
    delayMicroseconds(50);
}

void epdWriteData(uint8_t d) {
    digitalWrite(PIN_EPD_DC, HIGH);
    delayMicroseconds(30);
    digitalWrite(PIN_EPD_CS, LOW);
    delayMicroseconds(10);
    spiShiftByteSlow(d);
    delayMicroseconds(10);
    digitalWrite(PIN_EPD_CS, HIGH);
    delayMicroseconds(20);
}

void epdWriteBuffer(uint8_t cmd, const uint8_t* buf, uint32_t len) {
    epdWriteCommand(cmd);
    digitalWrite(PIN_EPD_DC, HIGH);
    delayMicroseconds(50);
    digitalWrite(PIN_EPD_CS, LOW);
    delayMicroseconds(10);
    for (uint32_t i = 0; i < len; ++i) {
        spiShiftByteFast(buf[i]);
        if ((i & 1023U) == 1023U) delay(0);
    }
    delayMicroseconds(10);
    digitalWrite(PIN_EPD_CS, HIGH);
    delayMicroseconds(50);
}

void epdHardwareReset() {
    pinMode(PIN_EPD_RST, OUTPUT);
    digitalWrite(PIN_EPD_RST, HIGH);
    delay(20);
    digitalWrite(PIN_EPD_RST, LOW);
    delay(5);
    digitalWrite(PIN_EPD_RST, HIGH);
    delay(20);
}

void epdSetRamArea(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
    epdWriteCommand(0x11);
    epdWriteData(0x03);
    epdWriteCommand(0x44);
    epdWriteData(x / 8);
    epdWriteData((x + w - 1) / 8);
    epdWriteCommand(0x45);
    epdWriteData(y & 0xFF);
    epdWriteData((y >> 8) & 0xFF);
    epdWriteData((y + h - 1) & 0xFF);
    epdWriteData(((y + h - 1) >> 8) & 0xFF);
    epdWriteCommand(0x4E);
    epdWriteData(x / 8);
    epdWriteCommand(0x4F);
    epdWriteData(y & 0xFF);
    epdWriteData((y >> 8) & 0xFF);
}

void epdInitController() {
    epdHardwareReset();
    epdWaitBusy(2000);
    epdWriteCommand(0x12); // SWRESET
    delay(10);
    epdWaitBusy(2000);

    epdWriteCommand(0x01); // Driver output control (200 gates = 0xC7)
    epdWriteData(0xC7);
    epdWriteData(0x00);
    epdWriteData(0x00);

    epdWriteCommand(0x3C); // Border waveform control: 0x00 drives outer glass border BLACK
    epdWriteData(0x00);

    epdWriteCommand(0x18); // Internal temperature sensor
    epdWriteData(0x80);

    epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
}

} // namespace

bool epdWaitBusy(uint32_t timeout_ms) {
    uint32_t t0 = millis();
    uint32_t t_rise = micros();
    while (digitalRead(PIN_EPD_BUSY) == LOW && (micros() - t_rise < 150000UL)) {
        delayMicroseconds(100);
    }
    while (digitalRead(PIN_EPD_BUSY) == HIGH) {
        if (millis() - t0 > timeout_ms) {
            return false;
        }
        delay(5);
    }
    return true;
}

EpdPanel::EpdPanel()
    : canvas_(EPD_WIDTH, EPD_HEIGHT) {}

void EpdPanel::begin() {
    pinMode(PIN_EPD_CS, OUTPUT);
    pinMode(PIN_EPD_DC, OUTPUT);
    pinMode(PIN_EPD_RST, OUTPUT);
    pinMode(PIN_EPD_SCK, OUTPUT);
    pinMode(PIN_EPD_MOSI, OUTPUT);
    pinMode(PIN_EPD_BUSY, INPUT);

    digitalWrite(PIN_EPD_CS, HIGH);
    digitalWrite(PIN_EPD_DC, HIGH);
    digitalWrite(PIN_EPD_RST, HIGH);
    digitalWrite(PIN_EPD_SCK, LOW);
    digitalWrite(PIN_EPD_MOSI, LOW);

    epdInitController();
    init_done_ = true;
    hibernating_ = false;
    rails_warm_ = false;
    async_refresh_active_ = false;
}

bool EpdPanel::isPanelBusy() const {
    return (digitalRead(PIN_EPD_BUSY) == HIGH) ||
           (async_refresh_active_ && (millis() - async_refresh_start_ms_ < 340UL));
}

bool EpdPanel::needsIdleScrub() const {
    return false;
}

uint32_t EpdPanel::cumulativeDirtyPixels() const {
    return rtc_dirty_pixels;
}

const char* EpdPanel::selectMode(bool force_full, bool allow_periodic_full) {
    (void)allow_periodic_full;
    if (force_full || rtc_first_frame_pending || !s_has_prev_frame) return "full";
    return "partial";
}

bool EpdPanel::draw(const GotchiUIState& ui, bool force_full) {
    if (drawing_) return false;

    if (ui.state != UiState::IDLE) {
        s_idle_start_ms = 0;
    } else if (s_idle_start_ms == 0) {
        s_idle_start_ms = millis();
    }

    const bool allow_periodic_full = (ui.state == UiState::IDLE &&
                                      s_idle_start_ms != 0 &&
                                      (millis() - s_idle_start_ms >= IDLE_SCRUB_DEFER_MS));
    const char* mode = selectMode(force_full, allow_periodic_full);
    return drawFrame(ui, mode);
}

bool EpdPanel::drawBlinkMaskedFull(const GotchiUIState& ui) {
    if (drawing_) return false;

    GotchiUIState blink_ui = ui;
    blink_ui.blink = true;
    // 1. Fast partial frame with blink=true (closed eyelids)
    drawFrame(blink_ui, "partial");

    // 2. Immediately execute full refresh scrub
    return drawFrame(ui, "full");
}

bool EpdPanel::drawFrame(const GotchiUIState& ui, const char* mode) {
    if (drawing_) return false;
    drawing_ = true;

    bool full = (strcmp(mode, "full") == 0);
    last_mode_ = mode;

    if (!full && ui.state != UiState::IDLE) {
        if (isPanelBusy()) {
            drawing_ = false;
            return false;
        }

        renderFrame(canvas_, ui, mode);
        const uint8_t* raw = canvas_.getBuffer();

        if (!rails_warm_) {
            // Ensure driver output control, internal temp sensor, and border black
            epdWriteCommand(0x01);
            epdWriteData(0xC7);
            epdWriteData(0x00);
            epdWriteData(0x00);
            epdWriteCommand(0x18);
            epdWriteData(0x80);
            epdWriteCommand(0x3C);
            epdWriteData(0x00);

            epdWriteCommand(0x22);
            epdWriteData(0xC0); // Pre-enable HV analog/clock once
            epdWriteCommand(0x20);
            epdWaitBusy(200);
            rails_warm_ = true;
        }

        uint32_t flipped = 0;
        for (uint32_t i = 0; i < EPD_BUF_BYTES; ++i) {
            flipped += __builtin_popcount(static_cast<unsigned int>(s_prev_frame[i] ^ raw[i]));
            s_prev_frame[i] |= static_cast<uint8_t>(~raw[i]);
        }
        rtc_dirty_pixels += flipped;

        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x26, s_prev_frame, EPD_BUF_BYTES);
        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x24, raw, EPD_BUF_BYTES);

        epdWriteCommand(0x22);
        epdWriteData(0xFC); // Partial LUT without 0x03 HV rail power-off (warm rails)
        epdWriteCommand(0x20);

        memcpy(s_prev_frame, raw, EPD_BUF_BYTES);
        s_has_prev_frame = true;
        rtc_consecutive_partials++;

        async_refresh_active_ = true;
        async_refresh_start_ms_ = millis();
        drawing_ = false;
        return true;
    }

    // When returning to UiState::IDLE or full refresh: wait while panel is busy,
    // power down rails, and execute synchronous settle floor.
    uint32_t t_busy_wait = millis();
    while (isPanelBusy()) {
        if (millis() - t_busy_wait > EPD_BUSY_TIMEOUT_MS) {
            break;
        }
        if (busy_poll_hook_ && !in_hook_) {
            in_hook_ = true;
            busy_poll_hook_();
            in_hook_ = false;
        }
        delay(5);
    }
    rails_warm_ = false;
    async_refresh_active_ = false;

    renderFrame(canvas_, ui, mode);
    const uint8_t* raw = canvas_.getBuffer();

    if (full || !init_done_ || hibernating_) {
        epdInitController();
        init_done_ = true;
        hibernating_ = false;

        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x26, raw, EPD_BUF_BYTES);
        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x24, raw, EPD_BUF_BYTES);

        epdWriteCommand(0x22);
        epdWriteData(0xF7); // Full refresh LUT
        epdWriteCommand(0x20);
    } else {
        // Ensure driver output control (200 gates), internal temp sensor, and border black
        // are intact after previous partial refresh's 0x03 analog/clock power-down.
        epdWriteCommand(0x01);
        epdWriteData(0xC7);
        epdWriteData(0x00);
        epdWriteData(0x00);
        epdWriteCommand(0x18);
        epdWriteData(0x80);
        epdWriteCommand(0x3C);
        epdWriteData(0x00);

        // Re-drive solid black pixels on return-to-idle partial refresh so ghosted/faded
        // blacks are refreshed jet-black via SSD1681 partial LUT (white-to-black transition).
        uint32_t flipped = 0;
        for (uint32_t i = 0; i < EPD_BUF_BYTES; ++i) {
            flipped += __builtin_popcount(static_cast<unsigned int>(s_prev_frame[i] ^ raw[i]));
            s_prev_frame[i] |= static_cast<uint8_t>(~raw[i]);
        }
        rtc_dirty_pixels += flipped;

        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x26, s_prev_frame, EPD_BUF_BYTES);
        epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
        epdWriteBuffer(0x24, raw, EPD_BUF_BYTES);

        epdWriteCommand(0x22);
        epdWriteData(0xFF); // Partial LUT + auto power-off HV charge pump (0xFC | 0x03) to prevent gray washout
        epdWriteCommand(0x20);
    }

    uint32_t t_rise0 = micros();
    while (digitalRead(PIN_EPD_BUSY) == LOW && (micros() - t_rise0 < 200000UL)) {
        delayMicroseconds(50);
    }
    last_busy_rise_us_ = micros() - t_rise0;

    uint32_t t_busy0 = millis();
    bool ok = true;
    // Ignore brief 4-5ms LUT-load / HV charge-pump switchover dips on BUSY;
    // require 3 consecutive LOW reads after at least 40ms before declaring waveform done.
    uint8_t low_streak = 0;
    while (true) {
        uint32_t elapsed_ms = millis() - t_busy0;
        if (elapsed_ms > EPD_BUSY_TIMEOUT_MS) {
            ok = false;
            break;
        }
        if (digitalRead(PIN_EPD_BUSY) == LOW) {
            if (elapsed_ms >= 40) {
                low_streak++;
                if (low_streak >= 3) break;
            }
        } else {
            low_streak = 0;
        }
        if (busy_poll_hook_ && !in_hook_) {
            in_hook_ = true;
            busy_poll_hook_();
            in_hook_ = false;
        }
        delay(5);
    }
    last_busy_high_ms_ = millis() - t_busy0;

    // Unconditional waveform settle floor (2650ms full / 340ms partial) so a floating or
    // bouncing BUSY line never cuts off an active e-paper waveform mid-refresh.
    const uint32_t min_waveform_ms = full ? 2650UL : 340UL;
    while (millis() - t_busy0 < min_waveform_ms) {
        if (busy_poll_hook_ && !in_hook_) {
            in_hook_ = true;
            busy_poll_hook_();
            in_hook_ = false;
        }
        delay(5);
    }

    memcpy(s_prev_frame, raw, EPD_BUF_BYTES);
    s_has_prev_frame = true;

    if (!ok) {
        rtc_first_frame_pending = true;
        drawing_ = false;
        return false;
    }

    if (full) {
        rtc_consecutive_partials = 0;
        rtc_dirty_pixels = 0;
        last_full_ms = millis();
        rtc_ms_since_full = 0;
        rtc_first_frame_pending = false;
        s_idle_start_ms = millis();
    } else {
        rtc_consecutive_partials++;
    }

    if (ui.sleeping) {
        epdWriteCommand(0x10);
        epdWriteData(0x01);
        hibernating_ = true;
    }

    drawing_ = false;
    return true;
}

void EpdPanel::prepareDeepSleep() {
    while (isPanelBusy()) {
        delay(5);
    }
    rails_warm_ = false;
    async_refresh_active_ = false;
    rtc_ms_since_full += millis() - last_full_ms;
    last_full_ms = millis();
    if (!init_done_ || hibernating_) {
        epdInitController();
        init_done_ = true;
        hibernating_ = false;
    }
    // Clear bistable e-paper screen to pure white (0xFF) before hibernating so the device visibly powers off.
    memset(s_prev_frame, 0xFF, EPD_BUF_BYTES);
    epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
    epdWriteBuffer(0x26, s_prev_frame, EPD_BUF_BYTES);
    epdSetRamArea(0, 0, EPD_WIDTH, EPD_HEIGHT);
    epdWriteBuffer(0x24, s_prev_frame, EPD_BUF_BYTES);
    epdWriteCommand(0x22);
    epdWriteData(0xF7);
    epdWriteCommand(0x20);
    uint32_t t0 = millis();
    delay(20);
    while (digitalRead(PIN_EPD_BUSY) == HIGH && (millis() - t0 < 3500)) {
        delay(5);
    }
    if (millis() - t0 < 50) {
        delay(2200);
    }
    rtc_first_frame_pending = true;
    rtc_consecutive_partials = 0;
    rtc_dirty_pixels = 0;
    epdWriteCommand(0x10);
    epdWriteData(0x01);
    hibernating_ = true;
}

#include "../../core/src/gotchi_core.cpp"

