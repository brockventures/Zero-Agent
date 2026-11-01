#pragma once
// ============================================================================
// GOTCHI V1 — Camera HAL (T2.5 OV2640 / OV3660 Stock + Optional OV5640 AF)
// ============================================================================

#include <Arduino.h>
#include "esp_camera.h"
#include "pins.h"

namespace gotchi_camera {

struct CameraInfo {
    bool initialized;
    uint16_t sensor_pid;
    const char* sensor_name;
    bool has_autofocus;
};

// Initialize DVP camera bus on XIAO ESP32S3 Sense pins and auto-detect sensor
CameraInfo init();

// Trigger single-shot VCM autofocus if OV5640 is attached (register 0x3022 = 0x08)
bool triggerAutofocus();

// Capture a JPEG frame in PSRAM; caller releases with esp_camera_fb_return(fb)
camera_fb_t* captureJpeg();

// Return camera sensor to idle standby and clear shared GPIO48 (Y9 / WS2812 RGB LED)
void standby();

// Check if camera DVP pads are tri-stated in standby
bool isInStandby();

// Safely modulate RGB LED while camera is in standby; returns false if camera is active
bool writeStandbyRgbLed(uint8_t r, uint8_t g, uint8_t b, uint8_t rgb_pin = PIN_CAM_Y9);

} // namespace gotchi_camera
