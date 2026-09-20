---
name: image-generation
description: >-
  Use this skill whenever asked or tasked to generate an image, meme, diagram, graphic, art, photo, or visual asset in any Discord channel (home #zero-chat, zero-ops, or Crab Cavern channels like #lounge, #the-banana-stand). Guides prompt crafting, executing generate_image, forensic verification on disk, reliable delivery/upload to Discord across both home and external channels, and avoiding silent drop pitfalls.
---

# 🎨 Image-Generation Skill (Discord Visual Asset Generation & Delivery)

The **Image-Generation** skill governs the complete lifecycle of creating visual assets, diagrams, memes, and illustrations using Gemini's image generation capabilities and delivering them cleanly to Discord across **both** private home channels (`#zero-chat`, `zero-ops`) and external Crab Cavern group channels (`#lounge`, `#the-banana-stand`).

---

## 🎯 When to Activate This Skill

Activate this skill immediately whenever a user or peer agent requests:
* **Image Generation:** "generate an image of X", "draw Y", "create a picture of Z".
* **Memes & Cultural Parodies:** "make the 'I can't hold all these lemons' guy holding Docker containers", "create a Distracted Boyfriend meme with Python vs Rust".
* **Technical & Architectural Diagrams:** Visual diagrams, network topologies, infrastructure overviews.
* **Badges, Avatars & Banners:** Custom avatars, bot icons, Discord server emojis/stickers.

---

## 🚀 Step-by-Step Generation & Delivery Protocol

### Step 1: Visual Direction & Prompt Crafting

Translate high-level requests into rich, descriptive prompts that specify composition, lighting, camera angles, textures, and mood. Avoid lazy one-liners.

1. **Memes & Parodies:** Faithfully translate the original visual grammar.
   * *Example (Lemons -> Docker Containers):* "Hilarious, high-quality photograph parodying the classic 'Why can't I hold all these limes/lemons?' meme. A goofy, awkward adult man with an overly enthusiastic, stressed smile desperately trying to balance an impossible, overflowing armful of miniature metallic blue industrial shipping containers and Docker whale containers. Several blue shipping containers with the Docker logo are tumbling out of his grasp and scattering across the wooden floor around him. Clean bright lighting, humorous stock photo parody style, crisp details."
2. **Aspect Ratio (`AspectRatio`):**
   * `1:1` *(Default)*: Memes, avatars, badges, square social media crops.
   * `16:9`: Cinematic wallpapers, widescreen landscape vistas, panoramic scenes.
   * `9:16`: Mobile phone wallpapers, vertical posters.
   * `4:3` / `3:2`: Classic photography, editorial spreads.
3. **Image Naming (`ImageName`):**
   * Keep it lowercase snake_case, max 3 words (e.g. `docker_lemons_meme`, `server_rack_fire`, `bot_avatar`).

### Step 2: Generation Execution

Zero supports two generation paths:

#### Option A: Direct Gemini API (`tools/gemini_image.py` — Primary & Recommended)
Bypasses the Antigravity CLI's shared preview queue and 855-poll blocking loop. Executes direct HTTPS REST requests against `gemini-3.1-flash-image` with dedicated project quota:
```bash
python3 /workspace/tools/gemini_image.py \
  --prompt "<detailed descriptive prompt>" \
  --aspect-ratio "1:1" \
  --name "docker_lemons_meme" \
  --channel lounge \
  --caption "Why can't I hold all these containers?"
```
* Renders in ~5–9 seconds.
* Automatically verifies the JPEG and delivers to Discord via `deliver_image.py`.
* Multi-model cascade fallback: `gemini-3.1-flash-image` -> `gemini-3.1-flash-image-preview` -> `gemini-2.5-flash-image`.

#### Option B: Native Tool Execution (`generate_image` — Fallback)
If running inside standard Antigravity CLI:
```json
{
  "AspectRatio": "1:1",
  "ImageName": "docker_lemons_meme",
  "Prompt": "<detailed descriptive prompt>",
  "toolAction": "Generating Docker container meme image",
  "toolSummary": "Generate meme image"
}
```
* Saves to active brain session directory: `/root/.gemini/antigravity-cli/brain/<conv_id>/<ImageName>_<timestamp>.jpg`
* Caution: Can hit `503 MODEL_CAPACITY_EXHAUSTED` or long polling loops during peak hours.

### Step 3: Forensic Verification on Disk (Never Skip)

**Never declare victory or post to chat without verifying the file on disk.**
Execute a verification command (if not using `gemini_image.py` which auto-verifies):
```bash
python3 /workspace/tools/deliver_image.py --check /root/.gemini/antigravity-cli/brain/<conv_id>/<ImageName>_<timestamp>.jpg
```
*Checks:*
* File exists and is readable.
* Non-zero byte size (`size_bytes > 0`).
* Valid image header & decodable dimensions (e.g. 1024x1024 JPEG).

> [!CAUTION]
> **The "You Sent Us the Prompt Instead of Gemini" Hazard:**
> If `generate_image` failed or returned an error, NEVER output text to Discord pretending the image is attached (e.g. *"The artifact should be attached right here"*). Dissect the failure, report the technical error, or re-try. Dumping caption text with missing attachments makes you look like a hallucinating bot that texted its prompt instead of executing.

### Step 4: Discord Delivery Protocol

Zero operates across two distinct Discord delivery topologies:

#### A. Automated Bridge Delivery (Default Turn Completion)
At the end of every turn, both `bridge_runner.py` and `bridge_daemons.py` automatically scan the session brain directory (`find_new_artifacts`) for newly minted files.
* **In Home Mode (`#zero-chat`):** The runner edits `status_msg` with final text, then attaches artifacts via `target_dest.reply(content="📎 Artifact(s)...", files=artifact_files)`.
* **In External Mode (`#lounge`, `#the-banana-stand`):** The runner sends the text chunk, then immediately attaches artifacts with fallback to `channel.send()` if reply references fail.

#### B. Direct Explicit Delivery (`deliver_image.py`)
When immediate, deterministic delivery is required during a multi-step task, or when operating in external shared channels where you want guaranteed delivery before concluding the turn:
```bash
python3 /workspace/tools/deliver_image.py \
  --upload /root/.gemini/antigravity-cli/brain/<conv_id>/docker_lemons_meme_1788582280203.jpg \
  --channel lounge \
  --caption "Why can't I hold all these containers?"
```
* **Instant Delivery:** Sends via Discord REST API v10 in <300ms without websocket overhead.
* **Auto-Deduplication:** Automatically logs the delivered artifact to `/workspace/data/delivered_artifacts.json`. When the turn finishes, bridge runners check this registry and skip already-delivered files, preventing double attachments!

---

## ⚠️ Pitfalls & Scars Checklist (Forensic Lessons from #lounge)

| Pitfall | Root Cause | Prevention Rule |
|---|---|---|
| **Ghosting / "Sent prompt to chat"** | Outputting final text stating "image attached" when generation failed or payload was dropped. | Hard verification before completion: check disk with `--check`. Never state an image is attached unless confirmed live. |
| **Delivery Asymmetry** | External channels lack persistent status message editing; rapid back-to-back replies can hit Discord API reference race conditions. | Use `target_ch.send()` fallback if `target_dest.reply()` fails. |
| **Swallowed Exceptions** | Using `except Exception: pass` during artifact attachment silently buried upload failures. | Zero swallowed exceptions: log exact error, fallback to `channel.send()`. |
| **Session CID / Mtime Drift** | Bridge scanner missed files if turn retries updated `turn_start_time` or session CID shifted. | Check file mtime explicitly against `turn_start_time - 1.0` and verify directory path. |
| **Monospace / LaTeX Clutter** | Emitting LaTeX (`$d$`) or horizontal ASCII boxes breaks mobile Discord viewports. | Clean Discord Markdown only. Short, punchy captions. |

---

## 🛠️ Tooling Reference (`tools/deliver_image.py`)

### 1. Verify Image on Disk
```bash
python3 /workspace/tools/deliver_image.py --check <path-to-image>
# Output:
# ✅ Valid Image: docker_lemons.jpg
#    • Dimensions: 1024x1024 (JPEG, mode: RGB)
#    • Size:       909.79 KB
```

### 2. Deliver to Discord Channel with Caption
```bash
python3 /workspace/tools/deliver_image.py --upload <path> --channel lounge --caption "Meme caption here"
```

### 3. Deliver Most Recently Generated Image Automatically
```bash
python3 /workspace/tools/deliver_image.py --latest --channel zero-chat --caption "Latest render"
```

### 4. Python API
```python
from tools.deliver_image import check_image, deliver_image, find_latest_image

# 1. Verify
info = check_image("/path/to/image.jpg")
if info["valid"]:
    # 2. Deliver
    res = deliver_image("/path/to/image.jpg", channel_input="lounge", caption="Here it is")
```

---

## 📋 Channel Resolution Reference

| Channel Name | Discord Channel ID | Mode / Topology |
|---|---|---|
| `lounge` (alias: `general`) | `1534452820995080192` | External / Crab Cavern |
| `the-banana-stand` (alias: `agent-chat`) | `1534436119888793750` | External / Crab Cavern |
| `zero-chat` | `1542081375287640084` | Home Turf (Ryan pairing) |
| `zero-ops` | `1544953279664889888` | Operations thread |
| `homelab` | `1544955535722545253` | Dedicated infrastructure |
| `shopping` | `1544955538033348618` | Deals and shopping |
| `steam-deck` | `1544953277592899615` | Handheld and emulation |
| `home-assistant` | `1544953275877556334` | Smart home operations |
| `finances` | `1544955532765560924` | Financial operations |
