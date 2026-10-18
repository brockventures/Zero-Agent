#!/usr/bin/env python3
import os
import sys
import argparse
import time
from pathlib import Path
import modal

app = modal.App("zero-3d-render-service")

render_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("trimesh", "numpy", "matplotlib", "pillow", "scipy", "fast-simplification")
)

@app.function(image=render_image, cpu=4, memory=8192, timeout=180)
def render_cloud_turntable(
    stl_bytes: bytes,
    target_height_mm: float = 0.0,
    n_frames: int = 24,
    elev: float = 24.0
) -> bytes:
    import io
    import trimesh
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    from PIL import Image

    print(f"Cloud Worker: Processing STL ({len(stl_bytes):,} bytes)...")
    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
    
    # Scale & Center (preserve scale if target_height_mm <= 0)
    if target_height_mm and target_height_mm > 0:
        scale = target_height_mm / mesh.extents[2]
        mesh.apply_scale(scale)
    bounds = mesh.bounds
    mesh.apply_translation([
        -((bounds[0][0] + bounds[1][0]) / 2.0),
        -((bounds[0][1] + bounds[1][1]) / 2.0),
        -bounds[0][2]
    ])

    print(f"Cloud Worker: Loaded mesh ({len(mesh.vertices):,} verts, {len(mesh.faces):,} faces)")
    
    # Decimate visual proxy if over 150k faces for faster rendering
    if len(mesh.faces) > 150000:
        target_faces = 100000
        reduction = 1.0 - (target_faces / len(mesh.faces))
        reduction = max(0.01, min(0.95, reduction))
        print(f"Cloud Worker: Decimating visual proxy by {reduction:.1%} from {len(mesh.faces):,} faces...")
        render_mesh = mesh.simplify_quadric_decimation(reduction)
    else:
        render_mesh = mesh

    # 3-Point Studio Lighting
    light1 = np.array([0.6, -0.6, 0.75])
    light1 /= np.linalg.norm(light1)
    light2 = np.array([-0.6, -0.5, 0.4])
    light2 /= np.linalg.norm(light2)
    light3 = np.array([0.1, 0.8, 0.6])
    light3 /= np.linalg.norm(light3)

    normals = render_mesh.face_normals
    d1 = np.clip(np.dot(normals, light1), 0, 1)
    d2 = np.clip(np.dot(normals, light2), 0, 1)
    d3 = np.clip(np.dot(normals, light3), 0, 1)
    intensity = 0.20 + 0.60 * d1 + 0.15 * d2 + 0.15 * d3
    base_color = np.array([0.92, 0.94, 0.97])
    face_colors = np.clip(base_color[None, :] * intensity[:, None], 0, 1)

    ext_x, ext_y, ext_z = render_mesh.extents
    max_xy = max(ext_x, ext_y)
    radius = max_xy * 0.65
    z_span = max(ext_z * 1.1, max_xy * 0.4)

    # 3-Point Studio Lighting vectors
    light1 = np.array([0.6, -0.6, 0.75])
    light1 /= np.linalg.norm(light1)
    light2 = np.array([-0.6, -0.5, 0.4])
    light2 /= np.linalg.norm(light2)
    light3 = np.array([0.1, 0.8, 0.6])
    light3 /= np.linalg.norm(light3)
    base_color = np.array([0.88, 0.92, 0.96])

    elev_rad = np.radians(elev)

    frames = []
    print(f"Cloud Worker: Rendering {n_frames} frames of 360° turntable with back-face culling & depth sorting...")
    for i, azim in enumerate(np.linspace(0, 360, n_frames, endpoint=False)):
        azim_rad = np.radians(azim)
        cam_vec = np.array([
            np.cos(elev_rad) * np.cos(azim_rad + np.pi/2),
            np.cos(elev_rad) * np.sin(azim_rad + np.pi/2),
            np.sin(elev_rad)
        ])
        cam_vec /= np.linalg.norm(cam_vec)

        # Back-face culling: cull faces pointing away from the camera
        dot = np.dot(render_mesh.face_normals, cam_vec)
        vis_mask = dot > 0.02
        if not np.any(vis_mask):
            vis_mask = np.ones(len(render_mesh.faces), dtype=bool)

        vis_faces = render_mesh.faces[vis_mask]
        vis_normals = render_mesh.face_normals[vis_mask]

        # 3-Point Lighting on visible faces
        d1 = np.clip(np.dot(vis_normals, light1), 0, 1)
        d2 = np.clip(np.dot(vis_normals, light2), 0, 1)
        d3 = np.clip(np.dot(vis_normals, light3), 0, 1)
        intensity = 0.20 + 0.60 * d1 + 0.15 * d2 + 0.15 * d3
        face_colors = np.clip(base_color[None, :] * intensity[:, None], 0, 1)

        # Depth-sorting (Painter's algorithm: draw farthest to nearest)
        tri_verts = render_mesh.vertices[vis_faces]
        face_centers = tri_verts.mean(axis=1)
        dist = np.dot(face_centers, cam_vec)
        order = np.argsort(dist)

        tri_verts = tri_verts[order]
        face_colors = face_colors[order]

        fig = plt.figure(figsize=(6, 7), facecolor='#090d16')
        ax = fig.add_subplot(111, projection='3d', facecolor='#090d16')
        ax.add_collection3d(Poly3DCollection(tri_verts, facecolors=face_colors, edgecolors='none', alpha=1.0))
        
        ax.set_xlim(-radius, radius)
        ax.set_ylim(-radius, radius)
        ax.set_zlim(0, z_span)
        ax.set_box_aspect([2 * radius, 2 * radius, z_span])
        ax.view_init(elev=elev, azim=azim)
        ax.axis('off')

        fig.canvas.draw()
        rgba = np.asarray(fig.canvas.buffer_rgba())
        frames.append(Image.fromarray(rgba).convert('RGB'))
        plt.close(fig)

    out_buf = io.BytesIO()
    frames[0].save(
        out_buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        optimize=True
    )
    print("Cloud Worker: Finished rendering turntable GIF!")
    return out_buf.getvalue()

def main():
    parser = argparse.ArgumentParser(description="Render 3D STL to 360° Turntable via Modal Cloud Compute")
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

    print(f"Dispatching {stl_file.name} ({stl_file.stat().st_size:,} bytes) to Modal cloud compute...")
    t0 = time.time()
    stl_bytes = stl_file.read_bytes()

    target_h = 0.0 if args.preserve_scale else args.target_height
    with app.run():
        gif_bytes = render_cloud_turntable.remote(
            stl_bytes,
            target_height_mm=target_h,
            elev=args.elev
        )

    out_file.write_bytes(gif_bytes)
    print(f"Received rendered turntable from Modal ({len(gif_bytes):,} bytes) in {time.time() - t0:.1f}s")
    print(f"Saved to: {out_file}")

    if args.channel:
        sys.path.insert(0, '/workspace/tools')
        from deliver_image import deliver_image
        caption = args.caption or f"🎬 **360° Studio Turntable (Modal Cloud Render)**: {stl_file.name}"
        res = deliver_image(str(out_file), channel_input=args.channel, caption=caption)
        print("Discord delivery:", res)

if __name__ == "__main__":
    main()
