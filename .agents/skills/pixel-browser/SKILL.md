---
name: pixel-browser
description: Emergency mobile browser automation fallback on physical Pixel 8 Pro (husky) via ADB CDP. Activates when server-side headless browsers hit aggressive anti-bot protections (Cloudflare Turnstile, DataDome, Akamai) requiring genuine mobile hardware/GPU signatures.
---

# 📱 Pixel Browser (Pixel 8 Pro Real-Device Automation)

## Overview
The `pixel-browser` skill provides real-device browser automation running on a physical **Google Pixel 8 Pro** (`husky`, Android 16, Google Tensor G3) tethered via USB to Host 2 (`<HOST_2_IP>:5037`, serial `<PIXEL_ADB_SERIAL>`). 

It connects to genuine mobile Google Chrome over the Chrome DevTools Protocol (CDP) forwarded across ADB, providing an unblockable execution environment that presents authentic mobile hardware fingerprints (Tensor G3, Mali-G715 GPU, authentic WebGL vendor strings, real touch dispatch, and residential network interfaces).

---

## When to Use

Activate this skill when standard server-side headless browsers (Playwright, Puppeteer, curl) fail due to anti-automation defenses:
1. **Aggressive Anti-Bot Walls:** Target website returns HTTP 403, Cloudflare Turnstile infinite challenge loops, DataDome mobile defense, or Akamai Bot Manager blocks.
2. **Device Fingerprint Enforcement:** Target site inspects Canvas/WebGL hardware rendering, battery APIs, or sensor arrays that headless desktop browsers cannot simulate.
3. **Session Cookie Extraction:** Capturing authenticated cookies or clearance tokens (`cf_clearance`) from a real mobile session to replay back into lightweight HTTP scripts.
4. **Mobile Layout & Viewport Verification:** Inspecting exact mobile rendering, viewport responsiveness, and touch UI layouts on physical Android hardware.

---

## Invariants & Guardrails

- **3D Printer Conflict Guard:** The Pixel 8 Pro serves as the bed camera for the Ender-3 V2 via IP Webcam (`http://<HOST_2_IP>:3333`). The tool automatically queries the webcam status before launching Chrome. If IP Webcam is streaming, execution **must abort immediately** unless `--force` is explicitly provided.
- **Display & Battery Longevity:**
  - Android suspends mobile Chrome JavaScript execution and rendering when the screen is asleep (`mWakefulness=Asleep`).
  - Sessions must wake and dismiss keyguard (`input keyevent 224` and `82`) on start.
  - Sessions **MUST unconditionally put the display to sleep (`input keyevent 26`)** upon termination via guaranteed `finally` context managers to prevent OLED burn-in and thermal battery degradation.
- **Transport Priority:** Always prioritize the high-speed USB daemon on Host 2 (`-H <HOST_2_IP> -P 5037 -s <PIXEL_ADB_SERIAL>`).
- **Tab Lifecycle & Single-Tab Invariant:**
  - Launching Chrome via Android `am start -d <url>` spawns an unmanaged new tab every invocation.
  - Automation must launch Chrome without `-d` and navigate the single existing page in-place.
  - Sessions must execute pre-clean and post-clean sweeps (`close_extra_tabs`) so that popups and secondary tabs are terminated immediately, keeping exactly one clean tab in Chrome.
  - Dedicated maintenance sweep: `python3 /workspace/tools/pixel_browser.py cleanup-tabs`.
- **Single-Turn Discipline:** Do not leave open interactive browser sessions hanging. Execute target actions (extract text, cookies, or screenshot) and tear down the CDP bridge immediately.

---

## Execution Workflow

All automation is orchestrated via `/workspace/tools/pixel_browser.py`.

### 1. Pre-Flight Health & Conflict Check
Check battery levels, thermal state, and confirm IP Webcam is idle:
```bash
python3 /workspace/tools/pixel_browser.py status
```
*Expected Output:*
```json
{
  "device": "Pixel 8 Pro (husky)",
  "transport": "usb",
  "serial": "<PIXEL_ADB_SERIAL>",
  "conflict": false,
  "conflict_message": "Clear (IP Webcam offline)",
  "battery_pct": 38,
  "temperature_c": 24.5,
  "charging": true,
  "screen_awake": false
}
```

### 2. Extract Page Text or Content
Navigate to the protected URL on mobile Chrome and dump the rendered text:
```bash
python3 /workspace/tools/pixel_browser.py browse "https://example.com" --extract text
```
To capture complete rendered DOM:
```bash
python3 /workspace/tools/pixel_browser.py browse "https://example.com" --extract html
```

### 3. Extract Anti-Bot Clearance Cookies
Retrieve cookies after page settlement for session replaying:
```bash
python3 /workspace/tools/pixel_browser.py browse "https://example.com" --extract cookies
```

### 4. Capture Mobile Layout Screenshot
Capture a full-viewport screenshot on real mobile Chrome:
```bash
python3 /workspace/tools/pixel_browser.py screenshot "https://example.com" -o /workspace/data/pixel_capture.png
```
*Pair with `deliver_image.py` or Discord embeds when user preview is requested.*

---

## Verification & Receipts

1. **Verify Screen Asleep Post-Run:**
   Confirm display was cleanly returned to sleep:
   ```bash
   python3 /workspace/tools/pixel_browser.py status | grep '"screen_awake": false'
   ```
2. **Unit Test Regression:**
   Run the test suite to verify mocks and conflict logic:
   ```bash
   python3 -m pytest /workspace/tools/test_pixel_browser.py -v
   ```

### Proactive Activation Invariant
Whenever a user request requires fetching 3D models, files, links, or documentation from sites guarded by Cloudflare Turnstile, DataDome, or anti-bot protections (e.g. MakerWorld, Printables, Thingiverse, Reddit):
1. **Never Silently Abandon or Pivot:** You are strictly forbidden from substituting procedural or mock CAD objects when a requested third-party asset is blocked by anti-bot checks.
2. **Immediate Failover to Pixel Browser:** Proactively engage `python3 /workspace/tools/pixel_browser.py browse "<url>"` or Playwright CDP against `http://127.0.0.1:9223`.
3. **Transparent Escalation:** If interactive authentication or credential entry is required, surface it to Ryan immediately per `ask_for_credentials_first.md`.
