#pragma once
// ============================================================================
// GOTCHI e-paper panel driver — Waveshare 1.54" V2 (SSD1681, 200x200) via GxEPD2.
// Port of poc/gotchi_poc.py `RefreshTracker` (policy) and
// `render_sprite_200x200` (layout, see sprite_render.cpp).
// Pins: include/pins.h (do not change here).
// ============================================================================

#include <Arduino.h>
#include <Adafruit_GFX.h>
#include "pins.h"
#include "ui_state.h"

#ifndef GxEPD_BLACK
#define GxEPD_BLACK 0x0000
#endif
#ifndef GxEPD_WHITE
#define GxEPD_WHITE 0xFFFF
#endif

// Refresh policy (RefreshTracker defaults).
static constexpr uint8_t  MAX_CONSECUTIVE_PARTIALS      = 35;
static constexpr uint8_t  MAX_PARTIALS_HARD_CEILING     = 80;
static constexpr uint32_t MAX_DIRTY_PIXELS_BEFORE_SCRUB = 140000UL;
static constexpr uint32_t IDLE_SCRUB_DEFER_MS          = 75000UL;
static constexpr uint32_t FULL_REFRESH_INTERVAL_MS      = 15UL * 60UL * 1000UL;
static constexpr uint32_t EPD_BUSY_TIMEOUT_MS           = 30000UL;

// Bounded wait while BUSY is HIGH. Returns false if still busy at the timeout.
bool epdWaitBusy(uint32_t timeout_ms = EPD_BUSY_TIMEOUT_MS);

// Draws the companion sprite frame (200x200) onto any Adafruit_GFX canvas.
// refresh_mode ("full" | "partial") only selects the header badge.
void renderFrame(Adafruit_GFX& d, const GotchiUIState& ui, const char* refresh_mode);

class EpdPanel {
public:
    EpdPanel();

    // Direct 3-wire SPI + SSD1681 init. Call once from setup().
    void begin();

    // "full" | "partial" per the refresh policy.
    // Periodic full refreshes (after MAX_CONSECUTIVE_PARTIALS or FULL_REFRESH_INTERVAL_MS)
    // run only when `allow_periodic_full` is true (i.e. returning to IDLE) so mid-turn
    // transitions (LISTENING -> THINKING -> SPEAKING) never flash the screen.
    const char* selectMode(bool force_full, bool allow_periodic_full = true);

    // Renders ui and refreshes the panel. Returns false on BUSY timeout;
    // the next draw is then forced full.
    bool draw(const GotchiUIState& ui, bool force_full = false);

    // True if consecutive partials, cumulative dirty pixels, or elapsed time exceed scrub thresholds.
    bool needsIdleScrub() const;

    // Cumulative dirty (flipped) pixels accumulated across partial refreshes since last full refresh.
    uint32_t cumulativeDirtyPixels() const;

    // Draws a fast partial frame with blink=true immediately before running a full refresh.
    bool drawBlinkMaskedFull(const GotchiUIState& ui);

    // Mode chosen by the most recent draw().
    const char* lastMode() const { return last_mode_; }

    // Duration (in ms) that BUSY stayed HIGH during the most recent refresh.
    uint32_t lastBusyHighMs() const { return last_busy_high_ms_; }
    uint32_t lastBusyRiseUs() const { return last_busy_rise_us_; }

    // Accumulates elapsed time since the last full refresh into RTC memory.
    // Call before entering deep sleep.
    void prepareDeepSleep();

    // Optional hook invoked every 5ms while waiting on BUSY/settle (guarded against re-entrancy).
    void setBusyPollHook(void (*hook)()) { busy_poll_hook_ = hook; }
    bool isDrawing() const { return drawing_; }

    // Non-blocking fire-and-forget partial refresh & warm HV rail telemetry
    bool isPanelBusy() const;
    bool areRailsWarm() const { return rails_warm_; }
    bool isAsyncRefreshActive() const { return async_refresh_active_; }

private:
    bool drawFrame(const GotchiUIState& ui, const char* mode);

    GFXcanvas1 canvas_;
    const char* last_mode_ = "full";
    uint32_t last_busy_high_ms_ = 0;
    uint32_t last_busy_rise_us_ = 0;
    bool hibernating_ = false;
    bool init_done_ = false;
    bool drawing_ = false;
    bool in_hook_ = false;
    void (*busy_poll_hook_)() = nullptr;
    bool rails_warm_ = false;
    bool async_refresh_active_ = false;
    uint32_t async_refresh_start_ms_ = 0;
};
