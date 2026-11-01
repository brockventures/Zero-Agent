#include "camera_hal.h"
#include "pins.h"
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <driver/rmt.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace gotchi_camera {

static CameraInfo s_info = {false, 0, "none", false};
static bool s_in_standby = false;
static bool s_xclk_paused = false;
static uint8_t s_reg_3017 = 0xFF;
static uint8_t s_reg_3018 = 0xFC;
static bool s_saved_pad_regs = false;
static SemaphoreHandle_t s_standby_mutex = nullptr;
#if defined(ARDUINO)
static rmt_obj_t* s_rmt_led = nullptr;
#endif

static SemaphoreHandle_t getStandbyMutex() {
    if (!s_standby_mutex) {
        s_standby_mutex = xSemaphoreCreateMutex();
    }
    return s_standby_mutex;
}

struct ScopedStandbyLock {
    SemaphoreHandle_t m;
    ScopedStandbyLock() : m(getStandbyMutex()) {
        if (m) xSemaphoreTake(m, portMAX_DELAY);
    }
    ~ScopedStandbyLock() {
        if (m) xSemaphoreGive(m);
    }
};

static void latchGpio21Off() {
    gpio_hold_dis(GPIO_NUM_21);
    pinMode(21, OUTPUT);
    digitalWrite(21, LOW);
    gpio_hold_en(GPIO_NUM_21);
}

#if defined(ARDUINO)
// Dedicated single-channel RMT WS2812 writer that re-attaches the GPIO output matrix
// before every transmission. Arduino ESP32 core 2.0.x `neopixelWrite()` only binds the
// RMT peripheral to the pin once behind a static `initialized` flag; any subsequent
// `pinMode()` call on shared GPIO48 (PIN_CAM_Y9) detaches the RMT signal from the pad.
static void writeWs2812Rgb(uint8_t pin, uint8_t r, uint8_t g, uint8_t b) {
    if (!s_rmt_led) {
        s_rmt_led = rmtInit(pin, RMT_TX_MODE, RMT_MEM_64);
        if (!s_rmt_led) {
            return;
        }
        rmtSetTick(s_rmt_led, 100);
    }
    // Re-attach the RMT TX channel output signal to `pin` in case camera/GPIO calls detached it.
    rmt_set_gpio(RMT_CHANNEL_0, RMT_MODE_TX, (gpio_num_t)pin, false);

    rmt_data_t led_data[24];
    const uint8_t color[3] = {g, r, b}; // WS2812 expects GRB byte order
    int i = 0;
    for (int col = 0; col < 3; ++col) {
        for (int bit = 0; bit < 8; ++bit) {
            if (color[col] & (1 << (7 - bit))) {
                led_data[i].level0 = 1;
                led_data[i].duration0 = 8; // 0.8 us
                led_data[i].level1 = 0;
                led_data[i].duration1 = 4; // 0.4 us
            } else {
                led_data[i].level0 = 1;
                led_data[i].duration0 = 4; // 0.4 us
                led_data[i].level1 = 0;
                led_data[i].duration1 = 8; // 0.8 us
            }
            ++i;
        }
    }
    rmtWriteBlocking(s_rmt_led, led_data, 24);
}
#endif

static void clearRgbLedOnY9() {
#if defined(ARDUINO)
    // Meshnology W11-MB-V0.2 wires an onboard WS2812 RGB LED to GPIO48 (shared with PIN_CAM_Y9).
    // Once the camera DVP pads are tri-stated (0x3017/0x3018 = 0x00), clock 24 bits of 0x000000
    // twice with a >280us reset latch and hold GPIO48 LOW during standby so the WS2812 stays dark.
    writeWs2812Rgb(PIN_CAM_Y9, 0, 0, 0);
    delayMicroseconds(400);
    writeWs2812Rgb(PIN_CAM_Y9, 0, 0, 0);
    delayMicroseconds(400);
    pinMode(PIN_CAM_Y9, OUTPUT);
    digitalWrite(PIN_CAM_Y9, LOW);
#endif
}

bool isInStandby() {
    ScopedStandbyLock lock;
    return s_in_standby;
}

bool writeStandbyRgbLed(uint8_t r, uint8_t g, uint8_t b, uint8_t rgb_pin) {
#if defined(ARDUINO)
    ScopedStandbyLock lock;
    if (!s_in_standby) {
        return false;
    }
    latchGpio21Off();
    writeWs2812Rgb(rgb_pin, r, g, b);
    if (r == 0 && g == 0 && b == 0) {
        delayMicroseconds(400);
        pinMode(rgb_pin, OUTPUT);
        digitalWrite(rgb_pin, LOW);
    }
    return true;
#else
    return false;
#endif
}

void standby() {
    ScopedStandbyLock lock;
    if (!s_info.initialized) {
        clearRgbLedOnY9();
        latchGpio21Off();
        s_in_standby = true;
        return;
    }
    sensor_t* s = esp_camera_sensor_get();
    if (s && s->set_reg) {
        if (s_info.sensor_pid == OV3660_PID || s_info.sensor_pid == OV5640_PID) {
            if (!s_saved_pad_regs && s->get_reg) {
                int r17 = s->get_reg(s, 0x3017, 0xFF);
                int r18 = s->get_reg(s, 0x3018, 0xFF);
                if (r17 > 0) s_reg_3017 = (uint8_t)r17;
                if (r18 > 0) s_reg_3018 = (uint8_t)r18;
                s_saved_pad_regs = true;
            }
            // Disable DVP pad output drivers (0x3017/0x3018 = 0x00) and assert software power-down (0x3008 bit 6)
            // so the camera sensor completely releases GPIO48 (Y9) before clocking 0x000000 into the WS2812.
            s->set_reg(s, 0x3017, 0xFF, 0x00);
            s->set_reg(s, 0x3018, 0xFF, 0x00);
            s->set_reg(s, 0x3008, 0x40, 0x40);
        } else if (s_info.sensor_pid == OV2640_PID) {
            s->set_reg(s, 0xFF, 0xFF, 0x01);
            s->set_reg(s, 0x09, 0x10, 0x10);
        }
        s_in_standby = true;
    } else {
        s_in_standby = true;
    }
    delay(2);
    if (s_info.initialized && !s_xclk_paused) {
        ledc_timer_pause(LEDC_LOW_SPEED_MODE, LEDC_TIMER_0);
        s_xclk_paused = true;
    }
    clearRgbLedOnY9();
    latchGpio21Off();
}

static void wakeFromStandby() {
    bool woke = false;
    {
        ScopedStandbyLock lock;
        if (!s_info.initialized || !s_in_standby) return;
#if defined(ARDUINO)
        writeWs2812Rgb(PIN_CAM_Y9, 0, 0, 0);
        delayMicroseconds(300);
#endif
        latchGpio21Off();
        s_in_standby = false;
        pinMode(PIN_CAM_Y9, INPUT);
        if (s_xclk_paused) {
            ledc_timer_resume(LEDC_LOW_SPEED_MODE, LEDC_TIMER_0);
            s_xclk_paused = false;
            delay(5); // Let 20 MHz XCLK stabilize before SCCB register writes
        }

        sensor_t* s = esp_camera_sensor_get();
        if (s && s->set_reg) {
            if (s_info.sensor_pid == OV3660_PID || s_info.sensor_pid == OV5640_PID) {
                s->set_reg(s, 0x3008, 0x40, 0x00);
                s->set_reg(s, 0x3017, 0xFF, s_reg_3017);
                s->set_reg(s, 0x3018, 0xFF, s_reg_3018);
            } else if (s_info.sensor_pid == OV2640_PID) {
                s->set_reg(s, 0xFF, 0xFF, 0x01);
                s->set_reg(s, 0x09, 0x10, 0x00);
            }
        }
        woke = true;
    }
    if (woke) {
        delay(120); // Let sensor PLL, AWB, and AEC loops wake from software power-down
    }
}

CameraInfo init() {
    camera_config_t config = {};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = PIN_CAM_Y2;
    config.pin_d1       = PIN_CAM_Y3;
    config.pin_d2       = PIN_CAM_Y4;
    config.pin_d3       = PIN_CAM_Y5;
    config.pin_d4       = PIN_CAM_Y6;
    config.pin_d5       = PIN_CAM_Y7;
    config.pin_d6       = PIN_CAM_Y8;
    config.pin_d7       = PIN_CAM_Y9;
    config.pin_xclk     = PIN_CAM_XCLK;
    config.pin_pclk     = PIN_CAM_PCLK;
    config.pin_vsync    = PIN_CAM_VSYNC;
    config.pin_href     = PIN_CAM_HREF;
    config.pin_sccb_sda = PIN_CAM_SIOD;
    config.pin_sccb_scl = PIN_CAM_SIOC;
    config.pin_pwdn     = -1;
    config.pin_reset    = -1;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size   = FRAMESIZE_VGA; // 640x480 fast multimodal snapshot
    config.jpeg_quality = 12;
    config.fb_count     = psramFound() ? 2 : 1;
    config.fb_location  = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;

    if (esp_camera_init(&config) != ESP_OK) {
        standby();
        return s_info;
    }

    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s_info.initialized = true;
        s_info.sensor_pid  = s->id.PID;
        if (s->id.PID == OV5640_PID) {
            s_info.sensor_name   = "OV5640";
            s_info.has_autofocus = true;
        } else if (s->id.PID == OV3660_PID) {
            s_info.sensor_name   = "OV3660";
            s_info.has_autofocus = false;
        } else {
            s_info.sensor_name   = "OV2640";
            s_info.has_autofocus = false;
        }
        if (s->set_whitebal)      s->set_whitebal(s, 1);
        if (s->set_awb_gain)      s->set_awb_gain(s, 1);
        if (s->set_wb_mode)       s->set_wb_mode(s, 0);
        if (s->set_exposure_ctrl) s->set_exposure_ctrl(s, 1);
        if (s->set_gain_ctrl)     s->set_gain_ctrl(s, 1);
    }
    delay(150); // Allow initial AWB/AEC calibration before entering standby
    standby();
    return s_info;
}

bool triggerAutofocus() {
    if (!s_info.initialized || !s_info.has_autofocus) return false;
    sensor_t* s = esp_camera_sensor_get();
    if (!s) return false;
    // OV5640 VCM single-shot autofocus trigger register (0x3022 = 0x08)
    return (s->set_reg(s, 0x3022, 0xFF, 0x08) == 0);
}

camera_fb_t* captureJpeg() {
    if (!s_info.initialized) return nullptr;
    wakeFromStandby();
    if (s_info.has_autofocus) {
        triggerAutofocus();
        delay(120);
    }
    // Flush 5 frames (~200ms) after waking from standby so AWB and AEC/AGC converge
    // and eliminate the uncalibrated green Bayer tint.
    for (int i = 0; i < 5; ++i) {
        camera_fb_t* flush_fb = esp_camera_fb_get();
        if (flush_fb) {
            esp_camera_fb_return(flush_fb);
        }
        delay(30);
    }
    return esp_camera_fb_get();
}

} // namespace gotchi_camera
