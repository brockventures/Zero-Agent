# GOTCHI Headless Android Smart-Pipe BLE Companion Relay (`android-ble-forwarder/`)

Headless Android foreground companion service (`F18` / `T4.2`) for out-of-home pocket operation. Eliminates mobile Wi-Fi hotspot battery drain by bonding to GOTCHI over **BLE 5.0 (`2M PHY`)** and relaying voice/vision turns over standard **HTTPS (`443`, Cloudflare Tunnel / reverse proxy — zero Tailscale or VPN required on the phone)** or LAN (`:8765`) to the GOTCHI Voice Gateway (`POST /api/voice/interact`).

---

## 1-Tap Phone Install (Pre-Compiled APK)

A pre-compiled debug APK is committed directly at [`android-ble-forwarder/releases/gotchi-ble-forwarder-debug.apk`](releases/gotchi-ble-forwarder-debug.apk) and published on [GitHub Releases (`v0.1.0`)](https://github.com/brockventures/gotchi/releases/download/v0.1.0/gotchi-ble-forwarder-debug.apk) for instant 1-tap installation without requiring local Android SDK or Gradle setup:

```bash
adb install -r android-ble-forwarder/releases/gotchi-ble-forwarder-debug.apk
```

Direct release download: [gotchi-ble-forwarder-debug.apk (v0.1.0)](https://github.com/brockventures/gotchi/releases/download/v0.1.0/gotchi-ble-forwarder-debug.apk)

---

## Launcher UI & BLE Bonding Setup (`GotchiBleBondingActivity`)

Launch the companion app on your phone from the app launcher or via ADB:

```bash
adb shell am start -n ventures.brock.gotchi.forwarder/.GotchiBleBondingActivity
```

The interactive setup card provides:
1. **Runtime Permission Flow:** Prompts for required `BLUETOOTH_SCAN`, `BLUETOOTH_CONNECT`, `POST_NOTIFICATIONS` (Android 13+), and optional `ACCESS_COARSE_LOCATION` for passive journal geotagging.
2. **Gateway Configuration:** Set the target Voice Gateway URL (`http://gotchi.local:8765` or HTTPS reverse proxy / Cloudflare Tunnel), optional `Bearer Token`, and unique `Node ID`. The phone's configured HTTPS gateway URL takes priority over the badge's local cleartext `hub_url` and automatically strips redundant `/api/voice/interact` suffixes.
3. **Audio Routing Mode:** Select between **Gotchi Speaker** (`4-bit IMA ADPCM` over BLE downlink) and **Phone Audio** (stream assistant speech directly to phone speaker or connected Bluetooth earbuds).
4. **Scan & Bond:** 1-tap scan and OS-level BLE pairing (`createBond()`) with automatic foreground service launch (`GotchiBleRelayService`).
5. **Disconnect / Disable BLE Relay:** 1-tap red disconnect button immediately severs the active GATT connection, halts BLE background scanning, cancels auto-reconnect timers, and persists `KEY_RELAY_ENABLED = false` so the service remains disabled across device reboots without disabling phone Bluetooth.

---

## Notification Controls & Background Lifecycle

- **Foreground Notification "Disconnect" Action:** The persistent foreground notification displays live status (`Connected to GOTCHI`, `Scanning...`, or `Disconnected`) and features a 1-tap **"Disconnect"** action button (`ACTION_DISCONNECT`). Tapping it instantly disconnects the GATT link and disables the relay service without needing to reopen the launcher UI.
- **Boot Persistence Gating:** `BootCompletedReceiver` checks `KEY_RELAY_ENABLED` in SharedPreferences; if the user disabled the relay, the background service will not auto-start on boot until re-enabled by tapping **SCAN & BOND GOTCHI**.

---

## Architecture & Smart Pipe Offload

1. **Auto-Scan & BLE 5.0 2M PHY Bonding:**
   - `GotchiBleRelayService` runs as a persistent `connectedDevice` foreground service (`START_STICKY`) started automatically on boot via `BootCompletedReceiver`.
   - Filters on GOTCHI's BLE GATT UART Service UUID (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`), negotiates `2M PHY` and a `512-byte` ATT MTU (`492-byte` max payload per packet after the 16-byte header and 4-byte CRC32 trailer).
   - Performs OS-level BLE bonding (`createBond()`) on discovery if unbonded.

2. **4-Bit IMA ADPCM BLE Audio Transcoding (`ImaAdpcmCodec` in `BleFramingCodec.kt`):**
   - **Uplink (`64 kbps` vs `256 kbps` raw PCM — 4x faster):** Reassembles `AUDIO_CHUNK` frames tagged with `FLAG_ADPCM` (`0x08`) / `GAD1` container magic (`16 kHz` 4-bit IMA ADPCM) and transcodes them on the phone into a standard 44-byte RIFF/WAVE `16 kHz` 16-bit mono PCM payload before posting to `/api/voice/interact`.
   - **Downlink (`96 kbps` vs `384 kbps` raw PCM — 4x faster):** When `audio_out_mode == "speaker"` over BLE, transcodes incoming `sentence` / `audio_chunk` `24 kHz` 16-bit PCM SSE payloads into `GAD1` 4-bit IMA ADPCM (`"format": "adpcm"`) before writing fragmented `SSE_EVENT` (`0x04`) frames to `CHAR_RX` (`6E400002-...`), where ESP32-S3 `gotchi_audio::playBase64AdpcmChunk` decodes and plays them in real time.
   - **Phone / Earbud Audio Routing (`audio_out_mode == "ble_phone"`):** Intercepts `sentence` / `audio_chunk` SSE events and plays the 24 kHz 16-bit mono PCM directly through Android `AudioTrack` (`USAGE_ASSISTANT`) to the phone speaker or paired Bluetooth earbuds while still forwarding `state` (`SPEAKING`) and `done` events to GOTCHI so the e-paper sprite animates in sync.

3. **Dead-Zone Store-and-Forward Spooling (`offline_spool/`):**
   - When the phone is in a cellular/Wi-Fi dead zone or the gateway returns `502`/`503`/`504`, `GotchiBleRelayService` spools the turn (`wav_b64`, `images`, `commit_meta`, `location_meta`) into `filesDir/offline_spool` (bounded to 64 items FIFO) and immediately returns an `offline_queued` SSE status + `done` frame over BLE.
   - `ConnectivityManager.NetworkCallback` automatically drains and replays spooled turns to `/api/voice/interact` as soon as HTTPS/LAN connectivity returns.

4. **Optional Coarse GPS Metadata Tagging:**
   - When `ACCESS_COARSE_LOCATION` is granted, `readCoarseLocationMeta()` attaches cached passive/network location coordinates (`lat`, `lon`, `location_accuracy_m`) from `LocationManager.getLastKnownLocation` to `/api/voice/interact` turns with zero active GPS radio drain.

---

## Runtime Configuration via ADB Intent

Configure the target gateway URL (standard HTTPS domain or LAN IP), per-device bearer token, and audio routing mode without rebuilding:

```bash
adb shell am start-foreground-service \
  -n ventures.brock.gotchi.forwarder/.GotchiBleRelayService \
  --es gateway_url "https://gotchi.example.com" \
  --es bearer_token "<YOUR_DEVICE_TOKEN>" \
  --es node_id "gotchi-pocket-01" \
  --es audio_out_mode "ble_phone"
```
