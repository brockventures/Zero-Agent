---
name: print3d-slicer
description: >-
  Use this skill whenever slicing 3D models (.stl, .obj, .step), preparing print jobs for the Creality Ender-3 V2, configuring OrcaSlicer parameters, offloading heavy CAD/mesh builds to Modal cloud compute, or dispatching print jobs autonomously to OctoPrint. Guides profile selection, tree support reasoning, G-code preview extraction, OctoPrint API execution, and Zero Watcher daemon supervision.
---

# 🖨️ Print3D-Slicer Skill (Autonomous Cloud Remeshing, OrcaSlicer & OctoPrint Dispatch)

The **Print3D-Slicer** skill governs the headless preparation, cloud-accelerated remeshing, parameter reasoning, slicing execution, and OctoPrint network dispatch for 3D models printing on the Creality Ender-3 V2.

---

## 🎯 When to Activate This Skill

Activate this skill whenever:
- Converting an `.stl`, `.obj`, or `.3mf` into sliced `.gcode` for the Ender-3 V2.
- Generating watertight solid meshes from raw 2D-to-3D outputs (Trellis, Meshy, Tripo3D) via Modal serverless compute.
- Offloading heavy 3D CAD transforms, voxelization, or 360° turntable rendering to cloud compute.
- Scaling models up or down while auditing physical FDM nozzle resolution limits.
- Running headless OrcaSlicer on Host 2 (`<HOST_2_IP>`).
- Extracting G-code metrics (estimated print time, filament grams, layer count, embedded thumbnail).
- Dispatching sliced files to OctoPrint and starting the autonomous Zero Printer Watcher daemon.

---

## 🛑 Host Resource & Active Print Concurrency Invariants

To protect real-time USB serial streaming (`/dev/ttyUSB0` at 115200 baud) to the Ender-3 V2 and prevent Host 2 lockups, enforce these non-negotiable compute gates:

1. **Active Print Concurrency Ban:**
   - Heavy CAD operations (voxel pitch <0.30mm, marching cubes, or software mesh rendering) are strictly forbidden on Host 2 while an active print is in flight over USB serial.
   - CPU starvation on Host 2 starves the OctoPrint serial ping-pong buffer, causing the hotend to dwell on the print, melt divots into perimeters, and stall the printer.
   - All heavy mesh remeshing, boolean operations, and 3D rendering must be offloaded to Modal cloud compute.
2. **Visual Decimation Proxy Rule:**
   - Never pass raw high-density meshes (>50,000 faces) to local CPU software depth-sorting.
   - Always create a 35,000-face quadric decimation proxy (`mesh.simplify_quadric_decimation(35000)`) if generating camera previews locally.
   - Full-resolution watertight meshes stay intact on disk strictly for OrcaSlicer.
3. **Worker Priority De-Elevation:**
   - Any local slicing or lightweight transforms on Host 2 must execute with `nice -n 19 ionice -c 3` inside Docker to guarantee CPU priority to real-time OctoPrint daemons.

---

## ☁️ Modal Serverless Cloud Compute Pipeline

Heavy CAD geometry generation, multi-mesh Boolean unions, voxel remeshing, and turntable rendering run serverlessly on Modal to keep Host 2 completely unloaded:

### Configuration & Environment:
- **Modal Profile:** `ryanbrock2011` configured via `/root/.modal.toml` and `/workspace/.env`.
- **Dedicated Virtual Environment:** `/workspace/.venv/modal/` (isolated from global container Python).
- **Execution Pattern:**
  ```bash
  /workspace/.venv/modal/bin/modal run /workspace/tools/modal_v3_builder.py
  /workspace/.venv/modal/bin/modal run /workspace/tools/modal_render_3d.py
  ```

### Cloud Tooling Architecture (Headless Blender 4.2 `bpy` + OpenVDB + Cycles):
1. **`tools/modal_v3_builder.py`:**
   - Runs headless **Blender 4.2 (`bpy==4.2.0`)** on an 8-vCPU, 16GB RAM Modal cloud container.
   - Executes native **OpenVDB Voxel Remeshing** (`REMESH` modifier in `VOXEL` mode, ~0.4s execution) instead of slow CPU numpy Marching Cubes.
   - Applies volume-preserving **Corrective Smooth** (`CORRECTIVE_SMOOTH` with `use_only_smooth=True` and `use_pin_boundary=True`) to eliminate voxel stepping while retaining crisp mechanical creases and edges.
   - Triangulates (`TRIANGULATE`), verifies manifold watertightness and $Z=0$ grounding via `trimesh`, and exports the monolithic STL to `/workspace/data/3dprint/`.
2. **`tools/modal_render_3d.py`:**
   - Renders path-traced 24-frame, 360° studio turntables using **Blender Cycles** (`CYCLES` engine with adaptive sampling).
   - Features a physical 3-point Area Light studio rig (warm Key, cool Fill, crisp Rim), Principled BSDF clay/sculpt shaders, ambient occlusion, and a matte ground plane that catches real contact shadows.
   - Automatically scales camera distance and area light wattage to any single part or multi-part assembly bounding box in true 1:1:1 proportion, delivering optimized GIF turntables directly to Discord.

---

## 📏 Miniature & Figurine Downscaling Protocol

When scaling down full-size models (e.g. 50% scale from 120.0mm down to 60.0mm height):

1. **Uniform Scaling & Grounding:**
   - Scale mesh coordinates uniformly: `mesh.apply_scale(0.50)`.
   - Re-ground bottom boundary strictly to `Z = 0.0`:
     ```python
     mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
     ```
2. **The 0.4mm Nozzle Feature Rule:**
   - A standard 0.4mm brass nozzle produces 0.40–0.45mm perimeter line widths.
   - Minimum self-supporting feature thickness is 2 perimeters (0.80mm).
   - Scaling a model to 50% cuts all physical wall thicknesses in half. Any concept feature originally 1.2mm becomes 0.60mm, which cannot resolve cleanly and breaks during support removal.
   - Ensure concept sculpts enforce $\ge 1.6\text{mm}$ feature thickness if intended for 50% downscaling.
3. **Infill & Shell Adjustments for Scaled Prints:**
   - Scaled miniatures lose internal volume faster than surface area ($V \propto s^3$).
   - Increase gyroid infill to 12%–15% to maintain structural core rigidity for small leg and neck cross-sections.
   - Maintain 3 outer perimeter walls (`wall_loops = 3`) to ensure perimeter strength.

---

## ⚙️ Slicing Execution Engine (OrcaSlicer on Host 2)

OrcaSlicer runs headlessly inside Docker on Host 2 (`<HOST_2_IP>`):

### 1. Slicing Command Pattern:
```bash
nice -n 19 sudo docker exec orcaslicer /opt/orcaslicer/bin/orca-slicer \
  --load-settings "/prints/my_machine.json;/prints/zero_figurine_process.json" \
  --load-filaments "/prints/my_filament.json" \
  --slice 0 \
  --outputdir /prints \
  /prints/model.stl
```

### 2. Autonomous Slicing Parameter Profiles:

| Model Archetype | Layer Height | Walls | Infill Type & % | Support Strategy |
|---|---|---|---|---|
| **Miniature / Figurine (60mm)** | `0.16mm` | 3 | Gyroid 12%–15% | **Organic Tree Supports** (Buildplate only, 30° threshold) |
| **Display Bust / Large Sculpture** | `0.16mm` | 3 | Gyroid 10% | **Organic Tree Supports** (Buildplate only) |
| **Functional Bracket / Tool** | `0.20mm` | 4–5 | Gyroid 25%–40% | **None** or **Snug Normal** (overhangs > 50°) |
| **Rapid Prototype / Fit Test** | `0.28mm` | 2 | Grid 10% | **None** |

### Key FDM Rules for Ender-3 V2:
1. **Always Use Gyroid Infill:** Linear infills (Grid, Triangle) cross over previously printed paths on the same layer, causing nozzle clicks that knock delicate miniatures off the bed. Gyroid infill never crosses itself.
2. **Zero-Brim & Ban on Rafts (Textured PEI Bed):**
   - The textured PEI spring steel build plate provides massive mechanical and thermal adhesion for PETG/PLA at 60°C bed temperature.
   - **Rafts are Strictly Banned (`raft_layers = 0`):** Rafts waste filament, distort bottom accuracy, and are a nightmare to peel.
   - **Brims are Banned on Flat Footprints (`brim_width = 0`):** Any model with flat contact area $>100\text{ mm}^2$ (enclosures, brackets, broad figurine pedestals) prints without a brim. Never fuse a 4–5mm brim to an enclosure edge requiring scraping or deburring.
   - **Detached Purge Skirt Only:** Use `skirt_loops = 1` with `skirt_distance = 4mm` strictly to prime the nozzle and purge stale plastic.
3. **Effortless Support Release Tuning (Tree Slim / Organic):**
   - **Top Z-Distance (`support_top_z_distance = 0.26mm`):** Default `0.20mm` creates a single 1-layer gap that thermally welds to sticky PETG. Increasing to `0.26mm` (1.3× layer height) establishes a clean thermal separation layer so supports snap off cleanly with zero scarring.
   - **Single-Wall Tree Trunks (`support_tree_wall_count = 1`):** Default 2-wall trunks become rigid structural pipes that require pliers to crush. A single perimeter wall makes branches brittle so they snap off effortlessly with finger pressure.
   - **XY Wall Clearance (`support_xy_distance = 0.50mm`):** Expands lateral clearance from default `0.35mm` to `0.50mm`, preventing growing vertical tree trunks from fusing into side walls.
   - **Loosened Interface Spacing (`support_interface_spacing = 0.80mm`):** Replaces solid interface sheets with sparse contact points that pop off without leaving scarred plastic pads.
4. **Bed Contact Stability:** Verify the bottom plane is completely flat at $Z = 0.0$. If a miniature base is slightly domed, apply planar bisecting or translation so the first layer has solid surface area.

---

## 📸 3D CAD Preview & Build Plate Visualization Standards

Whenever generating 3D renders, build plate previews, or layout visualizations:

1. **Mandatory Real-World Aspect Ratio Invariant (`ax.set_box_aspect`):**
   - **NEVER omit `ax.set_box_aspect()`** in matplotlib 3D projections.
   - Matplotlib defaults to cubic normalization, scaling all three axes ($X, Y, Z$) to equal visual screen dimensions.
   - For a 220×220mm build plate with a 20–35mm tall part, omitting `set_box_aspect` creates an artificial 6× to 10× vertical elongation ($Z$-axis stretch), distorting squat enclosures into towering chimneys.
   - **Always enforce:** `ax.set_box_aspect((x_span, y_span, z_span))` (e.g. `ax.set_box_aspect((220, 220, z_max))` for build plates, or `(radius * 2, radius * 2, z_span)` for isolated parts).

2. **Strict Ban on Naive Index-Strided Face Subsampling (`faces[::N]`):**
   - **NEVER** downsample triangular meshes by taking arbitrary stride slices of the face index array (`faces[::3]`, `faces[::4]`).
   - Slicing the index array breaks topological manifold continuity, dropping 67%–75% of surface triangles and rendering solid watertight plastic as a spiky, hollow, see-through wireframe birdcage.
   - For fast visualization:
     - Render intact watertight meshes with solid directional lighting (`Poly3DCollection` with surface normals lighting and `linewidths=0`, `edgecolors='none'`).
     - If mesh density exceeds 100,000 faces, offload to Modal (`tools/modal_render_3d.py`) or use proper volumetric quadric edge-collapse decimation (`mesh.simplify_quadric_decimation()`), never raw array slicing.

3. **Dual-View Preview Standard (Assembled Product + Build Plate Layout):**
   - Functional prints often require parts to be inverted or oriented sideways on the build plate to print support-free (e.g. lid roof resting flat against PEI bed).
   - An inverted part on a build plate does not visually reflect the intended consumer product.
   - Whenever staging a multi-part functional model, generate **both**:
     1. **Assembled Form Preview:** Shows the parts mated in their final upright configuration (e.g. lid on base, looking like the finished product) with 3-point directional lighting.
     2. **Accurate Plate Layout Preview:** Shows the parts oriented on the 220×220 mm bed in true 1:1:1 physical proportion.

---

## 🌐 OctoPrint REST API Dispatch & Dual-Tier Execution

### Method A: Host-Streaming (Fast Local Dispatch)
Used for standard prints (<4 hours):
```bash
curl -s -X POST \
  -H "X-Api-Key: ${OCTOPRINT_API_KEY}" \
  -F "file=@/workspace/data/3dprint/job.gcode" \
  -F "select=true" \
  -F "print=true" \
  "http://<HOST_2_IP>:8888/api/files/local"
```

### Method B: Host-Crash-Immune SD Card Dispatch
Used for long multi-hour prints (>4 hours) where host container reboots or network interruptions must never affect the physical print:
```bash
curl -s -X POST \
  -H "X-Api-Key: ${OCTOPRINT_API_KEY}" \
  -F "file=@/workspace/data/3dprint/job.gcode" \
  -F "select=true" \
  -F "print=true" \
  "http://<HOST_2_IP>:8888/api/files/sdcard"
```

---

## ⏸️ Configured OctoPrint Safe Pause & Resume Hooks

To eliminate nozzle burn divots and prevent bed adhesion loss during intentional pauses (filament changes, nozzle checks, or automated watcher pauses), OctoPrint is configured with these persistent G-code scripts in `/api/settings`:

- **After print job is paused (`afterPrintPaused`):**
  ```gcode
  ; --- Safe Pause Routine ---
  {% if pause_position.z is not none %}
  M83 ; relative extrusion
  G1 E-2.5 F3000 ; retract 2.5mm to eliminate oozing
  G91 ; relative coordinates
  G1 Z10 F1200 ; lift nozzle 10mm clear of model
  G90 ; absolute coordinates
  G1 X10 Y10 F6000 ; park in front-left corner
  ; Bed heater remains at target 60°C to preserve adhesion
  {% endif %}
  ```

- **Before print job is resumed (`beforePrintResumed`):**
  ```gcode
  ; --- Safe Resume Routine ---
  {% if pause_position.z is not none %}
  {% if pause_temperature[0].target is not none and pause_temperature[0].target > 0 %}
  M109 S{{ pause_temperature[0].target }} ; re-verify hotend temperature
  {% endif %}
  G90 ; absolute coordinates
  G1 X{{ pause_position.x }} Y{{ pause_position.y }} F6000 ; return to XY first
  G1 Z{{ pause_position.z }} F1200 ; descend to pause layer height
  M83 ; relative extrusion
  G1 E2.5 F1500 ; prime 2.5mm
  G90 ; restore absolute positioning
  {% endif %}
  ```

---

## 📹 Autonomous IP Webcam Lifecycle (Pixel 8 Pro)

The dedicated monitoring camera (Pixel 8 Pro running IP Webcam) is managed over ADB via `/workspace/tools/webcam_control.py`:

### 1. Mandatory Pre-Flight Camera Initialization:
Zero and the watcher daemon MUST actively bring the camera online before requesting snapshots or starting vision evaluations:
```bash
python3 /workspace/tools/webcam_control.py start
```
- **Wakes Device & Dismisses Lock:** Sends `KEYCODE_WAKEUP` and `wm dismiss-keyguard` over ADB.
- **Launches IP Webcam Activity:** Opens `com.pas.webcam/.Configuration`, scrolls, and taps `Start Server`.
- **Preserves OLED Display:** Dims screen brightness to 0.
- **High-Speed ADB Port Forwarding:** Establishes `adb forward tcp:3333 tcp:3333` across the active transport (USB via Host 2 `<HOST_2_IP>:5037` or Wi-Fi).
- **Endpoint Verification:** Polls `http://127.0.0.1:3333/status.json` to confirm the server is live and returning 200 OK before proceeding.

### 2. Camera Controls & Stale Image Immunity:
- **Start Camera:** `python3 /workspace/tools/webcam_control.py start` (brings camera online, verifies HTTP port).
- **Snapshot:** `python3 /workspace/tools/webcam_control.py snapshot --output /workspace/data/3dprint/snap.jpg` (pulls `/shot.jpg` and rotates 270°).
  - *Stale Disk Invariant:* Callers MUST delete the target snapshot path before capture.
  - *Strict Exit Code:* `snapshot` exits with `1` if frame acquisition fails, preventing downstream tools from reading dead buffers.
- **Stop Camera:** `python3 /workspace/tools/webcam_control.py stop` (force-stops app, closes port 3333, puts screen to sleep).

---

## 🛡️ Autonomous Watcher & Safe Teardown (`tools/zero_printer_watcher.py`)

Whenever a print begins, spin up the Zero Printer Watcher daemon detached:
```bash
python3 /workspace/tools/zero_printer_watcher.py --daemon
```

### Daemon Architecture:
1. **Pre-Flight Camera Boot:** Calls `webcam_control.py start` upon daemon startup to ensure the camera server is actively broadcasting before print monitoring loops begin.
2. **Option B Auto-Pause:** Evaluates snapshots every 20 minutes via Gemini 2.5 Flash vision. If catastrophic failure (spaghetti, detachment, bed drag) is detected with $\ge 70\%$ confidence:
   - Immediately calls OctoPrint's `/api/job` `pause` command.
   - Dispatches urgent spoken audio alert via Stefan profile: `"Catastrophic failure detected on {clean_name}. Print paused to prevent damage."`
   - Delivers urgent photo alert to `#3d-print` (`1555973316752449576`).
3. **Capture Failure Graceful Degradation:** If `capture_webcam_snapshot` fails (camera asleep or offline), the watcher logs a warning and skips the vision evaluation cycle. It NEVER evaluates stale images and NEVER pauses an active print solely due to camera network dropouts.
4. **Nominal Progress Receipts:** Delivers clean progress checks with hotend temperatures and vision confidence to `#3d-print`.
5. **Dynamic Dual-Cadence Teardown:**
   - Upon `PrintDone`, `PrintCancelled`, or `PrintFailed`, shifts polling cadence to 60 seconds.
   - Dispatches completion announcement: `"Print complete for {clean_name}. Part cooling on bed. Do not touch."`
   - Waits until the nozzle cools below 50.0°C to prevent heat creep.
   - Dispatches teardown announcement: `"Cooldown threshold satisfied. Cutting main power now."`
   - Stops IP Webcam on the Pixel 8 Pro via ADB.
   - Turns off `switch.3d_printer_power` via Home Assistant REST API.
   - Posts final teardown receipt to `#3d-print` and exits cleanly.

---

## 🔊 Physical Audio Announcement Architecture (`tools/phone_speech.py`)

Physical voice announcements broadcast into the 3D printer room directly through the Pixel 8 Pro's hardware speaker via neural TTS and ADB:

### Voice Profile & Synthesis:
- **TTS Engine:** Microsoft Edge TTS (`edge-tts`).
- **Profile:** Stefan (Ultron persona) — `en-US-SteffanNeural` (`-5Hz` pitch, `-5%` rate, volume: 21).
- **Transport Pipeline:** Synthesizes `.mp3` to `/workspace/scratch/audio/`, pushes to `/sdcard/Notifications/zero_announcement.mp3` over ADB, sets media stream volume to 21, wakes device, and triggers playback via Android `PreviewActivity`.

### 4 Audio Lifecycle Milestones:
1. **Extrusion Start (Initial Kick-Off):**
   `"Extrusion initialized for {clean_name}. Keep your hands off the build plate."`
   - Fires once when print first transitions to `Printing` ($Z \le 5.0\%$).
2. **Catastrophic Failure Auto-Pause (Option B):**
   `"Catastrophic failure detected on {clean_name}. Print paused to prevent damage."`
   - Fires synchronously before OctoPrint pause to alert nearby operators immediately.
3. **Print Complete (100% Cooldown Entrance):**
   `"Print complete for {clean_name}. Part cooling on bed. Do not touch."`
   - Fires immediately upon `PrintDone` while fans run to cool the hotend.
4. **Mains Cut & Safe Teardown:**
   `"Cooldown threshold satisfied. Cutting main power now."`
   - Fires right before cutting `switch.3d_printer_power` via Home Assistant.

---

## 🔄 End-to-End Discord Autonomous Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Ryan as Ryan (Discord)
    participant Zero as Zero (Antigravity)
    participant Watcher as Zero Watcher Daemon
    participant Phone as Pixel 8 Pro (Speaker & Cam)
    participant Octo as OctoPrint (Host 2 NAS)
    participant HA as Home Assistant

    Ryan->>Zero: "Plate is clear, print v3 at 50%"
    Zero->>Phone: python3 tools/webcam_control.py start (verify HTTP 200)
    Zero->>Octo: POST /api/files/local (select=true, print=true)
    Zero->>Watcher: python3 tools/zero_printer_watcher.py --daemon
    Watcher->>Phone: Stefan TTS: "Extrusion initialized..."
    Zero->>Ryan: "Bed heating. Zero Watcher active in #3d-print."
    loop Every 20 Minutes (Active Print)
        Watcher->>Phone: Capture snapshot (webcam_control.py snapshot)
        Watcher->>Watcher: Gemini 2.5 Flash Vision Failure Eval
        alt Catastrophic Failure Detected (>=70% conf)
            Watcher->>Octo: POST /api/job (command: pause)
            Watcher->>Phone: Stefan TTS: "Catastrophic failure detected..."
            Watcher->>Ryan: Delivers failure photo alert to #3d-print
        end
    end
    Octo-->>Watcher: State: "Operational" (Print Complete)
    Watcher->>Phone: Stefan TTS: "Print complete. Part cooling on bed."
    Watcher->>Ryan: Delivers celebration photo to #3d-print
    loop Every 60s (Cooldown Phase)
        Watcher->>Octo: Query nozzle temperature
    end
    Note over Watcher,HA: tool0 drops <= 50.0°C
    Watcher->>Phone: Stefan TTS: "Cooldown threshold satisfied..."
    Watcher->>Phone: Stop camera server & sleep Pixel 8 Pro
    Watcher->>HA: Turn off switch.3d_printer_power
    Watcher->>Ryan: Delivers final teardown receipt to #3d-print
```
        Watcher->>Octo: Query nozzle temperature
    end
    Note over Watcher,HA: tool0 drops <= 50.0°C
    Watcher->>Cam: Stop camera server & sleep Pixel 8 Pro
    Watcher->>HA: Turn off switch.3d_printer_power
    Watcher->>Ryan: Delivers final teardown receipt to #3d-print
```

