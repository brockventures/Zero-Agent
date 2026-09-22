---
name: ui-preview
description: >-
  Use this skill whenever capturing, verifying, or sharing visual previews of web apps, dashboards, local dev servers, or UI components to Discord.
  Executes on-demand headless Playwright screenshots (full page, desktop/mobile viewports, or precise CSS element crops) and delivers them directly to Discord channels.
---

# 📸 UI-Preview Skill (Headless Browser Screenshot & Discord Delivery)

The **UI-Preview** skill governs capturing high-fidelity, visual screenshots of web applications, dashboards (e.g. NasHost-01 Homepage, Mealie, Home Assistant), staging environments, or local development ports (`localhost:3000`, `localhost:5173`) and delivering them directly to Discord.

This completely replaces brittle, unreadable ASCII box art with real, crisp dark-mode viewport and component captures.

---

## 🎯 When to Activate This Skill

Activate this skill whenever:
1. **Sharing UI Verification:** A user or peer agent asks to see a UI change, card redesign, or layout fix ("what does it look like?", "grab a screenshot", "show me the dashboard").
2. **Component Cropping:** You need to present a specific component (e.g. telemetry cards, navigation header, status pill) without cluttering chat with full-page scrollback.
3. **Mobile vs Desktop Layout Testing:** Auditing responsive layouts across desktop (`1280x800`) vs mobile (`390x844`).
4. **Pre-Ship Visual Verification:** Verifying that a frontend build renders cleanly with zero layout shift (CLS = 0) before reporting completion.

---

## 🛠️ Tooling & Execution Runbook (`tools/capture_screenshot.py`)

### 1. Component / Card Crop & Direct Delivery to Discord
Capture specific UI elements by CSS/text selector and immediately dispatch to `#zero-chat` with a caption:
```bash
python3 /workspace/tools/capture_screenshot.py "http://localhost:8008" \
  --selector ".service:has-text('Zero'), .service:has-text('Ivy')" \
  --name "homepage_telemetry_cards" \
  --channel zero-chat \
  --caption "Live Homepage Telemetry Cards: Zero & Ivy (AppHost-02 .84)"
```
- Locates matching elements and computes their combined bounding box (+ 8px padding).
- Saves PNG to `/workspace/data/qa_screenshots/homepage_telemetry_cards.png`.
- Uploads directly to `#zero-chat` via Discord REST API v10 in <400ms.
- Registers in `/workspace/data/delivered_artifacts.json` to prevent duplicate attachment at turn completion.

### 2. Full Viewport Capture (Desktop & Mobile)
```bash
# Desktop (1280x800)
python3 /workspace/tools/capture_screenshot.py "http://localhost:3000" \
  --viewport desktop \
  --name "trade_screen_desktop" \
  --channel zero-chat

# Mobile (390x844 iPhone viewport)
python3 /workspace/tools/capture_screenshot.py "http://localhost:3000" \
  --viewport mobile \
  --name "trade_screen_mobile" \
  --channel zero-chat
```

### 3. Full-Page Capture
```bash
python3 /workspace/tools/capture_screenshot.py "http://localhost:8008" \
  --full-page \
  --name "serverbrock_homepage_full"
```

### 4. Programmatic Python Execution
```python
from tools.capture_screenshot import capture_and_deliver

result = capture_and_deliver(
    url="http://localhost:8008",
    selector=".service:has-text('Zero'), .service:has-text('Ivy')",
    name="telemetry_cards",
    channel="zero-chat",
    caption="Live Dashboard Telemetry",
    color_scheme="dark",
)

if result["success"]:
    print(f"Captured {result['dimensions']} ({result['size_kb']} KB)")
    if result["delivered"]:
        print(f"Delivered to Discord: {result['delivery_info']['message_id']}")
```

---

## 📐 Viewport Presets Reference

| Preset | Dimensions | Best For |
|---|---|---|
| `desktop` *(default)* | `1280x800` | Standard dashboard view, desktop web app auditing |
| `desktop-hd` | `1920x1080` | High-resolution wide desktop viewports |
| `desktop-wide` | `1440x900` | Laptop viewports |
| `mobile` | `390x844` | Mobile phone viewports (iPhone 14/15 frame) |
| `tablet` | `820x1180` | iPad / tablet responsive validation |
| Custom (`WxH`) | e.g. `1024x768` | Explicit arbitrary dimensions |

---

## 🔒 Operational Invariants & Best Practices

1. **Zero ASCII Box Art:**
   - NEVER fall back to monospace ASCII boxes (`┌───┐`, `│`, `└───┘`) for visual layout verification.
   - Use `tools/capture_screenshot.py` for visual deliverables, or clean native markdown cards for text telemetry.
2. **Automatic Session Artifact Preservation:**
   - `capture_screenshot.py` automatically copies captured PNGs to the active conversation brain directory (`/root/.gemini/antigravity-cli/brain/<conv_id>/`).
3. **Deduplication Hygiene:**
   - Delivering via `--channel <name>` automatically logs the file to `/workspace/data/delivered_artifacts.json`.
   - The bridge runner checks this registry at turn completion and skips re-attaching already-delivered files, preventing annoying double attachments.
4. **Ephemeral Playwright Instances:**
   - Playwright Chromium is launched ephemerally and closed immediately upon screenshot capture. Never leave zombie browser processes idling on Host 2.
5. **Visual Verification Before Chat:**
   - Always run `view_file` on the captured screenshot locally to inspect clarity and cropping before declaring victory to the user.
