---
name: print3d-image-gen
description: >-
  Use this skill whenever asked to generate or refine images intended for 3D printing, miniature fabrication, or 2D-to-3D mesh reconstruction (Meshy.ai, Trellis, Tripo3D). Enforces physical FDM manufacturability constraints (pure white background, diffuse studio lighting, grounded stance, zero thin wisps/overhangs) and delivers verified generation outputs to Discord.
---

# 🗿 Print3D-Image-Gen Skill (FDM-Optimized 2D Concept Generation)

The **Print3D-Image-Gen** skill governs the generation and conditioning of 2D images specifically engineered to convert cleanly into watertight 3D models (via Meshy.ai, Trellis, or Tripo3D) and physically fabricate on FDM printers like the Creality Ender-3 V3.

---

## 🎯 When to Activate This Skill

Activate this skill whenever Ryan or a user requests:
- "Generate an image for 3D printing" / "Design a miniature for my 3D printer"
- "Make a 2D concept for Meshy / 3D modeler"
- "Create a figurine / desk toy / sculpture to 3D print"
- "Generate a printable badge / medallion / bas-relief"

---

## 🔬 The FDM Manufacturability Invariants

AI 2D-to-3D generators infer depth geometry from pixel gradients. Standard AI images produce broken meshes because they contain dramatic cast shadows, perspective warping, and spindly elements that violate FDM printing physics. Every image generated under this skill must adhere to these six physical constraints:

1. **Pure White Background (`#FFFFFF`):**
   - The subject must be 100% isolated on a solid white or transparent background.
   - Zero floor shadows, gradient backdrops, atmospheric fog, or ambient scenery. Background noise converts directly into jagged floating geometry.

2. **Flat, Diffuse Studio Lighting:**
   - Soft, omnidirectional studio lighting only.
   - Strict ban on high-contrast directional shadows, rim lighting, or specular hot spots. Depth reconstruction engines interpret dark shadows as deep physical gouges or missing limbs.

3. **Grounded Stance & Stable Footprint:**
   - Both feet, wheels, or base supports must be firmly planted on an imaginary flat bottom plane or solid continuous pedestal.
   - Avoid floating poses, jumping action shots, or single-point balances that require massive support scaffolds or fail bed adhesion.

4. **Compact Silhouette & The 0.4mm Feature Rule:**
   - Keep limbs, tails, cloaks, and handheld accessories tucked close to the core torso.
   - Ban spindly projections: no floating antenna wires, wispy hair strands, paper-thin blades, or loose straps. A 0.4mm FDM nozzle cannot resolve features under 0.8mm (2 perimeters), and thin appendages inevitably snap when peeling tree supports.

5. **Orthographic or Telephoto Framing:**
   - Camera perspective must be neutral, eye-level, and flat (equivalent to a 70–100mm lens).
   - Never use wide-angle 24mm selfie angles or dramatic fish-eye views that distort geometric proportions.

6. **The "Texture vs Geometry" Invariant (Bold Physical Relief):**
   - 2D-to-3D engines (Trellis, Meshy) infer geometry from lighting gradients, but bake micro-details (text, fine panel lines, painted seams, logos) into flat 2D albedo/normal maps rather than actual polygon vertices.
   - Slicers and FDM printers strip textures and only slice physical vertices.
   - Prompts must explicitly instruct the generator to produce **deeply sculpted physical relief**, **bold 3D embossed lettering**, and **heavy recessed grooves** ($\ge 0.8\text{mm}$ in physical scale) rather than painted surface markings.
   - Ban flat micro-decals and painted lettering on pedestals. Explicitly prompt for "deep chiseled engravings" or "chunky raised embossed lettering".

---

## 📝 Prompt Formulation Architecture

Transform raw user concepts into structured fabrication prompts using this template:

```text
Isolated [scale/style] of [subject], neutral standing [A-pose/grounded pose], heavy physical surface relief, deeply sculpted seams and armor plates, solid compact silhouette with reinforced joints, diffuse omnidirectional studio lighting, pure solid white background, zero cast shadows, no floor textures, watertight solid organic form, clean edges, concept art for physical FDM miniature fabrication, 8k resolution
```

### Reference Prompt Catalog:

- **Miniature / Figurine:**
  > `"Isolated collectible 3D miniature of a cyberpunk robot warrior wearing a high-collar trench coat, neutral standing grounded pose firmly planted on circular pedestal, bold physical relief with deeply sculpted coat lapels, chiseled visor indentation, thick articulated fingers resting at side, solid compact silhouette, diffuse omnidirectional studio lighting, pure solid white background, zero cast shadows, no floor textures, watertight solid form, physical tabletop miniature fabrication concept, sharp focus, 8k"`

- **Functional Desk Accessory / Bracket:**
  > `"Isolated mechanical desk cable organizer shaped like a minimalist geometric dragon, heavy flat bottom base, solid thick walls, bevelled edges, diffuse even lighting, pure solid white background, no shadows, industrial design for 3D printing"`

- **Bas-Relief Medallion / Coin (HueForge / Emboss):**
  > `"Isolated circular brass challenge coin featuring an embossed wolf head profile in high relief with deep 1mm physical step edges, clean circular rim, solid raised surfaces, front view, flat diffuse lighting, pure white background, no drop shadows, 3D relief medallion"`

---

## 🚀 Execution Workflow

### Step 1: Visual Prompt Formulation
Analyze user intent, select appropriate subject archetype (figurine, functional part, relief), and inject the mandatory FDM invariants and bold physical relief constraints into the prompt.

### Step 2: Generation Execution
Execute generation using the dedicated image engine:

#### Primary Path (`tools/gemini_image.py`):
```bash
python3 /workspace/tools/gemini_image.py \
  --prompt "<structured prompt>" \
  --aspect-ratio "1:1" \
  --name "<snake_case_name>" \
  --channel zero-chat \
  --caption "FDM-optimized concept for [subject]. Ready for 3D mesh reconstruction."
```

#### Fallback Path (`generate_image` runtime tool):
```json
{
  "AspectRatio": "1:1",
  "ImageName": "shiba_miniature",
  "Prompt": "<structured prompt>",
  "toolAction": "Generating 3D-printable concept image",
  "toolSummary": "Generate 3D concept"
}
```

### Step 3: Forensic Verification on Disk
Confirm the image exists, has valid dimensions, and non-zero byte size before announcing to chat:
```bash
python3 /workspace/tools/deliver_image.py --check /root/.gemini/antigravity-cli/brain/<conv_id>/<ImageName>_<timestamp>.jpg
```

### Step 4: Discord Presentation & Handoff
Present the generated image with:
1. Short FDM suitability assessment (noting bed contact stability, feature relief, and support clearance).
2. Direct confirmation checkpoint before triggering 3D mesh reconstruction.

### Step 5: Handoff to 3D Mesh Reconstruction & Modal Cloud Pipeline
Once the 2D concept is approved:
1. Generate initial 3D mesh via Trellis / Meshy.
2. Ingest mesh components into `/workspace/tools/modal_v3_builder.py` to execute cloud voxelization, boolean union, and morphological solid remeshing.
3. Verify feature relief and FDM overhangs via `/workspace/tools/modal_render_3d.py` (360° turntable animation).
4. If scaling down (e.g. 50% scale), ensure the original 2D concept features are sculpted with $\ge 1.6\text{mm}$ thickness to guarantee $\ge 0.8\text{mm}$ physical resolution on a 0.4mm nozzle.
5. Handoff watertight STL to `print3d-slicer` for OrcaSlicer preparation.
