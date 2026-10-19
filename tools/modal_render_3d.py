#!/usr/bin/env python3
"""Modal Serverless 3D Turntable Renderer (Powered by Headless Blender 4.2 Cycles).

Renders path-traced 360° studio turntables with 3-point area lighting, ambient occlusion,
and contact ground shadows on Modal cloud compute so Host 2 takes 0% CPU load.
"""

import argparse
import io
import math
import os
import sys
import tempfile
import time
from pathlib import Path
import modal

app = modal.App("zero-3d-render-service")

render_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "libx11-6",
        "libxxf86vm1",
        "libxfixes3",
        "libxi6",
        "libxkbcommon0",
        "libsm6",
        "libice6",
        "libgl1",
        "libegl1",
        "libgomp1",
        "libxrender1",
    )
    .pip_install(
        "bpy==4.2.0",
        "trimesh",
        "numpy",
        "pillow",
        "scipy",
        "fast-simplification",
    )
)


def _setup_blender_studio_and_render(
    stl_bytes: bytes,
    target_height_mm: float = 0.0,
    n_frames: int = 24,
    elev: float = 24.0,
    samples: int = 16,
    res_x: int = 520,
    res_y: int = 580,
) -> tuple[bytes, bytes]:
    """Import STL into headless Blender 4.2, frame 1:1:1 camera + 3-point studio lighting, and render turntable."""
    import bpy
    import mathutils
    import trimesh
    from PIL import Image

    # 1. Normalize, scale, and ground mesh using trimesh first to guarantee exact bounding box & centering
    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
    if target_height_mm and target_height_mm > 0 and mesh.extents[2] > 0:
        scale = target_height_mm / mesh.extents[2]
        mesh.apply_scale(scale)

    bounds = mesh.bounds
    mesh.apply_translation([
        -((bounds[0][0] + bounds[1][0]) / 2.0),
        -((bounds[0][1] + bounds[1][1]) / 2.0),
        -bounds[0][2],
    ])

    ext_x, ext_y, ext_z = [float(v) for v in mesh.extents]
    max_dim = max(ext_x, ext_y, ext_z, 1.0)
    center_z = ext_z * 0.48

    with tempfile.TemporaryDirectory(prefix="zero_bpy_") as tmpdir:
        norm_stl = os.path.join(tmpdir, "normalized.stl")
        mesh.export(norm_stl, file_type="stl")

        # 2. Reset Blender to clean empty factory scene
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.stl_import(filepath=norm_stl)
        obj = bpy.context.selected_objects[0]
        bpy.context.view_layer.objects.active = obj

        # Smooth shading with weighted normals for clean mechanical & organic highlights
        bpy.ops.object.shade_smooth()

        # 3. Configure Cycles Engine (8-vCPU path tracing)
        scene = bpy.context.scene
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.05
        scene.cycles.use_denoising = False
        scene.cycles.max_bounces = 3
        scene.cycles.diffuse_bounces = 2
        scene.cycles.glossy_bounces = 2
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.image_settings.file_format = "PNG"

        # 4. Studio World Background (#090d16 dark slate)
        world = bpy.data.worlds.new("StudioWorld")
        scene.world = world
        world.use_nodes = True
        bg_node = world.node_tree.nodes.get("Background")
        if bg_node:
            bg_node.inputs[0].default_value = (0.008, 0.013, 0.024, 1.0)
            bg_node.inputs[1].default_value = 1.0

        # 5. Studio Matte Sculpt Material (Subtle subsurface & specular highlight)
        mat = bpy.data.materials.new(name="ZeroStudioClay")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.83, 0.87, 0.93, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.38
            if "Specular IOR Level" in bsdf.inputs:
                bsdf.inputs["Specular IOR Level"].default_value = 0.45
        obj.data.materials.append(mat)

        # 6. Matte Studio Floor (catches soft contact shadows & bounce light)
        bpy.ops.mesh.primitive_plane_add(size=max_dim * 12.0, location=(0.0, 0.0, 0.0))
        floor_obj = bpy.context.active_object
        floor_mat = bpy.data.materials.new(name="StudioFloor")
        floor_mat.use_nodes = True
        f_bsdf = floor_mat.node_tree.nodes.get("Principled BSDF")
        if f_bsdf:
            f_bsdf.inputs["Base Color"].default_value = (0.012, 0.018, 0.032, 1.0)
            f_bsdf.inputs["Roughness"].default_value = 0.72
        floor_obj.data.materials.append(floor_mat)

        # 7. 3-Point Studio Area Lighting (scaled dynamically to model dimensions)
        target_pt = mathutils.Vector((0.0, 0.0, center_z))
        dist_scale = max_dim * 1.8

        def add_area_light(name: str, rel_pos: tuple[float, float, float], intensity_mult: float, size_mult: float, color: tuple[float, float, float]):
            ldata = bpy.data.lights.new(name=name, type="AREA")
            # Power scales with distance squared in Blender physical lighting units
            ldata.energy = intensity_mult * (dist_scale ** 2)
            ldata.size = max_dim * size_mult
            ldata.color = color
            lobj = bpy.data.objects.new(name=name, object_data=ldata)
            scene.collection.objects.link(lobj)
            lobj.location = (
                rel_pos[0] * dist_scale,
                rel_pos[1] * dist_scale,
                center_z + rel_pos[2] * dist_scale,
            )
            direction = target_pt - lobj.location
            lobj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

        # Key light (warm front-right), Fill light (cool front-left), Rim light (crisp back-top)
        add_area_light("KeyLight", (0.85, -0.85, 0.95), 28.0, 1.1, (1.0, 0.98, 0.95))
        add_area_light("FillLight", (-0.95, -0.70, 0.45), 11.0, 1.4, (0.82, 0.90, 1.0))
        add_area_light("RimLight", (0.15, 1.05, 0.85), 24.0, 0.9, (0.90, 0.96, 1.0))

        # 8. Camera Rig & Turntable Pivot
        pivot = bpy.data.objects.new("TurntablePivot", None)
        scene.collection.objects.link(pivot)
        pivot.location = (0.0, 0.0, center_z)

        cam_data = bpy.data.cameras.new("TurntableCam")
        cam_data.lens = 55.0
        cam_data.clip_start = max_dim * 0.01
        cam_data.clip_end = max_dim * 50.0
        cam_obj = bpy.data.objects.new("TurntableCam", cam_data)
        scene.collection.objects.link(cam_obj)
        scene.camera = cam_obj
        cam_obj.parent = pivot

        # Compute framing radius so the model fills ~75% of the frame regardless of aspect ratio
        xy_radius = math.hypot(ext_x, ext_y) * 0.5
        bounding_radius = max(xy_radius * 1.15, ext_z * 0.58)
        cam_dist = bounding_radius * 2.55
        elev_rad = math.radians(elev)
        cam_y = -cam_dist * math.cos(elev_rad)
        cam_z = cam_dist * math.sin(elev_rad)
        cam_obj.location = (0.0, cam_y, cam_z)
        cam_dir = mathutils.Vector((0.0, 0.0, 0.0)) - cam_obj.location
        cam_obj.rotation_euler = cam_dir.to_track_quat("-Z", "Y").to_euler()

        # 9. Render N frames around 360°
        frames: list[Image.Image] = []
        for i in range(n_frames):
            angle_deg = i * (360.0 / n_frames)
            pivot.rotation_euler[2] = math.radians(angle_deg)
            frame_path = os.path.join(tmpdir, f"frame_{i:03d}.png")
            scene.render.filepath = frame_path
            bpy.ops.render.render(write_still=True)
            frames.append(Image.open(frame_path).convert("RGB"))

        gif_buf = io.BytesIO()
        frames[0].save(
            gif_buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=90,
            loop=0,
            optimize=True,
        )

        hero_buf = io.BytesIO()
        frames[0].save(hero_buf, format="PNG")

        return gif_buf.getvalue(), hero_buf.getvalue()


@app.function(image=render_image, cpu=8, memory=16384, timeout=240)
def render_cloud_turntable(
    stl_bytes: bytes,
    target_height_mm: float = 0.0,
    n_frames: int = 24,
    elev: float = 24.0,
) -> bytes:
    print(f"Cloud Worker (Blender 4.2 Cycles): Rendering {n_frames}-frame turntable ({len(stl_bytes):,} bytes)...")
    gif_bytes, _ = _setup_blender_studio_and_render(
        stl_bytes=stl_bytes,
        target_height_mm=target_height_mm,
        n_frames=n_frames,
        elev=elev,
    )
    print(f"Cloud Worker: Finished Cycles turntable GIF ({len(gif_bytes):,} bytes)!")
    return gif_bytes


def main():
    parser = argparse.ArgumentParser(description="Render 3D STL to 360° Cycles Turntable via Modal Cloud Compute (Blender 4.2)")
    parser.add_argument("stl_path", help="Path to input .stl file")
    parser.add_argument("--output", "-o", help="Path to save output .gif", default=None)
    parser.add_argument("--channel", "-c", help="Discord channel ID to deliver preview to", default=None)
    parser.add_argument("--caption", help="Caption for Discord delivery", default=None)
    parser.add_argument("--preserve-scale", action="store_true", help="Preserve native STL dimensions rather than scaling to 120mm")
    parser.add_argument("--target-height", type=float, default=120.0, help="Target height in mm if scaling")
    parser.add_argument("--elev", type=float, default=24.0, help="Camera elevation angle (degrees)")
    args = parser.parse_args()

    stl_file = Path(args.stl_path)
    if not stl_file.exists():
        print(f"Error: STL file not found: {stl_file}")
        sys.exit(1)

    out_file = Path(args.output) if args.output else stl_file.with_name(f"{stl_file.stem}_cloud_turntable.gif")

    print(f"Dispatching {stl_file.name} ({stl_file.stat().st_size:,} bytes) to Modal Blender 4.2 Cycles cloud worker...")
    t0 = time.time()
    stl_bytes = stl_file.read_bytes()

    target_h = 0.0 if args.preserve_scale else args.target_height
    with app.run():
        gif_bytes = render_cloud_turntable.remote(
            stl_bytes,
            target_height_mm=target_h,
            elev=args.elev,
        )

    out_file.write_bytes(gif_bytes)
    print(f"Received rendered Cycles turntable from Modal ({len(gif_bytes):,} bytes) in {time.time() - t0:.1f}s")
    print(f"Saved to: {out_file}")

    if args.channel:
        sys.path.insert(0, "/workspace/tools")
        from deliver_image import deliver_image
        caption = args.caption or f"🎬 **360° Studio Turntable (Modal Blender Cycles Render)**: {stl_file.name}"
        res = deliver_image(str(out_file), channel_input=args.channel, caption=caption)
        print("Discord delivery:", res)


if __name__ == "__main__":
    main()
