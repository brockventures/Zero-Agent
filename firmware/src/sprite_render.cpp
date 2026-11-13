// ============================================================================
// Companion sprite renderer — C++ port of poc/gotchi_poc.py
// `render_sprite_200x200`. Coordinates are the PIL ones (inclusive bboxes),
// converted to Adafruit_GFX calls. Arcs and thick strokes are built from
// primitives because Adafruit_GFX has no arc or stroke-width API.
// ============================================================================

#include <math.h>
#include <stdio.h>
#include <string.h>
#include <Fonts/FreeSansBold9pt7b.h>
#include "epd_panel.h"
#include "zero_skin_bitmap.h"
#include "zero_thinking_hand_1bit.h"

namespace {

constexpr int CAPTION_COLS = 31;     // 31 * 6 px body glyphs = 186 px, inside 200 px
constexpr int HEADER_TEXT_MAX_PX = 168;

// Uppercase display names (PIL draws state.upper()).
const char* const UI_STATE_UPPER[UI_STATE_COUNT] = {
    "IDLE", "LISTENING", "TRANSCRIBING", "THINKING", "SYNTHESIZING",
    "SPEAKING", "ERROR", "BUSY", "OFFLINE",
};

// PIL bbox [x0,y0,x1,y1] is inclusive. Circular ellipses only in this sprite.
void boxCenter(int x0, int y0, int x1, int y1, int& cx, int& cy, int& r) {
    cx = (x0 + x1) / 2;
    cy = (y0 + y1) / 2;
    r = (x1 - x0) / 2;
}

void fillBoxCircle(Adafruit_GFX& d, int x0, int y0, int x1, int y1, uint16_t c) {
    int cx, cy, r;
    boxCenter(x0, y0, x1, y1, cx, cy, r);
    d.fillCircle(cx, cy, r, c);
}

// PIL ellipse(outline, width=w): concentric rings drawn inward.
void ringBoxCircle(Adafruit_GFX& d, int x0, int y0, int x1, int y1, int w, uint16_t c) {
    int cx, cy, r;
    boxCenter(x0, y0, x1, y1, cx, cy, r);
    for (int i = 0; i < w && r - i >= 0; i++) {
        d.drawCircle(cx, cy, r - i, c);
    }
}

// PIL rectangle(outline, width=w): drawn inward.
void rectOutline(Adafruit_GFX& d, int x0, int y0, int x1, int y1, int w, uint16_t c) {
    for (int i = 0; i < w; i++) {
        d.drawRect(x0 + i, y0 + i, x1 - x0 + 1 - 2 * i, y1 - y0 + 1 - 2 * i, c);
    }
}

// PIL rounded_rectangle(outline, width=w).
void roundRectOutline(Adafruit_GFX& d, int x0, int y0, int x1, int y1, int r, int w, uint16_t c) {
    for (int i = 0; i < w; i++) {
        int rr = r - i;
        if (rr < 0) rr = 0;
        d.drawRoundRect(x0 + i, y0 + i, x1 - x0 + 1 - 2 * i, y1 - y0 + 1 - 2 * i, rr, c);
    }
}

// PIL line(points, width=w).
void thickLine(Adafruit_GFX& d, int x0, int y0, int x1, int y1, int w, uint16_t c) {
    for (int i = -(w - 1) / 2; i <= w / 2; i++) {
        d.drawLine(x0 + i, y0, x1 + i, y1, c);
        d.drawLine(x0, y0 + i, x1, y1 + i, c);
    }
}

// PIL arc(bbox, start, end, width=w). Angles in degrees, clockwise from +x
// (screen y grows downward, same as PIL). Sampled at 1 degree steps.
void arcStroke(Adafruit_GFX& d, int x0, int y0, int x1, int y1,
               float start_deg, float end_deg, int w, uint16_t c) {
    const float cx = (x0 + x1) / 2.0f;
    const float cy = (y0 + y1) / 2.0f;
    const float rx = (x1 - x0) / 2.0f;
    const float ry = (y1 - y0) / 2.0f;
    for (float a = start_deg; a <= end_deg; a += 1.0f) {
        float rad = a * (3.14159265f / 180.0f);
        int px = (int)lroundf(cx + rx * cosf(rad));
        int py = (int)lroundf(cy + ry * sinf(rad));
        d.fillRect(px - w / 2, py - w / 2, w, w, c);
    }
}

// PIL chord(bbox, start=0, end=180, fill=c): the lower half of the ellipse,
// filled. This sprite uses no other chord angles.
void fillLowerHalfEllipse(Adafruit_GFX& d, int x0, int y0, int x1, int y1, uint16_t c) {
    const float cx = (x0 + x1) / 2.0f;
    const float cy = (y0 + y1) / 2.0f;
    const float rx = (x1 - x0) / 2.0f;
    const float ry = (y1 - y0) / 2.0f;
    for (int py = (int)lroundf(cy); py <= y1; py++) {
        float dy = py - cy;
        float t = 1.0f - (dy / ry) * (dy / ry);
        if (t < 0.0f) t = 0.0f;
        int span = (int)lroundf(rx * sqrtf(t));
        d.drawFastHLine((int)lroundf(cx) - span, py, 2 * span + 1, c);
    }
}

// Draws text clipped to max_chars. The body font is the built-in 6x8 font.
// Adafruit's single-color setTextColor(c) also fills the background with c,
// so every text call passes an explicit background.
void printClipped(Adafruit_GFX& d, int x, int y, const char* s, size_t max_chars,
                  uint16_t fg, uint16_t bg) {
    char buf[CAPTION_COLS + 1];
    size_t n = strlen(s);
    if (n > max_chars) n = max_chars;
    if (n > CAPTION_COLS) n = CAPTION_COLS;
    memcpy(buf, s, n);
    buf[n] = '\0';
    d.setTextColor(fg, bg);
    d.setCursor(x, y);
    d.print(buf);
}

// Word-wraps text into at most max_rows rows of cols chars each.
// Breaks at the last space in a row when one exists, otherwise hard-breaks.
// Text beyond the last row is dropped.
void drawWrapped(Adafruit_GFX& d, int x, int y, int pitch, const char* text,
                 int cols, int max_rows, uint16_t fg, uint16_t bg) {
    size_t n = strlen(text);
    size_t pos = 0;
    for (int row = 0; row < max_rows && pos < n; row++) {
        while (pos < n && text[pos] == ' ') pos++;
        if (pos >= n) break;
        size_t stop = n;
        size_t lim = pos + (size_t)cols;
        if (lim < n) {
            stop = lim;
            size_t b = lim;
            while (b > pos && text[b] != ' ') b--;
            if (b > pos) stop = b;
        }
        printClipped(d, x, y + row * pitch, text + pos, stop - pos, fg, bg);
        pos = stop;
    }
}

// Shrinks text until its bounds fit within max_px, then prints it.
void printFitted(Adafruit_GFX& d, int x, int baseline, char* text, int max_px,
                 uint16_t fg, uint16_t bg) {
    int16_t bx, by;
    uint16_t bw, bh;
    d.getTextBounds(text, 0, 0, &bx, &by, &bw, &bh);
    size_t len = strlen(text);
    while (bw > max_px && len > 0) {
        text[--len] = '\0';
        d.getTextBounds(text, 0, 0, &bx, &by, &bw, &bh);
    }
    d.setTextColor(fg, bg);
    d.setCursor(x, baseline);
    d.print(text);
}

// 9x7 px Wi-Fi signal icon: 2x2 dot at x+4, y+5, inner chevron/arc at y+3, outer chevron/arc at y+1.
void drawWifiIcon(Adafruit_GFX& d, int x, int y, uint16_t fg) {
    d.drawFastHLine(x + 1, y + 1, 7, fg);
    d.drawPixel(x, y + 2, fg);
    d.drawPixel(x + 8, y + 2, fg);

    d.drawFastHLine(x + 3, y + 3, 3, fg);
    d.drawPixel(x + 2, y + 4, fg);
    d.drawPixel(x + 6, y + 4, fg);

    d.fillRect(x + 4, y + 5, 2, 2, fg);
}

// 7x9 px Bluetooth rune: vertical spine at x+3, y..y+8, diagonal crosses, right rune segments.
void drawBleIcon(Adafruit_GFX& d, int x, int y, uint16_t fg) {
    d.drawFastVLine(x + 3, y, 9, fg);
    d.drawLine(x + 1, y + 2, x + 5, y + 6, fg);
    d.drawLine(x + 1, y + 6, x + 5, y + 2, fg);
    d.drawLine(x + 3, y, x + 5, y + 2, fg);
    d.drawLine(x + 3, y + 8, x + 5, y + 6, fg);
}

// Stair-stepped 'Z', 'z', 'z' sleep glyphs near upper temple ((x, y+10), (x+10, y+4), (x+18, y-2)).
void drawSleepZzz(Adafruit_GFX& d, int x, int y, uint16_t fg, uint16_t bg) {
    d.setFont(nullptr);
    d.setTextSize(1);
    d.setTextColor(fg, bg);
    d.setCursor(x, y + 10);
    d.print('Z');
    d.setCursor(x + 10, y + 4);
    d.print('z');
    d.setCursor(x + 18, y - 2);
    d.print('z');
}

}  // namespace

void renderFrame(Adafruit_GFX& d, const GotchiUIState& ui, const char* refresh_mode) {
    d.fillScreen(GxEPD_WHITE);
    d.setTextWrap(false);  // wrapping is done explicitly below; no auto-wrap

    // Head posture shifts per state
    int dx = 0;
    int dy = 0;
    if (ui.state == UiState::LISTENING) {
        dx = 0;
        dy = -2;  // Lean-in
    } else if (ui.state == UiState::THINKING || ui.state == UiState::SYNTHESIZING) {
        dx = -2;
        dy = -2;  // Tilt
    } else if (ui.state == UiState::SPEAKING) {
        dx = 0;
        dy = (ui.anim_frame % 2 == 0) ? -1 : 1;  // Bob dy alternating -1/+1
    }

    const uint8_t frame = ui.anim_frame % 3;
    const bool is_zero = (ui.skin == "zero");
    const bool eyes_closed = ui.blink || ui.sleeping;

    if (is_zero) {
        dx = 0;
        dy = 0;
        int paste_x = 2 + dx;
        if (paste_x < 2) paste_x = 2;
        if (paste_x > 4) paste_x = 4;
        int paste_y = dy;

        d.fillRect(paste_x, max(0, paste_y), 196, min(177, paste_y + 177) - max(0, paste_y), GxEPD_BLACK);
        if (ui.state == UiState::IDLE) {
            d.drawBitmap(paste_x, paste_y, ZERO_IDLE_PORTRAIT_196x134, ZERO_PORTRAIT_W, ZERO_PORTRAIT_H, GxEPD_WHITE);
        } else {
            d.drawBitmap(paste_x, paste_y, ZERO_BASE_PORTRAIT_196x134, ZERO_PORTRAIT_W, ZERO_PORTRAIT_H, GxEPD_WHITE);
            switch (ui.state) {
                case UiState::LISTENING:
                    if (!eyes_closed) {
                        arcStroke(d, 45 + dx, 71 + dy, 69 + dx, 82 + dy, 190, 350, 4, GxEPD_WHITE);
                        arcStroke(d, 87 + dx, 65 + dy, 117 + dx, 78 + dy, 190, 350, 4, GxEPD_WHITE);
                        d.fillRoundRect(50 + dx, 81 + dy, 17, 20, 5, GxEPD_WHITE);
                        d.fillRoundRect(92 + dx, 78 + dy, 20, 21, 6, GxEPD_WHITE);
                    }
                    // Clear mouth faceplate box and draw clean closed smile arc matching idle expression
                    d.fillRect(58 + dx, 118 + dy, 42, 22, GxEPD_WHITE);
                    arcStroke(d, 63 + dx, 118 + dy, 97 + dx, 136 + dy, 20.0f, 160.0f, 3, GxEPD_BLACK);
                    // Balanced concentric soundwave arcs (3 frames)
                    {
                        static const struct { int x0, y0, x1, y1; float s, e; } arcs_left[] = {
                            {14, 77, 26, 105, 125.0f, 235.0f},
                            {8, 71, 22, 111, 125.0f, 235.0f},
                            {2, 65, 18, 117, 125.0f, 235.0f},
                        };
                        static const struct { int x0, y0, x1, y1; float s, e; } arcs_right[] = {
                            {174, 65, 186, 93, -55.0f, 55.0f},
                            {179, 59, 193, 99, -55.0f, 55.0f},
                            {184, 53, 198, 105, -55.0f, 55.0f},
                        };
                        for (int i = 0; i <= frame; i++) {
                            arcStroke(d, arcs_left[i].x0 + dx, arcs_left[i].y0 + dy, arcs_left[i].x1 + dx, arcs_left[i].y1 + dy, arcs_left[i].s, arcs_left[i].e, 2, GxEPD_BLACK);
                            arcStroke(d, arcs_right[i].x0 + dx, arcs_right[i].y0 + dy, arcs_right[i].x1 + dx, arcs_right[i].y1 + dy, arcs_right[i].s, arcs_right[i].e, 2, GxEPD_BLACK);
                        }
                    }
                    break;
                case UiState::SPEAKING: {
                    if (!ui.blink) {
                        arcStroke(d, 43 + dx, 73 + dy, 67 + dx, 84 + dy, 190, 350, 4, GxEPD_WHITE);
                        arcStroke(d, 85 + dx, 68 + dy, 115 + dx, 81 + dy, 190, 350, 4, GxEPD_WHITE);
                        d.fillRoundRect(48 + dx, 82 + dy, 17, 18, 5, GxEPD_WHITE);
                        d.fillRoundRect(90 + dx, 79 + dy, 20, 19, 6, GxEPD_WHITE);
                    }
                    // Bouncing 5-bar equalizer shifted x = 4 + i * 5
                    static const int eq_patterns[3][5] = {
                        {8, 16, 12, 18, 10},
                        {14, 8, 18, 10, 16},
                        {18, 12, 8, 16, 12},
                    };
                    const int* eq_b = eq_patterns[frame];
                    for (int i = 0; i < 5; i++) {
                        int bh = eq_b[i];
                        d.fillRect(4 + i * 5, 121 - bh / 2, 3, bh, GxEPD_BLACK);
                    }
                    // 3 mouth shapes
                    if (frame == 0) {
                        fillLowerHalfEllipse(d, 61 + dx, 119 + dy, 95 + dx, 139 + dy, GxEPD_BLACK);
                        d.fillRect(65 + dx, 128 + dy, 28, 3, GxEPD_WHITE);
                    } else if (frame == 1) {
                        fillBoxCircle(d, 70 + dx, 124 + dy, 86 + dx, 138 + dy, GxEPD_BLACK);
                        fillBoxCircle(d, 74 + dx, 127 + dy, 82 + dx, 135 + dy, GxEPD_WHITE);
                    } else if (frame == 2) {
                        d.fillRoundRect(63 + dx, 127 + dy, 29, 7, 3, GxEPD_BLACK);
                        d.fillRect(66 + dx, 129 + dy, 23, 3, GxEPD_WHITE);
                    }
                    break;
                }
                case UiState::TRANSCRIBING: {
                    if (!ui.blink) {
                        d.fillRoundRect(45 + frame, 84, 17, 17, 5, GxEPD_WHITE);
                        d.fillRoundRect(88 + frame, 81, 20, 18, 6, GxEPD_WHITE);
                    }
                    thickLine(d, 65, 131, 90, 129, 3, GxEPD_BLACK);

                    static const struct { int x, y; } wave_pts[] = {
                        {8, 146}, {12, 140}, {16, 147}, {20, 140}, {24, 146}, {28, 140}, {32, 146}
                    };
                    const int end_idx = 2 + frame * 2;
                    for (int i = 0; i < end_idx; i++) {
                        thickLine(d, wave_pts[i].x, wave_pts[i].y, wave_pts[i + 1].x, wave_pts[i + 1].y, 2, GxEPD_BLACK);
                    }

                    // Scribbling pencil at wave_pts[end_idx]
                    static const float pencil_angles[3] = {-108.0f, -114.0f, -118.0f};
                    float ang = pencil_angles[frame];
                    float rad = ang * (3.14159265f / 180.0f);
                    float ux = cosf(rad);
                    float uy = sinf(rad);
                    float nx = -uy;
                    float ny = ux;
                    float hw = 2.5f;
                    float length = 24.0f;
                    int tip_x = wave_pts[end_idx].x;
                    int tip_y = wave_pts[end_idx].y;
                    float cb_x = tip_x + ux * 6.0f;
                    float cb_y = tip_y + uy * 6.0f;
                    float ee_x = tip_x + ux * length;
                    float ee_y = tip_y + uy * length;

                    // Cone
                    d.fillTriangle(tip_x, tip_y,
                                   (int)lroundf(cb_x + nx * hw), (int)lroundf(cb_y + ny * hw),
                                   (int)lroundf(cb_x - nx * hw), (int)lroundf(cb_y - ny * hw),
                                   GxEPD_BLACK);
                    // Shaft
                    d.fillTriangle((int)lroundf(cb_x + nx * hw), (int)lroundf(cb_y + ny * hw),
                                   (int)lroundf(ee_x + nx * hw), (int)lroundf(ee_y + ny * hw),
                                   (int)lroundf(cb_x - nx * hw), (int)lroundf(cb_y - ny * hw),
                                   GxEPD_BLACK);
                    d.fillTriangle((int)lroundf(ee_x + nx * hw), (int)lroundf(ee_y + ny * hw),
                                   (int)lroundf(ee_x - nx * hw), (int)lroundf(ee_y - ny * hw),
                                   (int)lroundf(cb_x - nx * hw), (int)lroundf(cb_y - ny * hw),
                                   GxEPD_BLACK);
                    break;
                }
                case UiState::THINKING:
                case UiState::SYNTHESIZING:
                    if (!ui.blink) {
                        thickLine(d, 44 + dx, 74 + dy, 67 + dx, 72 + dy, 3, GxEPD_WHITE);
                        arcStroke(d, 84 + dx, 64 + dy, 114 + dx, 80 + dy, 190, 350, 4, GxEPD_WHITE);
                        d.fillRoundRect(47 + dx, 80 + dy, 17, 18, 5, GxEPD_WHITE);
                        d.fillRoundRect(88 + dx, 77 + dy, 20, 20, 6, GxEPD_WHITE);
                    }
                    d.fillRect(58 + dx, 118 + dy, 42, 20, GxEPD_WHITE);
                    thickLine(d, 65 + dx, 132 + dy, 91 + dx, 129 + dy, 3, GxEPD_BLACK);
                    // Two trail bubbles arcing up from Zero's forehead/temple into the thought cloud
                    ringBoxCircle(d, 28 + dx, 43 + dy, 35 + dx, 50 + dy, 2, GxEPD_BLACK);
                    ringBoxCircle(d, 22 + dx, 30 + dy, 33 + dx, 41 + dy, 2, GxEPD_BLACK);
                    // Puffy 4-lobe thought cloud
                    fillBoxCircle(d, 19 + dx, 9 + dy, 37 + dx, 27 + dy, GxEPD_BLACK);
                    fillBoxCircle(d, 28 + dx, 2 + dy, 48 + dx, 22 + dy, GxEPD_BLACK);
                    fillBoxCircle(d, 41 + dx, 6 + dy, 59 + dx, 24 + dy, GxEPD_BLACK);
                    fillBoxCircle(d, 29 + dx, 9 + dy, 49 + dx, 29 + dy, GxEPD_BLACK);
                    fillBoxCircle(d, 21 + dx, 11 + dy, 35 + dx, 25 + dy, GxEPD_WHITE);
                    fillBoxCircle(d, 30 + dx, 4 + dy, 46 + dx, 20 + dy, GxEPD_WHITE);
                    fillBoxCircle(d, 43 + dx, 8 + dy, 57 + dx, 22 + dy, GxEPD_WHITE);
                    fillBoxCircle(d, 31 + dx, 11 + dy, 47 + dx, 27 + dy, GxEPD_WHITE);
                    // 1/2/3 ellipsis dots inside thought cloud
                    d.fillRect(28 + dx, 13 + dy, 23, 7, GxEPD_WHITE);
                    {
                        static const int dot_xs[3] = {30, 38, 46};
                        for (int i = 0; i <= frame; i++) {
                            d.fillRect(dot_xs[i] + dx, 14 + dy, 4, 4, GxEPD_BLACK);
                        }
                    }
                    {
                        int hx = dx + (frame - 1) * 2;
                        int hy = dy + (1 - frame);
                        d.drawBitmap(18 + hx, 112 + hy, ZERO_HAND_A1_MASK_88x65, 88, 65, GxEPD_BLACK);
                        d.drawBitmap(18 + hx, 112 + hy, ZERO_HAND_A1_WHITE_88x65, 88, 65, GxEPD_WHITE);
                    }
                    break;
                case UiState::BUSY:
                    if (!ui.blink) {
                        thickLine(d, 44 + dx, 75 + dy, 67 + dx, 75 + dy, 3, GxEPD_WHITE);
                        thickLine(d, 85 + dx, 72 + dy, 111 + dx, 72 + dy, 3, GxEPD_WHITE);
                        d.fillTriangle(48 + dx, 82 + dy, 64 + dx, 82 + dy, 56 + dx, 90 + dy, GxEPD_WHITE);
                        d.fillTriangle(48 + dx, 100 + dy, 64 + dx, 100 + dy, 56 + dx, 90 + dy, GxEPD_WHITE);
                        d.fillTriangle(90 + dx, 79 + dy, 107 + dx, 79 + dy, 99 + dx, 88 + dy, GxEPD_WHITE);
                        d.fillTriangle(90 + dx, 96 + dy, 107 + dx, 96 + dy, 99 + dx, 88 + dy, GxEPD_WHITE);
                    }
                    thickLine(d, 66 + dx, 131 + dy, 94 + dx, 131 + dy, 3, GxEPD_BLACK);
                    break;
                case UiState::OFFLINE:
                    if (!ui.blink) {
                        arcStroke(d, 45 + dx, 83 + dy, 67 + dx, 97 + dy, 15, 165, 3, GxEPD_WHITE);
                        arcStroke(d, 87 + dx, 80 + dy, 111 + dx, 94 + dy, 15, 165, 3, GxEPD_WHITE);
                    }
                    thickLine(d, 72 + dx, 132 + dy, 88 + dx, 132 + dy, 2, GxEPD_BLACK);
                    break;
                default:
                    if (!ui.blink) {
                        thickLine(d, 47 + dx, 82 + dy, 65 + dx, 100 + dy, 4, GxEPD_WHITE);
                        thickLine(d, 47 + dx, 100 + dy, 65 + dx, 82 + dy, 4, GxEPD_WHITE);
                        thickLine(d, 88 + dx, 79 + dy, 109 + dx, 98 + dy, 4, GxEPD_WHITE);
                        thickLine(d, 88 + dx, 98 + dy, 109 + dx, 79 + dy, 4, GxEPD_WHITE);
                    }
                    thickLine(d, 63 + dx, 134 + dy, 98 + dx, 134 + dy, 3, GxEPD_BLACK);
                    break;
            }
        }

        // ui.blink / ui.sleeping: closed horizontal eyelid lines on Zero skin
        if (eyes_closed && (ui.state == UiState::IDLE ||
                            ui.state == UiState::LISTENING ||
                            ui.state == UiState::SPEAKING ||
                            ui.state == UiState::TRANSCRIBING)) {
            d.fillRect(40 + dx, 64 + dy, 32, 41, GxEPD_BLACK);
            d.fillRect(82 + dx, 60 + dy, 38, 43, GxEPD_BLACK);
            thickLine(d, 45 + dx, 82 + dy, 68 + dx, 82 + dy, 3, GxEPD_WHITE);
            thickLine(d, 87 + dx, 78 + dy, 114 + dx, 78 + dy, 3, GxEPD_WHITE);
        }

        if (ui.sleeping) {
            d.fillRoundRect(16, 26, 30, 24, 4, GxEPD_WHITE);
            drawSleepZzz(d, 18, 30, GxEPD_BLACK, GxEPD_WHITE);
        }

        // Ensure side borders stay white (2px margin)
        d.fillRect(0, 0, 2, 200, GxEPD_WHITE);
        d.fillRect(198, 0, 2, 200, GxEPD_WHITE);

        // Bottom light status bar (y = 179..199) in GxEPD_WHITE with GxEPD_BLACK text
        d.fillRect(0, 179, 200, 21, GxEPD_WHITE);
        d.setFont(&FreeSansBold9pt7b);
        char header[40];
        if (ui.sleeping) {
            snprintf(header, sizeof(header), "ZERO //ZZZ");
        } else if (ui.state == UiState::IDLE) {
            snprintf(header, sizeof(header), "ZERO");
        } else {
            snprintf(header, sizeof(header), "ZERO // %s", UI_STATE_UPPER[static_cast<uint8_t>(ui.state)]);
        }
        printFitted(d, 5, 194, header, HEADER_TEXT_MAX_PX, GxEPD_BLACK, GxEPD_WHITE);

        // Top-right status & battery badge with white backing box
        const bool has_bat = (ui.wall_power || ui.battery_pct >= 0);
        if (has_bat || ui.wifi_active || ui.ble_active) {
            char bat_badge[10] = "";
            int badge_x = 196;
            if (has_bat) {
                if (ui.wall_power) {
                    if (ui.battery_pct >= 0) {
                        snprintf(bat_badge, sizeof(bat_badge), "%d%%+", (int)ui.battery_pct);
                    } else {
                        snprintf(bat_badge, sizeof(bat_badge), "CHG");
                    }
                } else {
                    snprintf(bat_badge, sizeof(bat_badge), "%d%%", (int)ui.battery_pct);
                }
                badge_x = 195 - (int)strlen(bat_badge) * 6;
            }

            int cur_x = has_bat ? (badge_x - 3) : 195;
            int ble_x = -1;
            int wifi_x = -1;

            if (ui.ble_active) {
                cur_x -= 7;
                ble_x = cur_x;
                cur_x -= 3;
            }
            if (ui.wifi_active) {
                cur_x -= 9;
                wifi_x = cur_x;
                cur_x -= 3;
            }

            int box_left = (wifi_x >= 0 ? wifi_x : (ble_x >= 0 ? ble_x : badge_x)) - 2;
            if (box_left < 2) box_left = 2;
            d.fillRect(box_left, 4, 198 - box_left, 11, GxEPD_WHITE);

            if (wifi_x >= 0) {
                drawWifiIcon(d, wifi_x, 6, GxEPD_BLACK);
            }
            if (ble_x >= 0) {
                drawBleIcon(d, ble_x, 5, GxEPD_BLACK);
            }
            if (has_bat) {
                d.setFont(nullptr);
                d.setTextSize(1);
                d.setTextColor(GxEPD_BLACK, GxEPD_WHITE);
                d.setCursor(badge_x, 6);
                d.print(bat_badge);
            }
        }
        return;
    }

    // Header bar (y = 0..20): "GOTCHI // STATE" left, refresh badge right.
    d.fillRect(0, 0, 200, 21, GxEPD_BLACK);
    d.setFont(&FreeSansBold9pt7b);
    char header[40];
    if (ui.sleeping) {
        snprintf(header, sizeof(header), "GOTCHI // SLEEP");
    } else {
        snprintf(header, sizeof(header), "GOTCHI // %s", UI_STATE_UPPER[static_cast<uint8_t>(ui.state)]);
    }

    const bool has_bat = (ui.wall_power || ui.battery_pct >= 0);
    char bat_badge[10] = "";
    int badge_x = 195;
    if (has_bat) {
        if (ui.wall_power) {
            if (ui.battery_pct >= 0) {
                snprintf(bat_badge, sizeof(bat_badge), "%d%%+", (int)ui.battery_pct);
            } else {
                snprintf(bat_badge, sizeof(bat_badge), "CHG");
            }
        } else {
            snprintf(bat_badge, sizeof(bat_badge), "%d%%", (int)ui.battery_pct);
        }
        badge_x = 195 - (int)strlen(bat_badge) * 6;
        if (badge_x < 152) badge_x = 152;
    } else {
        badge_x = 164;
    }

    int cur_x = badge_x - 3;
    int ble_x = -1;
    int wifi_x = -1;

    if (ui.ble_active) {
        cur_x -= 7;
        ble_x = cur_x;
        cur_x -= 3;
    }
    if (ui.wifi_active) {
        cur_x -= 9;
        wifi_x = cur_x;
        cur_x -= 3;
    }

    int leftmost = (wifi_x >= 0 ? wifi_x : (ble_x >= 0 ? ble_x : badge_x));
    int max_header_px = HEADER_TEXT_MAX_PX;
    if (leftmost - 4 < max_header_px) {
        max_header_px = leftmost - 4;
    }
    printFitted(d, 5, 15, header, max_header_px, GxEPD_WHITE, GxEPD_BLACK);

    if (wifi_x >= 0) {
        drawWifiIcon(d, wifi_x, 7, GxEPD_WHITE);
    }
    if (ble_x >= 0) {
        drawBleIcon(d, ble_x, 6, GxEPD_WHITE);
    }

    d.setFont(nullptr);
    d.setTextSize(1);
    d.setTextColor(GxEPD_WHITE, GxEPD_BLACK);
    if (has_bat) {
        d.setCursor(badge_x, 7);
        d.print(bat_badge);
    } else {
        d.setCursor(badge_x, 7);
        d.print(strcmp(refresh_mode, "full") == 0 ? "FULL" : "PART");
    }

    // Outer border, width 2.
    rectOutline(d, 0, 0, 199, 199, 2, GxEPD_BLACK);

    // Character body: rounded box with two ears.
    d.fillRoundRect(42 + dx, 30 + dy, 117, 105, 18, GxEPD_WHITE);
    roundRectOutline(d, 42 + dx, 30 + dy, 158 + dx, 134 + dy, 18, 3, GxEPD_BLACK);
    d.fillTriangle(56 + dx, 30 + dy, 66 + dx, 18 + dy, 76 + dx, 30 + dy, GxEPD_BLACK);
    d.fillTriangle(124 + dx, 30 + dy, 134 + dx, 18 + dy, 144 + dx, 30 + dy, GxEPD_BLACK);

    // Face per state.
    if (eyes_closed) {
        thickLine(d, 64 + dx, 70 + dy, 84 + dx, 70 + dy, 3, GxEPD_BLACK);
        thickLine(d, 116 + dx, 70 + dy, 136 + dx, 70 + dy, 3, GxEPD_BLACK);
    } else {
        switch (ui.state) {
            case UiState::IDLE:
                // Calm eyes with highlights, subtle smile.
                fillBoxCircle(d, 66 + dx, 62 + dy, 82 + dx, 78 + dy, GxEPD_BLACK);
                fillBoxCircle(d, 118 + dx, 62 + dy, 134 + dx, 78 + dy, GxEPD_BLACK);
                fillBoxCircle(d, 70 + dx, 65 + dy, 75 + dx, 70 + dy, GxEPD_WHITE);
                fillBoxCircle(d, 122 + dx, 65 + dy, 127 + dx, 70 + dy, GxEPD_WHITE);
                break;

            case UiState::LISTENING:
                // Wide attentive eyes, open "O" mouth, sound-wave arcs.
                ringBoxCircle(d, 62 + dx, 58 + dy, 84 + dx, 80 + dy, 3, GxEPD_BLACK);
                fillBoxCircle(d, 69 + dx, 65 + dy, 77 + dx, 73 + dy, GxEPD_BLACK);
                ringBoxCircle(d, 116 + dx, 58 + dy, 138 + dx, 80 + dy, 3, GxEPD_BLACK);
                fillBoxCircle(d, 123 + dx, 65 + dy, 131 + dx, 73 + dy, GxEPD_BLACK);
                break;

            case UiState::TRANSCRIBING:
                // Eyes looking right, waveform bars.
                d.fillRect(64 + dx, 66 + dy, 21, 11, GxEPD_BLACK);
                d.fillRect(116 + dx, 66 + dy, 21, 11, GxEPD_BLACK);
                break;

            case UiState::THINKING:
            case UiState::SYNTHESIZING:
                // Eyes looking up, mouth line, thought bubbles.
                fillBoxCircle(d, 66 + dx, 56 + dy, 82 + dx, 72 + dy, GxEPD_BLACK);
                fillBoxCircle(d, 118 + dx, 56 + dy, 134 + dx, 72 + dy, GxEPD_BLACK);
                break;

            case UiState::SPEAKING:
                // Chevron eyes `^ ^`, open talking mouth.
                thickLine(d, 64 + dx, 74 + dy, 74 + dx, 62 + dy, 3, GxEPD_BLACK);
                thickLine(d, 74 + dx, 62 + dy, 84 + dx, 74 + dy, 3, GxEPD_BLACK);
                thickLine(d, 116 + dx, 74 + dy, 126 + dx, 62 + dy, 3, GxEPD_BLACK);
                thickLine(d, 126 + dx, 62 + dy, 136 + dx, 74 + dy, 3, GxEPD_BLACK);
                break;

            default:
                // ERROR / BUSY / OFFLINE: X eyes, flat/wavy mouth.
                thickLine(d, 64 + dx, 60 + dy, 82 + dx, 78 + dy, 3, GxEPD_BLACK);
                thickLine(d, 82 + dx, 60 + dy, 64 + dx, 78 + dy, 3, GxEPD_BLACK);
                thickLine(d, 118 + dx, 60 + dy, 136 + dx, 78 + dy, 3, GxEPD_BLACK);
                thickLine(d, 136 + dx, 60 + dy, 118 + dx, 78 + dy, 3, GxEPD_BLACK);
                break;
        }
    }

    if (ui.sleeping) {
        drawSleepZzz(d, 145, 35, GxEPD_BLACK, GxEPD_WHITE);
    }

    // Mouth and per-state animations
    switch (ui.state) {
        case UiState::IDLE:
            arcStroke(d, 82 + dx, 88 + dy, 118 + dx, 110 + dy, 15, 165, 3, GxEPD_BLACK);
            break;

        case UiState::LISTENING: {
            // Attentive open mouth
            ringBoxCircle(d, 91 + dx, 94 + dy, 109 + dx, 112 + dy, 3, GxEPD_BLACK);
            // Pulsing sound wave arcs (frame % 3: 1, 2, or 3 arcs)
            static const struct { int x0, y0, x1, y1; float s, e; } arcs_l[] = {
                {28, 64, 40, 100, 120.0f, 240.0f},
                {20, 56, 36, 108, 120.0f, 240.0f},
                {12, 48, 32, 116, 120.0f, 240.0f},
            };
            static const struct { int x0, y0, x1, y1; float s, e; } arcs_r[] = {
                {160, 64, 172, 100, -60.0f, 60.0f},
                {164, 56, 180, 108, -60.0f, 60.0f},
                {168, 48, 188, 116, -60.0f, 60.0f},
            };
            for (int i = 0; i <= frame; i++) {
                arcStroke(d, arcs_l[i].x0, arcs_l[i].y0, arcs_l[i].x1, arcs_l[i].y1, arcs_l[i].s, arcs_l[i].e, 2, GxEPD_BLACK);
                arcStroke(d, arcs_r[i].x0, arcs_r[i].y0, arcs_r[i].x1, arcs_r[i].y1, arcs_r[i].s, arcs_r[i].e, 2, GxEPD_BLACK);
            }
            break;
        }

        case UiState::TRANSCRIBING: {
            static const int bars[] = {6, 14, 10, 18, 8, 14, 6};
            for (int idx = 0; idx < 7; idx++) {
                int h = bars[idx];
                int bx = 72 + dx + idx * 9;
                int top = 104 + dy - h / 2;
                int bottom = 104 + dy + h / 2;
                d.fillRect(bx, top, 6, bottom - top + 1, GxEPD_BLACK);
            }
            break;
        }

        case UiState::THINKING:
        case UiState::SYNTHESIZING: {
            thickLine(d, 84 + dx, 102 + dy, 116 + dx, 102 + dy, 3, GxEPD_BLACK);
            // Temple bubbles
            ringBoxCircle(d, 162, 48, 170, 56, 2, GxEPD_BLACK);
            ringBoxCircle(d, 170, 36, 182, 48, 2, GxEPD_BLACK);
            // Thought bubble with 1/2/3 ellipsis dots
            arcStroke(d, 160, 20, 194, 38, 0, 360, 2, GxEPD_BLACK);
            static const int dot_xs[3] = {168, 176, 184};
            for (int i = 0; i <= frame; i++) {
                fillBoxCircle(d, dot_xs[i] - 2, 27, dot_xs[i] + 2, 31, GxEPD_BLACK);
            }
            break;
        }

        case UiState::SPEAKING: {
            // 3 mouth shapes based on frame % 3
            if (frame == 0) {
                fillLowerHalfEllipse(d, 80 + dx, 90 + dy, 120 + dx, 118 + dy, GxEPD_BLACK);
            } else if (frame == 1) {
                fillBoxCircle(d, 92 + dx, 96 + dy, 108 + dx, 112 + dy, GxEPD_BLACK);
            } else if (frame == 2) {
                d.fillRect(84 + dx, 102 + dy, 33, 6, GxEPD_BLACK);
            }
            // Bouncing 5-bar equalizer
            static const int eq_patterns[3][5] = {
                {8, 16, 12, 18, 10},
                {14, 8, 18, 10, 16},
                {18, 12, 8, 16, 12},
            };
            const int* eq_b = eq_patterns[frame];
            for (int i = 0; i < 5; i++) {
                int bh = eq_b[i];
                int bx = 14 + i * 5;
                d.fillRect(bx, 96 - bh / 2, 4, bh + 1, GxEPD_BLACK);
            }
            break;
        }

        default:
            arcStroke(d, 82 + dx, 98 + dy, 118 + dx, 118 + dy, 195, 345, 3, GxEPD_BLACK);
            break;
    }

    // Caption tray (y = 140..199): primary text, status line, SR/B info.
    d.fillRect(0, 140, 200, 2, GxEPD_BLACK);
    const char* primary = ui.reply.length() ? ui.reply.c_str()
                        : ui.transcript.length() ? ui.transcript.c_str()
                        : ui.status_text.length() ? ui.status_text.c_str()
                        : "READY";
    drawWrapped(d, 6, 146, 10, primary, CAPTION_COLS, 2, GxEPD_BLACK, GxEPD_WHITE);

    // Python shows the SR/bytes diagnostic only when there is no reply or transcript.
    if (!ui.reply.length() && !ui.transcript.length()) {
        char info[32];
        snprintf(info, sizeof(info), "SR:%uk B:%lu",
                 (unsigned)(ui.last_sample_rate / 1000),
                 (unsigned long)ui.audio_bytes_received);
        printClipped(d, 6, 168, info, CAPTION_COLS, GxEPD_BLACK, GxEPD_WHITE);
    }
    printClipped(d, 6, 182, ui.status_text.c_str(), CAPTION_COLS, GxEPD_BLACK, GxEPD_WHITE);
}
