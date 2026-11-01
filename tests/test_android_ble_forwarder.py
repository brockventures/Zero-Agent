"""
Tests for GOTCHI Headless Android BLE-to-Tailscale Forwarder (T4.2 / F18 / Issue #22).
Verifies project structure, AndroidManifest permissions/service declaration, and
cross-language wire-protocol parity between Kotlin BleFramingCodec, C++ ble_framing.h,
and Python backend/ble_framing.py.
"""

from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from backend.ble_framing import (
    BLE_DEFAULT_MTU,
    BLE_HEADER_SIZE,
    BLE_MAGIC,
    BLE_MAX_CHUNK_PAYLOAD,
    BLE_TRAILER_SIZE,
    FrameFlags,
    FrameType,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ANDROID_DIR = REPO_ROOT / "android-ble-forwarder"
MANIFEST_PATH = ANDROID_DIR / "app" / "src" / "main" / "AndroidManifest.xml"
CODEC_PATH = (
    ANDROID_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "forwarder"
    / "BleFramingCodec.kt"
)
SERVICE_PATH = (
    ANDROID_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "forwarder"
    / "GotchiBleRelayService.kt"
)
RECEIVER_PATH = (
    ANDROID_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "forwarder"
    / "BootCompletedReceiver.kt"
)
ACTIVITY_PATH = (
    ANDROID_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "forwarder"
    / "GotchiBleBondingActivity.kt"
)
UPDATER_PATH = (
    ANDROID_DIR
    / "app"
    / "src"
    / "main"
    / "java"
    / "ventures"
    / "brock"
    / "gotchi"
    / "forwarder"
    / "GotchiForwarderUpdater.kt"
)
APK_PATH = ANDROID_DIR / "releases" / "gotchi-ble-forwarder-debug.apk"


def test_android_project_files_exist():
    assert (ANDROID_DIR / "build.gradle.kts").is_file()
    assert (ANDROID_DIR / "settings.gradle.kts").is_file()
    assert (ANDROID_DIR / "app" / "build.gradle.kts").is_file()
    assert (ANDROID_DIR / "README.md").is_file()
    assert MANIFEST_PATH.is_file()
    assert CODEC_PATH.is_file()
    assert SERVICE_PATH.is_file()
    assert RECEIVER_PATH.is_file()
    assert ACTIVITY_PATH.is_file()
    assert UPDATER_PATH.is_file()
    assert APK_PATH.is_file()


def test_android_manifest_permissions_and_foreground_service():
    tree = ET.parse(MANIFEST_PATH)
    root = tree.getroot()
    android_ns = "{http://schemas.android.com/apk/res/android}"

    permissions = {
        elem.attrib.get(f"{android_ns}name")
        for elem in root.findall("uses-permission")
    }
    required_permissions = {
        "android.permission.BLUETOOTH_SCAN",
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.FOREGROUND_SERVICE",
        "android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE",
        "android.permission.INTERNET",
        "android.permission.RECEIVE_BOOT_COMPLETED",
        "android.permission.REQUEST_INSTALL_PACKAGES",
    }
    assert required_permissions.issubset(permissions)

    app_elem = root.find("application")
    assert app_elem is not None

    providers = {
        p.attrib.get(f"{android_ns}name"): p
        for p in app_elem.findall("provider")
    }
    assert ".FileProvider" in providers
    assert providers[".FileProvider"].attrib.get(f"{android_ns}authorities") == "${applicationId}.fileprovider"

    activities = {
        a.attrib.get(f"{android_ns}name"): a
        for a in app_elem.findall("activity")
    }
    assert ".GotchiBleBondingActivity" in activities
    launcher_act = activities[".GotchiBleBondingActivity"]
    assert launcher_act.attrib.get(f"{android_ns}exported") == "true"

    services = {
        s.attrib.get(f"{android_ns}name"): s
        for s in app_elem.findall("service")
    }
    assert ".GotchiBleRelayService" in services
    relay_service = services[".GotchiBleRelayService"]
    assert relay_service.attrib.get(f"{android_ns}foregroundServiceType") == "connectedDevice"

    receivers = {
        r.attrib.get(f"{android_ns}name"): r
        for r in app_elem.findall("receiver")
    }
    assert ".BootCompletedReceiver" in receivers


def test_kotlin_ble_framing_codec_wire_parity():
    codec_src = CODEC_PATH.read_text(encoding="utf-8")

    magic_match = re.search(r"const val MAGIC:\s*Int\s*=\s*0x([0-9A-Fa-f]+)", codec_src)
    assert magic_match and int(magic_match.group(1), 16) == BLE_MAGIC

    header_match = re.search(r"const val HEADER_BYTES:\s*Int\s*=\s*(\d+)", codec_src)
    assert header_match and int(header_match.group(1)) == BLE_HEADER_SIZE

    trailer_match = re.search(r"const val TRAILER_BYTES:\s*Int\s*=\s*(\d+)", codec_src)
    assert trailer_match and int(trailer_match.group(1)) == BLE_TRAILER_SIZE

    mtu_match = re.search(r"const val DEFAULT_GATT_MTU:\s*Int\s*=\s*(\d+)", codec_src)
    assert mtu_match and int(mtu_match.group(1)) == BLE_DEFAULT_MTU

    payload_match = re.search(r"const val DEFAULT_MAX_PAYLOAD:\s*Int\s*=\s*(\d+)", codec_src)
    assert payload_match and int(payload_match.group(1)) == BLE_MAX_CHUNK_PAYLOAD

    for ft in FrameType:
        pattern = rf"{ft.name}\(0x{int(ft):02X}\)"
        assert re.search(pattern, codec_src), f"Missing or mismatched FrameType {ft.name} in Kotlin codec"

    for ff in FrameFlags:
        pattern = rf"FLAG_{ff.name}:\s*Int\s*=\s*0x{int(ff):02X}"
        assert re.search(pattern, codec_src), f"Missing or mismatched FrameFlags {ff.name} in Kotlin codec"

    # Verify CRC16 is computed over first 14 bytes of the 16-byte header
    assert "crc16Ccitt(prefixBytes, 0, 14)" in codec_src
    assert "crc16Ccitt(raw, 0, 14)" in codec_src
    # Verify cross-frame sequence continuity and interrupted multi-chunk detection
    assert "seq_num_discontinuity" in codec_src
    assert "interrupted_multi_chunk_frame" in codec_src


def test_gotchi_ble_relay_service_capabilities():
    service_src = SERVICE_PATH.read_text(encoding="utf-8")

    # BLE 5.0 2M PHY and 512 MTU negotiation
    assert "PHY_LE_2M_MASK" in service_src
    assert "requestMtu(BleFramingCodec.DEFAULT_GATT_MTU)" in service_src

    # BLE OS bonding, dynamic rescan, and disconnect/disable relay actions
    assert "createBond()" in service_src
    assert "ACTION_RESCAN" in service_src
    assert "ACTION_DISCONNECT" in service_src
    assert "KEY_RELAY_ENABLED" in service_src
    assert "disconnectAndDisableRelay" in service_src
    assert "connectionStatus" in service_src
    assert "connectedDeviceName" in service_src

    # Gateway URL resolution and /api/voice/interact suffix normalization
    assert '.removeSuffix("/api/voice/interact")' in service_src

    # Activity UI disconnect control and BootReceiver relay gating
    activity_src = ACTIVITY_PATH.read_text(encoding="utf-8")
    assert "DISCONNECT / DISABLE BLE RELAY" in activity_src
    assert "ACTION_DISCONNECT" in activity_src
    assert "KEY_RELAY_ENABLED" in activity_src

    receiver_src = RECEIVER_PATH.read_text(encoding="utf-8")
    assert "KEY_RELAY_ENABLED" in receiver_src

    # GATT Service and Characteristic UUIDs
    assert "6E400001-B5A3-F393-E0A9-E50E24DCCA9E" in service_src
    assert "6E400002-B5A3-F393-E0A9-E50E24DCCA9E" in service_src
    assert "6E400003-B5A3-F393-E0A9-E50E24DCCA9E" in service_src

    # Tailscale HTTP multipart relay with Bearer token and audio_out_mode
    assert "/api/voice/interact" in service_src
    assert '"Authorization", "Bearer $bearerToken"' in service_src
    assert 'addFormDataPart("audio_out_mode", audioOutMode)' in service_src
    assert 'addFormDataPart(\n                        "image_$idx"' in service_src or '"image_$idx"' in service_src

    # Local phone/earbud playback when audio_out_mode == "ble_phone"
    assert 'audioOutMode == "ble_phone"' in service_src
    assert "playPcmOnPhoneSpeaker" in service_src

    # Smart Pipe offload capabilities:
    # 1. 4-bit IMA ADPCM uplink & downlink transcoding
    codec_src = CODEC_PATH.read_text(encoding="utf-8")
    assert "object ImaAdpcmCodec" in codec_src
    assert "decodeGad1OrAdpcmToWav" in codec_src
    assert "encodePcmToGad1" in codec_src
    assert "transcodeUplinkAudioIfNeeded" in service_src
    assert "transcodeDownlinkAudioEventToAdpcm" in service_src

    # 2. Dead-zone store-and-forward offline spooling + 4xx poison-pill eviction + SSE mid-stream guard
    assert "spoolTurnForOfflineReplay" in service_src
    assert "drainOfflineSpool" in service_src
    assert '"offline_queued"' in service_src
    assert "enum class PostOutcome" in service_src
    assert "PERMANENT_FAILURE" in service_src
    assert "responseStarted = true" in service_src
    assert "if (responseStarted)" in service_src
    assert "e is IllegalArgumentException" in service_src
    assert "catch (e: JSONException)" in service_src
    assert "encodePcmToGad1WithState" in codec_src
    assert "downlinkAdpcmPredictor" in service_src
    assert "downlinkAdpcmStepIndex" in service_src

    # 3. Passive coarse GPS location metadata tagging
    assert "readCoarseLocationMeta" in service_src
    assert 'addFormDataPart("lat"' in service_src
    assert 'addFormDataPart("lon"' in service_src
    assert 'addFormDataPart("location_accuracy_m"' in service_src
    assert 'addFormDataPart("offline_replay", "true")' in service_src


def test_compiled_apk_validity():
    assert APK_PATH.is_file(), f"APK not found at {APK_PATH}"
    assert APK_PATH.stat().st_size > 100_000, f"APK file suspiciously small: {APK_PATH.stat().st_size} bytes"
    assert zipfile.is_zipfile(APK_PATH), "APK is not a valid ZIP/APK archive"
    with zipfile.ZipFile(APK_PATH, "r") as zf:
        namelist = zf.namelist()
        assert "AndroidManifest.xml" in namelist
        assert "classes.dex" in namelist


def test_forwarder_updater_and_gateway_endpoints():
    from starlette.testclient import TestClient
    from backend.voice_gateway import create_app

    # GotchiForwarderUpdater Kotlin source validation
    updater_src = UPDATER_PATH.read_text(encoding="utf-8")
    assert "object GotchiForwarderUpdater" in updater_src
    assert "fun checkRemoteUpdate" in updater_src
    assert "fun downloadAndInstallApk" in updater_src
    assert "fun getInstalledVersionCode" in updater_src
    assert "class GotchiFileProvider" in updater_src
    assert "class FileProvider" in updater_src
    assert "/api/gotchi/forwarder/config" in updater_src
    assert "/api/gotchi/forwarder/apk.apk" in updater_src
    assert "application/vnd.android.package-archive" in updater_src
    assert "Intent.FLAG_GRANT_READ_URI_PERMISSION" in updater_src or "FLAG_GRANT_READ_URI_PERMISSION" in updater_src
    assert "Intent.FLAG_ACTIVITY_NEW_TASK" in updater_src or "FLAG_ACTIVITY_NEW_TASK" in updater_src

    # GotchiBleBondingActivity source validation
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")
    assert "showUpdateAvailablePrompt" in act_src
    assert "GotchiForwarderUpdater.checkRemoteUpdate" in act_src
    assert "UPDATE APP (WI-FI)" in act_src
    assert "GotchiForwarderUpdater.downloadAndInstallApk" in act_src

    # Voice Gateway endpoints: /api/gotchi/forwarder/config and /api/gotchi/forwarder/apk.apk
    app = create_app()
    client = TestClient(app)

    cfg_res = client.get("/api/gotchi/forwarder/config?node_id=gotchi-ble-forwarder-01")
    assert cfg_res.status_code == 200
    cfg = cfg_res.json()
    assert cfg["ok"] is True
    assert cfg["node_id"] == "gotchi-ble-forwarder-01"
    assert "version_code" in cfg
    assert cfg["version_code"] in (3, 4)
    assert cfg["apk_url"] == "/api/gotchi/forwarder/apk.apk"

    apk_res = client.get("/api/gotchi/forwarder/apk.apk")
    assert apk_res.status_code == 200
    assert apk_res.headers["content-type"] == "application/vnd.android.package-archive"
    assert len(apk_res.content) > 100_000


def test_gotchi_phone_engine_and_viewport_parity():
    # 1. Manifest additions: RECORD_AUDIO, CAMERA, camera feature, app metadata
    tree = ET.parse(MANIFEST_PATH)
    root = tree.getroot()
    android_ns = "{http://schemas.android.com/apk/res/android}"

    permissions = {
        elem.attrib.get(f"{android_ns}name")
        for elem in root.findall("uses-permission")
    }
    assert "android.permission.RECORD_AUDIO" in permissions
    assert "android.permission.CAMERA" in permissions

    features = {
        elem.attrib.get(f"{android_ns}name"): elem.attrib.get(f"{android_ns}required")
        for elem in root.findall("uses-feature")
    }
    assert "android.hardware.camera.any" in features
    assert features["android.hardware.camera.any"] == "false"

    app_elem = root.find("application")
    assert app_elem is not None
    assert app_elem.attrib.get(f"{android_ns}label") == "Gotchi"
    assert app_elem.attrib.get(f"{android_ns}icon") == "@mipmap/ic_launcher"
    assert app_elem.attrib.get(f"{android_ns}roundIcon") == "@mipmap/ic_launcher_round"

    # 2. Bundled assets & mipmaps
    assets_dir = ANDROID_DIR / "app" / "src" / "main" / "assets"
    assert (assets_dir / "zero_skin").is_dir()
    assert (assets_dir / "amos_skin").is_dir()
    assert (assets_dir / "zero_skin" / "zero_hd_idle_0.png").is_file()
    assert (assets_dir / "zero_skin" / "zero_hd_idle_0_blink.png").is_file()
    assert (assets_dir / "zero_skin" / "zero_hd_speaking_1.png").is_file()

    res_dir = ANDROID_DIR / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi"
    assert (res_dir / "ic_launcher.png").is_file()
    assert (res_dir / "ic_launcher_round").with_suffix(".png").is_file()

    # 3. GotchiBleRelayService isRelayTurnActive arbiter flag
    service_src = SERVICE_PATH.read_text(encoding="utf-8")
    assert "@Volatile\n        var isRelayTurnActive: Boolean = false" in service_src or "var isRelayTurnActive: Boolean = false" in service_src
    assert "isRelayTurnActive = true" in service_src
    assert "isRelayTurnActive = false" in service_src

    # 4. GotchiPhoneEngine source validation
    engine_file = (
        ANDROID_DIR
        / "app"
        / "src"
        / "main"
        / "java"
        / "ventures"
        / "brock"
        / "gotchi"
        / "forwarder"
        / "GotchiPhoneEngine.kt"
    )
    assert engine_file.is_file()
    eng_src = engine_file.read_text(encoding="utf-8")

    assert "class GotchiPhoneEngine" in eng_src
    assert "object GotchiChimeSynthesizer" in eng_src
    assert "class HeyZeroPreRollListener" in eng_src

    # 16kHz mic capture + 500ms pre-roll + 24kHz playback
    assert "const val MIC_SAMPLE_RATE = 16000" in eng_src
    assert "const val TTS_DEFAULT_SAMPLE_RATE = 24000" in eng_src
    assert "const val PRE_ROLL_MS = 500" in eng_src
    assert "PRE_ROLL_BYTES" in eng_src
    assert "wrapPcm16ToWav" in eng_src
    assert "extractPcmFromWavOrRaw" in eng_src

    # /api/voice/interact multipart WAV + optional camera JPEG attachment
    assert "/api/voice/interact" in eng_src
    assert "imageBytes: ByteArray? = null" in eng_src
    assert "audioPlaybackQueue" in eng_src
    assert "playPcmChunkSequential" in eng_src

    # Full-duplex /api/live/ws Go Live WebSocket + AEC + 300ms barge-in
    assert "/api/live/ws" in eng_src
    assert "AcousticEchoCanceler" in eng_src
    assert "BARGE_IN_SPEECH_MS = 300L" in eng_src
    assert '.put("type", "barge_in")' in eng_src

    # Smart wake-word arbitration checking !GotchiBleRelayService.isRelayTurnActive
    assert "GotchiBleRelayService.isRelayTurnActive" in eng_src
    assert "startWakeWordTurn" in eng_src

    # 24kHz tactile studio chimes (GotchiChimeSynthesizer)
    assert "const val CHIME_SR = 24000" in eng_src
    for chime in ["ptt_start", "ptt_stop", "snap", "thinking", "ready", "error"]:
        assert f'"{chime}"' in eng_src
    assert "0.924" in eng_src

    # Wake phrase spotter (HeyZeroPreRollListener)
    for phrase in ["hey zero", "hay zero", "hi zero", "okay zero"]:
        assert f'"{phrase}"' in eng_src

    # 5. GotchiPhoneViewportView source validation
    viewport_file = (
        ANDROID_DIR
        / "app"
        / "src"
        / "main"
        / "java"
        / "ventures"
        / "brock"
        / "gotchi"
        / "forwarder"
        / "GotchiPhoneViewportView.kt"
    )
    assert viewport_file.is_file()
    vp_src = viewport_file.read_text(encoding="utf-8")

    assert "class GotchiPhoneViewportView" in vp_src
    assert "zero_hd_" in vp_src
    assert "(uiState.animFrame / 12) % 3" in vp_src
    assert "uiState.blink" in vp_src
    assert "headerText" in vp_src
    assert "primaryText" in vp_src


def test_atomic_purple_v2_handheld_ui_and_settings_drawer():
    # 1. build.gradle.kts version bump to 2.1.0 (versionCode 4)
    gradle_file = ANDROID_DIR / "app" / "build.gradle.kts"
    assert gradle_file.is_file()
    gradle_src = gradle_file.read_text(encoding="utf-8")
    assert "versionCode = 4" in gradle_src
    assert 'versionName = "2.1.0"' in gradle_src

    # 2. GotchiBleBondingActivity UI & Chassis verification
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # Full-screen immersive mode flags
    assert "enableFullScreenImmersiveMode" in act_src
    assert "SYSTEM_UI_FLAG_IMMERSIVE_STICKY" in act_src
    assert "WindowInsets.Type.statusBars()" in act_src
    assert "BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE" in act_src

    # Translucent Atomic Purple (#CC6B2CF5 / #8A3FFC) resin shell
    assert "AtomicPurpleChassisView" in act_src
    assert "#CC6B2CF5" in act_src
    assert "#8A3FFC" in act_src

    # Internal matte-black V2.5 PCB window + Gold ENIG trace rails
    assert "chinWindowRect" in act_src
    assert "busW" in act_src
    assert "218, 165, 32" in act_src  # Gold ENIG RGB

    # CNC 6061 bead-blasted aluminum perimeter frame + chamfer highlight + top PDM mic pinhole
    assert "outerRailRect" in act_src
    assert "chamferRect" in act_src
    assert "micR" in act_src

    # Left-rail Chrome GBC power slider and lower USB-C recess
    assert "powerSliderRect" in act_src
    assert "usbCRect" in act_src

    # Top-left protruding aluminum/sapphire camera bump (OV5640)
    assert "camBumpCollarR" in act_src
    assert "camCx" in act_src and "camCy" in act_src

    # Front & top dual-mic pinholes
    assert "mic1Cx" in act_src and "mic1Cy" in act_src
    assert "mic2Cx" in act_src and "mic2Cy" in act_src

    # Recessed OLED viewport embedding GotchiPhoneViewportView
    assert "screenCutoutRect" in act_src
    assert "viewportChild" in act_src
    assert "GotchiPhoneViewportView" in act_src

    # Staggered Game Boy Color Chrome Silver tactile buttons (B lower-left: PTT / 2x LIVE & A upper-right: SNAP / VISION)
    assert "drawMachinedAluminumButton" in act_src
    assert "btnTopCx" in act_src and "btnBotCx" in act_src
    assert "btnBotCy < btnTopCy" in act_src
    assert '"PTT"' in act_src
    assert '"2x LIVE"' in act_src
    assert '"SNAP"' in act_src
    assert '"VISION"' in act_src
    assert "OV5640" in act_src
    assert "42, 184, 216" in act_src  # Cyan AR optical lens coating RGB

    # 5-Slot OG Game Boy diagonal speaker grate in sweeping bottom-right corner
    assert "drawDiagonalSpeakerGrate" in act_src
    assert "speakerSlotCount = 5" in act_src

    # Hybrid Tap-to-Talk latch (500L) and Standalone Snapshot Turn dispatch
    assert "btnTopHoldStartMs" in act_src
    assert "500L" in act_src
    assert "engine.isRecording()" in act_src
    assert "SystemClock.uptimeMillis() - btnTopHoldStartMs >= 500L" in act_src
    assert "dispatchStandaloneSnapshotTurn" in act_src
    assert "engine.dispatchStandaloneSnapshotTurn(imageBytes = jpeg)" in act_src

    # No under-screen LED glow bar (locked V2 hardware spec)
    assert "ledBarRect" not in act_src

    # Top-bezel glowing BLE Relay Status LED pill showing ACTIVE / BONDED vs STANDALONE
    assert "bleStatusPill" in act_src
    assert "BLE RELAY: ACTIVE" in act_src
    assert "BLE RELAY: BONDED" in act_src
    assert "BLE RELAY: STANDALONE" in act_src

    # Top-right ☰ Settings drawer button & dialog
    assert '"☰"' in act_src
    assert "showHamburgerSettingsDialog" in act_src or "showSettingsDialog" in act_src
    assert "settingsDialog" in act_src
    assert "SCAN & BOND TO GOTCHI" in act_src
    assert "DISCONNECT / DISABLE BLE RELAY" in act_src
    assert "UPDATE APP (WI-FI)" in act_src
    assert "radioSpeaker" in act_src and "radioPhone" in act_src
    assert "wakeWordCheckBox" in act_src

    # Smart wake-word arbitration integration
    assert "GotchiBleRelayService.isRelayTurnActive" in act_src or "isRelayTurnActive" in act_src
    assert "KEY_WAKE_WORD_ENABLED" in act_src

    # 3. Verify compiled APK bundles full-color HD Zero avatar assets
    assert APK_PATH.is_file()
    with zipfile.ZipFile(APK_PATH, "r") as zf:
        namelist = zf.namelist()
        assert any("zero_hd_idle_0.png" in name for name in namelist)
        assert any("zero_hd_speaking_1.png" in name for name in namelist)


def test_gotchi_phone_hybrid_tap_to_talk_and_standalone_snapshot():
    """Verify GotchiBleBondingActivity BTN_TOP hybrid tap-to-talk (500L latch)

    and BTN_BOT standalone snapshot turn dispatch when idle.
    """
    act_src = ACTIVITY_PATH.read_text(encoding="utf-8")

    # BTN_TOP Hybrid Tap-to-Talk: hold-to-talk >= 500L, quick tap latches open, second tap finishes
    assert "btnTopHoldStartMs" in act_src
    assert "500L" in act_src
    assert "engine.isRecording()" in act_src
    assert "SystemClock.uptimeMillis() - btnTopHoldStartMs >= 500L" in act_src

    # BTN_BOT Standalone Snapshot / 5-Frame Burst Turn dispatch when idle
    assert "dispatchStandaloneSnapshotTurn" in act_src
    assert "engine.dispatchStandaloneSnapshotTurn(imageBytes = jpeg)" in act_src
    assert "engine.stopPttAndCommit(imageBytes = jpeg)" in act_src




