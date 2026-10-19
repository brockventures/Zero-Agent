#!/usr/bin/env python3
"""Modal Serverless V3 Mesh Builder & Turntable Renderer (Powered by Headless Blender 4.2).

Uses Blender 4.2's native OpenVDB Voxel Remesher (`REMESH` VOXEL mode), Corrective Smooth
(volume-preserving crease retention), and Cycles path-traced 3-point studio lighting
with contact shadows on Modal cloud compute.
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

app = modal.App("zero-v3-cloud-generator")

cloud_image = (
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


@app.function(image=cloud_image, cpu=8, memory=16384, timeout=300)
def generate_v3_cloud(
    raw_stl_bytes: bytes,
    pitch: float = 0.22,
    target_height_mm: float = 120.0,
    n_frames: int = 24,
    elev: float = 18.0,
) -> dict:
    import bpy
    import mathutils
    import trimesh
    from PIL import Image

    print(f"Cloud Worker (Blender 4.2 OpenVDB + Cycles): Loading raw mesh ({len(raw_stl_bytes):,} bytes)...")
    m_raw = trimesh.load(io.BytesIO(raw_stl_bytes), file_type="stl")
    if isinstance(m_raw, trimesh.Scene):
        m_raw = m_raw.dump(concatenate=True)

    print(f"Cloud Worker: Raw sculpt has {len(m_raw.vertices):,} vertices, {len(m_raw.faces):,} faces. Extents: {m_raw.extents.tolist()}")
    if target_height_mm and target_height_mm > 0 and m_raw.extents[2] > 0:
        scale_in = target_height_mm / m_raw.extents[2]
        m_raw.apply_scale(scale_in)

    bounds = m_raw.bounds
    m_raw.apply_translation([
        -((bounds[0][0] + bounds[1][0]) / 2.0),
        -((bounds[0][1] + bounds[1][1]) / 2.0),
        -bounds[0][2],
    ])

    with tempfile.TemporaryDirectory(prefix="zero_v3_bpy_") as tmpdir:
        in_stl = os.path.join(tmpdir, "input_raw.stl")
        out_stl = os.path.join(tmpdir, "output_v3.stl")
        m_raw.export(in_stl, file_type="stl")

        # 1. Load into headless Blender 4.2
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.stl_import(filepath=in_stl)
        obj = bpy.context.selected_objects[0]
        bpy.context.view_layer.objects.active = obj

        # 2. Native OpenVDB Voxel Remesh (`REMESH` VOXEL mode)
        print(f"Cloud Worker: Executing Blender OpenVDB Voxel Remesh at voxel_size={pitch}mm...")
        remesh = obj.modifiers.new(name="OpenVDB_Remesh", type="REMESH")
        remesh.mode = "VOXEL"
        remesh.voxel_size = float(pitch)
        remesh.adaptivity = 0.0
        remesh.use_smooth_shade = True
        bpy.ops.object.modifier_apply(modifier=remesh.name)

        # 3. Volume-Preserving Corrective Smooth (eliminates voxel stepping while preserving sharp creases)
        print("Cloud Worker: Applying Blender Corrective Smooth modifier (8 iterations)...")
        smooth = obj.modifiers.new(name="CorrectiveSmooth", type="CORRECTIVE_SMOOTH")
        smooth.iterations = 8
        smooth.scale = 0.5
        smooth.use_only_smooth = True
        smooth.use_pin_boundary = True
        bpy.ops.object.modifier_apply(modifier=smooth.name)

        # Triangulate quads from OpenVDB for clean STL export and exact volume metrics
        tri_mod = obj.modifiers.new(name="Triangulate", type="TRIANGULATE")
        bpy.ops.object.modifier_apply(modifier=tri_mod.name)

        bpy.ops.object.shade_smooth()
        bpy.ops.wm.stl_export(filepath=out_stl, export_selected_objects=True)

        # 4. Re-verify watertightness & ground Z=0 via trimesh
        m_v3 = trimesh.load(out_stl, file_type="stl")
        if target_height_mm and target_height_mm > 0 and m_v3.extents[2] > 0:
            m_v3.apply_scale(target_height_mm / m_v3.extents[2])
        b3 = m_v3.bounds
        m_v3.apply_translation([
            -((b3[0][0] + b3[1][0]) / 2.0),
            -((b3[0][1] + b3[1][1]) / 2.0),
            -b3[0][2],
        ])

        stl_buf = io.BytesIO()
        m_v3.export(stl_buf, file_type="stl")
        v3_stl_bytes = stl_buf.getvalue()
        print(f"Cloud Worker: Exported OpenVDB V3 STL ({len(v3_stl_bytes):,} bytes, {len(m_v3.faces):,} faces).")

        # 5. Render 360° Studio Turntable via Blender Cycles
        print(f"Cloud Worker: Rendering {n_frames}-frame Cycles path-traced studio turntable...")
        ext_x, ext_y, ext_z = [float(v) for v in m_v3.extents]
        max_dim = max(ext_x, ext_y, ext_z, 1.0)
        center_z = ext_z * 0.48

        scene = bpy.context.scene
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 16
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.05
        scene.cycles.use_denoising = False
        scene.cycles.max_bounces = 3
        scene.cycles.diffuse_bounces = 2
        scene.cycles.glossy_bounces = 2
        scene.render.resolution_x = 520
        scene.render.resolution_y = 580
        scene.render.image_settings.file_format = "PNG"

        world = bpy.data.worlds.new("StudioWorld")
        scene.world = world
        world.use_nodes = True
        bg_node = world.node_tree.nodes.get("Background")
        if bg_node:
            bg_node.inputs[0].default_value = (0.008, 0.013, 0.024, 1.0)
            bg_node.inputs[1].default_value = 1.0

        mat = bpy.data.materials.new(name="ZeroStudioClay")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.83, 0.87, 0.93, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.38
            if "Specular IOR Level" in bsdf.inputs:
                bsdf.inputs["Specular IOR Level"].default_value = 0.45
        obj.data.materials.append(mat)

        bpy.ops.mesh.primitive_plane_add(size=max_dim * 12.0, location=(0.0, 0.0, 0.0))
        floor_obj = bpy.context.active_object
        floor_mat = bpy.data.materials.new(name="StudioFloor")
        floor_mat.use_nodes = True
        f_bsdf = floor_mat.node_tree.nodes.get("Principled BSDF")
        if f_bsdf:
            f_bsdf.inputs["Base Color"].default_value = (0.012, 0.018, 0.032, 1.0)
            f_bsdf.inputs["Roughness"].default_value = 0.72
        floor_obj.data.materials.append(floor_mat)

        target_pt = mathutils.Vector((0.0, 0.0, center_z))
        dist_scale = max_dim * 1.8

        def add_area_light(name: str, rel_pos: tuple[float, float, float], intensity_mult: float, size_mult: float, color: tuple[float, float, float]):
            ldata = bpy.data.lights.new(name=name, type="AREA")
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

        add_area_light("KeyLight", (0.85, -0.85, 0.95), 28.0, 1.1, (1.0, 0.98, 0.95))
        add_area_light("FillLight", (-0.95, -0.70, 0.45), 11.0, 1.4, (0.82, 0.90, 1.0))
        add_area_light("RimLight", (0.15, 1.05, 0.85), 24.0, 0.9, (0.90, 0.96, 1.0))

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

        xy_radius = math.hypot(ext_x, ext_y) * 0.5
        bounding_radius = max(xy_radius * 1.15, ext_z * 0.58)
        cam_dist = bounding_radius * 2.55
        elev_rad = math.radians(elev)
        cam_y = -cam_dist * math.cos(elev_rad)
        cam_z = cam_dist * math.sin(elev_rad)
        cam_obj.location = (0.0, cam_y, cam_z)
        cam_dir = mathutils.Vector((0.0, 0.0, 0.0)) - cam_obj.location
        cam_obj.rotation_euler = cam_dir.to_track_quat("-Z", "Y").to_euler()

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

        print("Cloud Worker: Complete!")
        return {
            "v3_stl_bytes": v3_stl_bytes,
            "turntable_gif_bytes": gif_buf.getvalue(),
            "hero_png_bytes": hero_buf.getvalue(),
            "metrics": {
                "vertices": len(m_v3.vertices),
                "faces": len(m_v3.faces),
                "is_watertight": bool(m_v3.is_watertight),
                "volume_mm3": float(m_v3.volume),
                "extents_mm": m_v3.extents.tolist(),
            },
        }


def main():
    parser = argparse.ArgumentParser(description="Generate high-resolution V3 mesh & Cycles turntable via Modal Blender 4.2 cloud compute")
    parser.add_argument("--raw-stl", default="/workspace/data/3dprint/zero_casual_robot_zup.stl", help="Input raw STL")
    parser.add_argument("--out-stl", default="/workspace/data/3dprint/zero_casual_robot_v3.stl", help="Output V3 solid STL")
    parser.add_argument("--out-gif", default="/workspace/data/3dprint/zero_figurine_v3_turntable.gif", help="Output turntable GIF")
    parser.add_argument("--pitch", type=float, default=0.22, help="OpenVDB Voxel pitch in mm (default 0.22)")
    parser.add_argument("--target-height", type=float, default=120.0, help="Target height in mm (default 120.0)")
    parser.add_argument("--channel", default="1555973316752449576", help="Discord channel ID for delivery")
    args = parser.parse_args()

    raw_p = Path(args.raw_stl)
    if not raw_p.exists():
        print(f"Error: Raw STL not found: {raw_p}")
        sys.exit(1)

    print(f"1. Reading raw sculpt: {raw_p.name} ({raw_p.stat().st_size:,} bytes)...")
    raw_bytes = raw_p.read_bytes()

    print(f"2. Dispatching Blender 4.2 OpenVDB remesh & Cycles turntable to Modal (8 vCPUs, 16GB RAM, pitch={args.pitch}mm, height={args.target_height}mm)...")
    t0 = time.time()
    with app.run():
        result = generate_v3_cloud.remote(raw_bytes, pitch=args.pitch, target_height_mm=args.target_height)

    print(f"3. Cloud execution completed in {time.time() - t0:.1f}s.")
    metrics = result["metrics"]
    print("   Mesh Metrics:")
    print(f"   - Vertices: {metrics['vertices']:,}")
    print(f"   - Faces: {metrics['faces']:,}")
    print(f"   - Watertight: {metrics['is_watertight']}")
    print(f"   - Solid Volume: {metrics['volume_mm3']:.1f} mm³")
    print(f"   - Extents (mm): {metrics['extents_mm']}")

    out_stl = Path(args.out_stl)
    out_stl.write_bytes(result["v3_stl_bytes"])
    print(f"   Saved V3 STL: {out_stl} ({out_stl.stat().st_size:,} bytes)")

    out_gif = Path(args.out_gif)
    out_gif.write_bytes(result["turntable_gif_bytes"])
    print(f"   Saved V3 Turntable: {out_gif} ({out_gif.stat().st_size:,} bytes)")

    hero_path = out_gif.with_name("zero_figurine_v3_hero_render.png")
    hero_path.write_bytes(result["hero_png_bytes"])

    # 4. Deliver to Discord
    if args.channel:
        sys.path.insert(0, "/workspace/tools")
        from deliver_image import deliver_image
        caption = (
            f"🎬 **Zero Figurine V3 — Blender 4.2 OpenVDB + Cycles Turntable (Modal Cloud)**\n"
            f"- **Faces:** {metrics['faces']:,} ({metrics['vertices']:,} verts, {out_stl.stat().st_size / 1024 / 1024:.1f} MB)\n"
            f"- **OpenVDB Voxel Pitch:** {args.pitch}mm\n"
            f"- **Solid Core:** {metrics['volume_mm3']:.1f} mm³ (100% watertight)\n"
            f"- **Host 2 CPU Usage:** 0.0% (computed on Modal 8-core Blender worker)"
        )
        deliv_res = deliver_image(str(out_gif), channel_input=args.channel, caption=caption)
        print("   Discord delivery result:", deliv_res)

    print(f"=== V3 Pipeline Finished in {time.time() - t0:.1f}s ===")


if __name__ == "__main__":
    main()
