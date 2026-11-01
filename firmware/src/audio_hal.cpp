#include "audio_hal.h"
#include "ble_framing.h"
#include "pins.h"
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mbedtls/base64.h>
#include <math.h>
#include <string.h>

namespace gotchi_audio {

static constexpr i2s_port_t PORT_PDM_RX = I2S_NUM_0;
static constexpr i2s_port_t PORT_I2S_TX = I2S_NUM_1;

static constexpr uint32_t REC_SAMPLE_RATE = 16000;
static constexpr size_t   PREROLL_SAMPLES = REC_SAMPLE_RATE / 2; // 500 ms = 8,000 samples (16 KB)
static constexpr size_t   MAX_REC_SAMPLES = REC_SAMPLE_RATE * 120; // 120s max in 8MB PSRAM = 1,920,000 samples (3.84 MB)
static constexpr size_t   WAV_HEADER_SIZE = 44;

// Go Live NLMS Acoustic Echo Cancellation (AEC) & 300ms Barge-In state (active ONLY in Go Live mode)
static constexpr size_t   AEC_TAPS              = 64;
static constexpr size_t   AEC_REF_RING_CAP      = 4096;
static constexpr uint32_t BARGE_IN_HOLD_MS      = 300;
static constexpr float    NLMS_STEP_MU          = 0.15f;
static constexpr float    NLMS_EPSILON          = 1e-4f;

static bool     s_live_aec_enabled        = false;
static bool     s_live_barge_in_triggered = false;
static float    s_barge_in_rms_thresh     = 0.008f;
static uint32_t s_barge_in_voiced_ms      = 0;
static float    s_aec_weights[AEC_TAPS]   = {0.0f};
static float    s_aec_x_history[AEC_TAPS] = {0.0f};
static int16_t  s_aec_ref_ring[AEC_REF_RING_CAP] = {0};
static size_t   s_aec_ref_write = 0;
static size_t   s_aec_ref_read  = 0;

static int16_t* s_preroll_ring    = nullptr;
static size_t   s_preroll_head    = 0;
static bool     s_preroll_full    = false;
static bool     s_preroll_enabled = true;
static bool     s_rx_running      = true;

// 1st-order single-pole 100 Hz high-pass filter state at 16 kHz (y[n] = x[n] - x[n-1] + 0.96 * y[n-1])
static float    s_hpf_prev_x      = 0.0f;
static float    s_hpf_prev_y      = 0.0f;

static inline int16_t applyHighPass100HzSample(int16_t sample) {
    float x = static_cast<float>(sample);
    float y = (x - s_hpf_prev_x) + 0.96f * s_hpf_prev_y;
    s_hpf_prev_x = x;
    s_hpf_prev_y = y;
    if (y > 32767.0f) y = 32767.0f;
    if (y < -32768.0f) y = -32768.0f;
    return static_cast<int16_t>(y);
}

static void applyHighPass100HzBuffer(int16_t* samples, size_t count) {
    if (!samples) return;
    for (size_t i = 0; i < count; ++i) {
        samples[i] = applyHighPass100HzSample(samples[i]);
    }
}

static constexpr size_t MAX_TURN_PCM_SAMPLES = 24000 * 25; // 25s @ 24 kHz = 1.2 MB PSRAM
static int16_t*         s_turn_pcm_buf          = nullptr;
static volatile size_t  s_turn_pcm_count        = 0;
static volatile size_t  s_turn_pcm_ready        = 0;
static volatile size_t  s_turn_pcm_played       = 0;
static volatile bool    s_turn_play_running     = false;
static volatile bool    s_turn_stream_finished  = false;
static volatile bool    s_turn_play_aborted     = false;
static TaskHandle_t     s_turn_play_task_handle = nullptr;
static uint32_t         s_turn_pcm_rate         = 24000;
static uint8_t*         s_wav_buf               = nullptr;
static volatile size_t s_rec_samples  = 0;
static size_t          s_max_samples  = MAX_REC_SAMPLES;
static uint32_t        s_cur_tx_rate  = 24000;
static bool            s_tx_running   = false;
static AbortCheckFn    s_abort_check  = nullptr;

static void pushAecReferenceSamples(const int16_t* tx_samples, size_t count, uint32_t tx_rate) {
    if (!s_live_aec_enabled || !tx_samples || count == 0) return;
    // Downsample/resample TX audio (typically 24 kHz) to 16 kHz reference ring
    for (size_t i = 0; i < count; ++i) {
        if (tx_rate == 24000 && (i % 3 == 2)) continue; // 2 of every 3 samples: 24 kHz -> 16 kHz
        s_aec_ref_ring[s_aec_ref_write] = tx_samples[i];
        s_aec_ref_write = (s_aec_ref_write + 1) % AEC_REF_RING_CAP;
        if (s_aec_ref_write == s_aec_ref_read) {
            s_aec_ref_read = (s_aec_ref_read + 1) % AEC_REF_RING_CAP;
        }
    }
}

static int16_t popAecReferenceSample() {
    if (s_aec_ref_read == s_aec_ref_write) return 0;
    int16_t s = s_aec_ref_ring[s_aec_ref_read];
    s_aec_ref_read = (s_aec_ref_read + 1) % AEC_REF_RING_CAP;
    return s;
}

int16_t processLiveAecSample(int16_t mic_sample, int16_t ref_sample) {
    float x = static_cast<float>(ref_sample) / 32768.0f;
    float d = static_cast<float>(mic_sample) / 32768.0f;

    for (size_t i = AEC_TAPS - 1; i > 0; --i) {
        s_aec_x_history[i] = s_aec_x_history[i - 1];
    }
    s_aec_x_history[0] = x;

    float y_hat = 0.0f;
    float norm = NLMS_EPSILON;
    for (size_t i = 0; i < AEC_TAPS; ++i) {
        y_hat += s_aec_weights[i] * s_aec_x_history[i];
        norm += s_aec_x_history[i] * s_aec_x_history[i];
    }

    float err = d - y_hat;
    float step = (NLMS_STEP_MU * err) / norm;
    for (size_t i = 0; i < AEC_TAPS; ++i) {
        s_aec_weights[i] += step * s_aec_x_history[i];
    }

    if (err > 1.0f) err = 1.0f;
    if (err < -1.0f) err = -1.0f;
    return static_cast<int16_t>(err * 32767.0f);
}

void setLiveAecEnabled(bool enabled, float barge_in_rms_thresh) {
    s_live_aec_enabled = enabled;
    s_live_barge_in_triggered = false;
    s_barge_in_rms_thresh = barge_in_rms_thresh > 0.001f ? barge_in_rms_thresh : 0.008f;
    s_barge_in_voiced_ms = 0;
    s_aec_ref_write = 0;
    s_aec_ref_read = 0;
    memset(s_aec_weights, 0, sizeof(s_aec_weights));
    memset(s_aec_x_history, 0, sizeof(s_aec_x_history));
}

bool isLiveAecEnabled() {
    return s_live_aec_enabled;
}

bool consumeLiveBargeInFlag() {
    bool triggered = s_live_barge_in_triggered;
    s_live_barge_in_triggered = false;
    return triggered;
}

void flushRxDmaBuffer() {
    int16_t discard[128];
    size_t bytes_read = 0;
    while (i2s_read(PORT_PDM_RX, discard, sizeof(discard), &bytes_read, 0) == ESP_OK && bytes_read > 0) {}
}

static void writeWavHeader(uint8_t* dst, uint32_t pcm_bytes, uint32_t sample_rate) {
    uint32_t riff_size   = 36 + pcm_bytes;
    uint32_t byte_rate   = sample_rate * 2;
    uint16_t block_align = 2;
    uint16_t bits        = 16;
    uint16_t fmt_pcm     = 1;
    uint16_t channels    = 1;
    uint32_t fmt_len     = 16;

    memcpy(dst + 0,  "RIFF", 4);
    memcpy(dst + 4,  &riff_size, 4);
    memcpy(dst + 8,  "WAVEfmt ", 8);
    memcpy(dst + 16, &fmt_len, 4);
    memcpy(dst + 20, &fmt_pcm, 2);
    memcpy(dst + 22, &channels, 2);
    memcpy(dst + 24, &sample_rate, 4);
    memcpy(dst + 28, &byte_rate, 4);
    memcpy(dst + 32, &block_align, 2);
    memcpy(dst + 34, &bits, 2);
    memcpy(dst + 36, "data", 4);
    memcpy(dst + 40, &pcm_bytes, 4);
}

bool init() {
    if (!s_turn_pcm_buf && psramFound()) {
        s_turn_pcm_buf = (int16_t*)ps_malloc(MAX_TURN_PCM_SAMPLES * sizeof(int16_t));
    }
    if (!s_preroll_ring) {
        s_preroll_ring = (int16_t*)(psramFound()
            ? ps_malloc(PREROLL_SAMPLES * sizeof(int16_t))
            : malloc(PREROLL_SAMPLES * sizeof(int16_t)));
    }
    if (!s_wav_buf) {
        if (psramFound()) {
            s_max_samples = MAX_REC_SAMPLES;
            s_wav_buf = (uint8_t*)ps_malloc(WAV_HEADER_SIZE + s_max_samples * sizeof(int16_t));
        } else {
            s_max_samples = 32000;
            s_wav_buf = (uint8_t*)malloc(WAV_HEADER_SIZE + s_max_samples * sizeof(int16_t));
        }
    }

    // 1. Configure I2S0 for Onboard PDM Microphone RX (CLK=GPIO42, DATA=GPIO41)
    i2s_config_t rx_cfg = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
        .sample_rate = REC_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 16,
        .dma_buf_len = 512,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0,
    };
    i2s_pin_config_t rx_pins = {
        .mck_io_num = I2S_PIN_NO_CHANGE,
        .bck_io_num = I2S_PIN_NO_CHANGE,
        .ws_io_num = PIN_PDM_CLK,     // GPIO 42 (PDM CLK)
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = PIN_PDM_DATA,  // GPIO 41 (PDM DATA)
    };
    if (i2s_driver_install(PORT_PDM_RX, &rx_cfg, 0, nullptr) != ESP_OK) return false;
    if (i2s_set_pin(PORT_PDM_RX, &rx_pins) != ESP_OK) return false;

    // 2. Configure I2S1 for MAX98357A Speaker TX (BCLK=GPIO44 silent at boot, LRCLK=GPIO43, DIN=GPIO8)
    // Use I2S_CHANNEL_FMT_RIGHT_LEFT (standard 2-slot stereo I2S, 32 BCLKs per LRCLK) and duplicate
    // mono samples into both Left and Right slots in playPcmChunk(). On 3.7V LiPo power, the
    // MAX98357A breakout's SD pull-up biases V_SD into the 0.77V..1.40V (Right-channel) or
    // 0.16V..0.77V ((L+R)/2) window, where I2S_CHANNEL_FMT_ONLY_LEFT outputs silence or -6 dB.
    i2s_config_t tx_cfg = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = s_cur_tx_rate,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 16,
        .dma_buf_len = 512,
        .use_apll = false,
        .tx_desc_auto_clear = true,
        .fixed_mclk = 0,
    };
    i2s_pin_config_t tx_pins = {
        .mck_io_num = I2S_PIN_NO_CHANGE,
        .bck_io_num = PIN_I2S_BCLK,   // GPIO 44 (D7)
        .ws_io_num = PIN_I2S_LRCLK,   // GPIO 43 (D6)
        .data_out_num = PIN_I2S_DIN,  // GPIO 8  (D9)
        .data_in_num = I2S_PIN_NO_CHANGE,
    };
    if (i2s_driver_install(PORT_I2S_TX, &tx_cfg, 0, nullptr) != ESP_OK) return false;
    if (i2s_set_pin(PORT_I2S_TX, &tx_pins) != ESP_OK) return false;
    i2s_set_clk(PORT_I2S_TX, s_cur_tx_rate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);
    i2s_stop(PORT_I2S_TX); // Keep BCLK idle (<1 uA MAX98357A shutdown) until playback

    return (s_preroll_ring != nullptr && s_wav_buf != nullptr);
}

static void ensureRxStarted() {
    if (!s_rx_running) {
        i2s_start(PORT_PDM_RX);
        s_rx_running = true;
    }
}

static void stopRxIfIdle() {
    if (!s_preroll_enabled && s_rx_running) {
        i2s_stop(PORT_PDM_RX);
        s_rx_running = false;
    }
}

void setPrerollEnabled(bool enabled) {
    if (s_preroll_enabled == enabled && (enabled == s_rx_running)) return;
    s_preroll_enabled = enabled;
    if (enabled) {
        ensureRxStarted();
    } else {
        s_preroll_head = 0;
        s_preroll_full = false;
        stopRxIfIdle();
    }
}

bool isPrerollEnabled() {
    return s_preroll_enabled;
}

void pumpPreroll() {
    if (!s_preroll_ring || !s_preroll_enabled) return;
    ensureRxStarted();
    int16_t chunk[128];
    size_t bytes_read = 0;
    if (i2s_read(PORT_PDM_RX, chunk, sizeof(chunk), &bytes_read, 0) == ESP_OK && bytes_read > 0) {
        size_t n = bytes_read / sizeof(int16_t);
        for (size_t i = 0; i < n; ++i) {
            s_preroll_ring[s_preroll_head] = applyHighPass100HzSample(chunk[i]);
            s_preroll_head = (s_preroll_head + 1) % PREROLL_SAMPLES;
            if (s_preroll_head == 0) s_preroll_full = true;
        }
    }
}

size_t readLivePcmChunk(int16_t* dst, size_t max_samples, float* out_rms) {
    if (out_rms) *out_rms = 0.0f;
    if (!dst || max_samples == 0) return 0;
    ensureRxStarted();
    size_t bytes_read = 0;
    if (i2s_read(PORT_PDM_RX, dst, max_samples * sizeof(int16_t), &bytes_read, 0) != ESP_OK || bytes_read < sizeof(int16_t)) {
        return 0;
    }
    size_t n = bytes_read / sizeof(int16_t);
    double sum_sq = 0.0;
    for (size_t i = 0; i < n; ++i) {
        dst[i] = applyHighPass100HzSample(dst[i]);
        if (s_live_aec_enabled) {
            dst[i] = processLiveAecSample(dst[i], popAecReferenceSample());
        }
        double v = static_cast<double>(dst[i]) / 32768.0;
        sum_sq += v * v;
    }
    if (out_rms && n > 0) {
        *out_rms = static_cast<float>(sqrt(sum_sq / static_cast<double>(n)));
    }
    return n;
}

bool beginPttRecording(bool use_preroll) {
    if (!s_wav_buf || !s_preroll_ring) return false;
    ensureRxStarted();
    int16_t* dst = reinterpret_cast<int16_t*>(s_wav_buf + WAV_HEADER_SIZE);
    s_rec_samples = 0;

    if (s_preroll_enabled && use_preroll) {
        // Copy 500ms circular pre-roll into start of WAV buffer so first syllable is never clipped
        if (s_preroll_full) {
            size_t tail_len = PREROLL_SAMPLES - s_preroll_head;
            memcpy(dst, s_preroll_ring + s_preroll_head, tail_len * sizeof(int16_t));
            memcpy(dst + tail_len, s_preroll_ring, s_preroll_head * sizeof(int16_t));
            s_rec_samples = PREROLL_SAMPLES;
        } else if (s_preroll_head > 0) {
            memcpy(dst, s_preroll_ring, s_preroll_head * sizeof(int16_t));
            s_rec_samples = s_preroll_head;
        }
    } else {
        s_hpf_prev_x = 0.0f;
        s_hpf_prev_y = 0.0f;
    }
    if (s_preroll_enabled && !use_preroll) {
        // Drop the stale ring so the next normal PTT turn starts from fresh idle audio.
        s_preroll_head = 0;
        s_preroll_full = false;
    }
    flushRxDmaBuffer();
    return true;
}

size_t pollPttRecording() {
    if (!s_wav_buf || s_rec_samples >= s_max_samples) return s_rec_samples;
    int16_t* dst = reinterpret_cast<int16_t*>(s_wav_buf + WAV_HEADER_SIZE);
    size_t room_bytes = (s_max_samples - s_rec_samples) * sizeof(int16_t);
    size_t to_read = room_bytes < 1024 ? room_bytes : 1024;
    size_t bytes_read = 0;
    if (i2s_read(PORT_PDM_RX, dst + s_rec_samples, to_read, &bytes_read, pdMS_TO_TICKS(10)) == ESP_OK) {
        size_t got_samples = bytes_read / sizeof(int16_t);
        applyHighPass100HzBuffer(dst + s_rec_samples, got_samples);
        s_rec_samples += got_samples;
    }
    return s_rec_samples;
}

static volatile bool s_ptt_task_running = false;
static TaskHandle_t  s_ptt_task_handle  = nullptr;

static void pttRecordTaskFn(void* /*arg*/) {
    while (s_ptt_task_running) {
        pollPttRecording();
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    s_ptt_task_handle = nullptr;
    vTaskDelete(nullptr);
}

bool startAsyncPttTask() {
    if (s_ptt_task_running) return true;
    s_ptt_task_running = true;
    BaseType_t rc = xTaskCreatePinnedToCore(
        pttRecordTaskFn,
        "gotchi_ptt_rx",
        4096,
        nullptr,
        configMAX_PRIORITIES - 2,
        &s_ptt_task_handle,
        0  // Pin to Core 0 so Core 1 chime + SPI e-paper refresh never starve I2S RX DMA
    );
    if (rc != pdPASS) {
        s_ptt_task_running = false;
        s_ptt_task_handle = nullptr;
        return false;
    }
    return true;
}

void stopAsyncPttTask() {
    if (!s_ptt_task_running) return;
    s_ptt_task_running = false;
    while (s_ptt_task_handle != nullptr) {
        delay(1);
    }
}

size_t getRecordedSamples() {
    return s_rec_samples;
}

uint32_t getRecordedMs() {
    return (uint32_t)((s_rec_samples * 1000ULL) / REC_SAMPLE_RATE);
}

float getRecentRms(size_t window_samples) {
    size_t n = s_rec_samples;
    if (!s_wav_buf || n == 0 || window_samples == 0) return 0.0f;
    size_t count = n < window_samples ? n : window_samples;
    size_t start = n - count;
    const int16_t* pcm = reinterpret_cast<const int16_t*>(s_wav_buf + WAV_HEADER_SIZE);
    double sum_sq = 0.0;
    for (size_t i = start; i < n; ++i) {
        double v = pcm[i] / 32768.0;
        sum_sq += v * v;
    }
    return sqrtf((float)(sum_sq / count));
}

void scanSpeechFrames(size_t& cursor_samples, float rms_thresh, uint32_t& voiced_frames, size_t& last_voiced_sample) {
    size_t n = s_rec_samples;
    constexpr size_t FRAME_SAMPLES = 320; // 20 ms non-overlapping frame at 16 kHz
    if (!s_wav_buf || n < cursor_samples + FRAME_SAMPLES) return;
    const int16_t* pcm = reinterpret_cast<const int16_t*>(s_wav_buf + WAV_HEADER_SIZE);
    while (cursor_samples + FRAME_SAMPLES <= n) {
        double sum_sq = 0.0;
        float peak = 0.0f;
        for (size_t i = 0; i < FRAME_SAMPLES; ++i) {
            float v = fabsf((float)pcm[cursor_samples + i]) / 32768.0f;
            sum_sq += (double)v * (double)v;
            if (v > peak) peak = v;
        }
        float frame_rms = sqrtf((float)(sum_sq / FRAME_SAMPLES));
        float crest = (frame_rms > 1e-6f) ? (peak / frame_rms) : 99.0f;
        cursor_samples += FRAME_SAMPLES;
        if (frame_rms >= rms_thresh && crest <= 4.5f) {
            ++voiced_frames;
            last_voiced_sample = cursor_samples;
        }
    }
}

WavView finishPttRecording() {
    stopRxIfIdle();
    size_t n = s_rec_samples;
    uint32_t pcm_bytes = n * sizeof(int16_t);
    writeWavHeader(s_wav_buf, pcm_bytes, REC_SAMPLE_RATE);

    const int16_t* pcm = reinterpret_cast<const int16_t*>(s_wav_buf + WAV_HEADER_SIZE);
    double sum_sq = 0.0;
    for (size_t i = 0; i < n; ++i) {
        double v = pcm[i] / 32768.0;
        sum_sq += v * v;
    }
    float rms = n > 0 ? sqrtf((float)(sum_sq / n)) : 0.0f;
    return WavView{s_wav_buf, WAV_HEADER_SIZE + pcm_bytes, rms};
}

WavView buildSilentWav(uint32_t duration_ms) {
    if (!s_wav_buf) return WavView{nullptr, 0, 0.0f};
    size_t samples = (REC_SAMPLE_RATE * duration_ms) / 1000;
    if (samples > s_max_samples) samples = s_max_samples;
    uint32_t pcm_bytes = samples * sizeof(int16_t);
    memset(s_wav_buf + WAV_HEADER_SIZE, 0, pcm_bytes);
    writeWavHeader(s_wav_buf, pcm_bytes, REC_SAMPLE_RATE);
    return WavView{s_wav_buf, WAV_HEADER_SIZE + pcm_bytes, 0.0f};
}

void setPlaybackAbortCheck(AbortCheckFn fn) {
    s_abort_check = fn;
}

bool playPcmChunk(const int16_t* samples, size_t sample_count, uint32_t sample_rate) {
    if (!samples || sample_count == 0) return true;
    if (sample_rate != s_cur_tx_rate && sample_rate >= 8000 && sample_rate <= 48000) {
        s_cur_tx_rate = sample_rate;
        i2s_set_clk(PORT_I2S_TX, s_cur_tx_rate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);
    }
    if (!s_tx_running) {
        i2s_start(PORT_I2S_TX);
        s_tx_running = true;
    }

    // Stream in 512-sample (~21 ms @ 24 kHz) stereo-duplicated slices so Barge-In / Cancel
    // buttons abort speaker output immediately and both Left & Right I2S slots carry full audio.
    constexpr size_t SLICE_SAMPLES = 512;
    static int16_t stereo_slice[SLICE_SAMPLES * 2];
    size_t offset = 0;
    while (offset < sample_count) {
        if (s_abort_check && s_abort_check()) {
            muteSpeaker();
            return false;
        }
        size_t chunk = (sample_count - offset) < SLICE_SAMPLES ? (sample_count - offset) : SLICE_SAMPLES;
        pushAecReferenceSamples(samples + offset, chunk, s_cur_tx_rate);

        for (size_t i = 0; i < chunk; ++i) {
            int16_t s = samples[offset + i];
            stereo_slice[i * 2]     = s;
            stereo_slice[i * 2 + 1] = s;
        }

        size_t bytes_written = 0;
        esp_err_t err = i2s_write(
            PORT_I2S_TX,
            stereo_slice,
            chunk * 2 * sizeof(int16_t),
            &bytes_written,
            pdMS_TO_TICKS(1000)
        );
        if (err != ESP_OK) return false;
        offset += chunk;
        delay(1);

        // When Go Live AEC is active, sample the PDM mic during playback, cancel echo,
        // and trigger full-duplex barge-in if near-end speech persists for >= 300 ms.
        if (s_live_aec_enabled) {
            static int16_t rx_slice[684];
            float cleaned_rms = 0.0f;
            size_t rx_n = readLivePcmChunk(rx_slice, 684, &cleaned_rms);
            if (rx_n > 0) {
                uint32_t slice_ms = (uint32_t)((rx_n * 1000ULL) / REC_SAMPLE_RATE);
                if (cleaned_rms >= s_barge_in_rms_thresh) {
                    s_barge_in_voiced_ms += (slice_ms > 0 ? slice_ms : 20);
                    if (s_barge_in_voiced_ms >= BARGE_IN_HOLD_MS) {
                        s_live_barge_in_triggered = true;
                        s_barge_in_voiced_ms = 0;
                        muteSpeaker();
                        return false;
                    }
                } else {
                    s_barge_in_voiced_ms = 0;
                }
            }
        }
    }
    return true;
}


static void turnPlaybackTaskFn(void* /*arg*/) {
    constexpr size_t SLICE_SAMPLES = 1024;
    while (s_turn_play_running) {
        size_t played = s_turn_pcm_played;
        size_t ready  = s_turn_pcm_ready;
        if (played < ready && s_turn_pcm_buf != nullptr) {
            size_t chunk = (ready - played) < SLICE_SAMPLES ? (ready - played) : SLICE_SAMPLES;
            if (!playPcmChunk(s_turn_pcm_buf + played, chunk, s_turn_pcm_rate)) {
                s_turn_play_aborted = true;
                break;
            }
            s_turn_pcm_played = played + chunk;
        } else if (s_turn_stream_finished) {
            break;
        } else {
            if (s_abort_check && s_abort_check()) {
                s_turn_play_aborted = true;
                muteSpeaker();
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
    s_turn_play_running = false;
    s_turn_play_task_handle = nullptr;
    vTaskDelete(nullptr);
}

static void stopTurnPlaybackTask() {
    if (s_turn_play_running || s_turn_play_task_handle != nullptr) {
        s_turn_stream_finished = true;
        s_turn_play_running = false;
        while (s_turn_play_task_handle != nullptr) {
            delay(2);
        }
    }
}

void clearTurnPcm() {
    stopTurnPlaybackTask();
    s_turn_pcm_count = 0;
    s_turn_pcm_ready = 0;
    s_turn_pcm_played = 0;
    s_turn_stream_finished = false;
    s_turn_play_aborted = false;
    s_turn_pcm_rate = 24000;
}

size_t getTurnPcmSamples() {
    return s_turn_pcm_count;
}

static void drainSpeakerDma() {
    if (!s_tx_running) return;
    // PORT_I2S_TX uses 16 DMA buffers of 512 stereo frames (8,192 frames = ~341 ms @ 24 kHz).
    // i2s_write() returns as soon as frames are queued in DMA RAM. Writing 17 slices of
    // 512 zero stereo frames (1,024 int16_t samples) clocks all 16 descriptors of real audio
    // out to the MAX98357A before i2s_stop() halts BCLK.
    static const int16_t kZeroStereoSlice[1024] = {0};
    for (int i = 0; i < 17; ++i) {
        if (s_abort_check && s_abort_check()) break;
        size_t written = 0;
        if (i2s_write(PORT_I2S_TX, kZeroStereoSlice, sizeof(kZeroStereoSlice), &written, pdMS_TO_TICKS(200)) != ESP_OK) {
            break;
        }
    }
}

static void applyOnDeviceSpeakerDsp(int16_t* pcm, size_t count, uint32_t sample_rate);

void commitTurnSentenceForPlayback() {
    if (!s_turn_pcm_buf || s_turn_pcm_count == 0) return;
    if (s_turn_pcm_count > s_turn_pcm_ready) {
        applyOnDeviceSpeakerDsp(
            s_turn_pcm_buf + s_turn_pcm_ready,
            s_turn_pcm_count - s_turn_pcm_ready,
            s_turn_pcm_rate
        );
        s_turn_pcm_ready = s_turn_pcm_count;
    }
    if (!s_turn_play_running && s_turn_play_task_handle == nullptr && !s_turn_play_aborted) {
        s_turn_play_running = true;
        s_turn_stream_finished = false;
        xTaskCreatePinnedToCore(
            turnPlaybackTaskFn,
            "turn_play",
            4096,
            nullptr,
            2,
            &s_turn_play_task_handle,
            0
        );
    }
}

bool appendBase64PcmToTurn(const char* b64_data, size_t b64_len, uint32_t sample_rate, bool is_adpcm) {
    if (!b64_data || b64_len == 0) return true;
    if (!s_turn_pcm_buf) {
        return is_adpcm
            ? playBase64AdpcmChunk(b64_data, b64_len, sample_rate)
            : playBase64PcmChunk(b64_data, b64_len, sample_rate);
    }
    if (sample_rate >= 8000 && sample_rate <= 48000) {
        s_turn_pcm_rate = sample_rate;
    }
    size_t max_out = (b64_len * 3) / 4 + 4;
    if (!is_adpcm) {
        size_t room_bytes = (MAX_TURN_PCM_SAMPLES - s_turn_pcm_count) * sizeof(int16_t);
        if (max_out <= room_bytes) {
            size_t out_len = 0;
            int rc = mbedtls_base64_decode(
                reinterpret_cast<unsigned char*>(s_turn_pcm_buf + s_turn_pcm_count),
                room_bytes,
                &out_len,
                reinterpret_cast<const unsigned char*>(b64_data),
                b64_len
            );
            if (rc == 0 && out_len >= 2) {
                s_turn_pcm_count += out_len / sizeof(int16_t);
                return true;
            }
        }
        return true;
    } else {
        uint8_t* adpcm_buf = (uint8_t*)(psramFound() ? ps_malloc(max_out) : malloc(max_out));
        if (!adpcm_buf) return false;
        size_t adpcm_len = 0;
        int rc = mbedtls_base64_decode(adpcm_buf, max_out, &adpcm_len, (const unsigned char*)b64_data, b64_len);
        if (rc == 0 && adpcm_len > 0 && s_turn_pcm_count < MAX_TURN_PCM_SAMPLES) {
            uint32_t decoded_sr = sample_rate;
            size_t room_samples = MAX_TURN_PCM_SAMPLES - s_turn_pcm_count;
            size_t got = gotchi_ble::decodeAdpcmToPcm(adpcm_buf, adpcm_len, s_turn_pcm_buf + s_turn_pcm_count, room_samples, &decoded_sr);
            s_turn_pcm_count += got;
            if (decoded_sr >= 8000 && decoded_sr <= 48000) s_turn_pcm_rate = decoded_sr;
        }
        free(adpcm_buf);
        return true;
    }
}

static void applyOnDeviceSpeakerDsp(int16_t* pcm, size_t count, uint32_t sample_rate) {
    if (!pcm || count < 4) return;
    // 1. Two-pole 300 Hz high-pass filter (at 24 kHz, alpha ~ 0.924) to strip sub-bass cone excursion
    const float fc = 300.0f;
    const float sr = (sample_rate >= 8000 && sample_rate <= 48000) ? (float)sample_rate : 24000.0f;
    const float rc = 1.0f / (2.0f * 3.14159265f * fc);
    const float dt = 1.0f / sr;
    const float alpha = rc / (rc + dt);

    float x1 = 0.0f, y1 = 0.0f;
    float x2 = 0.0f, y2 = 0.0f;
    float peak = 0.0f;

    for (size_t i = 0; i < count; ++i) {
        if ((i & 4095U) == 4095U) vTaskDelay(1);
        float x = (float)pcm[i];
        float s1 = alpha * (y1 + x - x1);
        x1 = x;
        y1 = s1;
        float s2 = alpha * (y2 + s1 - x2);
        x2 = s1;
        y2 = s2;
        if (s2 > 32767.0f) s2 = 32767.0f;
        if (s2 < -32768.0f) s2 = -32768.0f;
        pcm[i] = (int16_t)s2;
        float a = fabsf(s2);
        if (a > peak) peak = a;
    }

    if (peak < 80.0f) return;

    // 2. Battery-safe soft-knee limiter & leveler (peak ceiling 18,500 LSB).
    // Server gateway already applies vocal EQ + compression; capping sustained speech
    // at 18,500 LSB prevents Class-D current spikes across the slide switch from
    // browning out the battery rail on spoken replies.
    const float inv_peak = 1.0f / peak;
    const float drive = (peak < 12000.0f) ? 1.25f : 1.05f;
    const float norm_tanh = 1.0f / tanhf(drive);
    for (size_t i = 0; i < count; ++i) {
        if ((i & 4095U) == 4095U) vTaskDelay(1);
        float u = ((float)pcm[i]) * inv_peak;
        float c = tanhf(u * drive) * norm_tanh * 18500.0f;
        if (c > 32767.0f) c = 32767.0f;
        if (c < -32768.0f) c = -32768.0f;
        pcm[i] = (int16_t)c;
    }
}

bool playTurnPcm() {
    if (!s_turn_pcm_buf || s_turn_pcm_count == 0) return true;
    uint32_t rate = s_turn_pcm_rate;
    if (s_turn_pcm_count > s_turn_pcm_ready) {
        applyOnDeviceSpeakerDsp(
            s_turn_pcm_buf + s_turn_pcm_ready,
            s_turn_pcm_count - s_turn_pcm_ready,
            rate
        );
        s_turn_pcm_ready = s_turn_pcm_count;
    }

    bool ok = true;
    if (s_turn_play_running || s_turn_play_task_handle != nullptr) {
        s_turn_stream_finished = true;
        while (s_turn_play_task_handle != nullptr) {
            if (s_abort_check && s_abort_check()) {
                s_turn_play_aborted = true;
                s_turn_play_running = false;
            }
            delay(5);
        }
        ok = !s_turn_play_aborted;
    } else if (s_turn_pcm_played < s_turn_pcm_count && !s_turn_play_aborted) {
        ok = playPcmChunk(
            s_turn_pcm_buf + s_turn_pcm_played,
            s_turn_pcm_count - s_turn_pcm_played,
            rate
        );
    } else {
        ok = !s_turn_play_aborted;
    }

    s_turn_pcm_count = 0;
    s_turn_pcm_ready = 0;
    s_turn_pcm_played = 0;
    s_turn_stream_finished = false;
    s_turn_play_aborted = false;
    if (ok) {
        drainSpeakerDma();
    }
    muteSpeaker();
    return ok;
}

bool playBase64PcmChunk(const char* b64_data, size_t b64_len, uint32_t sample_rate) {
    if (!b64_data || b64_len == 0) return true;
    if (s_abort_check && s_abort_check()) {
        muteSpeaker();
        return false;
    }
    size_t max_out = (b64_len * 3) / 4 + 4;
    uint8_t* decoded = (uint8_t*)(psramFound() ? ps_malloc(max_out) : malloc(max_out));
    if (!decoded) return false;

    size_t out_len = 0;
    int rc = mbedtls_base64_decode(decoded, max_out, &out_len, (const unsigned char*)b64_data, b64_len);
    bool ok = false;
    if (rc == 0 && out_len >= 2) {
        ok = playPcmChunk(reinterpret_cast<const int16_t*>(decoded), out_len / sizeof(int16_t), sample_rate);
    }
    free(decoded);
    return ok;
}

bool playBase64AdpcmChunk(const char* b64_data, size_t b64_len, uint32_t sample_rate) {
    if (!b64_data || b64_len == 0) return true;
    if (s_abort_check && s_abort_check()) {
        muteSpeaker();
        return false;
    }
    size_t max_adpcm = (b64_len * 3) / 4 + 4;
    uint8_t* adpcm_buf = (uint8_t*)(psramFound() ? ps_malloc(max_adpcm) : malloc(max_adpcm));
    if (!adpcm_buf) return false;

    size_t adpcm_len = 0;
    int rc = mbedtls_base64_decode(adpcm_buf, max_adpcm, &adpcm_len, (const unsigned char*)b64_data, b64_len);
    if (rc != 0 || adpcm_len == 0) {
        free(adpcm_buf);
        return false;
    }

    size_t max_samples = adpcm_len * 2;
    int16_t* pcm_buf = (int16_t*)(psramFound()
        ? ps_malloc(max_samples * sizeof(int16_t))
        : malloc(max_samples * sizeof(int16_t)));
    if (!pcm_buf) {
        free(adpcm_buf);
        return false;
    }

    uint32_t decoded_sr = sample_rate;
    size_t sample_count = gotchi_ble::decodeAdpcmToPcm(adpcm_buf, adpcm_len, pcm_buf, max_samples, &decoded_sr);
    free(adpcm_buf);

    bool ok = false;
    if (sample_count > 0) {
        ok = playPcmChunk(pcm_buf, sample_count, decoded_sr);
    }
    free(pcm_buf);
    return ok;
}

bool isValidChimeKind(const char* kind) {
    if (!kind) return false;
    return strcmp(kind, "") == 0 ||
           strcmp(kind, "ready") == 0 ||
           strcmp(kind, "ptt_start") == 0 ||
           strcmp(kind, "ptt_stop") == 0 ||
           strcmp(kind, "snap") == 0 ||
           strcmp(kind, "thinking") == 0 ||
           strcmp(kind, "error") == 0;
}

namespace {

constexpr uint32_t CHIME_SR = 24000;

struct SimpleRng {
    uint32_t state;
    explicit SimpleRng(uint32_t seed) : state(seed ? seed : 1u) {}
    float uniformSigned() {
        // Xorshift32 deterministic PRNG -> [-1.0f, 1.0f]
        uint32_t x = state;
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        state = x;
        return ((float)(x & 0x00FFFFFFu) / 8388607.5f) - 1.0f;
    }
};

struct BiquadBp {
    float b0, b1, b2, a1, a2;
    float x1 = 0.0f, x2 = 0.0f, y1 = 0.0f, y2 = 0.0f;

    void init(float center_hz, float q) {
        float w0 = 2.0f * 3.14159265f * center_hz / (float)CHIME_SR;
        float alpha = sinf(w0) / (2.0f * q);
        float a0 = 1.0f + alpha;
        b0 = alpha / a0;
        b1 = 0.0f;
        b2 = -alpha / a0;
        a1 = (-2.0f * cosf(w0)) / a0;
        a2 = (1.0f - alpha) / a0;
    }

    float step(float x0) {
        float y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
        x2 = x1;
        x1 = x0;
        y2 = y1;
        y1 = y0;
        return y0;
    }
};

static inline float casioReedWave(float phase, float brightness = 1.0f) {
    return (
        1.00f * sinf(phase) +
        0.38f * brightness * sinf(2.0f * phase + 0.3f) +
        0.46f * brightness * sinf(3.0f * phase) +
        0.14f * brightness * sinf(4.0f * phase) +
        0.20f * brightness * sinf(5.0f * phase)
    ) * 0.62f;
}

static void addSwitchClick(
    float* mix_buf,
    size_t max_len,
    float start_sec,
    float ms = 7.0f,
    float freq = 2300.0f,
    float body_freq = 850.0f,
    float amp = 0.92f,
    float metallic = 0.45f
) {
    size_t idx0 = (size_t)(start_sec * CHIME_SR);
    size_t n = (size_t)(CHIME_SR * (ms / 1000.0f));
    if (n < 1) n = 1;
    for (size_t i = 0; i < n && (idx0 + i) < max_len; ++i) {
        float frac = (float)i / (float)n;
        float env = expf(-frac * 6.0f);
        float noise = sinf((float)i * 2.31f + cosf((float)i * 1.13f) * 4.0f);
        float t = (float)i / (float)CHIME_SR;
        float snap = sinf(2.0f * 3.14159265f * freq * t) +
                     0.55f * sinf(2.0f * 3.14159265f * (freq * 1.58f) * t);
        float body = sinf(2.0f * 3.14159265f * body_freq * t) * expf(-frac * 3.5f);
        float s = (0.45f * body + (1.0f - metallic) * 0.55f * snap + metallic * 0.45f * noise) * env * amp * 28500.0f;
        mix_buf[idx0 + i] += s;
    }
}

static void addReedNoteSmooth(
    float* mix_buf,
    size_t max_len,
    float start_sec,
    float freq,
    float ms,
    float amp = 0.68f,
    float click_ms = 6.5f,
    float click_freq = 2400.0f,
    float click_lead_ms = 8.0f,
    float attack_ms = 6.0f,
    float decay_tau = 0.36f,
    float release_ms = 45.0f,
    float vibrato_hz = 5.2f,
    float vibrato_depth = 0.006f
) {
    if (click_ms > 0.0f) {
        addSwitchClick(mix_buf, max_len, start_sec, click_ms, click_freq, 850.0f, 0.90f, 0.45f);
    }
    size_t lead_n = (size_t)(CHIME_SR * (click_lead_ms / 1000.0f));
    size_t note_n = (size_t)(CHIME_SR * (ms / 1000.0f));
    size_t idx0 = (size_t)(start_sec * CHIME_SR) + lead_n;
    size_t attack_n = (size_t)(CHIME_SR * (attack_ms / 1000.0f));
    if (attack_n < 1) attack_n = 1;
    size_t release_n = (size_t)(CHIME_SR * (release_ms / 1000.0f));
    if (release_n < 1) release_n = 1;
    if (release_n > note_n / 2) release_n = note_n / 2;

    float phase = 0.0f;
    for (size_t i = 0; i < note_n && (idx0 + i) < max_len; ++i) {
        float t = (float)i / (float)CHIME_SR;
        float frac = (float)i / (float)(note_n > 0 ? note_n : 1);
        float vib = 1.0f + ((t > 0.045f) ? (vibrato_depth * sinf(2.0f * 3.14159265f * vibrato_hz * t)) : 0.0f);
        float cur_f = freq * vib;
        phase += 2.0f * 3.14159265f * cur_f / (float)CHIME_SR;

        float env = (i < attack_n)
            ? ((float)i / (float)attack_n)
            : expf(-((frac - ((float)attack_n / (float)note_n)) / (decay_tau > 0.06f ? decay_tau : 0.06f)));

        size_t rem = note_n - 1 - i;
        if (rem < release_n) {
            float rel_frac = (float)rem / (float)release_n;
            float taper = 0.5f * (1.0f - cosf(3.14159265f * rel_frac));
            env *= taper;
        }

        float sat = tanhf(casioReedWave(phase, 1.0f) * amp * 1.35f);
        mix_buf[idx0 + i] += sat * env * 25500.0f;
    }
}

static void addShutterImpact(
    float* mix_buf,
    size_t max_len,
    float start_sec,
    float ms,
    float thump_start_hz,
    float thump_end_hz,
    float metal_hz,
    float noise_hz,
    float amp,
    uint32_t seed
) {
    size_t idx0 = (size_t)(start_sec * CHIME_SR);
    size_t n = (size_t)(CHIME_SR * (ms / 1000.0f));
    if (n < 1) n = 1;
    SimpleRng rng(seed);
    BiquadBp bp_noise, hi_noise;
    bp_noise.init(noise_hz, 1.4f);
    hi_noise.init(metal_hz * 1.25f, 2.2f);

    float phase_thump = 0.0f;
    for (size_t i = 0; i < n && (idx0 + i) < max_len; ++i) {
        float frac = (float)i / (float)n;
        float raw = rng.uniformSigned();
        float bpn = bp_noise.step(raw);
        float hin = hi_noise.step(raw);

        float cur_thump_f = thump_start_hz * powf(thump_end_hz / thump_start_hz, powf(frac, 0.55f));
        phase_thump += 2.0f * 3.14159265f * cur_thump_f / (float)CHIME_SR;
        float env_thump = expf(-frac * 5.2f);
        float env_snap  = expf(-frac * 7.5f);
        float env_hi    = expf(-frac * 12.0f);

        float t = (float)i / (float)CHIME_SR;
        float thump = sinf(phase_thump) * env_thump;
        float metal_ring = (sinf(2.0f * 3.14159265f * metal_hz * t) +
                            0.5f * sinf(2.0f * 3.14159265f * (metal_hz * 1.47f) * t)) * env_hi;
        float val = 0.48f * thump + 0.65f * bpn * env_snap + 0.45f * hin * env_hi + 0.28f * metal_ring;
        mix_buf[idx0 + i] += tanhf(val * amp * 1.7f) * 29500.0f;
    }
}

static void addCurtainZip(
    float* mix_buf,
    size_t max_len,
    float start_sec,
    float ms,
    float start_hz,
    float end_hz,
    float amp,
    uint32_t seed
) {
    size_t idx0 = (size_t)(start_sec * CHIME_SR);
    size_t n = (size_t)(CHIME_SR * (ms / 1000.0f));
    if (n < 1) n = 1;
    SimpleRng rng(seed);
    BiquadBp bp1, bp2;
    bp1.init(start_hz, 1.6f);
    bp2.init(end_hz, 1.8f);

    for (size_t i = 0; i < n && (idx0 + i) < max_len; ++i) {
        float frac = (float)i / (float)n;
        float raw = rng.uniformSigned();
        float y1 = bp1.step(raw);
        float y2 = bp2.step(raw);
        float t = (float)i / (float)CHIME_SR;
        float env = sinf(3.14159265f * powf(frac, 0.65f)) * (0.4f + 0.6f * frac);
        float ripple = 0.75f + 0.25f * sinf(2.0f * 3.14159265f * 680.0f * t);
        float mix = (1.0f - frac) * y1 + frac * y2;
        mix_buf[idx0 + i] += tanhf(mix * env * ripple * amp * 2.2f) * 26000.0f;
    }
}

static void addSpringRingTail(
    float* mix_buf,
    size_t max_len,
    float start_sec,
    float ms,
    float freq,
    float amp,
    uint32_t seed
) {
    size_t idx0 = (size_t)(start_sec * CHIME_SR);
    size_t n = (size_t)(CHIME_SR * (ms / 1000.0f));
    if (n < 1) n = 1;
    SimpleRng rng(seed);
    BiquadBp bp;
    bp.init(freq, 4.5f);

    for (size_t i = 0; i < n && (idx0 + i) < max_len; ++i) {
        float frac = (float)i / (float)n;
        float t = (float)i / (float)CHIME_SR;
        float env = expf(-frac * 5.8f);
        float ring = sinf(2.0f * 3.14159265f * freq * t) +
                     0.45f * sinf(2.0f * 3.14159265f * (freq * 1.31f) * t) +
                     0.35f * bp.step(rng.uniformSigned());
        mix_buf[idx0 + i] += ring * env * amp * 22000.0f;
    }
}

} // namespace

void playChime(const char* kind) {
    if (!kind || kind[0] == '\0') return;
    if (!isValidChimeKind(kind)) return;

    // Max chime duration is Build Ready (~800ms = 19,200 samples @ 24 kHz)
    constexpr size_t MAX_CHIME_SAMPLES = 20400;
    float* mix_buf = (float*)(psramFound()
        ? ps_malloc(MAX_CHIME_SAMPLES * sizeof(float))
        : malloc(MAX_CHIME_SAMPLES * sizeof(float)));
    int16_t* pcm_buf = (int16_t*)(psramFound()
        ? ps_malloc(MAX_CHIME_SAMPLES * sizeof(int16_t))
        : malloc(MAX_CHIME_SAMPLES * sizeof(int16_t)));

    if (!mix_buf || !pcm_buf) {
        if (mix_buf) free(mix_buf);
        if (pcm_buf) free(pcm_buf);
        return;
    }
    memset(mix_buf, 0, MAX_CHIME_SAMPLES * sizeof(float));
    size_t total_samples = 0;

    if (strcmp(kind, "ptt_start") == 0) {
        // 1C-2A-2 PTT Start (~295ms): Switch latch + F5 (118ms) -> C6 (185ms)
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 698.5f, 118.0f, 0.68f, 7.5f, 2300.0f, 10.0f, 6.0f, 0.38f, 28.0f, 5.2f, 0.006f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.102f, 1046.5f, 185.0f, 0.68f, 6.5f, 2750.0f, 8.0f, 6.0f, 0.36f, 55.0f, 5.2f, 0.007f);
        total_samples = (size_t)(0.300f * CHIME_SR);
    } else if (strcmp(kind, "ptt_stop") == 0) {
        // 1C-2A-2 PTT Stop (~270ms): Switch release + C6 (108ms) -> F5 (165ms) + mechanical lock click
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 1046.5f, 108.0f, 0.68f, 7.5f, 2600.0f, 10.0f, 6.0f, 0.36f, 26.0f, 5.2f, 0.005f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.092f, 698.5f, 165.0f, 0.68f, 6.5f, 1800.0f, 8.0f, 6.0f, 0.36f, 48.0f, 5.2f, 0.006f);
        addSwitchClick(mix_buf, MAX_CHIME_SAMPLES, 0.258f, 6.0f, 1550.0f, 700.0f, 0.80f, 0.45f);
        total_samples = (size_t)(0.272f * CHIME_SR);
    } else if (strcmp(kind, "snap") == 0) {
        // 1C-2A Classic 35mm SLR Focal-Plane Curtain ("ka-tshhh-CHACK" + spring ring)
        addShutterImpact(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 7.0f, 850.0f, 300.0f, 3900.0f, 3200.0f, 0.82f, 11u);
        addShutterImpact(mix_buf, MAX_CHIME_SAMPLES, 0.014f, 19.0f, 580.0f, 155.0f, 3100.0f, 2300.0f, 0.98f, 12u);
        addCurtainZip(mix_buf, MAX_CHIME_SAMPLES, 0.022f, 38.0f, 1900.0f, 4600.0f, 0.68f, 13u);
        addShutterImpact(mix_buf, MAX_CHIME_SAMPLES, 0.068f, 22.0f, 640.0f, 135.0f, 3650.0f, 2850.0f, 1.00f, 14u);
        addShutterImpact(mix_buf, MAX_CHIME_SAMPLES, 0.094f, 11.0f, 420.0f, 160.0f, 2900.0f, 2200.0f, 0.68f, 15u);
        addSpringRingTail(mix_buf, MAX_CHIME_SAMPLES, 0.098f, 55.0f, 3850.0f, 0.26f, 16u);
        total_samples = (size_t)(0.160f * CHIME_SR);
    } else if (strcmp(kind, "thinking") == 0) {
        // 1C-2A Thinking / Tool Pulse (~385ms): Two relay latches into warm G5 -> B5 interval
        addSwitchClick(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 6.5f, 2200.0f, 820.0f, 0.88f, 0.45f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.035f, 784.0f, 140.0f, 0.54f, 5.0f, 2400.0f, 6.0f, 6.0f, 0.36f, 35.0f, 5.2f, 0.0048f);
        addSwitchClick(mix_buf, MAX_CHIME_SAMPLES, 0.165f, 6.5f, 2600.0f, 920.0f, 0.88f, 0.45f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.200f, 987.8f, 180.0f, 0.56f, 5.0f, 2750.0f, 6.0f, 6.0f, 0.36f, 55.0f, 5.2f, 0.006f);
        total_samples = (size_t)(0.390f * CHIME_SR);
    } else if (strcmp(kind, "ready") == 0) {
        // 1C-2A-2 (Warm Studio Fade) Build Complete (~795ms):
        // Fast 298ms 4-step climb + legato overlap + 240ms Wurlitzer-style natural cosine fade-out tail
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 698.5f, 130.0f, 0.68f, 6.5f, 2250.0f, 7.0f, 6.0f, 0.40f, 42.0f, 5.2f, 0.006f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.096f, 880.0f, 130.0f, 0.68f, 6.0f, 2500.0f, 7.0f, 6.0f, 0.40f, 42.0f, 5.2f, 0.006f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.192f, 1046.5f, 135.0f, 0.68f, 6.0f, 2750.0f, 7.0f, 6.0f, 0.40f, 45.0f, 5.2f, 0.006f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.298f, 698.5f, 490.0f, 0.40f, 7.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.298f, 1046.5f, 490.0f, 0.44f, 0.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.298f, 1396.9f, 490.0f, 0.46f, 0.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f);
        total_samples = (size_t)(0.798f * CHIME_SR);
    } else if (strcmp(kind, "error") == 0) {
        // 1C-2A Error / Cancel (~450ms): 3-step descending reed (E5 -> C5 -> A4)
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.000f, 659.3f, 122.0f, 0.68f, 7.0f, 1950.0f, 10.0f, 6.0f, 0.35f, 28.0f, 5.2f, 0.0042f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.105f, 523.3f, 122.0f, 0.68f, 6.0f, 1650.0f, 8.0f, 6.0f, 0.35f, 28.0f, 5.2f, 0.0042f);
        addReedNoteSmooth(mix_buf, MAX_CHIME_SAMPLES, 0.210f, 440.0f, 230.0f, 0.68f, 6.5f, 1350.0f, 8.0f, 6.0f, 0.36f, 75.0f, 5.2f, 0.006f);
        total_samples = (size_t)(0.452f * CHIME_SR);
    }

    if (total_samples > MAX_CHIME_SAMPLES) total_samples = MAX_CHIME_SAMPLES;
    // Two-pole 300 Hz high-pass filter at 24 kHz (alpha = 0.924) to strip sub-bass coil current spikes,
    // paired with 24,000 LSB peak ceiling so chimes are crisp and clearly audible without rail sag.
    const float alpha = 0.924f;
    float x1 = 0.0f, y1 = 0.0f;
    float x2 = 0.0f, y2 = 0.0f;
    for (size_t i = 0; i < total_samples; ++i) {
        float x = tanhf(mix_buf[i] / 28000.0f) * 24000.0f;
        float s1 = alpha * (y1 + x - x1);
        x1 = x;
        y1 = s1;
        float s2 = alpha * (y2 + s1 - x2);
        x2 = s1;
        y2 = s2;
        if (s2 > 32767.0f) s2 = 32767.0f;
        if (s2 < -32768.0f) s2 = -32768.0f;
        pcm_buf[i] = (int16_t)s2;
    }

    AbortCheckFn saved = s_abort_check;
    bool saved_aec = s_live_aec_enabled;
    s_abort_check = nullptr;
    s_live_aec_enabled = false;
    playPcmChunk(pcm_buf, total_samples, CHIME_SR);
    drainSpeakerDma();
    s_live_aec_enabled = saved_aec;
    s_abort_check = saved;
    muteSpeaker();

    free(mix_buf);
    free(pcm_buf);
}

void muteSpeaker() {
    i2s_zero_dma_buffer(PORT_I2S_TX);
    i2s_stop(PORT_I2S_TX);
    s_tx_running = false;
}

} // namespace gotchi_audio
