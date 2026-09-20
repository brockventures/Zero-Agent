---
name: mlb-stream
description: >-
  Extracts direct live unauthenticated HLS (.m3u8) streams for any MLB game (Chicago Cubs, LA Dodgers, etc.)
  from the mlb24all.ir / MLB66 Bungee backend and casts directly to Home Assistant Android TV / VLC players.
---

# ⚾ MLB Stream Extractor & Living Room TV Cast Skill

The **MLB Stream** skill enables Zero to automatically discover, resolve, and dispatch live Major League Baseball HLS video streams directly to the living room television with zero manual browser navigation, zero ad popups, native full-screen playback, and unmuted audio.

---

## 🎯 When to Activate This Skill
* The user asks to watch, stream, or put on an MLB game (e.g. *"put on the Cubs"*, *"watch the Dodgers game"*, *"stream baseball"*, *"game on"*).
* Resolving live `.m3u8` master playlist URLs for any MLB team.
* Dispatched automatically via chat in `#zero-chat` or scripted in Home Assistant.

---

## 🏗️ Technical Architecture & Pipeline

```
1. Client Prompt ("put on the Cubs")
   ↳ tools/mlb_stream_extractor.py --team cubs --cast
2. Backend Query
   ↳ Fetches live stateshot from mlb24all.ir (MLB66 Bungee API)
   ↳ Resolves team ID, active/live game, and Home/Away broadcast feed
3. Stream Token Generation
   ↳ POST https://api.mlb24all.ir/api/v2/generate_stream_info
   ↳ Yields unauthenticated raw master HLS playlist (.m3u8)
4. Home Assistant Dispatch & Wake Handling
   ↳ Probes remote.living_room_tv & media_player.living_room_tv_2 states
   ↳ If cold-start (off/standby): sends remote.turn_on and tears down stale Cast sessions (1.5s)
   ↳ Waits 12.0s for Samsung TV power-on + Broadlink IR input switch automation (1763854768768)
   ↳ Dispatches initial media_player.play_media -> media_player.living_room_tv_2 (vlc:// intent)
   ↳ Post-Launch Verification (Cold Boot Guard): Waits 2.5s and probes attributes.app_id; if Android TV launcher boot stole focus, re-dispatches VLC intent once
   ↳ Decoupled release: ceases intent dispatch immediately once VLC is confirmed active
5. Television Playback
   ↳ Native VLC for Android TV renders hardware-accelerated full screen + unmuted audio
```

---

## 🛠️ CLI Tool Usage & Syntax

The primary executable lives at [`/workspace/tools/mlb_stream_extractor.py`](file:///workspace/tools/mlb_stream_extractor.py).

### Quick Commands

```bash
# 1. Cast the Chicago Cubs game directly to the Living Room TV
python3 /workspace/tools/mlb_stream_extractor.py --team cubs --cast

# 2. Cast any other MLB team (e.g. Dodgers, Yankees, Brewers)
python3 /workspace/tools/mlb_stream_extractor.py --team dodgers --cast

# 3. Resolve only the stream URL (for testing or feeding external players)
python3 /workspace/tools/mlb_stream_extractor.py --team cubs --url-only

# 4. Dump full game and broadcast metadata as JSON
python3 /workspace/tools/mlb_stream_extractor.py --team cubs --json
```

### CLI Flags
* `--team <name>`: Target team substring (case-insensitive, default: `cubs`).
* `--feed <auto|home|away|national>`: Preferred broadcast feed (default: `auto`, which detects Home/Away status and automatically selects the target team's broadcast).
* `--cast`: Wakes the TV remote and sends the stream URL to Home Assistant.
* `--entity <id>`: Target HA media player entity (default: `media_player.living_room_tv_2`).
* `--json`: Outputs structured JSON with matchup, live scores, broadcast feed, and stream URL.
* `--url-only`: Prints strictly the resolved `.m3u8` master playlist URL.

---

## ⚙️ Hardware & Protocol Requirements

1. **VLC for Android TV (`org.videolan.vlc`):**
   * Installed on Google TV Streamer / Chromecast.
   * Intercepts incoming `https://.../master.m3u8` video intents and renders hardware-accelerated 60fps video.
   * Avoids Chromium browser autoplay audio muting and ad popups entirely.
2. **Home Assistant Integration & Cold-Start Wake Handling:**
   * Uses `androidtv_remote` (`media_player.living_room_tv_2` and `remote.living_room_tv`).
   * Triggering `remote.turn_on` wakes the Google TV, automatically engaging automation `Chromecast change source` (`1763854768768`) to power on the Samsung TV and switch inputs via Broadlink IR.
   * **Cold-Start Timing & Intent Swallowing Hazard:** Google TV Streamer / Android TV takes ~10-12s to complete the Samsung TV power-on + Broadlink IR input switch handshake. During a cold boot, the Android TV launcher UI initialization finishes *after* the initial wake event, which can steal focus and swallow the initial `play_media` intent.
   * **Single-Shot Verification & Re-Dispatch:** Wait 2.5s after the initial dispatch. If `media_player.living_room_tv_2.attributes.app_id != org.videolan.vlc`, re-dispatch the `vlc://` intent once to guarantee foreground focus.
   * **Decoupled Intent Dispatch (The Invariant):** After the single-shot verification, NEVER continuously poll or spam `play_media` intents. Continuous intent bombardment tramples active VLC surface buffers and crashes Android TV back to the launcher or YouTube (`scar_android_tv_vlc_intent_dispatch.md`).
   * **Warm TV Fast-Path:** When the TV is already awake and on (`remote.living_room_tv == on` and media player active), the 12s sleep is bypassed completely, executing instant dispatch (<1s).

---

## 🚨 Operational Triage & Failure Modes

* **Symptom: TV powers on and switches inputs, but stays on Google TV home screen (game doesn't load):**
  - **Root Cause:** Android TV launcher boot sequence stole window focus right after the initial intent was delivered.
  - **Automated Fix:** `mlb_stream_extractor.py` performs a 2.5s post-launch check on `attributes.app_id` and auto-refires the VLC intent once if stuck on launcher.
  - **Manual Recovery:** Re-run `python3 /workspace/tools/mlb_stream_extractor.py --team cubs --cast`. Because the TV is now warm, it bypasses the 12s delay and immediately pops VLC into the foreground.
* **Symptom: Stale Cast session blocks VLC:**
  - **Root Cause:** A paused or idle Google Cast backdrop or YouTube receiver blocks native app surface creation.
  - **Mitigation:** The pipeline explicitly powers down `media_player.living_room_tv` before dispatching `vlc://` to `media_player.living_room_tv_2`.
* **Symptom: VLC launches but shows stream playback error:**
  - **Root Cause:** Stream token expired or node backend flapped.
  - **Recovery:** Re-running the script queries `mlb24all.ir` for fresh `generate_stream_info` tokens and active flavor IDs.

---

## ⏰ Automated Game-Day Sidecar (`cubs_game_notifier`)

The Cubs Game Day Notifier runs via the persistent Karakos scheduler (`schedule.json`):
* **Interval:** Every 5 minutes (`300s`).
* **Detection Window:** Automatically checks `statsapi.mlb.com` for today's Cubs game. When first pitch is ~10 minutes out (0-15m window), emits a Discord alert to `#zero-chat`.
* **One-Click TV Cast:** Dispatches Discord message with interactive `[CHOICES: Cast Cubs on TV | Dismiss]` buttons.
* **Deduplication:** Tracks alerted game PKs in `/workspace/data/cubs_notifier_state.json` to prevent duplicate pings.
* **On-Demand Discord Hooks:** Trigger manually anytime with `!cubs` or `/cubs`.

```bash
# Test notification output for the upcoming game
python3 /workspace/tools/cubs_notifier.py --test

# Run sidecar wrapper check
python3 /workspace/tools/sidecars.py cubs --test
```

---

## 🧪 Testing & Verification

Unit tests are maintained in:
```bash
python3 -m unittest /workspace/tools/test_mlb_stream_extractor.py
python3 -m unittest /workspace/tools/test_cubs_notifier.py
```
