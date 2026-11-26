#!/usr/bin/env python3
"""Render the remaining 6 Floor 3 curtain state patches on Modal L4 GPU.

Reuses the exact Modal blender_image, polyhaven_vol ('zero-polyhaven-cache'),
AgX color pipeline, lighting setup, scene hero build, and locked Orthographic
camera from /workspace/data/scripts/prototype_floor3_blender_cycles.py.

Renders at 1600x1067 with 96 Cycles samples + denoising, then computes
diff-masked feathered RGBA WebP patches against the unmodified base render
from the same lighting pass for:
  1. Rosie Pink Curtains closed (day + night)
  2. Isaac Curtains open (day + night)
  3. Isaac Curtains closed (day + night)

Does NOT touch the 12 already-rendered WebP patches in /workspace/data/renders/patches/.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image

PATCHES_DIR = Path("/workspace/data/renders/patches")
GLB_PATH = Path("/workspace/data/renders/floor3_option1.glb")
PROTOTYPE_SCRIPT_PATH = Path("/workspace/data/scripts/prototype_floor3_blender_cycles.py")

RENDER_WIDTH = 1600
RENDER_HEIGHT = 1067
CYCLES_SAMPLES = 96
MIN_WEBP_BYTES = 1024

EXISTING_12_PATCHES: tuple[str, ...] = (
    "floor1_front_door_closed_day.webp",
    "floor1_front_door_closed_night.webp",
    "floor1_front_door_open_day.webp",
    "floor1_front_door_open_night.webp",
    "floor1_garage_door_closed_day.webp",
    "floor1_garage_door_closed_night.webp",
    "floor3_kitchen_door_open_day.webp",
    "floor3_kitchen_door_open_night.webp",
    "floor3_kitchen_window_open_day.webp",
    "floor3_kitchen_window_open_night.webp",
    "floor3_rosie_door_open_day.webp",
    "floor3_rosie_door_open_night.webp",
)

NEW_6_PATCHES: tuple[str, ...] = (
    "floor3_rosie_curtains_closed_day.webp",
    "floor3_rosie_curtains_closed_night.webp",
    "floor3_isaac_curtains_open_day.webp",
    "floor3_isaac_curtains_open_night.webp",
    "floor3_isaac_curtains_closed_day.webp",
    "floor3_isaac_curtains_closed_night.webp",
)

ALL_18_PATCHES: tuple[str, ...] = EXISTING_12_PATCHES + NEW_6_PATCHES


def _ensure_modal_credentials() -> None:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return
    if Path.home().joinpath(".modal.toml").exists():
        return
    for env_path in (Path("/workspace/.env"), Path("/secrets/.env")):
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROFILE") and v and not os.environ.get(k):
                    os.environ[k] = v
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return
    secrets_path = Path("/secrets/env.json")
    if secrets_path.exists():
        data = json.loads(secrets_path.read_text(encoding="utf-8"))
        for k in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
            if data.get(k) and not os.environ.get(k):
                os.environ[k] = str(data[k])


_ensure_modal_credentials()
import modal

app = modal.App("zero-hearth-f3-curtain-patches")
polyhaven_vol = modal.Volume.from_name("zero-polyhaven-cache", create_if_missing=True)

blender_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "curl",
        "xz-utils",
        "libx11-6",
        "libxi6",
        "libxxf86vm1",
        "libxfixes3",
        "libxrender1",
        "libgl1",
        "libglu1-mesa",
        "libsm6",
        "libxkbcommon0",
        "libegl1",
        "libwayland-client0",
    )
    .run_commands(
        "curl -fsSL https://download.blender.org/release/Blender4.2/blender-4.2.3-linux-x64.tar.xz -o /tmp/blender.tar.xz",
        "tar -xf /tmp/blender.tar.xz -C /opt && mv /opt/blender-4.2.3-linux-x64 /opt/blender && rm -f /tmp/blender.tar.xz",
        "ln -sf /opt/blender/blender /usr/local/bin/blender",
        "blender --version",
    )
)

CURTAIN_RENDER_TAIL = r'''
# ==============================================================================
# CURTAIN STATE PATCHES (1600x1067, 96 samples + denoising)
# ==============================================================================
import json as _json
from bpy_extras.object_utils import world_to_camera_view as _w2c

scene.render.resolution_x = 1600
scene.render.resolution_y = 1067
scene.render.resolution_percentage = 100
scene.cycles.samples = 96
scene.cycles.use_adaptive_sampling = True
scene.cycles.adaptive_threshold = 0.01
scene.cycles.use_denoising = True
scene.cycles.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.compression = 15
scene.render.film_transparent = True
scene.render.image_settings.color_mode = 'RGBA'

# Verify exact locked Orthographic camera parameters
cam_target = Vector((2.1, 0.2, 4.0))
cam_pos = cam_target + Vector((33.28 * 1.5, -47.76 * 1.5, 35.84 * 1.5))
cam_data.type = 'ORTHO'
cam_data.ortho_scale = 60.0
cam_obj.location = cam_pos
cam_obj.rotation_euler = (cam_target - cam_pos).to_track_quat('-Z', 'Y').to_euler()
bpy.context.view_layer.update()


def _set_visible(objs, visible):
    for o in objs:
        o.hide_render = not visible
        o.hide_viewport = not visible


def _build_pleated_panel(
    name,
    axis,
    u0,
    u1,
    v_center,
    z0,
    z1,
    mat,
    n_pleats,
    amp_top,
    amp_bot,
    bunched=False,
    phase_offset=0.0,
):
    """Build a vertical pleated curtain fabric panel along axis='X' or axis='Y'."""
    nu = max(36, int(n_pleats * 14))
    nz = 20
    mesh = bpy.data.meshes.new(name + "_Mesh")
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)

    bm = bmesh.new()
    grid = []
    for iz in range(nz + 1):
        tz = iz / nz
        z = z0 + (z1 - z0) * tz
        # Top pinch header vs fuller bottom drape; subtle waist gather when bunched open
        amp_base = amp_bot * (1.0 - 0.30 * tz) + amp_top * (0.30 * tz)
        if tz > 0.92:
            amp_base *= 0.80
        waist = 1.0
        if bunched:
            waist = 1.0 - 0.16 * math.exp(-((tz - 0.38) / 0.18) ** 2)
        row = []
        for iu in range(nu + 1):
            tu = iu / nu
            if bunched:
                # Subtle hourglass gather toward outer wall edge
                u_mid = 0.5 * (u0 + u1)
                u = u_mid + (u1 - u0) * (tu - 0.5) * waist
            else:
                u = u0 + (u1 - u0) * tu
            phase = phase_offset + 2.0 * math.pi * n_pleats * tu
            pleat = math.sin(phase) + 0.14 * math.sin(2.0 * phase) + 0.08 * math.sin(0.5 * phase + 0.6)
            v = v_center + amp_base * pleat
            if axis == "X":
                co = (u, v, z)
            else:
                co = (v, u, z)
            row.append(bm.verts.new(co))
        grid.append(row)

    bm.verts.ensure_lookup_table()
    for iz in range(nz):
        for iu in range(nu):
            v00 = grid[iz][iu]
            v10 = grid[iz][iu + 1]
            v11 = grid[iz + 1][iu + 1]
            v01 = grid[iz + 1][iu]
            bm.faces.new((v00, v10, v11, v01))

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()

    for poly in mesh.polygons:
        poly.use_smooth = True
    mesh.materials.append(mat)

    sol = obj.modifiers.new("Solidify", 'SOLIDIFY')
    sol.thickness = 0.026
    sol.offset = 0.0
    sol.use_rim = True
    return obj


def _project_roi(bounds_min, bounds_max, pad_px=28):
    xs = []
    ys = []
    for x in (bounds_min[0], bounds_max[0]):
        for y in (bounds_min[1], bounds_max[1]):
            for z in (bounds_min[2], bounds_max[2]):
                ndc = _w2c(scene, cam_obj, Vector((x, y, z)))
                xs.append(ndc.x * scene.render.resolution_x)
                ys.append((1.0 - ndc.y) * scene.render.resolution_y)
    return [
        max(0, int(math.floor(min(xs) - pad_px))),
        max(0, int(math.floor(min(ys) - pad_px))),
        min(scene.render.resolution_x, int(math.ceil(max(xs) + pad_px))),
        min(scene.render.resolution_y, int(math.ceil(max(ys) + pad_px))),
    ]


# ------------------------------------------------------------------------------
# 1. Rosie Pink Curtains Closed (X=[13.70, 21.10], Y=10.62, Z=[0.05, 6.20])
# ------------------------------------------------------------------------------
rosie_orig_curtains = (
    find_fixture_meshes("rosie-north-curtain-west")
    + find_fixture_meshes("rosie-north-curtain-east")
)
mat_rosie_curtain = make_pbr_mat(
    "HeroPatch_RosieBlushCurtain",
    (0.687, 0.468, 0.503),
    roughness=0.88,
    sheen=0.76,
    bump_scale=44.0,
    bump_strength=0.055,
)
rosie_closed_objs = [
    _build_pleated_panel(
        "HeroPatch_RosieCurtainClosed_W",
        axis="X",
        u0=13.70,
        u1=17.405,
        v_center=10.62,
        z0=0.05,
        z1=6.18,
        mat=mat_rosie_curtain,
        n_pleats=12,
        amp_top=0.072,
        amp_bot=0.102,
        bunched=False,
        phase_offset=0.0,
    ),
    _build_pleated_panel(
        "HeroPatch_RosieCurtainClosed_E",
        axis="X",
        u0=17.395,
        u1=21.10,
        v_center=10.62,
        z0=0.05,
        z1=6.18,
        mat=mat_rosie_curtain,
        n_pleats=12,
        amp_top=0.072,
        amp_bot=0.102,
        bunched=False,
        phase_offset=math.pi,
    ),
]
# Subtle curtain rings along the existing rod (Z=6.195, Y=10.66)
for r_idx in range(25):
    rx_ring = 13.76 + r_idx * ((21.04 - 13.76) / 24.0)
    ring = add_cyl(
        f"HeroPatch_RosieCurtainRing_{r_idx}",
        (rx_ring, 10.66, 6.195),
        0.048,
        0.024,
        mat_black_iron,
        verts=14,
        rot_euler=(0.0, math.pi * 0.5, 0.0),
    )
    rosie_closed_objs.append(ring)

# ------------------------------------------------------------------------------
# 2. Isaac Curtains (X=23.15, Y=[-8.80, -4.20], Z=[0.05, 2.13])
# ------------------------------------------------------------------------------
mat_isaac_curtain = make_pbr_mat(
    "HeroPatch_IsaacOatmealCurtain",
    (0.74, 0.68, 0.58),
    roughness=0.88,
    sheen=0.74,
    bump_scale=46.0,
    bump_strength=0.065,
)
isaac_rod_objs = [
    add_cyl(
        "HeroPatch_IsaacCurtainRod",
        (23.15, -6.50, 2.13),
        0.028,
        4.84,
        mat_black_iron,
        verts=16,
        rot_euler=(math.pi * 0.5, 0.0, 0.0),
    ),
    add_cyl(
        "HeroPatch_IsaacCurtainFinial_S",
        (23.15, -8.94, 2.13),
        0.046,
        0.06,
        mat_black_iron,
        verts=16,
        rot_euler=(math.pi * 0.5, 0.0, 0.0),
    ),
    add_cyl(
        "HeroPatch_IsaacCurtainFinial_N",
        (23.15, -4.06, 2.13),
        0.046,
        0.06,
        mat_black_iron,
        verts=16,
        rot_euler=(math.pi * 0.5, 0.0, 0.0),
    ),
    add_box("HeroPatch_IsaacRodBracket_S", (23.21, -8.84, 2.13), (0.12, 0.04, 0.04), mat_black_iron, bevel_w=0.005, segs=1),
    add_box("HeroPatch_IsaacRodBracket_N", (23.21, -4.16, 2.13), (0.12, 0.04, 0.04), mat_black_iron, bevel_w=0.005, segs=1),
]

isaac_open_objs = [
    _build_pleated_panel(
        "HeroPatch_IsaacCurtainOpen_S",
        axis="Y",
        u0=-8.80,
        u1=-8.02,
        v_center=23.14,
        z0=0.05,
        z1=2.13,
        mat=mat_isaac_curtain,
        n_pleats=6,
        amp_top=0.095,
        amp_bot=0.118,
        bunched=True,
        phase_offset=0.0,
    ),
    _build_pleated_panel(
        "HeroPatch_IsaacCurtainOpen_N",
        axis="Y",
        u0=-4.98,
        u1=-4.20,
        v_center=23.14,
        z0=0.05,
        z1=2.13,
        mat=mat_isaac_curtain,
        n_pleats=6,
        amp_top=0.095,
        amp_bot=0.118,
        bunched=True,
        phase_offset=math.pi,
    ),
]

isaac_closed_objs = [
    _build_pleated_panel(
        "HeroPatch_IsaacCurtainClosed_S",
        axis="Y",
        u0=-8.80,
        u1=-6.495,
        v_center=23.14,
        z0=0.05,
        z1=2.13,
        mat=mat_isaac_curtain,
        n_pleats=9,
        amp_top=0.085,
        amp_bot=0.108,
        bunched=False,
        phase_offset=0.0,
    ),
    _build_pleated_panel(
        "HeroPatch_IsaacCurtainClosed_N",
        axis="Y",
        u0=-6.505,
        u1=-4.20,
        v_center=23.14,
        z0=0.05,
        z1=2.13,
        mat=mat_isaac_curtain,
        n_pleats=9,
        amp_top=0.085,
        amp_bot=0.108,
        bunched=False,
        phase_offset=math.pi,
    ),
]

rois = {
    "rosie_curtains_closed": _project_roi((13.45, 10.35, 0.0), (21.35, 11.20, 6.32), pad_px=28),
    "isaac_curtains_open": _project_roi((22.75, -9.10, 0.0), (23.45, -3.95, 2.25), pad_px=24),
    "isaac_curtains_closed": _project_roi((22.75, -9.10, 0.0), (23.45, -3.95, 2.25), pad_px=24),
}
with open(os.path.join(out_dir, "__rois__.json"), "w", encoding="utf-8") as rf:
    _json.dump(rois, rf)
print(f"[blender] ROIs computed: {rois}", flush=True)


def _apply_variant(state_name):
    if state_name == "base":
        _set_visible(rosie_orig_curtains, True)
        _set_visible(rosie_closed_objs, False)
        _set_visible(isaac_rod_objs, False)
        _set_visible(isaac_open_objs, False)
        _set_visible(isaac_closed_objs, False)
    elif state_name == "rosie_curtains_closed":
        _set_visible(rosie_orig_curtains, False)
        _set_visible(rosie_closed_objs, True)
        _set_visible(isaac_rod_objs, False)
        _set_visible(isaac_open_objs, False)
        _set_visible(isaac_closed_objs, False)
    elif state_name == "isaac_curtains_open":
        _set_visible(rosie_orig_curtains, True)
        _set_visible(rosie_closed_objs, False)
        _set_visible(isaac_rod_objs, True)
        _set_visible(isaac_open_objs, True)
        _set_visible(isaac_closed_objs, False)
    elif state_name == "isaac_curtains_closed":
        _set_visible(rosie_orig_curtains, True)
        _set_visible(rosie_closed_objs, False)
        _set_visible(isaac_rod_objs, True)
        _set_visible(isaac_open_objs, False)
        _set_visible(isaac_closed_objs, True)
    bpy.context.view_layer.update()


# Pass A: Daylight renders (exposure -0.22, day_portals enabled)
for state_name in ("base", "rosie_curtains_closed", "isaac_curtains_open", "isaac_curtains_closed"):
    _apply_variant(state_name)
    out_png = os.path.join(out_dir, f"{state_name}_day.png")
    scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    print(f"[blender] Rendered {os.path.basename(out_png)}", flush=True)

# Pass B: Evening Base lighting (matches floor3_cycles_evening_base.png)
for lp in day_portals:
    lp.hide_render = True
    lp.data.energy = 0.0

sun_day.data.energy = 0.36
sun_day.data.color = (0.56, 0.72, 1.0)
sun_day.data.angle = math.radians(6.0)
sky_fill.data.energy = 580.0
sky_fill.data.color = (0.48, 0.64, 0.95)
w_cam_bg.inputs['Color'].default_value = (0.020, 0.028, 0.046, 1.0)
w_sky_bg.inputs['Color'].default_value = (0.28, 0.42, 0.72, 1.0)
w_sky_bg.inputs['Strength'].default_value = 0.24
scene.view_settings.exposure = 0.25

for state_name in ("base", "rosie_curtains_closed", "isaac_curtains_open", "isaac_curtains_closed"):
    _apply_variant(state_name)
    out_png = os.path.join(out_dir, f"{state_name}_night.png")
    scene.render.filepath = out_png
    bpy.ops.render.render(write_still=True)
    print(f"[blender] Rendered {os.path.basename(out_png)}", flush=True)
'''


def build_blender_script(prototype_path: Path = PROTOTYPE_SCRIPT_PATH) -> str:
    """Extract the exact Floor 3 Cycles scene setup from prototype_floor3_blender_cycles.py."""
    src = prototype_path.read_text(encoding="utf-8")
    start_marker = "BLENDER_SCRIPT = r'''\n"
    end_marker = 'scene.render.filepath = os.path.join(out_dir, "floor3_cycles_day.png")'
    start_idx = src.find(start_marker)
    if start_idx == -1:
        raise RuntimeError(f"Could not locate BLENDER_SCRIPT start in {prototype_path}")
    body_start = start_idx + len(start_marker)
    end_idx = src.find(end_marker, body_start)
    if end_idx == -1:
        raise RuntimeError(f"Could not locate day render marker in {prototype_path}")
    prefix = src[body_start:end_idx]
    return prefix + "\n" + CURTAIN_RENDER_TAIL


@app.function(
    image=blender_image,
    volumes={"/polyhaven": polyhaven_vol},
    gpu=["L4", "A10G", "L40S", "T4"],
    cpu=4.0,
    memory=16384,
    timeout=1200,
)
def _render_f3_curtain_variants_remote(glb_bytes: bytes, blender_script: str) -> dict[str, bytes]:
    import tempfile as _tmp
    import time as _time

    with _tmp.TemporaryDirectory(prefix="hearth-f3-curtains-") as td:
        glb_path = os.path.join(td, "floor3_option1.glb")
        script_path = os.path.join(td, "render_curtain_patches.py")
        out_dir = os.path.join(td, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(glb_path, "wb") as f:
            f.write(glb_bytes)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(blender_script)

        print("[modal] Launching Blender 4.2 Cycles on L4 GPU...", flush=True)
        proc = subprocess.Popen(
            [
                "blender",
                "--background",
                "--python-exit-code",
                "1",
                "--python",
                script_path,
                "--",
                glb_path,
                "/polyhaven",
                out_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        tail_lines: list[str] = []
        last_log = _time.monotonic()
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.rstrip("\n")
            tail_lines.append(s)
            if len(tail_lines) > 120:
                tail_lines.pop(0)
            now = _time.monotonic()
            if (
                s.startswith("[blender]")
                or "Saved:" in s
                or "Error" in s
                or "Traceback" in s
                or (now - last_log) >= 15.0
            ):
                print(s, flush=True)
                last_log = now
        rc = proc.wait(timeout=1100)
        if rc != 0:
            print("\n".join(tail_lines[-80:]), file=sys.stderr, flush=True)
            raise RuntimeError(f"Blender Cycles failed (rc={rc})")

        results: dict[str, bytes] = {}
        for fpath in sorted(Path(out_dir).iterdir()):
            if fpath.suffix in (".png", ".json"):
                results[fpath.name] = fpath.read_bytes()
        print(f"[modal] Returning {len(results)} rendered artifacts.", flush=True)
        return results


def compute_diff_feathered_patch(
    variant_rgba: "np.ndarray",
    base_rgba: "np.ndarray",
    roi: tuple[int, int, int, int],
    *,
    diff_threshold: int,
    min_blob_px: int = 16,
    dilate_iters: int = 2,
    feather_sigma: float = 1.4,
) -> "Image.Image":
    """Create a transparent RGBA patch from variant vs base using ROI-clamped diff & feathered alpha."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    x0, y0, x1, y1 = roi
    rgb_diff = np.max(
        np.abs(variant_rgba[:, :, :3].astype(np.int16) - base_rgba[:, :, :3].astype(np.int16)),
        axis=2,
    )
    alpha_diff = np.abs(variant_rgba[:, :, 3].astype(np.int16) - base_rgba[:, :, 3].astype(np.int16))
    diff = np.maximum(rgb_diff, alpha_diff)

    roi_mask = np.zeros(diff.shape, dtype=bool)
    roi_mask[y0:y1, x0:x1] = True
    raw_mask = (diff >= diff_threshold) & roi_mask

    # Filter out isolated denoiser speckles smaller than min_blob_px
    labeled, n_comp = ndimage.label(raw_mask)
    if n_comp > 0:
        counts = np.bincount(labeled.ravel())
        keep = counts >= min_blob_px
        keep[0] = False
        clean_mask = keep[labeled]
        if not np.any(clean_mask):
            keep = counts >= 4
            keep[0] = False
            clean_mask = keep[labeled]
    else:
        clean_mask = raw_mask

    if dilate_iters > 0:
        clean_mask = ndimage.binary_dilation(clean_mask, iterations=dilate_iters)
    clean_mask = ndimage.binary_fill_holes(clean_mask)
    clean_mask &= roi_mask

    feathered = ndimage.gaussian_filter(clean_mask.astype(np.float32), sigma=feather_sigma)
    alpha_u8 = np.clip(np.round(feathered * 255.0), 0, 255).astype(np.uint8)
    alpha_u8 = np.minimum(alpha_u8, variant_rgba[:, :, 3])

    out = variant_rgba.copy()
    out[:, :, 3] = alpha_u8
    out[alpha_u8 == 0, :3] = 0
    return Image.fromarray(out, mode="RGBA")


def _load_rgba_from_bytes(png_bytes: bytes) -> "np.ndarray":
    from io import BytesIO
    import numpy as np
    from PIL import Image

    with Image.open(BytesIO(png_bytes)) as im:
        return np.array(im.convert("RGBA"))


def verify_all_patches(patches_dir: Path = PATCHES_DIR) -> tuple[bool, list[str]]:
    """Verify all 18 expected WebPs exist, are >1 KB, and are valid RGBA 1600x1067 WebPs."""
    from PIL import Image

    errors: list[str] = []
    for fname in ALL_18_PATCHES:
        fpath = patches_dir / fname
        if not fpath.is_file():
            errors.append(f"MISSING: {fpath}")
            continue
        size_bytes = fpath.stat().st_size
        if size_bytes <= MIN_WEBP_BYTES:
            errors.append(f"TOO SMALL ({size_bytes} bytes <= {MIN_WEBP_BYTES}): {fpath}")
            continue
        try:
            with Image.open(fpath) as im:
                im.load()
                if im.format != "WEBP":
                    errors.append(f"INVALID FORMAT ({im.format} != WEBP): {fpath}")
                elif im.mode != "RGBA":
                    errors.append(f"INVALID MODE ({im.mode} != RGBA): {fpath}")
                elif im.size != (RENDER_WIDTH, RENDER_HEIGHT):
                    errors.append(f"INVALID SIZE ({im.size} != {(RENDER_WIDTH, RENDER_HEIGHT)}): {fpath}")
                elif im.getchannel("A").getbbox() is None:
                    errors.append(f"EMPTY ALPHA CHANNEL: {fpath}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"CORRUPT WEBP ({exc}): {fpath}")
    return (len(errors) == 0), errors


def render_patches() -> int:
    import numpy as np

    if not GLB_PATH.is_file():
        print(f"ERROR: Missing GLB input: {GLB_PATH}", file=sys.stderr, flush=True)
        return 1
    if not PROTOTYPE_SCRIPT_PATH.is_file():
        print(f"ERROR: Missing prototype script: {PROTOTYPE_SCRIPT_PATH}", file=sys.stderr, flush=True)
        return 1

    PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] Loading {GLB_PATH} ({GLB_PATH.stat().st_size:,} bytes)...", flush=True)
    glb_bytes = GLB_PATH.read_bytes()
    blender_script = build_blender_script(PROTOTYPE_SCRIPT_PATH)

    print("[2/3] Rendering 8 Cycles passes (1600x1067, 96 samples + denoiser) on Modal L4...", flush=True)
    with modal.enable_output():
        with app.run():
            outputs = _render_f3_curtain_variants_remote.remote(glb_bytes, blender_script)

    rois: dict[str, list[int]] = json.loads(outputs["__rois__.json"].decode("utf-8"))
    print(f"[2/3] Remote render complete. ROIs: {rois}", flush=True)

    base_day = _load_rgba_from_bytes(outputs["base_day.png"])
    base_night = _load_rgba_from_bytes(outputs["base_night.png"])

    jobs: tuple[tuple[str, str, str, np.ndarray, int], ...] = (
        ("rosie_curtains_closed_day.png", "rosie_curtains_closed", "floor3_rosie_curtains_closed_day.webp", base_day, 12),
        ("rosie_curtains_closed_night.png", "rosie_curtains_closed", "floor3_rosie_curtains_closed_night.webp", base_night, 8),
        ("isaac_curtains_open_day.png", "isaac_curtains_open", "floor3_isaac_curtains_open_day.webp", base_day, 12),
        ("isaac_curtains_open_night.png", "isaac_curtains_open", "floor3_isaac_curtains_open_night.webp", base_night, 8),
        ("isaac_curtains_closed_day.png", "isaac_curtains_closed", "floor3_isaac_curtains_closed_day.webp", base_day, 12),
        ("isaac_curtains_closed_night.png", "isaac_curtains_closed", "floor3_isaac_curtains_closed_night.webp", base_night, 8),
    )

    print("[3/3] Generating 6 diff-masked feathered RGBA WebP patches...", flush=True)
    for png_name, roi_key, webp_name, base_arr, thresh in jobs:
        var_arr = _load_rgba_from_bytes(outputs[png_name])
        x0, y0, x1, y1 = rois[roi_key]
        patch_im = compute_diff_feathered_patch(
            var_arr,
            base_arr,
            (x0, y0, x1, y1),
            diff_threshold=thresh,
        )
        out_path = PATCHES_DIR / webp_name
        patch_im.save(out_path, format="WEBP", quality=90, method=6)
        alpha = np.array(patch_im)[:, :, 3]
        nz = int(np.sum(alpha > 0))
        print(
            f"  Saved {out_path} ({out_path.stat().st_size:,} bytes, nonzero_alpha_px={nz})",
            flush=True,
        )

    ok, errors = verify_all_patches(PATCHES_DIR)
    if not ok:
        for err in errors:
            print(f"VERIFY ERROR: {err}", file=sys.stderr, flush=True)
        return 1

    print(f"SUCCESS: All {len(ALL_18_PATCHES)} RGBA 1600x1067 WebP patches verified in {PATCHES_DIR}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render and verify the remaining 6 Floor 3 curtain WebP patches on Modal L4."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify all 18 expected WebPs in /workspace/data/renders/patches/ without rendering.",
    )
    args: Any = parser.parse_args(argv)

    if args.verify_only:
        ok, errors = verify_all_patches(PATCHES_DIR)
        if not ok:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr, flush=True)
            return 1
        for fname in ALL_18_PATCHES:
            fpath = PATCHES_DIR / fname
            print(f"OK: {fname} ({fpath.stat().st_size:,} bytes, RGBA {RENDER_WIDTH}x{RENDER_HEIGHT} WEBP)", flush=True)
        print(f"VERIFIED: All {len(ALL_18_PATCHES)} patches present and valid.", flush=True)
        return 0

    return render_patches()


if __name__ == "__main__":
    raise SystemExit(main())
