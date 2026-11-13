#pragma once
// ============================================================================
// GOTCHI UI state — C++ mirror of poc/gotchi_poc.py `GotchiUIState`.
// Field defaults match the Python dataclass.
// ============================================================================

#include <Arduino.h>
#include <string.h>
#include "../../core/include/gotchi_core.h"

struct GotchiUIState {
    UiState state = UiState::IDLE;
    String transcript;
    String reply;
    bool     expects_reply = false;  // reply ends with '?' (backend `reply` event); reopen mic after playback
    String status_text = "READY (HOLD PTT)";
    String error_code;
    String skin = "gotchi"; // "gotchi" | "zero"
    uint16_t thinking_seconds = 0;
    uint32_t audio_bytes_received = 0;
    uint32_t last_sample_rate = 24000;
    int8_t   battery_pct = -1;       // 0..100% when V1 VBAT divider on D4 is active (-1 if unwired)
    uint16_t battery_mv = 0;         // LiPo millivolts (3300..4200 mV)
    bool     wall_power = false;     // true when 5V VBUS wall brick or USB host power is detected
    uint8_t  anim_frame = 0;
    bool     blink = false;
    bool     wifi_active = false;
    bool     ble_active = false;
    bool     sleeping = false;
};
