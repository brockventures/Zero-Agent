#!/usr/bin/env python3
"""
gemini_image.py - Direct Google Gemini API Image Generation Pipeline

Bypasses Antigravity CLI's shared developer preview queue by making direct
HTTPS REST calls to the Gemini API (models/gemini-3.1-flash-image) using
the provisioned GEMINI_API_KEY.

Features:
1. Direct API Execution: 5-8s average render latency on Google TPU cluster.
2. Multi-Model Fallback: gemini-3.1-flash-image -> gemini-3.1-flash-image-preview -> gemini-2.5-flash-image.
3. Hard Client Timeout: Default 30s timeout prevents process hangs.
4. Integrated Verification: Auto-verifies JPEG dimensions, format, and byte size via check_image().
5. One-Click Discord Delivery: Optional --channel and --caption arguments deliver via deliver_image.py.
"""

import os
import sys
import json
import time
import base64
import argparse
import requests
from pathlib import Path
from typing import Optional, Dict, Any

sys.path.insert(0, "/workspace")
try:
    from tools.deliver_image import check_image, deliver_image, BRAIN_ROOT
except ImportError:
    from deliver_image import check_image, deliver_image, BRAIN_ROOT

DATA_DIR = Path("/workspace/data")
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

MODEL_CASCADE = [
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
]

ASPECT_RATIO_PROMPTS = {
    "16:9": "in widescreen cinematic 16:9 aspect ratio",
    "9:16": "in vertical portrait 9:16 aspect ratio",
    "4:3": "in classic 4:3 aspect ratio",
    "3:4": "in vertical 3:4 aspect ratio",
    "3:2": "in 3:2 aspect ratio",
    "2:3": "in vertical 2:3 aspect ratio",
    "1:1": "in square 1:1 format",
}


def get_gemini_api_key() -> str:
    """Retrieve Gemini API key from environment, secrets/env.json, or .env."""
    key = os.getenv("GEMINI_API_KEY")
    if key and key.strip():
        return key.strip()

    secrets_env = Path("/secrets/env.json")
    if secrets_env.exists():
        try:
            with open(secrets_env, "r", encoding="utf-8") as f:
                d = json.load(f)
                if "GEMINI_API_KEY" in d and d["GEMINI_API_KEY"].strip():
                    return d["GEMINI_API_KEY"].strip()
        except Exception:
            pass

    workspace_env = Path("/workspace/.env")
    if workspace_env.exists():
        try:
            with open(workspace_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        k = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if k:
                            return k
        except Exception:
            pass

    raise RuntimeError("GEMINI_API_KEY not found in environment, /secrets/env.json, or /workspace/.env")


def resolve_output_dir(custom_dir: Optional[str] = None) -> Path:
    """Resolve output directory: custom -> active brain session -> /workspace/data/images/."""
    if custom_dir:
        p = Path(custom_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    conv_id = os.getenv("ANTIGRAVITY_CONVERSATION_ID")
    if conv_id and (BRAIN_ROOT / conv_id).exists():
        return BRAIN_ROOT / conv_id

    return IMAGES_DIR


def generate_image_api(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_name: str = "image",
    output_dir: Optional[str] = None,
    timeout: int = 30,
    reference_image: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate an image directly via Gemini API, with optional reference image conditioning.
    Returns dictionary with image metadata and verification results.
    """
    api_key = get_gemini_api_key()
    out_dir = resolve_output_dir(output_dir)

    clean_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in image_name.lower().strip())
    clean_name = clean_name.strip("_") or "gemini_render"
    timestamp_ms = int(time.time() * 1000)
    out_file = out_dir / f"{clean_name}_{timestamp_ms}.jpg"

    ratio_guidance = ASPECT_RATIO_PROMPTS.get(aspect_ratio.strip())
    full_prompt = prompt.strip()
    if ratio_guidance and ratio_guidance.lower() not in full_prompt.lower():
        full_prompt = f"{full_prompt}, {ratio_guidance}"

    parts = []
    if reference_image:
        ref_path = Path(reference_image)
        if ref_path.exists() and ref_path.is_file():
            ref_bytes = ref_path.read_bytes()
            ref_b64 = base64.b64encode(ref_bytes).decode("utf-8")
            ref_mime = "image/png" if ref_path.suffix.lower() == ".png" else "image/jpeg"
            parts.append({
                "inlineData": {
                    "mimeType": ref_mime,
                    "data": ref_b64
                }
            })

    parts.append({"text": full_prompt})

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ]
    }
    headers = {"Content-Type": "application/json"}

    last_error = None
    for model in MODEL_CASCADE:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                res_data = resp.json()
                candidates = res_data.get("candidates", [])
                if not candidates:
                    last_error = f"Model {model} returned 200 but 0 candidates"
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "inlineData" in part:
                        data_b64 = part["inlineData"].get("data")
                        mime_type = part["inlineData"].get("mimeType", "image/jpeg")
                        if data_b64:
                            raw_bytes = base64.b64decode(data_b64)
                            out_file.write_bytes(raw_bytes)

                            v_info = check_image(out_file)
                            if not v_info["valid"]:
                                return {
                                    "success": False,
                                    "error": f"Image written to disk failed verification: {v_info.get('error')}",
                                    "path": str(out_file),
                                    "model": model,
                                }

                            return {
                                "success": True,
                                "path": str(out_file),
                                "filename": out_file.name,
                                "model": model,
                                "prompt": full_prompt,
                                "mime_type": mime_type,
                                "width": v_info["width"],
                                "height": v_info["height"],
                                "size_bytes": v_info["size_bytes"],
                                "size_kb": v_info["size_kb"],
                            }
                last_error = f"Model {model} response did not contain inlineData image parts"
            else:
                last_error = f"Model {model} HTTP {resp.status_code}: {resp.text[:250]}"
        except requests.exceptions.Timeout:
            last_error = f"Model {model} timed out after {timeout}s"
        except Exception as e:
            last_error = f"Model {model} error: {e}"

    return {
        "success": False,
        "error": f"All Gemini image models failed. Last error: {last_error}",
        "prompt": full_prompt,
    }


def main():
    parser = argparse.ArgumentParser(description="Direct Gemini API Image Generation & Discord Delivery")
    parser.add_argument("--prompt", required=True, help="Descriptive prompt for image generation")
    parser.add_argument("--aspect-ratio", default="1:1", help="Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:2)")
    parser.add_argument("--name", default="gemini_render", help="Filename slug (snake_case)")
    parser.add_argument("--output-dir", default=None, help="Directory to save generated image")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP request timeout in seconds")
    parser.add_argument("--channel", default=None, help="Discord channel name or ID to auto-deliver")
    parser.add_argument("--caption", default="", help="Caption for Discord delivery")
    parser.add_argument("--reference-image", default=None, help="Path to reference image for conditioning")

    args = parser.parse_args()

    result = generate_image_api(
        prompt=args.prompt,
        aspect_ratio=args.aspect_ratio,
        image_name=args.name,
        output_dir=args.output_dir,
        timeout=args.timeout,
        reference_image=args.reference_image,
    )

    if not result["success"]:
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))

    if args.channel:
        caption = args.caption or f"🎨 **{args.name}**\n`{args.prompt}`"
        delivery = deliver_image(result["path"], channel_input=args.channel, caption=caption)
        print(json.dumps(delivery, indent=2))
        if not (delivery.get("delivered") or delivery.get("success")):
            sys.exit(2)


if __name__ == "__main__":
    main()
