#!/usr/bin/env python3
"""
GOTCHI V1 — Bench POC Client & 200x200 1-Bit E-Paper Sprite Engine
==================================================================
Runs on Raspberry Pi Zero W (driving the attached Waveshare e-ink panel or saving 200x200
1-bit framebuffers for webcam verification) and talks to GOTCHI's `backend/voice_gateway.py`
`POST /api/voice/interact` SSE pipeline.

Features:
- Exact 200x200 1-bit monochrome companion sprite renderer matching the Waveshare 1.54" V2
  (SSD1681) resolution across all 7 states: `idle`, `listening`, `transcribing`, `thinking`,
  `synthesizing`, `speaking`, `error` (plus `busy` / `offline`).
- Refresh policy ported from `client/eink-node` (`PanelManager`):
  - Full refresh on first frame, after `max_consecutive_partials` (8) partials, or after
    `full_refresh_minutes` (15 min); automatic full-refresh recovery if a partial fails.
  - Bounded 30s `BUSY` timeout safeguard.
- Full Mirrormere `/api/voice/interact` multipart 16 kHz mono PCM WAV uploader and streaming
  SSE event parser (`state`, `transcript`, `status`, `thinking`, `reply`, `audio_chunk`
  base64 24 kHz PCM decode, `error`, `done`).
- Pi Zero W adapter mode (`--epd-7in5`): centers the 200x200 GOTCHI viewport inside a 1:1
  bezel frame on the Pi Zero W's attached e-ink display so Amos's webcam can visually verify
  real e-ink state transitions live on hardware.
"""

import argparse
import base64
import io
import json
import math
import os
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

VALID_STATES = (
    "idle",
    "listening",
    "transcribing",
    "thinking",
    "synthesizing",
    "speaking",
    "error",
    "busy",
    "offline",
)


def wrap_pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM bytes in a standard 44-byte RIFF WAV header."""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,  # bits per sample
        b"data",
        data_size,
    )
    return header + pcm_bytes


def build_multipart_wav(
    wav_bytes: Optional[bytes] = None,
    node_id: str = "gotchi-poc-01",
    session_id: str = "sess-poc-01",
    boundary: str = "---GotchiBoundary7MA4YWxkTrZu0gW",
    image_jpeg: Optional[bytes] = None,
    burst_images: Optional[List[bytes]] = None,
    prompt: Optional[str] = None,
    audio_out_mode: Optional[str] = None,
) -> Tuple[bytes, str]:
    """Construct multipart/form-data payload for POST /api/voice/interact."""
    crlf = b"\r\n"
    parts = []

    if wav_bytes is not None and len(wav_bytes) > 0:
        parts.extend([
            b"--" + boundary.encode() + crlf,
            b'Content-Disposition: form-data; name="audio"; filename="ptt.wav"' + crlf,
            b"Content-Type: audio/wav" + crlf + crlf,
            wav_bytes + crlf,
        ])

    if burst_images:
        for i, img_b in enumerate(burst_images):
            if img_b:
                parts.extend([
                    b"--" + boundary.encode() + crlf,
                    f'Content-Disposition: form-data; name="image_{i}"; filename="snap_{i}.jpg"'.encode() + crlf,
                    b"Content-Type: image/jpeg" + crlf + crlf,
                    img_b + crlf,
                ])
    elif image_jpeg:
        parts.extend([
            b"--" + boundary.encode() + crlf,
            b'Content-Disposition: form-data; name="image"; filename="snap.jpg"' + crlf,
            b"Content-Type: image/jpeg" + crlf + crlf,
            image_jpeg + crlf,
        ])

    parts.extend([
        b"--" + boundary.encode() + crlf,
        b'Content-Disposition: form-data; name="node_id"' + crlf + crlf,
        node_id.encode() + crlf,
        b"--" + boundary.encode() + crlf,
        b'Content-Disposition: form-data; name="session_id"' + crlf + crlf,
        session_id.encode() + crlf,
    ])

    if prompt:
        parts.extend([
            b"--" + boundary.encode() + crlf,
            b'Content-Disposition: form-data; name="prompt"' + crlf + crlf,
            prompt.encode() + crlf,
        ])

    if audio_out_mode:
        parts.extend([
            b"--" + boundary.encode() + crlf,
            b'Content-Disposition: form-data; name="audio_out_mode"' + crlf + crlf,
            audio_out_mode.encode() + crlf,
        ])

    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def count_dirty_pixels(im1: Image.Image, im2: Image.Image) -> int:
    """Count differing 1-bit monochrome pixels between two frames via bit diffs."""
    b1 = im1.convert("1").tobytes()
    b2 = im2.convert("1").tobytes()
    return sum((x ^ y).bit_count() for x, y in zip(b1, b2))


@dataclass
class RefreshTracker:
    """Implements the `PanelManager.write_frame` partial/full refresh policy with 30s BUSY guard."""
    max_consecutive_partials: int = 35
    max_partials_hard_ceiling: int = 80
    dirty_pixel_budget: int = 140000
    idle_scrub_defer_s: float = 75.0
    cumulative_dirty_pixels: int = 0
    full_refresh_minutes: float = 15.0
    busy_timeout_s: float = 30.0
    has_drawn_initial: bool = False
    consecutive_partials: int = 0
    last_full_refresh_ts: float = 0.0
    history: List[str] = field(default_factory=list)

    @property
    def hard_ceiling(self) -> int:
        return self.max_partials_hard_ceiling

    @hard_ceiling.setter
    def hard_ceiling(self, val: int):
        self.max_partials_hard_ceiling = val

    @property
    def max_dirty_pixels(self) -> int:
        return self.dirty_pixel_budget

    @max_dirty_pixels.setter
    def max_dirty_pixels(self, val: int):
        self.dirty_pixel_budget = val

    def needs_scrub(self, now: Optional[float] = None) -> bool:
        """Check if periodic scrub thresholds (partials, dirty-pixel budget, or time) are exceeded."""
        now = time.monotonic() if now is None else now
        if self.consecutive_partials >= self.max_consecutive_partials:
            return True
        if self.cumulative_dirty_pixels >= self.dirty_pixel_budget:
            return True
        if (now - self.last_full_refresh_ts) >= (self.full_refresh_minutes * 60.0):
            return True
        return False

    def select_refresh_mode(
        self,
        now: Optional[float] = None,
        force_full: bool = False,
        idle_duration_s: float = 0.0,
    ) -> str:
        now = time.monotonic() if now is None else now
        if force_full or not self.has_drawn_initial:
            return "full"
        if self.consecutive_partials >= self.max_partials_hard_ceiling:
            return "full"
        if self.needs_scrub(now=now):
            if idle_duration_s >= self.idle_scrub_defer_s:
                return "full"
        return "partial"

    def record_write(
        self,
        mode: str,
        succeeded: bool = True,
        now: Optional[float] = None,
        dirty_pixels: int = 0,
    ) -> str:
        now = time.monotonic() if now is None else now
        if not succeeded and mode == "partial":
            # Failed partial recovers immediately with a full refresh
            mode = "full"
        if mode == "full":
            self.has_drawn_initial = True
            self.consecutive_partials = 0
            self.cumulative_dirty_pixels = 0
            self.last_full_refresh_ts = now
        else:
            self.consecutive_partials += 1
            self.cumulative_dirty_pixels += dirty_pixels
        self.history.append(mode)
        return mode


@dataclass
class GotchiUIState:
    state: str = "idle"
    transcript: str = ""
    reply: str = ""
    status_text: str = "READY (HOLD PTT)"
    thinking_seconds: int = 0
    audio_bytes_received: int = 0
    last_sample_rate: int = 24000
    error_code: str = ""
    skin: str = "gotchi"  # "gotchi" | "zero"
    anim_frame: int = 0
    blink: bool = False
    battery_pct: int = -1
    battery_mv: int = 0
    wall_power: bool = False


def parse_sse_stream(
    lines: Iterable[str],
    ui: Optional[GotchiUIState] = None,
    on_state_change: Optional[Callable[[GotchiUIState], None]] = None,
    on_audio_pcm: Optional[Callable[[bytes, int, bool], None]] = None,
) -> GotchiUIState:
    """
    Parse Mirrormere `text/event-stream` lines (`event: <name>` + `data: <json>`) and update
    GOTCHI device state + decode base64 PCM audio chunks (`24 kHz` default).
    """
    if ui is None:
        ui = GotchiUIState()

    cur_event: Optional[str] = None
    cur_data_lines: List[str] = []

    def flush_event(ev: Optional[str], data_lines: List[str]):
        if not ev or not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}

        prev_state = ui.state
        if ev == "state":
            st = payload.get("state", "idle")
            ui.state = st if st in VALID_STATES else "idle"
            ui.status_text = st.upper()
        elif ev == "transcript":
            ui.transcript = payload.get("transcript", "")
            spk = payload.get("speaker", "")
            ui.status_text = f"HEARD ({spk})" if spk else "HEARD"
        elif ev == "status":
            ui.status_text = payload.get("status", ui.status_text)
        elif ev == "thinking":
            ui.state = "thinking"
            ui.thinking_seconds = int(payload.get("elapsed_seconds", 0))
            ui.status_text = f"THINKING ({ui.thinking_seconds}s)"
        elif ev == "reply":
            ui.reply = payload.get("reply", "")
            ui.status_text = "SPEAKING"
        elif ev in ("audio_chunk", "sentence"):
            # Live gateway (2026-10-18) sends `sentence` with `audio_b64`; legacy
            # `audio_chunk` carries the same PCM under `data`.
            ui.state = "speaking"
            b64 = payload.get("audio_b64" if ev == "sentence" else "data", "")
            sr = int(payload.get("sample_rate", 24000) or 24000)
            is_final = bool(payload.get("is_final", False))
            pcm = base64.b64decode(b64) if b64 else b""
            ui.audio_bytes_received += len(pcm)
            ui.last_sample_rate = sr
            if on_audio_pcm and pcm:
                on_audio_pcm(pcm, sr, is_final)
        elif ev == "turn":
            pass  # turn bookkeeping only; nothing to show
        elif ev == "error":
            ui.state = "error"
            ui.error_code = payload.get("error", "error")
            ui.status_text = payload.get("message", ui.error_code)[:22]
        elif ev == "done":
            ui.state = "idle"
            dur = payload.get("duration_ms", 0)
            ui.status_text = f"DONE ({dur}ms)"

        if on_state_change and (ui.state != prev_state or ev in ("transcript", "reply", "error", "done", "thinking")):
            on_state_change(ui)

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            flush_event(cur_event, cur_data_lines)
            cur_event = None
            cur_data_lines = []
        elif line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            cur_data_lines.append(line.split(":", 1)[1].strip())

    flush_event(cur_event, cur_data_lines)
    return ui


def render_sprite_200x200(ui: GotchiUIState, refresh_mode: str = "partial") -> Image.Image:
    """
    Render the 200x200 1-bit monochrome e-paper display frame (black=0, white=255)
    for the Waveshare 1.54" V2 (SSD1681).
    """
    im = Image.new("L", (200, 200), 255)
    d = ImageDraw.Draw(im)

    try:
        f_hdr = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11)
        f_txt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
        f_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except Exception:
        f_hdr = ImageFont.load_default()
        f_txt = f_hdr
        f_big = f_hdr

    # Head posture shifts per state
    dx, dy = 0, 0
    if ui.state == "listening":
        dx, dy = 0, -2  # Lean-in
    elif ui.state in ("thinking", "synthesizing"):
        dx, dy = -2, -2  # Tilt
    elif ui.state == "speaking":
        dx = 0
        dy = -1 if (ui.anim_frame % 2 == 0) else 1  # Bob dy alternating -1/+1

    frame = ui.anim_frame % 3

    if getattr(ui, "skin", "gotchi") == "zero":
        if ui.state in ("thinking", "synthesizing"):
            dx = 0
        asset_dir = os.path.join(os.path.dirname(__file__), "assets", "zero_skin")
        st_name = ui.state if ui.state in VALID_STATES else "idle"
        prebaked_path = os.path.join(asset_dir, f"zero_{st_name}_200x200_1bit.png")
        if os.path.exists(prebaked_path):
            prebaked = Image.open(prebaked_path).convert("L")
            cropped = prebaked.crop((2, 21, 198, 198))
            canvas = Image.new("L", (200, 200), 255)
            # Paste portrait with head posture shift, clamping to keep 2px white bezel
            paste_x = max(2, min(4, 2 + dx))
            canvas.paste(cropped, (paste_x, dy))
            d = ImageDraw.Draw(canvas)

            # ui.blink: closed horizontal eyelid lines on Zero skin
            if ui.blink and st_name in ("idle", "listening", "speaking", "transcribing"):
                d.rectangle([40 + dx, 64 + dy, 40 + dx + 31, 64 + dy + 40], fill=0)
                d.rectangle([82 + dx, 60 + dy, 82 + dx + 37, 60 + dy + 42], fill=0)
                d.line([(45 + dx, 82 + dy), (68 + dx, 82 + dy)], fill=255, width=3)
                d.line([(87 + dx, 78 + dy), (114 + dx, 78 + dy)], fill=255, width=3)

            # 3-frame animation loops on Zero skin
            if st_name == "listening":
                # Pulsing soundwave arcs (paper-white backdrop, side boxes removed)
                arcs_left = [
                    ([14 + dx, 77 + dy, 26 + dx, 105 + dy], 125, 235),
                    ([8 + dx, 71 + dy, 22 + dx, 111 + dy], 125, 235),
                    ([2 + dx, 65 + dy, 18 + dx, 117 + dy], 125, 235),
                ]
                arcs_right = [
                    ([174 + dx, 65 + dy, 186 + dx, 93 + dy], -55, 55),
                    ([179 + dx, 59 + dy, 193 + dx, 99 + dy], -55, 55),
                    ([184 + dx, 53 + dy, 198 + dx, 105 + dy], -55, 55),
                ]
                for i in range(frame + 1):
                    bbox_l, s_l, e_l = arcs_left[i]
                    d.arc(bbox_l, start=s_l, end=e_l, fill=0, width=2)
                    bbox_r, s_r, e_r = arcs_right[i]
                    d.arc(bbox_r, start=s_r, end=e_r, fill=0, width=2)

            elif st_name in ("thinking", "synthesizing"):
                # 1/2/3 ellipsis dots inside thought cloud
                num_dots = frame + 1
                d.rectangle([28 + dx, 13 + dy, 50 + dx, 19 + dy], fill=255)
                dot_xs = [30 + dx, 38 + dx, 46 + dx]
                for i in range(num_dots):
                    d.rectangle([dot_xs[i], 14 + dy, dot_xs[i] + 3, 17 + dy], fill=0)

                # Composite A1 1-bit humanoid robot hand stroking chin
                hx = dx + (frame - 1) * 2
                hy = dy + (1 - frame)
                mask_path = "/workspace/data/zero_thinking_hand_1bit_mask.png"
                white_path = "/workspace/data/zero_thinking_hand_1bit_white.png"
                if os.path.exists(mask_path) and os.path.exists(white_path):
                    h_mask = Image.open(mask_path).convert("L")
                    h_white = Image.open(white_path).convert("L")
                    black_box = Image.new("L", h_mask.size, 0)
                    white_box = Image.new("L", h_white.size, 255)
                    canvas.paste(black_box, (18 + hx, 112 + hy), mask=h_mask)
                    canvas.paste(white_box, (18 + hx, 112 + hy), mask=h_white)
                    d = ImageDraw.Draw(canvas)

            elif st_name == "transcribing":
                # Clear eye area on base portrait to black
                d.rectangle([40, 75, 75, 105], fill=0)
                d.rectangle([82, 70, 120, 103], fill=0)
                # Visor eyes looking focused
                if not ui.blink:
                    d.rounded_rectangle([45 + frame, 84, 45 + frame + 16, 84 + 16], radius=5, fill=255)
                    d.rounded_rectangle([88 + frame, 81, 88 + frame + 19, 81 + 17], radius=6, fill=255)

                # Clear mouth area to white faceplate, then draw mouth line
                d.rectangle([60, 125, 95, 138], fill=255)
                d.line([(65, 131), (90, 129)], fill=0, width=3)

                # Clear waveform/pencil area to white
                d.rectangle([4, 120, 40, 155], fill=255)

                # Wave points and scribbling pencil
                wave_pts = [(8, 146), (12, 140), (16, 147), (20, 140), (24, 146), (28, 140), (32, 146)]
                end_idx = 2 + frame * 2
                for i in range(end_idx):
                    d.line([wave_pts[i], wave_pts[i + 1]], fill=0, width=2)

                # Scribbling pencil at wave_pts[end_idx]
                pencil_angles = [-108.0, -114.0, -118.0]
                ang = pencil_angles[frame]
                rad = math.radians(ang)
                ux, uy = math.cos(rad), math.sin(rad)
                nx, ny = -uy, ux
                hw = 2.5
                length = 24.0
                tip_x, tip_y = wave_pts[end_idx]
                cb_x, cb_y = tip_x + ux * 6.0, tip_y + uy * 6.0
                ee_x, ee_y = tip_x + ux * length, tip_y + uy * length

                # Cone
                d.polygon([(tip_x, tip_y), (cb_x + nx * hw, cb_y + ny * hw), (cb_x - nx * hw, cb_y - ny * hw)], fill=0)
                # Shaft
                d.polygon([
                    (cb_x + nx * hw, cb_y + ny * hw),
                    (ee_x + nx * hw, ee_y + ny * hw),
                    (ee_x - nx * hw, ee_y - ny * hw),
                    (cb_x - nx * hw, cb_y - ny * hw),
                ], fill=0)

            elif st_name == "speaking":
                # Bouncing 5-bar equalizer shifted x = 4 + i * 5 (paper-white backdrop, side box removed)
                eq_patterns = [
                    [8, 16, 12, 18, 10],
                    [14, 8, 18, 10, 16],
                    [18, 12, 8, 16, 12],
                ]
                eq_b = eq_patterns[frame]
                for i in range(5):
                    bh = eq_b[i]
                    d.rectangle([4 + i * 5, 121 - bh // 2, 4 + i * 5 + 2, 121 + bh // 2], fill=0)

                # 3 mouth shapes
                if frame == 0:
                    d.rectangle([60 + dx, 118 + dy, 96 + dx, 140 + dy], fill=0)
                    d.chord([61 + dx, 119 + dy, 95 + dx, 139 + dy], start=0, end=180, fill=0, outline=0)
                    d.rectangle([65 + dx, 128 + dy, 92 + dx, 130 + dy], fill=255)
                elif frame == 1:
                    d.rectangle([60 + dx, 118 + dy, 96 + dx, 140 + dy], fill=0)
                    d.ellipse([70 + dx, 124 + dy, 86 + dx, 138 + dy], fill=255)
                    d.ellipse([74 + dx, 127 + dy, 82 + dx, 135 + dy], fill=0)
                elif frame == 2:
                    d.rectangle([60 + dx, 118 + dy, 96 + dx, 140 + dy], fill=0)
                    d.rounded_rectangle([63 + dx, 127 + dy, 63 + dx + 28, 127 + dy + 6], radius=3, fill=0)
                    d.rectangle([66 + dx, 129 + dy, 88 + dx, 131 + dy], fill=255)

            # Ensure side borders stay white (margin)
            d.rectangle([0, 0, 1, 199], fill=255)
            d.rectangle([198, 0, 199, 199], fill=255)

            # Bottom bar
            d.rectangle([0, 179, 199, 199], fill=255)
            hdr_text = "ZERO" if st_name == "idle" else f"ZERO // {st_name.upper()}"
            d.text((5, 183), hdr_text, fill=0, font=f_hdr)
            badge = "FULL" if refresh_mode == "full" else "PART"
            d.text((164, 183), badge, fill=0, font=f_hdr)

            # Top-right battery badge with white backing box
            if ui.wall_power or ui.battery_pct >= 0:
                if ui.wall_power:
                    bat_badge = f"{int(ui.battery_pct)}%+" if ui.battery_pct >= 0 else "CHG"
                else:
                    bat_badge = f"{int(ui.battery_pct)}%"
                badge_x = 195 - len(bat_badge) * 6
                d.rectangle([badge_x - 2, 4, 198, 15], fill=255)
                d.text((badge_x, 6), bat_badge, fill=0, font=f_txt)

            return canvas.point(lambda px: 255 if px > 128 else 0, mode="1")

    # Top status bar (y = 0..20)
    d.rectangle([0, 0, 199, 20], fill=0)
    d.text((5, 4), f"GOTCHI // {ui.state.upper()}", fill=255, font=f_hdr)
    if ui.wall_power or ui.battery_pct >= 0:
        if ui.wall_power:
            bat_badge = f"{int(ui.battery_pct)}%+" if ui.battery_pct >= 0 else "CHG"
        else:
            bat_badge = f"{int(ui.battery_pct)}%"
        badge_x = 195 - len(bat_badge) * 6
        if badge_x < 152:
            badge_x = 152
        d.text((badge_x, 4), bat_badge, fill=255, font=f_txt)
    else:
        badge = "FULL" if refresh_mode == "full" else "PART"
        d.text((164, 4), badge, fill=255, font=f_hdr)

    # Outer border
    d.rectangle([0, 0, 199, 199], outline=0, width=2)

    # Companion character face box (center 100+dx, 84+dy)
    d.rounded_rectangle([42 + dx, 30 + dy, 158 + dx, 134 + dy], radius=18, outline=0, width=3, fill=255)
    # Little ears / antennas
    d.polygon([(56 + dx, 30 + dy), (66 + dx, 18 + dy), (76 + dx, 30 + dy)], outline=0, fill=0)
    d.polygon([(124 + dx, 30 + dy), (134 + dx, 18 + dy), (144 + dx, 30 + dy)], outline=0, fill=0)

    st = ui.state

    # Render eyes: closed horizontal eyelid lines if ui.blink, else open eyes
    if ui.blink:
        d.line([(64 + dx, 70 + dy), (84 + dx, 70 + dy)], fill=0, width=3)
        d.line([(116 + dx, 70 + dy), (136 + dx, 70 + dy)], fill=0, width=3)
    else:
        if st == "idle":
            d.ellipse([66 + dx, 62 + dy, 82 + dx, 78 + dy], fill=0)
            d.ellipse([118 + dx, 62 + dy, 134 + dx, 78 + dy], fill=0)
            d.ellipse([70 + dx, 65 + dy, 75 + dx, 70 + dy], fill=255)
            d.ellipse([122 + dx, 65 + dy, 127 + dx, 70 + dy], fill=255)
        elif st == "listening":
            d.ellipse([62 + dx, 58 + dy, 84 + dx, 80 + dy], outline=0, width=3)
            d.ellipse([69 + dx, 65 + dy, 77 + dx, 73 + dy], fill=0)
            d.ellipse([116 + dx, 58 + dy, 138 + dx, 80 + dy], outline=0, width=3)
            d.ellipse([123 + dx, 65 + dy, 131 + dx, 73 + dy], fill=0)
        elif st == "transcribing":
            d.rectangle([64 + dx, 66 + dy, 84 + dx, 76 + dy], fill=0)
            d.rectangle([116 + dx, 66 + dy, 136 + dx, 76 + dy], fill=0)
        elif st in ("thinking", "synthesizing"):
            d.ellipse([66 + dx, 56 + dy, 82 + dx, 72 + dy], fill=0)
            d.ellipse([118 + dx, 56 + dy, 134 + dx, 72 + dy], fill=0)
        elif st == "speaking":
            d.line([(64 + dx, 74 + dy), (74 + dx, 62 + dy), (84 + dx, 74 + dy)], fill=0, width=3)
            d.line([(116 + dx, 74 + dy), (126 + dx, 62 + dy), (136 + dx, 74 + dy)], fill=0, width=3)
        else:
            d.line([(64 + dx, 60 + dy), (82 + dx, 78 + dy)], fill=0, width=3)
            d.line([(82 + dx, 60 + dy), (64 + dx, 78 + dy)], fill=0, width=3)
            d.line([(118 + dx, 60 + dy), (136 + dx, 78 + dy)], fill=0, width=3)
            d.line([(136 + dx, 60 + dy), (118 + dx, 78 + dy)], fill=0, width=3)

    # Mouth and per-state animations
    if st == "idle":
        d.arc([82 + dx, 88 + dy, 118 + dx, 110 + dy], start=15, end=165, fill=0, width=3)
    elif st == "listening":
        # Attentive open mouth
        d.ellipse([91 + dx, 94 + dy, 109 + dx, 112 + dy], outline=0, width=3)
        # Pulsing sound wave arcs (frame % 3: 1, 2, or 3 arcs)
        arcs_l = [
            ([28, 64, 40, 100], 120, 240),
            ([20, 56, 36, 108], 120, 240),
            ([12, 48, 32, 116], 120, 240),
        ]
        arcs_r = [
            ([160, 64, 172, 100], -60, 60),
            ([164, 56, 180, 108], -60, 60),
            ([168, 48, 188, 116], -60, 60),
        ]
        for i in range(frame + 1):
            bbox_l, sl, el = arcs_l[i]
            d.arc(bbox_l, start=sl, end=el, fill=0, width=2)
            bbox_r, sr, er = arcs_r[i]
            d.arc(bbox_r, start=sr, end=er, fill=0, width=2)
    elif st == "transcribing":
        for idx, h in enumerate([6, 14, 10, 18, 8, 14, 6]):
            bx = 72 + dx + idx * 9
            d.rectangle([bx, 104 + dy - h // 2, bx + 5, 104 + dy + h // 2], fill=0)
    elif st in ("thinking", "synthesizing"):
        d.line([(84 + dx, 102 + dy), (116 + dx, 102 + dy)], fill=0, width=3)
        # Temple bubbles
        d.ellipse([162, 48, 170, 56], outline=0, width=2)
        d.ellipse([170, 36, 182, 48], outline=0, width=2)
        # Thought bubble with 1/2/3 ellipsis dots
        d.ellipse([160, 20, 194, 38], outline=0, width=2)
        num_dots = frame + 1
        dot_xs = [168, 176, 184]
        for i in range(num_dots):
            d.ellipse([dot_xs[i] - 2, 27, dot_xs[i] + 2, 31], fill=0)
    elif st == "speaking":
        # 3 mouth shapes based on frame % 3
        if frame == 0:
            d.chord([80 + dx, 90 + dy, 120 + dx, 118 + dy], start=0, end=180, fill=0, outline=0)
        elif frame == 1:
            d.ellipse([92 + dx, 96 + dy, 108 + dx, 112 + dy], fill=0)
        elif frame == 2:
            d.rectangle([84 + dx, 102 + dy, 116 + dx, 107 + dy], fill=0)
        # Bouncing 5-bar equalizer
        eq_patterns = [
            [8, 16, 12, 18, 10],
            [14, 8, 18, 10, 16],
            [18, 12, 8, 16, 12],
        ]
        eq_b = eq_patterns[frame]
        for i in range(5):
            bh = eq_b[i]
            bx = 14 + i * 5
            d.rectangle([bx, 96 - bh // 2, bx + 3, 96 + bh // 2], fill=0)
    else:
        d.arc([82 + dx, 98 + dy, 118 + dx, 118 + dy], start=195, end=345, fill=0, width=3)

    # Bottom caption tray (y = 140..199)
    d.line([(0, 140), (199, 140)], fill=0, width=2)
    line1 = (ui.reply or ui.transcript or ui.status_text or "READY")[:26]
    line2 = (ui.reply[26:52] if ui.reply else (ui.transcript[26:52] if ui.transcript else f"SR:{ui.last_sample_rate//1000}k B:{ui.audio_bytes_received}"))
    d.text((6, 146), line1, fill=0, font=f_big)
    d.text((6, 166), line2[:28], fill=0, font=f_txt)
    d.text((6, 182), ui.status_text[:28], fill=0, font=f_txt)

    return im.point(lambda p: 255 if p > 128 else 0, mode="1")


def render_contact_sheet(out_path: str) -> str:
    """Render all 7 GOTCHI e-paper sprite states into a single verification sheet."""
    samples = [
        GotchiUIState(state="idle", status_text="READY (HOLD PTT)"),
        GotchiUIState(state="listening", status_text="REC 16kHz PDM MIC..."),
        GotchiUIState(state="transcribing", status_text="WHISPER STT..."),
        GotchiUIState(state="thinking", thinking_seconds=2, status_text="THINKING (2s)"),
        GotchiUIState(state="synthesizing", transcript="What's the weight?", status_text="KOKORO TTS..."),
        GotchiUIState(state="speaking", reply="50g total — 5g lighter than Humane Pin!", audio_bytes_received=48000, status_text="PLAYING 24kHz I2S"),
        GotchiUIState(state="error", error_code="hub_busy", status_text="409 HUB BUSY"),
    ]
    cols = 4
    rows = 2
    pad = 16
    W = cols * 200 + (cols + 1) * pad
    H = rows * 200 + (rows + 1) * pad + 48
    canvas = Image.new("RGB", (W, H), (24, 28, 36))
    draw = ImageDraw.Draw(canvas)
    try:
        f_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except Exception:
        f_title = ImageFont.load_default()
    draw.text((pad, 12), "GOTCHI V1 — 200x200 1-Bit E-Paper Companion Sprite States (Waveshare 1.54-inch V2 & Pi Zero W POC)", fill=(240, 246, 252), font=f_title)

    tracker = RefreshTracker()
    for idx, st in enumerate(samples):
        r = idx // cols
        c = idx % cols
        mode = tracker.record_write(tracker.select_refresh_mode())
        frame_1bit = render_sprite_200x200(st, refresh_mode=mode).convert("RGB")
        x = pad + c * (200 + pad)
        y = 48 + pad + r * (200 + pad)
        canvas.paste(frame_1bit, (x, y))

    canvas.save(out_path)
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GOTCHI V1 Pi Zero W / Bench POC Client")
    parser.add_argument("--demo-sheet", default=None, help="Output path for 7-state sprite contact sheet PNG")
    args = parser.parse_args()
    if args.demo_sheet:
        p = render_contact_sheet(args.demo_sheet)
        print(f"Wrote {p}")


def is_whole_utterance_go_live(transcript: str) -> bool:
    """Return True iff the normalized transcript is strictly the phrase 'go live' (PRD F21)."""
    cleaned = "".join(
        c.lower() if c.isalnum() else " "
        for c in (transcript or "")
    )
    return " ".join(cleaned.split()) == "go live"
