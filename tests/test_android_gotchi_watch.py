"""
Tests for GOTCHI Standalone Wear OS Pixel Watch Application (`android-gotchi-watch/`).
Verifies:
1. Project structure & Wear OS configuration:
   - `ventures.brock.gotchi.watch` namespace and applicationId
   - minSdk = 30 (Wear OS 3/4/5), targetSdk = 34, compileSdk = 34
   - versionCode = 5, versionName = "1.3.0"
   - README documenting wireless ADB installation (`adb connect <watch-ip>:5555 && ./gradlew installDebug`).
2. Wear OS AndroidManifest declarations:
   - `<uses-feature android:name="android.hardware.type.watch" />`
   - Permissions: `RECORD_AUDIO`, `INTERNET`, `ACCESS_NETWORK_STATE`, `WAKE_LOCK`, `VIBRATE`
   - Launcher `.GotchiWatchActivity` with `exported="true"`.
3. Offline sprite assets:
   - 200x200 1-bit monochrome PNGs for all 9 states for both `zero_skin` and `amos_skin`.
4. `GotchiEpaperView.kt`:
   - 200x200 1-bit monochrome canvas centered in circular OLED viewport
   - RefreshTracker with 5 partial refreshes before a 450 ms full-flash purge
   - Circular bezel status arcs for Push-to-Talk (amber) and 'Hey Zero' wake listener (cyan).
5. `PinchGestureDetector.kt`:
   - Stem hardware keys: `KEYCODE_STEM_PRIMARY`, `KEYCODE_STEM_1`, `KEYCODE_HEADSETHOOK`, `KEYCODE_SPACE`, `KEYCODE_ENTER`, `KEYCODE_DPAD_CENTER`
   - Native Wear OS One-Handed Gestures (Double Pinch `ACTION_PRIMARY` / Wrist Turn `ACTION_DISMISS`)
   - Hybrid on-wrist IMU micro-impulse pinch detection (`Sensor.TYPE_LINEAR_ACCELERATION` 1.6f..9.5f + `Sensor.TYPE_GYROSCOPE` <= 2.5f at `SENSOR_DELAY_UI`)
   - State-to-action resolution (START_PTT, STOP_PTT_AND_COMMIT, TRIGGER_BARGE_IN) and suppression.
6. `HeyZeroPreRollListener.kt`:
   - Continuous 16 kHz 16-bit mono circular pre-roll ring buffer (500 ms / 16,000 bytes / 8,000 samples)
   - Wake phrase spotter matching "hey zero", "hay zero", "hi zero", "okay zero"
   - Normalized transcript matching and snapshot handoff with haptic confirmation.
7. `GotchiWatchEngine.kt` wire-protocol parity:
   - 16 kHz mic capture with 500 ms circular pre-roll + 24 kHz TTS playback
   - 44-byte RIFF WAV header (`wrapPcm16ToWav`) and WAV chunk walker (`extractPcmFromWavOrRaw`)
   - `POST /api/voice/interact` multipart WAV upload + streaming SSE parser (`sentence`, `audio_chunk`)
   - `/api/live/ws` Go Live WebSocket with hardware `AcousticEchoCanceler` and 300 ms barge-in
   - `startWakeWordTurn(preRollPcm)` seamless pre-roll injection
   - Node ID regex validation (`^[A-Za-z0-9_-]{1,64}$`).
8. `GotchiWatchActivity.kt`:
   - Complete integration of view, engine, pinch detector, and pre-roll listener
   - Wear OS rotary crown scroll handling (`AXIS_SCROLL`) and hardware stem key dispatch.
9. High-res 512x512 RGBA Phone and Watch launcher icons (`ic_launcher.png` & `ic_launcher_round.png`).
"""

from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image

from poc.gotchi_poc import VALID_STATES

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCH_DIR = REPO_ROOT / "android-gotchi-watch"
MANIFEST_PATH = WATCH_DIR / "app" / "src" / "main" / "AndroidManifest.xml"
APP_GRADLE_PATH = WATCH_DIR / "app" / "build.gradle.kts"
SETTINGS_GRADLE_PATH = WATCH_DIR / "settings.gradle.kts"
ROOT_GRADLE_PATH = WATCH_DIR / "build.gradle.kts"
README_PATH = WATCH_DIR / "README.md"

PKG_DIR = (
    WATCH_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "watch"
)
EPAPER_VIEW_PATH = PKG_DIR / "GotchiEpaperView.kt"
PINCH_PATH = PKG_DIR / "PinchGestureDetector.kt"
PRE_ROLL_PATH = PKG_DIR / "HeyZeroPreRollListener.kt"
ENGINE_PATH = PKG_DIR / "GotchiWatchEngine.kt"
ACTIVITY_PATH = PKG_DIR / "GotchiWatchActivity.kt"
CHIME_SYNTH_PATH = PKG_DIR / "GotchiChimeSynthesizer.kt"
UPDATER_PATH = PKG_DIR / "GotchiWatchUpdater.kt"

ZERO_SKIN_DIR = WATCH_DIR / "app" / "src" / "main" / "assets" / "zero_skin"
AMOS_SKIN_DIR = WATCH_DIR / "app" / "src" / "main" / "assets" / "amos_skin"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_watch_project_structure_and_isolation():
    assert ROOT_GRADLE_PATH.is_file(), f"Missing {ROOT_GRADLE_PATH}"
    assert SETTINGS_GRADLE_PATH.is_file(), f"Missing {SETTINGS_GRADLE_PATH}"
    assert APP_GRADLE_PATH.is_file(), f"Missing {APP_GRADLE_PATH}"
    assert README_PATH.is_file(), f"Missing {README_PATH}"
    assert MANIFEST_PATH.is_file(), f"Missing {MANIFEST_PATH}"
    assert EPAPER_VIEW_PATH.is_file(), f"Missing {EPAPER_VIEW_PATH}"
    assert PINCH_PATH.is_file(), f"Missing {PINCH_PATH}"
    assert PRE_ROLL_PATH.is_file(), f"Missing {PRE_ROLL_PATH}"
    assert ENGINE_PATH.is_file(), f"Missing {ENGINE_PATH}"
    assert ACTIVITY_PATH.is_file(), f"Missing {ACTIVITY_PATH}"
    assert CHIME_SYNTH_PATH.is_file(), f"Missing {CHIME_SYNTH_PATH}"
    assert UPDATER_PATH.is_file(), f"Missing {UPDATER_PATH}"

    # Verify namespace and SDK constraints in build.gradle.kts
    gradle_src = APP_GRADLE_PATH.read_text(encoding="utf-8")
    assert 'namespace = "ventures.brock.gotchi.watch"' in gradle_src
    assert 'applicationId = "ventures.brock.gotchi.watch"' in gradle_src
    assert "minSdk = 30" in gradle_src
    assert "targetSdk = 34" in gradle_src
    assert "compileSdk = 34" in gradle_src
    assert "versionCode = 5" in gradle_src
    assert 'versionName = "1.3.0"' in gradle_src
    assert "androidx.wear:wear" in gradle_src

    # Verify README documentation for wireless ADB deployment
    readme_src = README_PATH.read_text(encoding="utf-8")
    assert "adb connect <watch-ip>:5555" in readme_src
    assert "./gradlew installDebug" in readme_src


def test_watch_manifest_and_wear_os_declarations():
    tree = ET.parse(MANIFEST_PATH)
    root = tree.getroot()
    android_ns = "{http://schemas.android.com/apk/res/android}"

    # Verify Wear OS hardware feature declaration
    features = {
        elem.attrib.get(f"{android_ns}name")
        for elem in root.findall("uses-feature")
    }
    assert "android.hardware.type.watch" in features
    assert "android.hardware.microphone" in features

    # Verify required permissions
    permissions = {
        elem.attrib.get(f"{android_ns}name")
        for elem in root.findall("uses-permission")
    }
    required = {
        "android.permission.RECORD_AUDIO",
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.WAKE_LOCK",
        "android.permission.VIBRATE",
        "android.permission.REQUEST_INSTALL_PACKAGES",
    }
    assert required.issubset(permissions)

    # Verify launcher activity and custom circular launcher icons
    app_elem = root.find("application")
    assert app_elem is not None
    assert app_elem.attrib.get(f"{android_ns}icon") == "@mipmap/ic_launcher"
    assert app_elem.attrib.get(f"{android_ns}roundIcon") == "@mipmap/ic_launcher_round"

    # Verify custom launcher icons exist and have valid PNG signatures
    res_dir = WATCH_DIR / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi"
    ic_launcher = res_dir / "ic_launcher.png"
    ic_launcher_round = res_dir / "ic_launcher_round.png"
    assert ic_launcher.is_file(), f"Missing launcher icon: {ic_launcher}"
    assert ic_launcher_round.is_file(), f"Missing round launcher icon: {ic_launcher_round}"
    assert ic_launcher.read_bytes().startswith(PNG_SIGNATURE)
    assert ic_launcher_round.read_bytes().startswith(PNG_SIGNATURE)

    activities = {
        a.attrib.get(f"{android_ns}name"): a
        for a in app_elem.findall("activity")
    }
    assert ".GotchiWatchActivity" in activities
    act = activities[".GotchiWatchActivity"]
    assert act.attrib.get(f"{android_ns}exported") == "true"
    assert act.attrib.get(f"{android_ns}keepScreenOn") == "true"
    assert act.attrib.get(f"{android_ns}showWhenLocked") == "true"
    assert act.attrib.get(f"{android_ns}turnScreenOn") == "true"

    # Verify FileProvider for in-app APK installation
    providers = {
        p.attrib.get(f"{android_ns}name"): p
        for p in app_elem.findall("provider")
    }
    assert ".FileProvider" in providers
    prov = providers[".FileProvider"]
    assert prov.attrib.get(f"{android_ns}exported") == "false"
    assert prov.attrib.get(f"{android_ns}grantUriPermissions") == "true"
    assert prov.attrib.get(f"{android_ns}authorities") == "${applicationId}.fileprovider"


def test_bundled_sprite_assets_zero_and_amos_skins():
    import numpy as np

    assert ZERO_SKIN_DIR.is_dir(), f"Missing Zero skin directory: {ZERO_SKIN_DIR}"
    assert AMOS_SKIN_DIR.is_dir(), f"Missing Amos skin directory: {AMOS_SKIN_DIR}"

    # Verify HD Zero skin assets (256x256 RGBA)
    zero_dir = WATCH_DIR / "app" / "src" / "main" / "assets" / "zero_skin"
    assert (zero_dir / "zero_hd_idle.png").is_file()
    assert (zero_dir / "zero_hd_idle_blink.png").is_file()
    for st in ["thinking", "transcribing", "speaking", "listening"]:
        for frame in range(3):
            assert (zero_dir / f"zero_hd_{st}_{frame}.png").is_file()
            assert (zero_dir / f"zero_hd_{st}_{frame}_blink.png").is_file()

    for st in VALID_STATES:
        zero_png = ZERO_SKIN_DIR / f"zero_{st}_200x200_1bit.png"
        amos_png = AMOS_SKIN_DIR / f"amos_{st}_200x200_1bit.png"

        assert zero_png.is_file(), f"Missing zero sprite for state: {st}"
        assert amos_png.is_file(), f"Missing amos sprite for state: {st}"

        # Ensure valid PNG binary header
        with open(zero_png, "rb") as f:
            assert f.read(8) == PNG_SIGNATURE, f"Corrupted PNG: {zero_png}"
        with open(amos_png, "rb") as f:
            assert f.read(8) == PNG_SIGNATURE, f"Corrupted PNG: {amos_png}"

        # Ensure Zero skin sprites have baked-in headers and tray bars stripped to black (0)
        img = Image.open(zero_png)
        assert img.size == (200, 200)
        arr = np.array(img.convert("L"))
        assert arr[:22, :].max() == 0, f"Zero {st} has non-zero pixels in header y < 22"
        assert arr[146:, :].max() == 0, f"Zero {st} has non-zero pixels in footer tray y >= 146"
        assert arr[:, :2].max() == 0, f"Zero {st} has non-zero pixels in left border x < 2"
        assert arr[:, 198:].max() == 0, f"Zero {st} has non-zero pixels in right border x >= 198"
        assert arr[22:146, 2:198].max() > 0, f"Zero {st} missing character pixels in center"

    # Verify HD Zero avatar frames (256x256 RGBA)
    for hd_name in ["zero_hd_idle_0.png", "zero_hd_idle_0_blink.png", "zero_hd_speaking_1.png"]:
        hd_png = ZERO_SKIN_DIR / hd_name
        assert hd_png.is_file(), f"Missing HD Zero sprite: {hd_name}"
        with open(hd_png, "rb") as f:
            assert f.read(8) == PNG_SIGNATURE, f"Corrupted PNG: {hd_png}"
        hd_img = Image.open(hd_png)
        assert hd_img.size == (256, 256), f"Wrong size {hd_img.size} for {hd_name}"
        assert hd_img.mode == "RGBA", f"Wrong mode {hd_img.mode} for {hd_name}"


def test_epaper_view_circular_bezel_and_refresh_tracker():
    view_src = EPAPER_VIEW_PATH.read_text(encoding="utf-8")

    # Dimensions
    assert "const val EPD_WIDTH = 200" in view_src
    assert "const val EPD_HEIGHT = 200" in view_src

    # RefreshTracker invariants (5 partial refreshes before 450 ms full-flash purge)
    assert "val maxConsecutivePartials: Int = 5" in view_src
    assert "const val FULL_REFRESH_MS = 450L" in view_src

    # Circular bezel status arc indicators
    assert "BEZEL_PTT_ACTIVE_COLOR" in view_src
    assert "BEZEL_WAKE_ACTIVE_COLOR" in view_src
    assert "canvas.drawCircle(cx, cy, bezelRadius, bezelPaint)" in view_src
    assert "canvas.drawArc(bezelRect, 135f, 90f, false, bezelPaint)" in view_src
    assert "canvas.drawArc(bezelRect, 315f, 90f, false, bezelPaint)" in view_src

    # 9 operational states
    for st in VALID_STATES:
        assert f'"{st}"' in view_src, f"Missing state {st} in GotchiEpaperView"

    # Circular borderless viewport clipping & scaling
    assert "destRect.set(cx - innerRadius, cy - innerRadius, cx + innerRadius, cy + innerRadius)" in view_src
    assert "Path().apply { addCircle(cx, cy, innerRadius, Path.Direction.CW) }" in view_src
    assert "canvas.clipPath(clipPath)" in view_src

    # Square outer borders and dividers removed
    assert "c.drawRect(1f, 1f, 199f, 199f" not in view_src
    assert "c.drawRect(0f, 0f, 200f, 20f" not in view_src
    assert "c.drawLine(0f, 148f, 200f, 148f" not in view_src

    # Centered top status & bottom caption lines
    assert "paint.textAlign = Paint.Align.CENTER" in view_src
    assert 'val headerText = if (st == "idle") prefix else "$prefix // ${st.uppercase()}"' in view_src
    assert "c.drawText(headerText, 100f, 19f, paint)" in view_src
    assert "c.drawText(line1, 100f, 162f, paint)" in view_src
    assert "c.drawText(line2.take(22), 100f, 177f, paint)" in view_src

    # Native high-resolution 60 FPS viewport & portrait scaling
    assert "const val PORTRAIT_SCALE_RATIO = 0.65f" in view_src
    assert "const val ANIM_FRAME_INTERVAL_MS = 16L" in view_src
    assert "fun drawNativeHighResViewport(" in view_src
    assert "val hdCx = cx" in view_src
    assert "val hdCy = cy - innerRadius * 0.04f" in view_src
    assert "(uiState.animFrame / 16) % 3" in view_src
    assert "drawNativeHighResViewport(canvas, cx, cy, innerRadius)" in view_src
    assert "postInvalidateOnAnimation()" in view_src

    # Dark-mode Zero skin mapping & boundary stripping
    assert 'uiState.skin == "zero"' in view_src
    assert "y < 22 || y >= 146 || x < 2 || x >= 198" in view_src

    # Zero HD avatar loader and cache
    assert "getZeroHdBitmap" in view_src
    assert "zeroHdCache" in view_src


def test_pinch_gesture_detector_invariants():
    pinch_src = PINCH_PATH.read_text(encoding="utf-8")

    # Stem and hardware keycodes
    assert "KeyEvent.KEYCODE_STEM_PRIMARY" in pinch_src
    assert "KeyEvent.KEYCODE_STEM_1" in pinch_src
    assert "KeyEvent.KEYCODE_HEADSETHOOK" in pinch_src
    assert "KeyEvent.KEYCODE_SPACE" in pinch_src
    assert "KeyEvent.KEYCODE_ENTER" in pinch_src
    assert "KeyEvent.KEYCODE_DPAD_CENTER" in pinch_src

    # Native Wear OS One-Handed Gestures (Double Pinch & Wrist Turn)
    assert "ACTION_PRIMARY" in pinch_src
    assert "ACTION_DISMISS" in pinch_src
    assert "GESTURE_DOUBLE_PINCH" in pinch_src
    assert "GESTURE_WRIST_TURN" in pinch_src
    assert "onDoublePinchGesture" in pinch_src
    assert "onWristTurnGesture" in pinch_src
    assert "onDismissTriggered" in pinch_src

    # Hybrid on-wrist IMU micro-impulse pinch detection
    assert "Sensor.TYPE_LINEAR_ACCELERATION" in pinch_src
    assert "Sensor.TYPE_GYROSCOPE" in pinch_src
    assert "SensorManager.SENSOR_DELAY_UI" in pinch_src
    assert 'tryTriggerPinch("imu_pinch")' in pinch_src
    assert "DEFAULT_LINEAR_ACCEL_THRESHOLD = 3.4f" in pinch_src
    assert "DEFAULT_MIN_LINEAR_ACCEL_THRESHOLD = 3.4f" in pinch_src
    assert "DEFAULT_MAX_LINEAR_ACCEL_THRESHOLD = 11.5f" in pinch_src
    assert "DEFAULT_MAX_GYRO_THRESHOLD = 1.2f" in pinch_src
    assert "DOUBLE_PINCH_WINDOW_MS = 550L" in pinch_src
    assert "DOUBLE_PINCH_MIN_GAP_MS = 120L" in pinch_src
    assert "var requireDoubleImpulse: Boolean = true" in pinch_src

    # Thresholds and cooldown debounce
    assert "const val COOLDOWN_MS = 450L" in pinch_src

    # State-to-action resolution
    assert "enum class PinchAction" in pinch_src
    assert "START_PTT" in pinch_src
    assert "STOP_PTT_AND_COMMIT" in pinch_src
    assert "TRIGGER_BARGE_IN" in pinch_src
    assert "fun resolveActionForState(" in pinch_src

    # Suppression flag
    assert "var isSuppressed: Boolean" in pinch_src


def test_hey_zero_pre_roll_listener_invariants():
    listener_src = PRE_ROLL_PATH.read_text(encoding="utf-8")

    # Audio invariants: 16 kHz 16-bit mono with 500 ms circular pre-roll
    assert "const val SAMPLE_RATE = 16000" in listener_src
    assert "const val PRE_ROLL_MS = 500" in listener_src
    assert "const val PRE_ROLL_BYTES = (SAMPLE_RATE * BYTES_PER_SAMPLE * PRE_ROLL_MS) / 1000" in listener_src

    # Wake phrase candidates
    assert '"hey zero"' in listener_src
    assert '"hay zero"' in listener_src
    assert '"hi zero"' in listener_src
    assert '"okay zero"' in listener_src

    # Normalization & matching logic
    assert "fun normalizeTranscript(" in listener_src
    assert "fun isWakePhrase(" in listener_src
    assert "class CircularPreRollBuffer(" in listener_src
    assert "fun snapshot(): ByteArray" in listener_src

    # Haptic feedback & callback
    assert "VibrationEffect.createOneShot" in listener_src
    assert "onWakeWordDetected" in listener_src


def test_watch_engine_wire_protocol_parity():
    eng_src = ENGINE_PATH.read_text(encoding="utf-8")

    # 16 kHz audio capture, 500 ms pre-roll, 24 kHz TTS playback
    assert "const val MIC_SAMPLE_RATE = 16000" in eng_src
    assert "const val TTS_DEFAULT_SAMPLE_RATE = 24000" in eng_src
    assert "const val PRE_ROLL_MS = 500" in eng_src
    assert "const val PRE_ROLL_BYTES = (MIC_SAMPLE_RATE * 2 * PRE_ROLL_MS) / 1000" in eng_src
    assert "fun wrapPcm16ToWav(" in eng_src
    assert "fun extractPcmFromWavOrRaw(" in eng_src

    # Node ID validation matching ^[A-Za-z0-9_-]{1,64}$
    assert 'Regex("^[A-Za-z0-9_-]{1,64}$")' in eng_src

    # Default Gateway URL pointing to Host 2 Gotchi voice gateway proxy (port 8769)
    assert "DEFAULT_GATEWAY_URL" in eng_src
    assert ":8769" in eng_src

    # Multipart /api/voice/interact upload
    assert "/api/voice/interact" in eng_src
    assert '"Authorization", "Bearer ${config.bearerToken}"' in eng_src
    assert 'addFormDataPart("audio_out_mode", safeAudioOut)' in eng_src

    # Streaming SSE parser handling sentence / audio_chunk lookahead
    assert '"sentence", "audio_chunk"' in eng_src
    assert 'obj.optString("audio_b64"' in eng_src

    # Go Live full-duplex WebSocket (/api/live/ws) + AEC + 300 ms barge-in
    assert "/api/live/ws" in eng_src
    assert "AcousticEchoCanceler" in eng_src
    assert '.put("type", "barge_in")' in eng_src

    # Pre-roll wake word turn handoff
    assert "fun startWakeWordTurn(preRollPcm: ByteArray)" in eng_src
    assert "fun triggerBargeIn()" in eng_src

    # Sequential TTS audio playback queue and barge-in queue draining
    assert "audioPlaybackQueue = Channel<Pair<ByteArray, Int>>(Channel.UNLIMITED)" in eng_src
    assert "playbackMutex = Mutex()" in eng_src
    assert "playbackWorkerJob = scope.launch" in eng_src
    assert "fun playPcmChunkSequential(" in eng_src
    assert "val durationMs = (pcmBytes.size * 1000L) / (sampleRate * 2)" in eng_src
    assert "delay(durationMs + 40L)" in eng_src
    assert "while (audioPlaybackQueue.tryReceive().isSuccess) {}" in eng_src
    assert "audioPlaybackQueue.trySend(Pair(pcmBytes, sampleRate))" in eng_src


def test_watch_activity_hardware_integration():
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # Integrates View, Engine, Pinch detector, and PreRoll listener
    assert "GotchiEpaperView" in act_src
    assert "GotchiWatchEngine" in act_src
    assert "PinchGestureDetector" in act_src
    assert "HeyZeroPreRollListener" in act_src

    # Rotary Crown scroll event handling (AXIS_SCROLL)
    assert "MotionEvent.AXIS_SCROLL" in act_src
    assert "onGenericMotionEvent" in act_src

    # Stem hardware key dispatch
    assert "onKeyDown" in act_src
    assert "pinchDetector.onKeyEvent(event)" in act_src

    # Lifecycle callbacks and gesture handlers
    assert "handlePinchOrStemTrigger" in act_src
    assert "handleDismissTrigger" in act_src
    assert "handleWakeWordDetected" in act_src
    assert "syncImuSuppressionState" in act_src
    assert "pinchDetector.onDismissTriggered" in act_src
    assert "engine.cancelPttCapture()" in act_src

    # Focusable circular container, legacy 8765 -> 8769 migration, and IMU suppression state
    assert ":8765" in act_src
    assert ":8769" in act_src
    assert "isFocusable = true" in act_src
    assert "isFocusableInTouchMode = true" in act_src
    assert 'state in setOf("transcribing", "thinking", "synthesizing")' in act_src

    # Stay-awake & ambient mode hardware integration
    assert "FLAG_KEEP_SCREEN_ON" in act_src
    assert "KEY_KEEP_SCREEN_ON" in act_src
    assert "KEY_CHIMES_ENABLED" in act_src
    assert "setShowWhenLocked(true)" in act_src
    assert "setTurnScreenOn(true)" in act_src
    assert "applyKeepScreenOnFlag" in act_src
    assert "gotchi:watch_session_wakelock" in act_src

    # In-app Wi-Fi self-updater & remote config synchronization
    assert "GotchiWatchUpdater" in act_src
    assert "GotchiWatchUpdater.syncRemoteConfig" in act_src
    assert "UPDATE APP (WI-FI)" in act_src
    assert "downloadAndInstallApk" in act_src


def test_watch_engine_ima_adpcm_and_async_pushback_parity():
    eng_src = ENGINE_PATH.read_text(encoding="utf-8")
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # 4-bit IMA-ADPCM (GAD1 container + raw nibble stream)
    assert 'const val ADPCM_MAGIC = "GAD1"' in eng_src
    assert "val IMA_INDEX_TABLE" in eng_src
    assert "val IMA_STEP_TABLE" in eng_src
    assert "fun decodeImaAdpcm(" in eng_src
    assert "fun decodeAudioPayload(" in eng_src
    assert 'fmt == "adpcm" || fmt == "adpcm_ima"' in eng_src

    # Async push-back: notification polling, tap-to-play pending answer, hold-to-skip
    assert "const val DEFAULT_NOTIFY_POLL_INTERVAL_MS = 180000L" in eng_src
    assert "/api/gotchi/notify" in eng_src
    assert "/api/gotchi/notify/ack" in eng_src
    assert "/api/gotchi/pending_answer" in eng_src
    assert "/api/gotchi/pending_answer/ack" in eng_src
    assert "fun fetchAndPlayPendingAnswer()" in eng_src
    assert "fun skipPendingAnswer()" in eng_src
    assert "fun stopNotificationPolling()" in eng_src
    assert "engine.fetchAndPlayPendingAnswer()" in act_src
    assert "engine.skipPendingAnswer()" in act_src
    assert "engine.stopNotificationPolling()" in act_src


def test_watch_battery_and_orientation_enhancements():
    listener_src = PRE_ROLL_PATH.read_text(encoding="utf-8")
    pinch_src = PINCH_PATH.read_text(encoding="utf-8")
    eng_src = ENGINE_PATH.read_text(encoding="utf-8")
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # HeyZeroPreRollListener wrist-orientation and acoustic RMS gating
    assert "MIN_WRIST_Z_GRAVITY" in listener_src
    assert "TYPE_WRIST_TILT_GESTURE" in listener_src
    assert "WAKE_RMS_GATE_THRESHOLD" in listener_src
    assert "isWatchInSpeakingOrientation" in listener_src
    assert "pauseForActiveTurn" in listener_src
    assert "resumeAfterTurn" in listener_src

    # Pixel Watch 4/5 Native Wear OS One-Handed Gestures + Hybrid IMU micro-impulse
    assert "ACTION_PRIMARY" in pinch_src
    assert "ACTION_DISMISS" in pinch_src
    assert "GESTURE_DOUBLE_PINCH" in pinch_src
    assert "GESTURE_WRIST_TURN" in pinch_src
    assert "onWristTurnGesture" in pinch_src
    assert "cancelPttCapture" in eng_src
    assert "cancelPttCapture" in act_src
    assert "Sensor.TYPE_LINEAR_ACCELERATION" in pinch_src
    assert "Sensor.TYPE_GYROSCOPE" in pinch_src
    assert "SensorManager.SENSOR_DELAY_UI" in pinch_src

    # GotchiWatchActivity lifecycle wiring and settings keys
    assert "KEY_OLED_DARK_MODE" in act_src
    assert "KEY_WRIST_ORIENTATION_GATE" in act_src
    assert "preRollListener.pauseForActiveTurn()" in act_src
    assert "preRollListener.resumeAfterTurn()" in act_src


def test_watch_chimes_and_keep_awake_ambient_mode():
    chime_src = CHIME_SYNTH_PATH.read_text(encoding="utf-8")
    eng_src = ENGINE_PATH.read_text(encoding="utf-8")
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # GotchiChimeSynthesizer 24 kHz DSP synthesis parity
    assert "const val CHIME_SR = 24000" in chime_src
    assert "const val MAX_CHIME_SAMPLES = 20400" in chime_src
    assert 'VALID_CHIME_KINDS' in chime_src
    for kind in ["ready", "ptt_start", "ptt_stop", "snap", "thinking", "error"]:
        assert f'"{kind}"' in chime_src

    assert "fun casioReedWave" in chime_src
    assert "fun addSwitchClick" in chime_src
    assert "fun addReedNoteSmooth" in chime_src
    assert "fun addShutterImpact" in chime_src
    assert "fun addCurtainZip" in chime_src
    assert "fun addSpringRingTail" in chime_src
    assert "0.924" in chime_src
    assert "fun synthesizeChimePcm16(" in chime_src

    # GotchiWatchEngine chime integration
    assert "var chimesEnabled: Boolean = true" in eng_src
    assert "fun playChime(kind: String)" in eng_src
    assert "GotchiChimeSynthesizer.synthesizeChimePcm16(kind)" in eng_src
    assert 'playChime("ptt_start")' in eng_src
    assert 'playChime("ptt_stop")' in eng_src
    assert 'playChime("snap")' in eng_src
    assert 'playChime("thinking")' in eng_src
    assert 'playChime("ready")' in eng_src
    assert 'playChime("error")' in eng_src

    # GotchiWatchActivity keep-awake, wake lock & chime UI
    assert 'const val KEY_KEEP_SCREEN_ON = "keep_screen_on"' in act_src
    assert 'const val KEY_CHIMES_ENABLED = "chimes_enabled"' in act_src
    assert "WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON" in act_src
    assert "setShowWhenLocked(true)" in act_src
    assert "setTurnScreenOn(true)" in act_src
    assert "applyKeepScreenOnFlag" in act_src
    assert '"gotchi:watch_session_wakelock"' in act_src
    assert "KEEP AWAKE:" in act_src
    assert "CHIMES:" in act_src


def test_watch_updater_and_gateway_endpoints():
    from starlette.testclient import TestClient
    from backend.voice_gateway import create_app

    # GotchiWatchUpdater Kotlin source validation
    updater_src = UPDATER_PATH.read_text(encoding="utf-8")
    assert "object GotchiWatchUpdater" in updater_src
    assert "fun syncRemoteConfig" in updater_src
    assert "fun downloadAndInstallApk" in updater_src
    assert "class GotchiFileProvider" in updater_src
    assert "class FileProvider" in updater_src
    assert "/api/gotchi/watch/config" in updater_src
    assert "/api/gotchi/watch/apk.apk" in updater_src
    assert "application/vnd.android.package-archive" in updater_src
    assert "Intent.FLAG_GRANT_READ_URI_PERMISSION" in updater_src or "FLAG_GRANT_READ_URI_PERMISSION" in updater_src
    assert "Intent.FLAG_ACTIVITY_NEW_TASK" in updater_src or "FLAG_ACTIVITY_NEW_TASK" in updater_src
    assert "getInstalledVersionCode" in updater_src
    assert "onUpdateAvailable" in updater_src

    # GotchiWatchActivity source validation
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")
    assert "showUpdateAvailablePrompt" in act_src
    assert "TRIPLE_TAP_WINDOW_MS" in act_src
    assert "onUpdateAvailable" in act_src

    # Voice Gateway endpoints: /api/gotchi/watch/config and /api/gotchi/watch/apk.apk
    app = create_app()
    client = TestClient(app)

    cfg_res = client.get("/api/gotchi/watch/config?node_id=gotchi-pixel-watch-01")
    assert cfg_res.status_code == 200
    cfg = cfg_res.json()
    assert cfg["ok"] is True
    assert cfg["node_id"] == "gotchi-pixel-watch-01"
    assert "version_code" in cfg
    assert cfg["version_code"] in (4, 5)
    assert cfg["min_linear_accel"] == 3.4
    assert cfg["max_gyro"] == 1.2
    assert cfg["require_double_impulse"] is True
    assert cfg["ptt_silence_ms"] == 1500
    assert cfg["apk_url"] == "/api/gotchi/watch/apk.apk"

    apk_res = client.get("/api/gotchi/watch/apk.apk")
    assert apk_res.status_code == 200
    assert apk_res.headers["content-type"] == "application/vnd.android.package-archive"
    assert len(apk_res.content) > 1000


def test_gotchi_launcher_icons_512x512_rgba():
    icon_paths = [
        REPO_ROOT / "android-ble-forwarder" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.png",
        REPO_ROOT / "android-ble-forwarder" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher_round.png",
        REPO_ROOT / "android-gotchi-watch" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.png",
        REPO_ROOT / "android-gotchi-watch" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher_round.png",
    ]
    for p in icon_paths:
        assert p.is_file(), f"Missing launcher icon: {p}"
        with Image.open(p) as img:
            assert img.size == (512, 512), f"{p} has size {img.size}, expected (512, 512)"
            assert img.mode == "RGBA", f"{p} has mode {img.mode}, expected RGBA"
