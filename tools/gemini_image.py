#!/usr/bin/env python3
"""
gemini_image.py - Antigravity Subscription-First Image Generation & Paid-API Guard Pipeline

Routes image generation by default through the Antigravity CLI subscription (`agy`
built-in `image-generator` subagent / `--model=gemini-4-argon-low`) at $0 API cost.
Direct HTTPS REST calls to `generativelanguage.googleapis.com` with `GEMINI_API_KEY`
are strictly gated behind explicit `--allow-paid-api` (or `GEMINI_IMAGE_ALLOW_PAID=1`)
and enforced by a hard daily budget cap to prevent unexpected Google Cloud billing.

Features:
1. Subscription-First Execution: Uses Antigravity CLI subscription OAuth by default ($0 API billing).
2. Paid-API Gate & Budget Guard: Requires `--allow-paid-api` and enforces a daily call cap before touching paid endpoints.
3. Multi-Model Fallback (Paid API): gemini-3.1-flash-image -> gemini-3.1-flash-image-preview -> gemini-2.5-flash-image.
4. Hard Client Timeout: Configurable timeout prevents process hangs.
5. Integrated Verification: Auto-verifies image dimensions, format, and byte size via check_image().
6. One-Click Discord Delivery: Optional --channel and --caption arguments deliver via deliver_image.py.
"""

import os
import re
import sys
import json
import time
import shutil
import base64
import argparse
import subprocess
import requests
from pathlib import Path
from typing import Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from tools.deliver_image import check_image, deliver_image, BRAIN_ROOT, IMAGE_EXTENSIONS
except ImportError:
    from deliver_image import check_image, deliver_image, BRAIN_ROOT, IMAGE_EXTENSIONS

DATA_DIR = Path("/workspace/data")
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

BUDGET_STATE_FILE = DATA_DIR / "gemini_image_paid_budget.json"
DEFAULT_DAILY_PAID_LIMIT = 5
SUBSCRIPTION_MODEL = "gemini-4-argon-low"

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


def _is_paid_api_allowed(allow_paid_api: bool = False) -> bool:
    """Check whether paid Gemini REST API calls are explicitly authorized."""
    if allow_paid_api:
        return True
    env_val = os.getenv("GEMINI_IMAGE_ALLOW_PAID", "").strip().lower()
    return env_val in ("1", "true", "yes")


def check_and_record_paid_budget(
    budget_file: Path = BUDGET_STATE_FILE,
    daily_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Enforce a hard daily budget cap on paid Gemini API image calls.
    Prevents runaway loops or unapproved multi-image batch jobs from exhausting billing.
    """
    if daily_limit is None:
        try:
            daily_limit = int(os.getenv("GEMINI_IMAGE_DAILY_PAID_LIMIT", str(DEFAULT_DAILY_PAID_LIMIT)))
        except ValueError:
            daily_limit = DEFAULT_DAILY_PAID_LIMIT

    now = time.time()
    window_start = now - 86400.0
    timestamps = []

    if budget_file.exists():
        try:
            data = json.loads(budget_file.read_text(encoding="utf-8"))
            raw_ts = data.get("timestamps", [])
            if isinstance(raw_ts, list):
                timestamps = [float(t) for t in raw_ts if isinstance(t, (int, float)) and float(t) > window_start]
        except Exception:
            timestamps = []

    if len(timestamps) >= daily_limit:
        return {
            "allowed": False,
            "used_24h": len(timestamps),
            "daily_limit": daily_limit,
            "error": (
                f"Paid Gemini Image API daily budget exceeded ({len(timestamps)}/{daily_limit} calls in last 24h). "
                "Use default Antigravity subscription mode or raise GEMINI_IMAGE_DAILY_PAID_LIMIT."
            ),
        }

    timestamps.append(now)
    try:
        budget_file.parent.mkdir(parents=True, exist_ok=True)
        budget_file.write_text(json.dumps({"timestamps": timestamps}, indent=2), encoding="utf-8")
    except Exception:
        pass

    return {
        "allowed": True,
        "used_24h": len(timestamps),
        "daily_limit": daily_limit,
    }


def _find_extracted_or_recent_image(
    stdout_text: str,
    out_file: Path,
    start_time: float,
    search_dirs: list[Path],
) -> Optional[Path]:
    """Locate the generated image file from destination, stdout paths, or recent brain/output artifacts."""
    if out_file.exists() and out_file.is_file() and out_file.stat().st_size > 0:
        return out_file

    # Check paths mentioned in agy stdout
    path_matches = re.findall(r"(/[^\s\"'`<>()\[\]]+\.(?:jpg|jpeg|png|webp))", stdout_text, flags=re.IGNORECASE)
    for raw_match in path_matches:
        candidate = Path(raw_match)
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    # Scan search directories for newly created image files
    recent_candidates: list[Path] = []
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        try:
            for item in sdir.rglob("*"):
                if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in IMAGE_EXTENSIONS:
                    if item.stat().st_mtime >= (start_time - 2.0) and item.stat().st_size > 0:
                        recent_candidates.append(item)
        except Exception:
            continue

    if recent_candidates:
        return max(recent_candidates, key=lambda p: p.stat().st_mtime)

    return None


def generate_image_subscription(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_name: str = "image",
    output_dir: Optional[str] = None,
    timeout: int = 90,
    reference_image: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate an image using the Antigravity CLI subscription (`agy` built-in
    `image-generator` subagent / `--model=gemini-4-argon-low`) at $0 API cost,
    copy/move the resulting artifact to `out_file`, and validate with `check_image()`.
    """
    out_dir = resolve_output_dir(output_dir)

    clean_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in image_name.lower().strip())
    clean_name = clean_name.strip("_") or "gemini_render"
    timestamp_ms = int(time.time() * 1000)
    out_file = out_dir / f"{clean_name}_{timestamp_ms}.jpg"

    ratio_guidance = ASPECT_RATIO_PROMPTS.get(aspect_ratio.strip())
    full_prompt = prompt.strip()
    if ratio_guidance and ratio_guidance.lower() not in full_prompt.lower():
        full_prompt = f"{full_prompt}, {ratio_guidance}"

    ref_clause = ""
    if reference_image:
        ref_path = Path(reference_image)
        if ref_path.exists() and ref_path.is_file():
            ref_clause = f" Reference image path for conditioning: {ref_path.resolve()}."

    agy_prompt = (
        f"Invoke the image-generator subagent to generate an image "
        f"with AspectRatio='{aspect_ratio.strip()}', ImageName='{clean_name}', "
        f"and Prompt: {full_prompt}.{ref_clause} "
        f"Wait for the image-generator subagent to finish, save or copy the resulting image to {out_file}, "
        f"and reply with ONLY the absolute file path."
    )

    cmd = [
        "agy",
        f"--model={SUBSCRIPTION_MODEL}",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        f"-p={agy_prompt}",
    ]

    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Antigravity subscription image generation timed out after {timeout}s",
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to invoke Antigravity CLI ('agy') for subscription image generation: {e}",
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }

    stdout_str = (proc.stdout or "") + "\n" + (proc.stderr or "")
    search_dirs = [out_dir]
    if BRAIN_ROOT.exists():
        search_dirs.append(BRAIN_ROOT)
    if IMAGES_DIR not in search_dirs:
        search_dirs.append(IMAGES_DIR)

    found_path = _find_extracted_or_recent_image(stdout_str, out_file, start_time, search_dirs)
    if not found_path:
        err_detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[:300]
        return {
            "success": False,
            "error": f"Antigravity subscription generation produced no image artifact on disk: {err_detail}",
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }

    if found_path.resolve() != out_file.resolve():
        try:
            shutil.copy2(found_path, out_file)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to copy generated artifact {found_path} to {out_file}: {e}",
                "path": str(found_path),
                "model": f"subscription:{SUBSCRIPTION_MODEL}",
                "prompt": full_prompt,
            }

    v_info = check_image(out_file)
    if not v_info.get("valid"):
        return {
            "success": False,
            "error": f"Image written to disk failed verification: {v_info.get('error')}",
            "path": str(out_file),
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }

    mime_type = "image/png" if out_file.suffix.lower() == ".png" else "image/jpeg"
    return {
        "success": True,
        "path": str(out_file),
        "filename": out_file.name,
        "model": f"subscription:{SUBSCRIPTION_MODEL}",
        "billing_mode": "subscription",
        "prompt": full_prompt,
        "mime_type": mime_type,
        "width": v_info["width"],
        "height": v_info["height"],
        "size_bytes": v_info["size_bytes"],
        "size_kb": v_info["size_kb"],
    }


def generate_image_api(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_name: str = "image",
    output_dir: Optional[str] = None,
    timeout: int = 30,
    reference_image: Optional[str] = None,
    allow_paid_api: bool = False,
) -> Dict[str, Any]:
    """
    Generate an image. By default routes through the $0 Antigravity CLI subscription
    (`generate_image_subscription`). Only touches `https://generativelanguage.googleapis.com`
    with `GEMINI_API_KEY` when `allow_paid_api=True` (or `GEMINI_IMAGE_ALLOW_PAID=1`)
    and the daily paid API budget guard passes.
    """
    if not _is_paid_api_allowed(allow_paid_api):
        return generate_image_subscription(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            image_name=image_name,
            output_dir=output_dir,
            timeout=timeout,
            reference_image=reference_image,
        )

    budget_check = check_and_record_paid_budget()
    if not budget_check["allowed"]:
        return {
            "success": False,
            "error": budget_check["error"],
            "prompt": prompt.strip(),
            "billing_mode": "paid_api_blocked",
        }

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
                                "billing_mode": "paid_api",
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


generate_image = generate_image_api


def main():
    parser = argparse.ArgumentParser(description="Antigravity Subscription-First Image Generation & Discord Delivery")
    parser.add_argument("--prompt", required=True, help="Descriptive prompt for image generation")
    parser.add_argument("--aspect-ratio", default="1:1", help="Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:2)")
    parser.add_argument("--name", default="gemini_render", help="Filename slug (snake_case)")
    parser.add_argument("--output-dir", default=None, help="Directory to save generated image")
    parser.add_argument("--timeout", type=int, default=60, help="Generation timeout in seconds")
    parser.add_argument("--channel", default=None, help="Discord channel name or ID to auto-deliver")
    parser.add_argument("--caption", default="", help="Caption for Discord delivery")
    parser.add_argument("--reference-image", default=None, help="Path to reference image for conditioning")
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        default=False,
        help="Explicitly authorize paid Google Cloud Gemini REST API calls (subject to daily budget guard)",
    )

    args = parser.parse_args()

    result = generate_image_api(
        prompt=args.prompt,
        aspect_ratio=args.aspect_ratio,
        image_name=args.name,
        output_dir=args.output_dir,
        timeout=args.timeout,
        reference_image=args.reference_image,
        allow_paid_api=args.allow_paid_api,
    )

    if not result["success"]:
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))

    if args.channel:
        caption = args.caption.strip() if args.caption else None
        delivery = deliver_image(result["path"], channel_input=args.channel, caption=caption)
        print(json.dumps(delivery, indent=2))
        if not (delivery.get("delivered") or delivery.get("success")):
            sys.exit(2)


if __name__ == "__main__":
    main()
