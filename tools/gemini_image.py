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
7. Hearth Weather-Pack Holiday & Birthday Generator: Generates multi-variant (`_v1.jpg`..`_v3.jpg`)
   Studio Ghibli watercolor holiday & birthday scene packs in 16:9 right-weighted layout.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from tools.deliver_image import (
        BRAIN_ROOT,
        IMAGE_EXTENSIONS,
        check_image,
        deliver_image,
    )
except ImportError:
    from deliver_image import BRAIN_ROOT, IMAGE_EXTENSIONS, check_image, deliver_image

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

DEFAULT_GHIBLI_PACK_STYLE = (
    "Hand-painted Studio Ghibli watercolor and gouache illustration with delicate warm ink linework, "
    "lush storybook botanical and architectural detail, soft luminous natural lighting, gentle film grain, "
    "composed in horizontal widescreen 16:9 format with ALL characters positioned in the right 42% of the frame "
    "and wide open atmospheric scenery on the left 58% of the frame so left-side weather UI text never covers faces, "
    "no floating UI, no watermarks, no overlaid typography"
)

HEARTH_HOLIDAY_SCENES: Dict[str, tuple[str, ...]] = {
    "thanksgiving": (
        (
            "Crisp golden-hour Thanksgiving morning along a sunlit brick garden lane beside a mossy stone wall "
            "decorated with autumn harvest wreaths, heirloom orange and cream pumpkins, drifting amber maple leaves, "
            "and three tiny cheerful black soot sprites carrying acorns in the grass; characters dressed in cozy knit "
            "beanies and warm autumn jackets walking happily together on the right side"
        ),
        (
            "Warm sunlit Thanksgiving afternoon in a scenic hillside orchard and cottage garden with a woven picnic "
            "basket of crisp apples, persimmons, and mini pumpkins on a wooden bench, golden autumn foliage glowing "
            "across rolling hills on the left, characters gathered joyfully on the right side in cozy sweaters and scarves"
        ),
        (
            "Cozy amber-lit Thanksgiving twilight on a warm wooden cottage porch overlooking misty lavender-gold "
            "autumn hills on the left, glowing paper lanterns, autumn leaf garlands, and warm golden window light "
            "streaming onto the veranda while characters share a happy peaceful moment on the right side"
        ),
    ),
    "christmas": (
        (
            "Crisp festive Christmas morning along a lightly frosted brick cottage path bordered by evergreen pine "
            "trees strung with warm golden fairy lights, cedar garlands with red velvet bows on a wooden gate, "
            "and playful tiny soot sprites wearing miniature red knit scarves; characters bundled in cozy winter "
            "puffer coats and knit hats smiling together on the right side"
        ),
        (
            "Warm sunlit Christmas afternoon inside a storybook timber cottage bay window looking out over a winter "
            "pine garden on the left, featuring a lush decorated Douglas fir Christmas tree glowing with warm lights, "
            "kraft-paper wrapped presents with red ribbons, and a classic wooden toy train while characters celebrate "
            "in cozy festive knit sweaters on the right side"
        ),
        (
            "Magical Christmas Eve twilight on a lantern-lit stone veranda overlooking a serene misty winter valley "
            "and glowing evergreen trees on the left, stockings and warm golden hearth light visible through paned "
            "cottage windows, characters gathered warmly in cozy winter coats and scarves on the right side"
        ),
    ),
    "winter_solstice": (
        (
            "Serene Winter Solstice sunrise over mist-draped coastal redwood and pine hills on the left, soft "
            "low-angle golden sunbeams filtering through frosted branches onto a mossy stone garden path with "
            "red winterberries and pinecones, characters in warm winter coats and knit hats on the right side"
        ),
        (
            "Peaceful sunlit Winter Solstice afternoon beside a charming cedar garden pavilion overlooking crisp "
            "winter hills and camellia bushes in bloom on the left, evergreen boughs and woven lantern ornaments, "
            "characters enjoying the calm winter sunshine in cozy layered knitwear on the right side"
        ),
        (
            "Enchanting Winter Solstice longest-night twilight along a winding garden path illuminated by warm "
            "glowing paper and brass lanterns under a deep indigo starry sky on the left, tiny gentle glowing "
            "forest sprites peeking from mossy stones while characters admire the lantern glow on the right side"
        ),
    ),
    "new_years": (
        (
            "Bright crisp New Year's Day sunrise overlooking sunlit mist-kissed hills and coastal pines on the left, "
            "festive gold-and-ivory paper star garlands fluttering on a garden terrace, characters bundled in warm "
            "winter jackets and cozy scarves welcoming the new year with bright smiles on the right side"
        ),
        (
            "Joyful New Year's afternoon on a cozy sunlit wooden deck overlooking a winter valley on the left, "
            "decorated with golden paper lanterns, festive streamers, and warm cider mugs on a side table while "
            "characters celebrate happily in festive winter sweaters on the right side"
        ),
        (
            "Magical early-evening New Year's Eve celebration on a cozy lantern-lit hillside terrace overlooking "
            "distant twinkling town lights and gentle golden fireworks blooming in a twilight indigo sky on the left, "
            "characters bundled warmly on the right side watching the festive sparkle with delight"
        ),
    ),
}

HEARTH_HOLIDAY_LEGACY_STATES: Dict[str, tuple[str, ...]] = {
    "thanksgiving": (
        "cold_puffer",
        "cool_evening",
        "morning_mild",
        "morning_rain",
        "rainy_boots",
        "warm_pants",
    ),
    "christmas": (
        "cold_puffer",
        "cool_evening",
        "morning_mild",
        "morning_rain",
        "rainy_boots",
        "warm_pants",
    ),
    "winter_solstice": (
        "cold_puffer",
        "cool_evening",
        "morning_mild",
        "morning_rain",
        "rainy_boots",
        "warm_pants",
    ),
    "new_years": (
        "cold_puffer",
        "cool_evening",
        "morning_mild",
        "morning_rain",
        "rainy_boots",
        "warm_pants",
    ),
}

HEARTH_BIRTHDAY_SLOTS: tuple[str, ...] = ("morning", "afternoon", "evening")


def _sanitize_slug(raw_name: str, fallback: str = "gemini_render") -> tuple[str, str]:
    """Normalize an image_name into a safe filename stem and file extension."""
    trimmed = raw_name.strip()
    ext = ".jpg"
    lower = trimmed.lower()
    for candidate_ext in (".jpg", ".jpeg", ".png", ".webp"):
        if lower.endswith(candidate_ext):
            ext = ".png" if candidate_ext == ".png" else ".jpg"
            trimmed = trimmed[: -len(candidate_ext)]
            break
    clean = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in trimmed.lower().strip())
    clean = clean.strip("_") or fallback
    return clean, ext


def _resolve_target_file(out_dir: Path, image_name: str, exact_filename: bool = False) -> tuple[str, Path]:
    """Resolve output target path (either timestamped or exact filename)."""
    clean_name, ext = _sanitize_slug(image_name)
    if exact_filename:
        return clean_name, out_dir / f"{clean_name}{ext}"
    timestamp_ms = int(time.time() * 1000)
    return clean_name, out_dir / f"{clean_name}_{timestamp_ms}{ext}"


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
    preferred_stem: Optional[str] = None,
) -> Optional[Path]:
    """Locate the generated image file from destination, stdout paths, or recent brain/output artifacts."""
    if out_file.exists() and out_file.is_file() and out_file.stat().st_size > 0:
        if out_file.stat().st_mtime >= (start_time - 2.0):
            return out_file

    # Check paths mentioned in agy stdout
    path_matches = re.findall(r"(/[^\s\"'`<>()\[\]]+\.(?:jpg|jpeg|png|webp))", stdout_text, flags=re.IGNORECASE)
    for raw_match in path_matches:
        candidate = Path(raw_match)
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            if candidate.resolve() != out_file.resolve() or candidate.stat().st_mtime >= (start_time - 2.0):
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
        if preferred_stem:
            stem_lower = preferred_stem.lower()
            matching = [p for p in recent_candidates if stem_lower in p.stem.lower()]
            if matching:
                return max(matching, key=lambda p: p.stat().st_mtime)
        return max(recent_candidates, key=lambda p: p.stat().st_mtime)

    return None


def generate_image_subscription(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_name: str = "image",
    output_dir: Optional[str] = None,
    timeout: int = 90,
    reference_image: Optional[str] = None,
    exact_filename: bool = False,
) -> Dict[str, Any]:
    """
    Generate an image using the Antigravity CLI subscription (`agy` built-in
    `image-generator` subagent / `--model=gemini-4-argon-low`) at $0 API cost,
    copy/move the resulting artifact to `out_file`, and validate with `check_image()`.
    """
    out_dir = resolve_output_dir(output_dir)
    clean_name, out_file = _resolve_target_file(out_dir, image_name, exact_filename=exact_filename)

    staging_token = f"{clean_name}_{int(time.time() * 1000)}_{os.getpid()}_{threading.get_ident()}"
    staging_file = out_dir / f"staging_{staging_token}{out_file.suffix}" if exact_filename else out_file

    ratio_guidance = ASPECT_RATIO_PROMPTS.get(aspect_ratio.strip())
    full_prompt = prompt.strip()
    if ratio_guidance and ratio_guidance.lower() not in full_prompt.lower():
        full_prompt = f"{full_prompt}, {ratio_guidance}"

    ref_clause = ""
    if reference_image:
        ref_path = Path(reference_image)
        if ref_path.exists() and ref_path.is_file():
            ref_clause = f" Reference image path for conditioning: {ref_path.resolve()}."

    agy_image_name = staging_token if exact_filename else clean_name
    agy_prompt = (
        f"Invoke the image-generator subagent to generate an image "
        f"with AspectRatio='{aspect_ratio.strip()}', ImageName='{agy_image_name}', "
        f"and Prompt: {full_prompt}.{ref_clause} "
        f"Wait for the image-generator subagent to finish, save or copy the resulting image to {staging_file}, "
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
        staging_file.unlink(missing_ok=True) if exact_filename else None
        return {
            "success": False,
            "error": f"Antigravity subscription image generation timed out after {timeout}s",
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }
    except Exception as e:
        staging_file.unlink(missing_ok=True) if exact_filename else None
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

    found_path = _find_extracted_or_recent_image(
        stdout_str,
        staging_file,
        start_time,
        search_dirs,
        preferred_stem=agy_image_name,
    )
    if not found_path:
        err_detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[:300]
        return {
            "success": False,
            "error": f"Antigravity subscription generation produced no image artifact on disk: {err_detail}",
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }

    if found_path.resolve() != staging_file.resolve():
        try:
            shutil.copy2(found_path, staging_file)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to copy generated artifact {found_path} to {staging_file}: {e}",
                "path": str(found_path),
                "model": f"subscription:{SUBSCRIPTION_MODEL}",
                "prompt": full_prompt,
            }

    v_info = check_image(staging_file)
    if not v_info.get("valid"):
        if exact_filename:
            staging_file.unlink(missing_ok=True)
        return {
            "success": False,
            "error": f"Image written to disk failed verification: {v_info.get('error')}",
            "path": str(out_file),
            "model": f"subscription:{SUBSCRIPTION_MODEL}",
            "prompt": full_prompt,
        }

    if staging_file.resolve() != out_file.resolve():
        try:
            os.replace(staging_file, out_file)
        except Exception as e:
            staging_file.unlink(missing_ok=True)
            return {
                "success": False,
                "error": f"Failed to finalize staged image {staging_file} to {out_file}: {e}",
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
    exact_filename: bool = False,
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
            exact_filename=exact_filename,
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
    _, out_file = _resolve_target_file(out_dir, image_name, exact_filename=exact_filename)

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
                    "data": ref_b64,
                }
            })

    parts.append({"text": full_prompt})

    payload = {
        "contents": [
            {
                "parts": parts,
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

                cand_parts = candidates[0].get("content", {}).get("parts", [])
                for part in cand_parts:
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


def build_hearth_holiday_prompt(
    holiday_key: str,
    variant_index: int,
    cast_description: str,
    style_prompt: str = DEFAULT_GHIBLI_PACK_STYLE,
) -> str:
    """Build a Studio Ghibli 16:9 right-weighted holiday card prompt for a given holiday and variant (1..N)."""
    norm_key = holiday_key.strip().lower()
    if norm_key not in HEARTH_HOLIDAY_SCENES:
        supported = ", ".join(sorted(HEARTH_HOLIDAY_SCENES))
        raise ValueError(f"Unsupported holiday_key {holiday_key!r}. Supported: {supported}")
    scenes = HEARTH_HOLIDAY_SCENES[norm_key]
    idx = (max(1, int(variant_index)) - 1) % len(scenes)
    scene_desc = scenes[idx]
    cast_clean = cast_description.strip().rstrip(".")
    return f"{style_prompt}. Scene: {scene_desc}. Characters (positioned in the right half of the frame): {cast_clean}."


def build_hearth_birthday_prompt(
    slot: str,
    celebrant_description: str,
    cast_description: str,
    age: Optional[int] = None,
    season_hint: str = "cozy winter",
    style_prompt: str = DEFAULT_GHIBLI_PACK_STYLE,
) -> str:
    """Build a Studio Ghibli 16:9 right-weighted birthday card prompt for morning, afternoon, or evening."""
    norm_slot = slot.strip().lower()
    if norm_slot not in HEARTH_BIRTHDAY_SLOTS:
        raise ValueError(f"Unsupported birthday slot {slot!r}. Expected one of {HEARTH_BIRTHDAY_SLOTS}")

    age_badge = (
        f"wearing a festive paper birthday crown clearly displaying the number '{age}'"
        if age is not None
        else "wearing a festive paper birthday crown"
    )
    celebrant_clean = celebrant_description.strip().rstrip(".")
    cast_clean = cast_description.strip().rstrip(".")
    season_clean = season_hint.strip() or "cozy seasonal"

    slot_scenes = {
        "morning": (
            f"Warm sunlit {season_clean} birthday morning inside a cozy timber-and-stone kitchen with a wide paned "
            f"window on the left overlooking a crisp {season_clean} garden, pastel balloons, and a stack of birthday "
            f"pancakes with berries and a single lit candle on a wooden breakfast table; {celebrant_clean} ({age_badge}) "
            f"and family cast ({cast_clean}) gathered joyfully on the right side of the frame"
        ),
        "afternoon": (
            f"Cheerful {season_clean} birthday afternoon in a sunlit sunroom and cottage veranda overlooking {season_clean} "
            f"trees and garden scenery on the left, colorful paper bunting, balloons, and a classic wooden toy train set "
            f"and wrapped birthday presents on a cozy rug while {celebrant_clean} ({age_badge}) and family cast "
            f"({cast_clean}) play happily on the right side of the frame"
        ),
        "evening": (
            f"Magical cozy {season_clean} birthday evening in a warm lantern-lit cottage dining room with twilight blue "
            f"windows on the left, glowing string lights, and a homemade frosted birthday cake with lit candles on a "
            f"wooden table as {celebrant_clean} ({age_badge}) prepares to make a wish surrounded by ({cast_clean}) "
            f"on the right side of the frame"
        ),
    }
    return f"{style_prompt}. Scene: {slot_scenes[norm_slot]}."


def sync_legacy_holiday_states(pack_dir: Path, holiday_key: str) -> list[str]:
    """
    Populate legacy single-state `holiday_<key>_<state>.jpg` files from multi-variant
    `holiday_<key>_v1..v3.jpg` assets so both variant-aware and legacy clients resolve cleanly.
    """
    norm_key = holiday_key.strip().lower()
    states = HEARTH_HOLIDAY_LEGACY_STATES.get(norm_key, ())
    if not states:
        return []

    variants = [pack_dir / f"holiday_{norm_key}_v{i}.jpg" for i in (1, 2, 3)]
    available = [p for p in variants if p.exists() and p.stat().st_size > 0]
    if not available:
        return []

    v_morning = available[0]
    v_afternoon = available[min(1, len(available) - 1)]
    v_evening = available[min(2, len(available) - 1)]

    state_source_map = {
        "morning_mild": v_morning,
        "morning_rain": v_morning,
        "warm_pants": v_afternoon,
        "rainy_boots": v_afternoon,
        "cold_puffer": v_evening,
        "cool_evening": v_evening,
    }

    written: list[str] = []
    for st in states:
        src = state_source_map.get(st, available[0])
        dst = pack_dir / f"holiday_{norm_key}_{st}.jpg"
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
            written.append(str(dst))
    return written


def generate_hearth_holiday_pack(
    pack_dir: str | Path,
    cast_description: str,
    holidays: Sequence[str] = ("thanksgiving", "christmas", "winter_solstice", "new_years"),
    variants_per_holiday: int = 3,
    birthday_specs: Optional[Sequence[Dict[str, Any]]] = None,
    reference_image: Optional[str] = None,
    skip_existing: bool = False,
    sync_legacy_states: bool = True,
    max_workers: int = 3,
    timeout: int = 120,
    allow_paid_api: bool = False,
) -> Dict[str, Any]:
    """
    Generate and verify Studio Ghibli holiday multi-variant (`holiday_<key>_v1..vN.jpg`)
    and optional birthday (`birthday_<key>_<slot>.jpg`) cards in `pack_dir`.
    """
    target_dir = Path(pack_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, int(variants_per_holiday))

    jobs: list[Dict[str, Any]] = []
    skipped: list[str] = []

    for h_key in holidays:
        norm_key = h_key.strip().lower()
        for v_idx in range(1, count + 1):
            stem = f"holiday_{norm_key}_v{v_idx}"
            dest = target_dir / f"{stem}.jpg"
            if skip_existing and dest.exists() and check_image(dest).get("valid"):
                skipped.append(str(dest))
                continue
            prompt = build_hearth_holiday_prompt(norm_key, v_idx, cast_description)
            jobs.append({"kind": "holiday", "key": norm_key, "stem": stem, "prompt": prompt})

    for bspec in birthday_specs or []:
        b_key, _ = _sanitize_slug(str(bspec.get("key") or bspec.get("name") or "member"), fallback="member")
        celebrant_desc = str(bspec.get("celebrant_description") or bspec.get("name") or b_key)
        b_cast = str(bspec.get("cast_description") or cast_description)
        b_age = bspec.get("age")
        b_season = str(bspec.get("season_hint") or "cozy winter")
        slots = bspec.get("slots") or HEARTH_BIRTHDAY_SLOTS
        for slot in slots:
            norm_slot = str(slot).strip().lower()
            stem = f"birthday_{b_key}_{norm_slot}"
            dest = target_dir / f"{stem}.jpg"
            if skip_existing and dest.exists() and check_image(dest).get("valid"):
                skipped.append(str(dest))
                continue
            prompt = build_hearth_birthday_prompt(
                slot=norm_slot,
                celebrant_description=celebrant_desc,
                cast_description=b_cast,
                age=int(b_age) if b_age is not None else None,
                season_hint=b_season,
            )
            jobs.append({"kind": "birthday", "key": b_key, "stem": stem, "prompt": prompt})

    generated: list[Dict[str, Any]] = []
    failures: list[Dict[str, Any]] = []

    def _run_job(job: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        res = generate_image_api(
            prompt=job["prompt"],
            aspect_ratio="16:9",
            image_name=job["stem"],
            output_dir=str(target_dir),
            timeout=timeout,
            reference_image=reference_image,
            allow_paid_api=allow_paid_api,
            exact_filename=True,
        )
        return job, res

    worker_count = max(1, min(int(max_workers), len(jobs) or 1))
    if jobs:
        if worker_count == 1:
            for job in jobs:
                j, res = _run_job(job)
                if res.get("success"):
                    generated.append(res)
                else:
                    failures.append({"stem": j["stem"], "error": res.get("error"), "result": res})
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                future_map = {pool.submit(_run_job, job): job for job in jobs}
                for fut in as_completed(future_map):
                    j, res = fut.result()
                    if res.get("success"):
                        generated.append(res)
                    else:
                        failures.append({"stem": j["stem"], "error": res.get("error"), "result": res})

    legacy_synced: list[str] = []
    if sync_legacy_states:
        for h_key in holidays:
            legacy_synced.extend(sync_legacy_holiday_states(target_dir, h_key))

    generated.sort(key=lambda r: r.get("filename", ""))
    return {
        "success": len(failures) == 0,
        "pack_dir": str(target_dir),
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "legacy_synced_count": len(legacy_synced),
        "generated": generated,
        "skipped": skipped,
        "legacy_synced": legacy_synced,
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description="Antigravity Subscription-First Image Generation & Discord Delivery")
    parser.add_argument("--prompt", default=None, help="Descriptive prompt for single image generation")
    parser.add_argument("--aspect-ratio", default="1:1", help="Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:2)")
    parser.add_argument("--name", default="gemini_render", help="Filename slug (snake_case)")
    parser.add_argument("--exact-name", action="store_true", default=False, help="Write exact <name>.jpg without timestamp suffix")
    parser.add_argument("--output-dir", default=None, help="Directory to save generated image")
    parser.add_argument("--timeout", type=int, default=90, help="Generation timeout in seconds")
    parser.add_argument("--channel", default=None, help="Discord channel name or ID to auto-deliver")
    parser.add_argument("--caption", default="", help="Caption for Discord delivery")
    parser.add_argument("--reference-image", default=None, help="Path to reference image for conditioning")
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        default=False,
        help="Explicitly authorize paid Google Cloud Gemini REST API calls (subject to daily budget guard)",
    )
    parser.add_argument("--hearth-pack-dir", default=None, help="Generate Studio Ghibli holiday pack into this directory")
    parser.add_argument("--cast-description", default=None, help="Character cast description for --hearth-pack-dir")
    parser.add_argument(
        "--holidays",
        default="thanksgiving,christmas,winter_solstice,new_years",
        help="Comma-separated holiday keys for --hearth-pack-dir",
    )
    parser.add_argument("--variants", type=int, default=3, help="Variants per holiday for --hearth-pack-dir")
    parser.add_argument("--skip-existing", action="store_true", default=False, help="Skip valid existing pack files")
    parser.add_argument("--no-sync-legacy-states", action="store_true", default=False, help="Disable legacy state file sync")
    parser.add_argument("--max-workers", type=int, default=3, help="Max concurrent workers for --hearth-pack-dir")

    args = parser.parse_args()

    if args.hearth_pack_dir:
        if not args.cast_description or not args.cast_description.strip():
            parser.error("--cast-description is required when using --hearth-pack-dir")
        holiday_keys = [h.strip() for h in args.holidays.split(",") if h.strip()]
        pack_res = generate_hearth_holiday_pack(
            pack_dir=args.hearth_pack_dir,
            cast_description=args.cast_description,
            holidays=holiday_keys,
            variants_per_holiday=args.variants,
            reference_image=args.reference_image,
            skip_existing=args.skip_existing,
            sync_legacy_states=not args.no_sync_legacy_states,
            max_workers=args.max_workers,
            timeout=args.timeout,
            allow_paid_api=args.allow_paid_api,
        )
        if not pack_res["success"]:
            print(json.dumps(pack_res, indent=2), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(pack_res, indent=2))
        return

    if not args.prompt or not args.prompt.strip():
        parser.error("the following arguments are required: --prompt (unless --hearth-pack-dir is specified)")

    result = generate_image_api(
        prompt=args.prompt,
        aspect_ratio=args.aspect_ratio,
        image_name=args.name,
        output_dir=args.output_dir,
        timeout=args.timeout,
        reference_image=args.reference_image,
        allow_paid_api=args.allow_paid_api,
        exact_filename=args.exact_name,
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
