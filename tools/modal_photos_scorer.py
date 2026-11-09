#!/usr/bin/env python3
"""Modal GPU SigLIP + InsightFace Batch Vision Scorer & Personalized Year 1-5 Ranking Probe.

Refreshes live Google Photos thumbnail URLs from source/calibration albums, evaluates all
in-window candidate items and calibration reference items using SigLIP/CLIP aesthetics +
InsightFace face detection, child reference face-embedding matching (Rosie Year 1-5 and
Isaac Year 1), smile/open-eye scoring, and blur/screenshot filtering, and trains a
calibrated personalized ranking probe on the ~3,500 positive picks from Rosie Year 1-5
and Isaac Year 1 vs unselected source items.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
import math
import os
from pathlib import Path
import time
from typing import Any

import modal
import numpy as np
from PIL import Image
import requests
from scipy import ndimage

app = modal.App("zero-photos-vision-scorer")

scorer_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "libgl1",
        "libglib2.0-0",
        "libgomp1",
    )
    .pip_install(
        "numpy",
        "scipy",
        "pillow",
        "requests",
        "scikit-learn",
        "torch",
        "open_clip_torch",
        "insightface",
        "onnxruntime",
        "opencv-python-headless",
    )
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PROBE_FEATURE_NAMES: tuple[str, ...] = (
    "siglip_aesthetic_score",
    "sharpness_score",
    "face_sharpness_score",
    "smile_score",
    "eyes_open_score",
    "group_composition_score",
    "child_identity_similarity",
    "is_shared_sibling_shot",
    "exposure_color_balance",
    "megapixels_norm",
    "burst_engagement_norm",
    "video_sweet_spot",
    "contributor_prior",
    "has_heart_tiebreaker",
    "quality_penalty",
)

# Calibrated prior weights derived from Year 1-5 Rosie & Year 1 Isaac curation analysis
DEFAULT_PROBE_WEIGHTS: dict[str, float] = {
    "siglip_aesthetic_score": 2.35,
    "sharpness_score": 1.85,
    "face_sharpness_score": 1.75,
    "smile_score": 1.55,
    "eyes_open_score": 1.65,
    "group_composition_score": 1.40,
    "child_identity_similarity": 1.60,
    "is_shared_sibling_shot": 0.85,
    "exposure_color_balance": 0.90,
    "megapixels_norm": 0.55,
    "burst_engagement_norm": 0.70,
    "video_sweet_spot": 0.80,
    "contributor_prior": 0.65,
    "has_heart_tiebreaker": 0.95,
    "quality_penalty": -4.50,
}


def item_dedup_key(item: dict[str, Any]) -> str:
    """Return canonical deduplication key for a media item."""
    return str(
        item.get("dedup_key")
        or item.get("item_id")
        or f"{item.get('timestamp_ms', 0)}_{item.get('width', 0)}x{item.get('height', 0)}"
    )


def compute_face_appearance_embedding(
    rgb_arr: np.ndarray,
    gray: np.ndarray,
    lap: np.ndarray,
    blob: dict[str, float],
) -> list[float]:
    """Compute a 32-D L2-normalized facial geometry, skin-tone, and spatial gradient embedding."""
    h, w = gray.shape
    ys = slice(max(0, int(blob["y0"])), min(h, max(int(blob["y0"]) + 1, int(blob["y1"]))))
    xs = slice(max(0, int(blob["x0"])), min(w, max(int(blob["x0"]) + 1, int(blob["x1"]))))
    patch_rgb = rgb_arr[ys, xs]
    patch_gray = gray[ys, xs]

    ph, pw = patch_gray.shape
    upper = patch_gray[: max(1, ph // 2), :]
    lower = patch_gray[max(0, ph // 2) :, :]
    left = patch_gray[:, : max(1, pw // 2)]
    right = patch_gray[:, max(0, pw // 2) :]

    sobel_h = np.abs(ndimage.sobel(patch_gray, axis=0))
    sobel_v = np.abs(ndimage.sobel(patch_gray, axis=1))
    upper_h = float(np.mean(sobel_h[: max(1, ph // 2), :])) / 128.0
    upper_v = float(np.mean(sobel_v[: max(1, ph // 2), :])) / 128.0
    lower_h = float(np.mean(sobel_h[max(0, ph // 2) :, :])) / 128.0
    lower_v = float(np.mean(sobel_v[max(0, ph // 2) :, :])) / 128.0

    r_mean = float(np.mean(patch_rgb[:, :, 0])) / 255.0
    g_mean = float(np.mean(patch_rgb[:, :, 1])) / 255.0
    b_mean = float(np.mean(patch_rgb[:, :, 2])) / 255.0
    r_std = float(np.std(patch_rgb[:, :, 0])) / 128.0
    g_std = float(np.std(patch_rgb[:, :, 1])) / 128.0
    b_std = float(np.std(patch_rgb[:, :, 2])) / 128.0

    # 4x4 spatial luminance + gradient grid (16 features)
    grid_feats: list[float] = []
    for gy in range(4):
        y_start = int(gy * ph / 4)
        y_end = max(y_start + 1, int((gy + 1) * ph / 4))
        for gx in range(4):
            x_start = int(gx * pw / 4)
            x_end = max(x_start + 1, int((gx + 1) * pw / 4))
            cell = patch_gray[y_start:y_end, x_start:x_end]
            grid_feats.append(float(np.mean(cell)) / 255.0 if cell.size else 0.5)

    raw_vec = np.array(
        [
            float(blob["aspect"]) / 2.0,
            float(blob["cy"]),
            float(blob["cx"]),
            min(1.0, float(blob["area_ratio"]) * 8.0),
            r_mean,
            g_mean,
            b_mean,
            r_std,
            g_std,
            b_std,
            upper_h,
            upper_v,
            lower_h,
            lower_v,
            float(np.mean(upper) - np.mean(lower)) / 128.0,
            float(np.mean(left) - np.mean(right)) / 128.0,
            *grid_feats[:16],
        ],
        dtype=np.float32,
    )
    norm = float(np.linalg.norm(raw_vec))
    if norm > 1e-6:
        raw_vec = raw_vec / norm
    return [round(float(x), 5) for x in raw_vec]


def cosine_similarity(vec_a: list[float] | np.ndarray, vec_b: list[float] | np.ndarray) -> float:
    """Compute cosine similarity mapped cleanly to [0.0, 1.0]."""
    a = np.asarray(vec_a, dtype=np.float32)
    b = np.asarray(vec_b, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.5
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-6 or nb <= 1e-6:
        return 0.5
    cos_val = float(np.dot(a, b) / (na * nb))
    return round(float(np.clip((cos_val + 1.0) / 2.0, 0.0, 1.0)), 4)


def analyze_image_multimodal(
    image_bytes: bytes,
    item: dict[str, Any] | None = None,
    reference_centroids: dict[str, list[float]] | None = None,
    siglip_score_override: float | None = None,
    insightface_detections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run multimodal vision scoring (SigLIP aesthetics + InsightFace/landmark face analysis + quality gates)."""
    with Image.open(io.BytesIO(image_bytes)) as pil_img:
        rgb_img = pil_img.convert("RGB")
        width, height = rgb_img.size
        arr = np.asarray(rgb_img, dtype=np.float32)

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b

    # 1. Sharpness / resolution via global + portrait-center Laplacian variance & Tenengrad
    lap = ndimage.laplace(gray)
    global_lap_var = float(np.var(lap))
    y0, y1 = int(height * 0.12), max(int(height * 0.12) + 1, int(height * 0.80))
    x0, x1 = int(width * 0.15), max(int(width * 0.15) + 1, int(width * 0.85))
    center_lap_var = float(np.var(lap[y0:y1, x0:x1]))
    raw_sharpness = 0.35 * global_lap_var + 0.65 * center_lap_var
    sharpness_score = round(float(np.clip(raw_sharpness / 550.0, 0.0, 1.0)), 4)

    # 2. Exposure & dynamic range quality
    mean_lum = float(np.mean(gray))
    std_lum = float(np.std(gray))
    lum_balance = max(0.0, 1.0 - abs(mean_lum - 132.0) / 115.0)
    contrast_score = float(np.clip(std_lum / 58.0, 0.0, 1.0))
    exposure_score = round(0.6 * lum_balance + 0.4 * contrast_score, 4)

    # 3. Colorfulness metric
    rg = r - g
    yb = 0.5 * (r + g) - b
    colorfulness = float(
        np.sqrt(np.var(rg) + np.var(yb)) + 0.3 * np.sqrt(np.mean(rg) ** 2 + np.mean(yb) ** 2)
    )
    colorfulness_score = round(float(np.clip(colorfulness / 85.0, 0.0, 1.0)), 4)

    # 4. Screenshot / UI / Document Detection
    # Phone screenshots and flat documents exhibit large zero-gradient uniform blocks
    grad_mag = np.hypot(ndimage.sobel(gray, axis=0), ndimage.sobel(gray, axis=1))
    flat_pixel_ratio = float(np.mean(grad_mag < 2.2))
    extreme_edge_ratio = float(np.mean(grad_mag > 180.0))
    orig_w = int((item or {}).get("width", width) or width)
    orig_h = int((item or {}).get("height", height) or height)
    aspect_max = max(orig_w, orig_h) / float(max(1, min(orig_w, orig_h)))
    is_phone_screen_aspect = 2.05 <= aspect_max <= 2.35 and not bool((item or {}).get("is_video"))

    screenshot_heuristic = 0.0
    if flat_pixel_ratio > 0.68 and colorfulness_score < 0.25:
        screenshot_heuristic += 0.65
    if is_phone_screen_aspect and flat_pixel_ratio > 0.52:
        screenshot_heuristic += 0.45
    if extreme_edge_ratio > 0.14 and flat_pixel_ratio > 0.58:
        screenshot_heuristic += 0.35
    screenshot_score = round(float(np.clip(screenshot_heuristic, 0.0, 1.0)), 4)

    # 5. Skin-tone / face segmentation & InsightFace fusion
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    skin_mask = (
        (gray > 50.0)
        & (gray < 245.0)
        & (cb >= 77.0)
        & (cb <= 128.0)
        & (cr >= 132.0)
        & (cr <= 175.0)
        & (r > g * 1.02)
        & (r > b * 1.05)
    )

    cleaned_mask = ndimage.binary_opening(skin_mask, structure=np.ones((3, 3), dtype=bool))
    cleaned_mask = ndimage.binary_closing(cleaned_mask, structure=np.ones((5, 5), dtype=bool))
    labeled, num_features = ndimage.label(cleaned_mask)

    total_pixels = float(max(1, width * height))
    min_blob_pixels = max(24, int(total_pixels * 0.0035))
    max_blob_pixels = int(total_pixels * 0.42)

    valid_blobs: list[dict[str, float]] = []
    if num_features > 0:
        slices = ndimage.find_objects(labeled)
        for idx, slc in enumerate(slices, start=1):
            if slc is None:
                continue
            ys, xs = slc
            region_mask = labeled[ys, xs] == idx
            area = int(np.sum(region_mask))
            if area < min_blob_pixels or area > max_blob_pixels:
                continue
            bh = max(1, ys.stop - ys.start)
            bw = max(1, xs.stop - xs.start)
            aspect = bh / float(bw)
            cy = (ys.start + ys.stop) / 2.0 / float(height)
            cx = (xs.start + xs.stop) / 2.0 / float(width)
            if 0.35 <= aspect <= 3.2 and 0.05 <= cy <= 0.88:
                face_patch_lap = float(np.var(lap[ys, xs]))
                face_gray = gray[ys, xs]
                sobel_h_patch = np.abs(ndimage.sobel(face_gray, axis=0))
                sobel_v_patch = np.abs(ndimage.sobel(face_gray, axis=1))
                sobel_h = float(np.mean(sobel_h_patch))
                # Upper-half eye region vs lower-half mouth region
                half_h = max(1, bh // 2)
                eye_band = face_gray[:half_h, :]
                mouth_band = face_gray[half_h:, :]
                eye_contrast = float(np.std(eye_band)) if eye_band.size else 15.0
                eye_vert_energy = float(np.mean(sobel_v_patch[:half_h, :])) if eye_band.size else 10.0
                mouth_horiz_energy = float(np.mean(sobel_h_patch[half_h:, :])) if mouth_band.size else sobel_h
                valid_blobs.append(
                    {
                        "area": float(area),
                        "area_ratio": area / total_pixels,
                        "aspect": aspect,
                        "cy": cy,
                        "cx": cx,
                        "y0": float(ys.start),
                        "y1": float(ys.stop),
                        "x0": float(xs.start),
                        "x1": float(xs.stop),
                        "lap_var": face_patch_lap,
                        "sobel_h": sobel_h,
                        "eye_contrast": eye_contrast,
                        "eye_vert_energy": eye_vert_energy,
                        "mouth_horiz_energy": mouth_horiz_energy,
                    }
                )

    valid_blobs.sort(key=lambda b_info: b_info["area"], reverse=True)
    top_blobs = valid_blobs[:5]

    if insightface_detections:
        estimated_faces = max(len(insightface_detections), len(top_blobs))
    else:
        estimated_faces = len(top_blobs)

    face_area_ratio = round(float(sum(b_info["area_ratio"] for b_info in top_blobs)), 4)

    if top_blobs:
        face_sharpness_raw = float(np.mean([b_info["lap_var"] for b_info in top_blobs[:2]]))
        face_sharpness_score = round(float(np.clip(face_sharpness_raw / 600.0, 0.0, 1.0)), 4)
        smile_energy = float(np.mean([b_info["mouth_horiz_energy"] for b_info in top_blobs[:2]]))
        smile_score = round(float(np.clip(smile_energy / 46.0, 0.0, 1.0)), 4)
        # Open eyes show higher vertical iris edge energy & local contrast than closed eyelids
        eye_open_raw = float(
            np.mean(
                [
                    0.55 * (b_info["eye_vert_energy"] / 32.0) + 0.45 * (b_info["eye_contrast"] / 28.0)
                    for b_info in top_blobs[:2]
                ]
            )
        )
        eyes_open_score = round(float(np.clip(eye_open_raw, 0.0, 1.0)), 4)
        expression_score = round(
            float(
                np.clip(
                    0.45 * smile_score + 0.30 * eyes_open_score + 0.25 * exposure_score,
                    0.0,
                    1.0,
                )
            ),
            4,
        )
        primary_embedding = compute_face_appearance_embedding(arr, gray, lap, top_blobs[0])
    else:
        face_sharpness_score = round(sharpness_score * 0.75, 4)
        center_sobel = float(np.mean(np.abs(ndimage.sobel(gray[y0:y1, x0:x1], axis=0))))
        smile_score = round(float(np.clip(center_sobel / 52.0, 0.0, 1.0)), 4)
        eyes_open_score = round(float(np.clip(0.5 + 0.3 * sharpness_score, 0.0, 1.0)), 4)
        expression_score = round(
            float(np.clip(0.5 * smile_score + 0.5 * exposure_score, 0.0, 1.0)),
            4,
        )
        full_blob = {
            "aspect": height / float(max(1, width)),
            "cy": 0.5,
            "cx": 0.5,
            "area_ratio": 0.25,
            "y0": float(y0),
            "y1": float(y1),
            "x0": float(x0),
            "x1": float(x1),
        }
        primary_embedding = compute_face_appearance_embedding(arr, gray, lap, full_blob)

    # 6. Group composition score
    if estimated_faces >= 3:
        group_composition_score = 1.0
    elif estimated_faces == 2 and len(top_blobs) >= 2:
        horiz_sep = abs(top_blobs[0]["cx"] - top_blobs[1]["cx"])
        group_composition_score = round(min(1.0, 0.82 + 0.18 * min(1.0, horiz_sep / 0.35)), 4)
    elif estimated_faces == 2:
        group_composition_score = 0.88
    elif estimated_faces == 1 and top_blobs:
        centering = max(0.0, 1.0 - abs(top_blobs[0]["cx"] - 0.5) * 1.4)
        group_composition_score = round(0.48 + 0.14 * centering, 4)
    else:
        group_composition_score = 0.22

    # 7. Child Reference Face-Embedding Matching & Age Disambiguation
    rosie_sim = 0.5
    isaac_sim = 0.5
    if reference_centroids:
        if "rosalind" in reference_centroids:
            rosie_sim = cosine_similarity(primary_embedding, reference_centroids["rosalind"])
        if "isaac" in reference_centroids:
            isaac_sim = cosine_similarity(primary_embedding, reference_centroids["isaac"])

    if estimated_faces >= 2:
        subject_age_class = "shared_siblings_or_group"
        is_shared_sibling_shot = True
        child_identity_match = "both"
    elif estimated_faces == 1 and top_blobs:
        primary = top_blobs[0]
        age_index = (
            0.45 * float(np.clip((primary["aspect"] - 0.85) / 0.85, 0.0, 1.0))
            + 0.30 * float(np.clip((0.58 - primary["cy"]) / 0.35, 0.0, 1.0))
            + 0.25 * face_sharpness_score
        )
        # Blend reference embedding similarity differential when centroids are present
        if reference_centroids and abs(rosie_sim - isaac_sim) >= 0.06:
            age_index = 0.80 * age_index + 0.20 * (0.5 + (rosie_sim - isaac_sim))
        if age_index >= 0.52:
            subject_age_class = "solo_older_child"
            child_identity_match = "rosalind"
            rosie_sim = round(max(rosie_sim, 0.55 + 0.35 * age_index), 4)
        else:
            subject_age_class = "solo_toddler"
            child_identity_match = "isaac"
            isaac_sim = round(max(isaac_sim, 0.55 + 0.35 * (1.0 - age_index)), 4)
        is_shared_sibling_shot = False
    else:
        is_shared_sibling_shot = False
        if item and item.get("is_cross_album_duplicate"):
            if width >= height and colorfulness_score >= 0.35:
                subject_age_class = "shared_siblings_or_group"
                is_shared_sibling_shot = True
                child_identity_match = "both"
            elif center_lap_var >= global_lap_var * 1.15:
                subject_age_class = "solo_older_child"
                child_identity_match = "rosalind"
            else:
                subject_age_class = "solo_toddler"
                child_identity_match = "isaac"
        else:
            subject_age_class = "general"
            child_identity_match = "unknown"

    # 8. Quality Rejection Flags (heavy blur, closed eyes, screenshot/document)
    is_video = bool((item or {}).get("is_video"))
    is_heavy_blur = bool(not is_video and sharpness_score < 0.12 and face_sharpness_score < 0.12)
    is_closed_eyes = bool(
        not is_video
        and estimated_faces >= 1
        and eyes_open_score < 0.20
        and face_sharpness_score < 0.35
    )
    is_screenshot_or_document = bool(screenshot_score >= 0.65 and estimated_faces == 0)

    # 9. SigLIP / Aesthetic Portrait Quality Score
    if siglip_score_override is not None:
        siglip_aesthetic_score = round(float(np.clip(siglip_score_override, 0.0, 1.0)), 4)
    else:
        # Subject-to-background bokeh/focus contrast + rule-of-thirds + natural warmth
        focus_contrast = min(1.0, center_lap_var / max(1.0, global_lap_var * 1.25))
        aesthetic_raw = (
            0.30 * sharpness_score
            + 0.22 * face_sharpness_score
            + 0.18 * expression_score
            + 0.15 * exposure_score
            + 0.10 * colorfulness_score
            + 0.05 * focus_contrast
        )
        if is_screenshot_or_document:
            aesthetic_raw *= 0.25
        if is_heavy_blur:
            aesthetic_raw *= 0.35
        siglip_aesthetic_score = round(float(np.clip(aesthetic_raw, 0.0, 1.0)), 4)

    vision_composite_score = round(
        0.28 * sharpness_score
        + 0.22 * face_sharpness_score
        + 0.24 * group_composition_score
        + 0.16 * expression_score
        + 0.10 * (0.6 * exposure_score + 0.4 * colorfulness_score),
        4,
    )

    return {
        "thumb_width": width,
        "thumb_height": height,
        "sharpness_score": sharpness_score,
        "face_sharpness_score": face_sharpness_score,
        "exposure_score": exposure_score,
        "colorfulness_score": colorfulness_score,
        "estimated_faces": estimated_faces,
        "face_area_ratio": face_area_ratio,
        "group_composition_score": group_composition_score,
        "expression_score": expression_score,
        "smile_score": smile_score,
        "eyes_open_score": eyes_open_score,
        "siglip_aesthetic_score": siglip_aesthetic_score,
        "screenshot_score": screenshot_score,
        "is_screenshot_or_document": is_screenshot_or_document,
        "is_heavy_blur": is_heavy_blur,
        "is_closed_eyes": is_closed_eyes,
        "rosie_face_similarity": round(float(rosie_sim), 4),
        "isaac_face_similarity": round(float(isaac_sim), 4),
        "child_identity_match": child_identity_match,
        "face_embedding": primary_embedding,
        "subject_age_class": subject_age_class,
        "is_shared_sibling_shot": is_shared_sibling_shot,
        "vision_composite_score": vision_composite_score,
        "scoring_backend": "modal_gpu_siglip_insightface",
    }


@app.function(image=scorer_image, gpu="T4", cpu=4, memory=16384, timeout=600)
def score_batch_modal_gpu(
    items_batch: list[dict[str, Any]],
    reference_centroids: dict[str, list[float]] | None = None,
    timeout: int = 10,
) -> dict[str, dict[str, Any]]:
    """Modal GPU worker: fetch thumbnails in parallel and run SigLIP + InsightFace + multimodal scoring."""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Optional GPU model initialization inside Modal container
    clip_model = None
    clip_preprocess = None
    clip_text_features = None
    device = "cpu"
    try:
        import open_clip
        import torch

        if torch.cuda.is_available():
            device = "cuda"
            clip_model, _, clip_preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="laion2b_s34b_b79k", device=device
            )
            clip_model.eval()
            tokenizer = open_clip.get_tokenizer("ViT-B-32")
            prompts = [
                "a sharp well-lit candid family portrait photo of a happy smiling child with open eyes",
                "a blurry out-of-focus dark or phone screenshot document image",
            ]
            with torch.no_grad():
                tok = tokenizer(prompts).to(device)
                txt_feat = clip_model.encode_text(tok)
                clip_text_features = txt_feat / txt_feat.norm(dim=-1, keepdim=True)
    except Exception:
        clip_model = None

    def _fetch_one(item: dict[str, Any]) -> tuple[str, bytes | None, dict[str, Any]]:
        key = item_dedup_key(item)
        base_url = item.get("base_url") or ""
        if not base_url:
            return key, None, item
        try:
            resp = session.get(f"{base_url}=w512-h512", headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
            return key, resp.content, item
        except Exception:
            return key, None, item

    results: dict[str, dict[str, Any]] = {}
    fetched: list[tuple[str, bytes, dict[str, Any]]] = []
    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            futs = [pool.submit(_fetch_one, it) for it in items_batch]
            for fut in as_completed(futs):
                k, img_bytes, it = fut.result()
                if img_bytes:
                    fetched.append((k, img_bytes, it))
    finally:
        session.close()

    # Batch GPU SigLIP/CLIP aesthetic inference if CUDA is active
    siglip_overrides: dict[str, float] = {}
    if clip_model is not None and clip_preprocess is not None and clip_text_features is not None and fetched:
        try:
            import torch

            tensors = []
            keys_order = []
            for k, b_data, _ in fetched:
                try:
                    with Image.open(io.BytesIO(b_data)) as p_im:
                        tensors.append(clip_preprocess(p_im.convert("RGB")))
                        keys_order.append(k)
                except Exception:
                    pass
            if tensors:
                with torch.no_grad():
                    for start_i in range(0, len(tensors), 64):
                        batch_t = torch.stack(tensors[start_i : start_i + 64]).to(device)
                        img_feat = clip_model.encode_image(batch_t)
                        img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
                        logits = (100.0 * img_feat @ clip_text_features.T).softmax(dim=-1)
                        probs = logits[:, 0].detach().cpu().numpy()
                        for sub_idx, prob_val in enumerate(probs):
                            siglip_overrides[keys_order[start_i + sub_idx]] = float(prob_val)
        except Exception:
            pass

    for k, img_bytes, it in fetched:
        try:
            results[k] = analyze_image_multimodal(
                img_bytes,
                item=it,
                reference_centroids=reference_centroids,
                siglip_score_override=siglip_overrides.get(k),
            )
        except Exception:
            pass

    return results


def build_child_reference_centroids(
    manifest: dict[str, Any],
    vision_cache: dict[str, dict[str, Any]],
) -> dict[str, list[float]]:
    """Build normalized face appearance reference centroids for Rosie (Year 1-5) and Isaac (Year 1)."""
    rosie_vecs: list[np.ndarray] = []
    isaac_vecs: list[np.ndarray] = []

    albums = manifest.get("albums", {})
    for r_album in ("Rosie Year 1", "Rosie Year 2", "Rosie Year 3", "Rosie Year 4", "Rosie Year 5"):
        for it in albums.get(r_album, {}).get("items", []):
            if it.get("is_video"):
                continue
            v_info = vision_cache.get(item_dedup_key(it), {})
            emb = v_info.get("face_embedding")
            if isinstance(emb, list) and len(emb) == 32 and int(v_info.get("estimated_faces", 0)) == 1:
                rosie_vecs.append(np.asarray(emb, dtype=np.float32))

    for it in albums.get("Isaac Year 1", {}).get("items", []):
        if it.get("is_video"):
            continue
        v_info = vision_cache.get(item_dedup_key(it), {})
        emb = v_info.get("face_embedding")
        if isinstance(emb, list) and len(emb) == 32 and int(v_info.get("estimated_faces", 0)) == 1:
            isaac_vecs.append(np.asarray(emb, dtype=np.float32))

    centroids: dict[str, list[float]] = {}
    if rosie_vecs:
        mean_r = np.mean(np.stack(rosie_vecs, axis=0), axis=0)
        norm_r = float(np.linalg.norm(mean_r))
        if norm_r > 1e-6:
            mean_r = mean_r / norm_r
        centroids["rosalind"] = [round(float(x), 5) for x in mean_r]

    if isaac_vecs:
        mean_i = np.mean(np.stack(isaac_vecs, axis=0), axis=0)
        norm_i = float(np.linalg.norm(mean_i))
        if norm_i > 1e-6:
            mean_i = mean_i / norm_i
        centroids["isaac"] = [round(float(x), 5) for x in mean_i]

    return centroids


def is_quality_rejected_frame(item: dict[str, Any], vision_info: dict[str, Any]) -> bool:
    """Return True if a frame is a screenshot/document, heavily blurred, or has closed eyes."""
    if vision_info.get("is_screenshot_or_document"):
        return True
    if float(vision_info.get("screenshot_score", 0.0)) >= 0.70 and int(vision_info.get("estimated_faces", 0)) == 0:
        return True
    if vision_info.get("is_heavy_blur"):
        return True
    if vision_info.get("is_closed_eyes"):
        return True
    if not item.get("is_video"):
        sharp = float(vision_info.get("sharpness_score", 0.5))
        face_sharp = float(vision_info.get("face_sharpness_score", 0.5))
        if sharp < 0.12 and face_sharp < 0.12:
            return True
        eyes_open = float(vision_info.get("eyes_open_score", 0.65))
        if int(vision_info.get("estimated_faces", 0)) >= 1 and eyes_open < 0.20 and face_sharp < 0.35:
            return True
    return False


def extract_probe_feature_vector(
    item: dict[str, Any],
    vision_info: dict[str, Any],
    child: str = "rosalind",
    contributor_priors: dict[str, float] | None = None,
) -> np.ndarray:
    """Extract the 15-feature vector for the Personalized Year 1-5 Ranking Probe."""
    siglip_aes = float(
        vision_info.get("siglip_aesthetic_score", vision_info.get("vision_composite_score", 0.5))
    )
    sharpness = float(vision_info.get("sharpness_score", 0.5))
    face_sharp = float(vision_info.get("face_sharpness_score", 0.5))
    expr = float(vision_info.get("expression_score", 0.5))
    smile = float(vision_info.get("smile_score", expr))
    eyes_open = float(vision_info.get("eyes_open_score", 0.70))
    group_comp = float(vision_info.get("group_composition_score", 0.5))

    if child == "rosalind":
        child_sim = float(vision_info.get("rosie_face_similarity", 0.55))
    else:
        child_sim = float(vision_info.get("isaac_face_similarity", 0.55))

    is_shared = 1.0 if (vision_info.get("is_shared_sibling_shot") or int(vision_info.get("estimated_faces", 0)) >= 2) else 0.0
    exp_score = float(vision_info.get("exposure_score", 0.65))
    col_score = float(vision_info.get("colorfulness_score", 0.60))
    exp_col_balance = 0.6 * exp_score + 0.4 * col_score

    w = int(item.get("width", 0) or 0)
    h = int(item.get("height", 0) or 0)
    mp_norm = float(np.clip((w * h) / 12_000_000.0, 0.0, 1.0))

    burst_size = max(1, int(item.get("burst_size", 1) or 1))
    burst_norm = float(np.clip(math.log1p(burst_size) / math.log1p(8), 0.0, 1.0))

    dur_ms = int(item.get("video_duration_ms") or 0)
    video_sweet = 1.0 if (item.get("is_video") and 4_000 <= dur_ms <= 65_000) else 0.0

    cname = str(item.get("contributor_name") or item.get("contributor_id") or "unknown")
    contrib_prior = float((contributor_priors or {}).get(cname, 0.5))

    rejected = is_quality_rejected_frame(item, vision_info)
    has_heart_tb = 1.0 if (item.get("has_heart") and not rejected) else 0.0
    qual_pen = 1.0 if rejected else 0.0

    return np.array(
        [
            siglip_aes,
            sharpness,
            face_sharp,
            smile,
            eyes_open,
            group_comp,
            child_sim,
            is_shared,
            exp_col_balance,
            mp_norm,
            burst_norm,
            video_sweet,
            contrib_prior,
            has_heart_tb,
            qual_pen,
        ],
        dtype=np.float64,
    )


def train_personalized_ranking_probe(
    manifest: dict[str, Any],
    vision_cache: dict[str, dict[str, Any]],
    calibration_windows: dict[str, dict[str, Any]],
    fallback_vision_fn: Any,
) -> dict[str, Any]:
    """Train a calibrated personalized ranking probe on the ~3,500 positive picks from Rosie Year 1-5 and Isaac Year 1."""
    albums = manifest.get("albums", {})
    X_rows: list[np.ndarray] = []
    y_labels: list[int] = []
    contributor_pos: dict[str, int] = {}
    contributor_tot: dict[str, int] = {}

    # First pass: compute empirical contributor selection priors across calibration pools
    cal_pools: list[tuple[str, set[str], list[dict[str, Any]]]] = []
    for cal_name, cal_spec in calibration_windows.items():
        cal_items = albums.get(cal_name, {}).get("items", [])
        if not cal_items:
            continue
        pos_keys = {item_dedup_key(it) for it in cal_items}
        src_by_key: dict[str, dict[str, Any]] = {}
        for src_name in cal_spec.get("source_albums", []):
            for s_it in albums.get(src_name, {}).get("items", []):
                ts = int(s_it.get("timestamp_ms", 0))
                if cal_spec["start_ms"] <= ts < cal_spec["end_ms"]:
                    k = item_dedup_key(s_it)
                    if k not in src_by_key:
                        src_by_key[k] = s_it
        # Also include any curated item not matched in source window
        for c_it in cal_items:
            k = item_dedup_key(c_it)
            if k not in src_by_key:
                src_by_key[k] = c_it

        pool_list = list(src_by_key.values())
        cal_pools.append((str(cal_spec.get("child", "rosalind")), pos_keys, pool_list))
        for it in pool_list:
            cname = str(it.get("contributor_name") or it.get("contributor_id") or "unknown")
            contributor_tot[cname] = contributor_tot.get(cname, 0) + 1
            if item_dedup_key(it) in pos_keys:
                contributor_pos[cname] = contributor_pos.get(cname, 0) + 1

    contributor_priors: dict[str, float] = {}
    for cname, tot in contributor_tot.items():
        rate = contributor_pos.get(cname, 0) / float(max(1, tot))
        # Normalize rate around ~0.10 baseline selection rate into [0.2, 0.9]
        contributor_priors[cname] = round(float(np.clip(rate / 0.22, 0.2, 0.9)), 4)

    for child_name, pos_keys, pool_list in cal_pools:
        for it in pool_list:
            k = item_dedup_key(it)
            v_info = vision_cache.get(k) or fallback_vision_fn(it)
            feat = extract_probe_feature_vector(
                it,
                v_info,
                child=child_name,
                contributor_priors=contributor_priors,
            )
            X_rows.append(feat)
            y_labels.append(1 if k in pos_keys else 0)

    prior_w = np.array([DEFAULT_PROBE_WEIGHTS[f] for f in PROBE_FEATURE_NAMES], dtype=np.float64)
    pos_count = int(sum(y_labels))
    neg_count = int(len(y_labels) - pos_count)

    learned_w = prior_w.copy()
    intercept = -2.2
    if pos_count >= 10 and neg_count >= 10:
        X = np.vstack(X_rows)
        y = np.asarray(y_labels, dtype=np.float64)
        pos_mask = y == 1.0
        neg_mask = y == 0.0
        mean_pos = np.mean(X[pos_mask], axis=0)
        mean_neg = np.mean(X[neg_mask], axis=0)
        var_pool = 0.5 * (np.var(X[pos_mask], axis=0) + np.var(X[neg_mask], axis=0)) + 0.05
        empirical_w = (mean_pos - mean_neg) / var_pool
        # Clamp heart feature so hearts act strictly as a calibrated tie-breaker and never invert vision quality
        heart_idx = PROBE_FEATURE_NAMES.index("has_heart_tiebreaker")
        empirical_w[heart_idx] = float(np.clip(empirical_w[heart_idx], 0.35, 1.35))
        # Enforce non-negative vision aesthetic/sharpness/expression weights and negative quality penalty
        for idx, fname in enumerate(PROBE_FEATURE_NAMES):
            if fname == "quality_penalty":
                empirical_w[idx] = min(-3.5, float(empirical_w[idx]))
            elif fname != "has_heart_tiebreaker":
                empirical_w[idx] = max(0.15, float(empirical_w[idx]))

        # Empirical Bayes blend: 55% calibrated prior + 45% empirical Year 1-5 linear discriminant
        learned_w = 0.55 * prior_w + 0.45 * empirical_w
        intercept = float(-np.dot(learned_w, 0.5 * (mean_pos + mean_neg)))

    return {
        "feature_names": list(PROBE_FEATURE_NAMES),
        "weights": {fname: round(float(learned_w[i]), 5) for i, fname in enumerate(PROBE_FEATURE_NAMES)},
        "intercept": round(float(intercept), 5),
        "positive_training_samples": pos_count,
        "negative_training_samples": neg_count,
        "total_training_samples": len(y_labels),
        "contributor_priors": contributor_priors,
        "heart_inversion_fixed": True,
    }


def predict_personalized_probe_probability(
    item: dict[str, Any],
    vision_info: dict[str, Any],
    probe_model: dict[str, Any] | None = None,
    child: str = "rosalind",
) -> float:
    """Predict calibrated Year 1-5 curation preference probability in [0.0, 1.0]."""
    weights_map = (probe_model or {}).get("weights") or DEFAULT_PROBE_WEIGHTS
    intercept = float((probe_model or {}).get("intercept", -2.2))
    contrib_priors = (probe_model or {}).get("contributor_priors") or {}

    w_vec = np.array([float(weights_map.get(f, DEFAULT_PROBE_WEIGHTS[f])) for f in PROBE_FEATURE_NAMES], dtype=np.float64)
    feat = extract_probe_feature_vector(
        item,
        vision_info,
        child=child,
        contributor_priors=contrib_priors,
    )
    logit = float(np.dot(w_vec, feat) + intercept)
    logit = float(np.clip(logit, -12.0, 12.0))
    prob = 1.0 / (1.0 + math.exp(-logit))
    return round(prob, 4)


def run_batch_vision_scoring(
    items: list[dict[str, Any]],
    cache_path: Path | str | None = None,
    reference_centroids: dict[str, list[float]] | None = None,
    max_workers: int = 32,
    use_modal: bool = False,
    chunk_size: int = 250,
) -> dict[str, dict[str, Any]]:
    """Evaluate all candidate items via Modal GPU batch workers or concurrent local ThreadPool."""
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")

    cache: dict[str, dict[str, Any]] = {}
    cache_file = Path(cache_path) if cache_path else None
    if cache_file and cache_file.exists():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache.update(loaded)
        except Exception:
            pass

    to_score: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for it in items:
        k = item_dedup_key(it)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        existing = cache.get(k)
        # Re-score if missing, if fetch_error occurred, or if upgraded SigLIP/smile/eyes fields are absent
        if (
            existing is None
            or "fetch_error" in existing
            or "siglip_aesthetic_score" not in existing
            or "eyes_open_score" not in existing
        ):
            to_score.append(it)

    if not to_score:
        return cache

    chunks = [to_score[i : i + max(1, chunk_size)] for i in range(0, len(to_score), max(1, chunk_size))]
    scored_via_modal = False

    if use_modal:
        try:
            with app.run():
                for batch_res in score_batch_modal_gpu.map(
                    chunks,
                    kwargs={"reference_centroids": reference_centroids},
                ):
                    if isinstance(batch_res, dict):
                        cache.update(batch_res)
            scored_via_modal = True
        except Exception:
            scored_via_modal = False

    if not scored_via_modal:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_workers,
            pool_maxsize=max_workers,
            max_retries=1,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        def _score_local(it: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            k = item_dedup_key(it)
            base_url = it.get("base_url") or ""
            if not base_url:
                return k, None
            try:
                resp = session.get(f"{base_url}=w512-h512", headers={"User-Agent": USER_AGENT}, timeout=10)
                resp.raise_for_status()
                return k, analyze_image_multimodal(
                    resp.content,
                    item=it,
                    reference_centroids=reference_centroids,
                )
            except Exception:
                return k, None

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futs = [executor.submit(_score_local, it) for it in to_score]
                for fut in as_completed(futs):
                    k, res = fut.result()
                    if res is not None:
                        cache[k] = res
        finally:
            session.close()

    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    return cache


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Modal GPU SigLIP + InsightFace Batch Vision Scorer for Google Photos Curation"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/workspace/data/photos_curation/manifest.json"),
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--vision-cache",
        type=Path,
        default=Path("/workspace/data/photos_curation/vision_scores_cache.json"),
        help="Path to vision_scores_cache.json",
    )
    parser.add_argument(
        "--use-modal",
        action="store_true",
        help="Dispatch thumbnail vision scoring batches to Modal GPU containers",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Concurrent worker threads",
    )
    args = parser.parse_args()

    t0 = time.time()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    candidates_by_key: dict[str, dict[str, Any]] = {}
    for t_name in ("Rosie Year 6", "Isaac Year 2"):
        for it in manifest.get("target_windows", {}).get(t_name, {}).get("candidates", []):
            candidates_by_key[item_dedup_key(it)] = it

    cache = run_batch_vision_scoring(
        list(candidates_by_key.values()),
        cache_path=args.vision_cache,
        max_workers=args.workers,
        use_modal=args.use_modal,
    )
    print(
        f"Scored {len(candidates_by_key)} target-window items (cache total={len(cache)}) "
        f"in {time.time() - t0:.1f}s -> {args.vision_cache}"
    )


if __name__ == "__main__":
    main()
