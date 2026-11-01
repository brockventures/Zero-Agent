# Gotchi Wear OS — Google Pixel Watch Companion App

A standalone Wear OS companion application (`ventures.brock.gotchi.watch`) providing full hardware parity with the GOTCHI voice client ecosystem on the Google Pixel Watch (Wear OS 3, 4, and 5).

### Release v1.2.0 (versionCode 3)
- **Thinking A1 Animation:** Illustrated humanoid robot hand stroking Zero's chin across HD (256x256 RGBA) and 1-bit monochrome (200x200) frames.
- **Transcribing 1 Animation:** Focused visor eyes looking down-left paired with an active golden scribbling pencil generating a progressive waveform.
- **Centered Viewport:** Perfect circular OLED alignment (`hdCx = cx`, `hdCy = cy - innerRadius * 0.04f`) resolving header collision.
- **Smoothed Animation Cadence:** 16-frame animation stepping (`(uiState.animFrame / 16) % 3`).


---

## Architecture & Features

1. **Circular E-Paper Display Engine (`GotchiEpaperView`):**
   - Centered 200×200 1-bit monochrome e-paper canvas (`GDEY0154D67-FT` V2.6 profile) on a pure black OLED substrate.
   - Dual circular bezel status arcs:
     - **Amber / Coral arc (left, 135°–225°):** Active Push-to-Talk audio recording indicator.
     - **Cyan arc (right, 315°–45°):** Continuous "Hey Zero" pre-roll wake listener indicator.
   - 9 operational states: `idle`, `listening`, `transcribing`, `thinking`, `synthesizing`, `speaking`, `error`, `busy`, `offline`.
   - Bundled offline sprite packs for both `zero_skin` and `amos_skin` (1-bit monochrome pre-rendered assets).
   - E-paper refresh cadence tracker: 5 partial refreshes followed by a 450 ms full-flash purge.

2. **Finger-Pinch & Hardware Key Push-to-Talk (`PinchGestureDetector`):**
   - **Stem Keys:** Supports Wear OS crown and physical stem buttons (`KEYCODE_STEM_PRIMARY`, `KEYCODE_STEM_1`, `KEYCODE_HEADSETHOOK`).
   - **On-Wrist IMU Detection:** `Sensor.TYPE_LINEAR_ACCELERATION` transient micro-impulse detection paired with `Sensor.TYPE_GYROSCOPE` angular velocity bounds (rejects full-arm swings).
   - **Automated State Dispatch:**
     - `IDLE` / `READY` -> `startPttCapture(autoVadStop = true)`
     - `LISTENING` / `RECORDING` -> `stopPttAndCommit()`
     - `SPEAKING` / `SYNTHESIZING` -> `triggerBargeIn()`

3. **Continuous 500 ms Pre-Roll Listener (`HeyZeroPreRollListener`):**
   - Continuous 16 kHz 16-bit mono circular PCM ring buffer (`PRE_ROLL_MS = 500`, `PRE_ROLL_BYTES = 16000`).
   - Offline speech recognition spotter matching `"hey zero"`, `"hay zero"`, `"hi zero"`, and `"okay zero"`.
   - On wake-word detection, snapshots the 500 ms circular buffer and seamlessly hands off to `engine.startWakeWordTurn(preRollPcm)` with haptic feedback.

4. **Wire-Protocol Parity (`GotchiWatchEngine`):**
   - `POST /api/voice/interact`: Multipart 16 kHz WAV turn upload with streaming SSE event decoding (`sentence`, `audio_chunk` 24 kHz playback).
   - `GET /api/live/ws`: Full-duplex WebSocket streaming with hardware `AcousticEchoCanceler` and 300 ms sustained VAD barge-in.
   - `Regex("^[A-Za-z0-9_-]{1,64}$")` node ID validation.

---

## Wireless ADB Deployment to Pixel Watch

### 1. Enable Developer Options & Wireless Debugging
1. On your Google Pixel Watch, navigate to **Settings > System > About > Versions**.
2. Tap **Build number** 7 times until Developer Mode is unlocked.
3. Go back to **Settings > Developer options**.
4. Enable **ADB debugging** and **Wireless debugging**.
5. Note the watch's IP address and port (default `5555` or dynamic pairing port).

### 2. Connect via ADB
```bash
# Connect to Pixel Watch over local Wi-Fi
adb connect <watch-ip>:5555

# Verify device is authorized and listed
adb devices
```

### 3. Build & Install Debug APK
From `/workspace/gotchi/android-gotchi-watch`:
```bash
./gradlew installDebug
```

Alternatively, from the repository root:
```bash
./gradlew :app:installDebug
```

Launch the app directly on the watch:
```bash
adb shell am start -n ventures.brock.gotchi.watch/.GotchiWatchActivity
```

---

## Hardware Controls & Gestures

| Trigger / Control | Action |
|-------------------|--------|
| **Single Pinch / Stem Key** | Toggle Push-to-Talk capture (auto-VAD silence cutoff) |
| **Rotary Crown Scroll** | Cycle skins (`zero` ➔ `amos` ➔ `gotchi`) |
| **Single Screen Tap** | Push-to-Talk trigger / Barge-in |
| **Double Screen Tap** | Toggle Go Live full-duplex session (`/api/live/ws`) |
| **Long Press Screen** | Toggle "Hey Zero" pre-roll wake listener (ON/OFF) |
| **Speaking State Pinch** | Immediate speech barge-in (stops TTS audio output) |
