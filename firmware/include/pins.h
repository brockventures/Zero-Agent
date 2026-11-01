#pragma once
// ============================================================================
// GOTCHI V1 — Locked Hardware Pinout Map (Seeed Studio XIAO ESP32S3 Sense)
// Matches docs/HARDWARE_ASSEMBLY.md & cad/build_case.py (Rev 4.1)
// ============================================================================

// 1. Waveshare 1.54" E-Paper Module V2 (SSD1681, 200x200, 8-pin PH2.0 harness)
#define PIN_EPD_RST     1   // D0 (RTC_GPIO1)
#define PIN_EPD_CS      2   // D1 (RTC_GPIO2)
#define PIN_EPD_DC      4   // D3 (RTC_GPIO4)
#define PIN_EPD_BUSY    6   // D5 (RTC_GPIO6)
#define PIN_EPD_SCK     7   // D8
#define PIN_EPD_MOSI    9   // D10

// 2. Tactile Buttons + Multiplexed V1 Battery Fuel Gauge (D4) & 5V Wall-Brick Sense (D2)
//   - PIN_BTN_TOP_PTT (D2 / GPIO3 / RTC_GPIO3 / ADC1_CH2): Momentary switch to GND + single 220k series resistor from 5V (VBUS)
//   - PIN_BTN_BOT_MOD (D4 / GPIO5 / RTC_GPIO5 / ADC1_CH4): Momentary switch to GND + 220k/220k divider from switched BAT+ to GND
#define PIN_BTN_TOP_PTT 3   // D2 (RTC_GPIO3 / ADC1_CH2) — Push-to-Talk, Deep-Sleep ext0 Wake & 5V VBUS Sense
#define PIN_BTN_BOT_MOD 5   // D4 (RTC_GPIO5 / ADC1_CH4) — Camera Snap/Burst & VBAT LiPo Fuel Gauge (avoids GPIO0 strap!)
#define PIN_ADC_PRESSED_MAX_MV   400   // Button shorted to GND (< 400 mV)
#define PIN_VBAT_DIV_MIN_MV      1460  // Minimum valid VBAT/2 divider idle voltage (~2.92V LiPo under load; above ~1370 mV pull-up bleed when switch OFF)
#define PIN_VBAT_DIV_MAX_MV      2350  // Maximum valid VBAT/2 divider idle voltage (~4.70V ceiling)
#define PIN_VBUS_WALL_MIN_MV     650   // D2 reading with internal 45k INPUT_PULLDOWN + 220k to 5V VBUS (~849 mV nominal at 5.0V)

// 3. MAX98357A I2S Class-D Amplifier (Pop-Free Boot Assignment)
#define PIN_I2S_LRCLK   43  // D6 (UART0_TX)
#define PIN_I2S_BCLK    44  // D7 (UART0_RX — silent at ROM boot so amp stays muted!)
#define PIN_I2S_DIN     8   // D9

// 4. Sense Daughterboard Onboard Digital PDM Microphone (16 kHz 16-bit Mono)
#define PIN_PDM_CLK     42
#define PIN_PDM_DATA    41

// 5. Sense Daughterboard DVP Camera Bus (OV2640 / OV3660 / OV5640 Auto-Detect)
#define PIN_CAM_XCLK    10
#define PIN_CAM_SIOD    40
#define PIN_CAM_SIOC    39
#define PIN_CAM_VSYNC   38
#define PIN_CAM_HREF    47
#define PIN_CAM_PCLK    13
#define PIN_CAM_Y9      48
#define PIN_CAM_Y8      11
#define PIN_CAM_Y7      12
#define PIN_CAM_Y6      14
#define PIN_CAM_Y5      16
#define PIN_CAM_Y4      18
#define PIN_CAM_Y3      17
#define PIN_CAM_Y2      15
