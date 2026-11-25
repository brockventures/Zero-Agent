#!/usr/bin/env python3
"""Zero Doorbell Smart Face-Picker Daemon.

Persistent Home Assistant WebSocket daemon that intercepts Nest doorbell chime
events (`event.doorbell_chime`), polls for the finalized MP4 event clip,
extracts all frames with ffmpeg, scores each frame locally with UltraFace
RFB-320 ONNX + Laplacian sharpness, uploads the clearest frame to Home Assistant
`/config/www/doorbell_best.jpg`, and silently updates mobile doorbell
notifications in-place (`tag='doorbell-ring'`, `alert_once=True`).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
from PIL import Image

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = "/workspace"
for _p in (REPO_ROOT, WORKSPACE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
    from tools.daemon_singleton import (
        acquire_singleton_lock,
        is_lock_held,
        reconcile_live_daemon_pid,
        release_singleton_lock,
        terminate_duplicate_daemons,
    )
except ImportError:
    import daemon_singleton as _ds
    import nas_config  # type: ignore[no-redef]

    acquire_singleton_lock = _ds.acquire_singleton_lock
    is_lock_held = _ds.is_lock_held
    reconcile_live_daemon_pid = _ds.reconcile_live_daemon_pid
    release_singleton_lock = _ds.release_singleton_lock
    terminate_duplicate_daemons = _ds.terminate_duplicate_daemons

STATE_DIR = os.environ.get("DATA_DIR", "/workspace/data")
DEFAULT_MODEL_PATH = os.environ.get(
    "ULTRAFACE_MODEL_PATH",
    os.path.join(STATE_DIR, "models", "ultraface_rfb_320.onnx"),
)
LOG_FILE = os.environ.get(
    "DOORBELL_FACE_PICKER_LOG",
    os.path.join(STATE_DIR, "doorbell_face_picker.log"),
)
PID_FILE = os.path.join(STATE_DIR, "doorbell_face_picker.pid")
LOCK_FILE = os.path.join(STATE_DIR, "doorbell_face_picker.lock")

SCRIPT_NAME = "doorbell_face_picker.py"
DOORBELL_ENTITY_ID = "event.doorbell_chime"
DEFAULT_NOTIFY_TARGETS: tuple[str, ...] = (
    "notify.mobile_app_rqb_pixel_11_xl",
    "notify.mobile_app_pixel_9_pro_ecab",
)
NOTIFICATION_TAG = "doorbell-ring"
NOTIFICATION_CHANNEL = "Doorbell"
NOTIFICATION_CLICK_ACTION = "/lovelace/entryway"
REMOTE_BEST_JPG_CMD = (
    "docker exec -i home-assistant sh -c "
    "'mkdir -p /config/www && cat > /config/www/doorbell_best.jpg'"
)

_running = True


def log(msg: str) -> None:
    """Write a timestamped line to stdout and the daemon log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_ha_config(env_path: str = "/secrets/env.json") -> tuple[str, str]:
    """Load HA_BASE_URL and HA_ACCESS_TOKEN from /secrets/env.json with env overrides."""
    file_data: dict[str, Any] = {}
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    file_data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    ha_url = (
        os.environ.get("HA_BASE_URL")
        or str(file_data.get("HA_BASE_URL") or "")
    ).strip().rstrip("/")
    ha_token = (
        os.environ.get("HA_ACCESS_TOKEN")
        or str(file_data.get("HA_ACCESS_TOKEN") or "")
    ).strip()
    return ha_url, ha_token


def build_ws_url(ha_url: str) -> str:
    """Convert an HTTP(S) Home Assistant base URL into its /api/websocket URL."""
    base = ha_url.strip().rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    elif base.startswith(("ws://", "wss://")):
        ws_base = base
    else:
        ws_base = "ws://" + base
    return f"{ws_base}/api/websocket"


def compute_laplacian_sharpness(gray_arr: np.ndarray) -> float:
    """Compute the variance of the 2D discrete Laplacian of a grayscale image array."""
    arr = np.asarray(gray_arr, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0

    lap = (
        arr[:-2, 1:-1]
        + arr[2:, 1:-1]
        + arr[1:-1, :-2]
        + arr[1:-1, 2:]
        - 4.0 * arr[1:-1, 1:-1]
    )
    return float(np.var(lap))


def score_face_frame(
    conf: float,
    box: Sequence[float],
    face_sharpness: float,
) -> float:
    """Score a frame's detected face by confidence, box area, sharpness, and edge clipping.

    Returns 0.0 if conf < 0.65 or normalized box area < 0.005.
    Applies a 0.65x penalty if any box edge is clipped near the frame boundary
    (x1 < 0.02 or x2 > 0.98 or y1 < 0.02 or y2 > 0.98).
    """
    if conf < 0.65 or not box or len(box) < 4:
        return 0.0

    x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
    width = max(0.0, x2 - x1)
    height = max(0.0, y2 - y1)
    area = width * height
    if area < 0.005:
        return 0.0

    sharp_factor = math.log1p(max(0.0, float(face_sharpness)))
    score = float(conf) * math.sqrt(area) * sharp_factor
    if x1 < 0.02 or x2 > 0.98 or y1 < 0.02 or y2 > 0.98:
        score *= 0.65
    return float(score)


class UltraFaceDetector:
    """Local ONNX Runtime wrapper for UltraFace RFB-320 face detection & sharpness scoring."""

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        session: Any = None,
    ) -> None:
        self.model_path = model_path
        if session is not None:
            self.session = session
        else:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            opts.intra_op_num_threads = 2
            self.session = ort.InferenceSession(
                model_path,
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        self.input_name = self.session.get_inputs()[0].name

    @staticmethod
    def _crop_face_sharpness(gray_full: np.ndarray, box: Sequence[float]) -> float:
        h, w = gray_full.shape[:2]
        x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        px1 = max(0, min(w, round(max(0.0, x1) * w)))
        py1 = max(0, min(h, round(max(0.0, y1) * h)))
        px2 = max(0, min(w, round(min(1.0, x2) * w)))
        py2 = max(0, min(h, round(min(1.0, y2) * h)))
        if px2 - px1 < 3 or py2 - py1 < 3:
            return 0.0
        return compute_laplacian_sharpness(gray_full[py1:py2, px1:px2])

    def detect(
        self,
        image_or_path: Image.Image | str | os.PathLike[str],
    ) -> tuple[float, list[float], float, float]:
        """Run UltraFace RFB-320 inference on a PIL Image (or path).

        Returns:
            (best_conf, best_box, face_sharpness, full_sharpness)
        """
        if isinstance(image_or_path, (str, os.PathLike)):
            with Image.open(image_or_path) as loaded_img:
                rgb_img = loaded_img.convert("RGB")
        else:
            rgb_img = image_or_path.convert("RGB")

        gray_full = np.asarray(rgb_img.convert("L"), dtype=np.float32)
        full_sharpness = compute_laplacian_sharpness(gray_full)

        resized = rgb_img.resize((320, 240), Image.Resampling.BILINEAR)
        rgb_arr = np.asarray(resized, dtype=np.float32)
        norm_arr = (rgb_arr - 127.0) / 128.0
        input_tensor = np.transpose(norm_arr, (2, 0, 1))[np.newaxis, ...].astype(
            np.float32
        )

        outputs = self.session.run(None, {self.input_name: input_tensor})
        confs = np.asarray(outputs[0], dtype=np.float32)
        boxes = np.asarray(outputs[1], dtype=np.float32)

        face_probs = confs[0, :, 1]
        raw_boxes = boxes[0, :, :]
        if face_probs.size == 0:
            return 0.0, [0.0, 0.0, 0.0, 0.0], 0.0, full_sharpness

        passing_indices = np.where(face_probs >= 0.65)[0]
        if passing_indices.size > 0:
            # Evaluate top candidate anchors by confidence and pick highest composite score
            sorted_passing = passing_indices[
                np.argsort(face_probs[passing_indices])[::-1]
            ][:10]
            best_conf = 0.0
            best_box = [0.0, 0.0, 0.0, 0.0]
            best_face_sharp = 0.0
            best_composite = -1.0
            for idx in sorted_passing:
                c = float(face_probs[int(idx)])
                b = [float(v) for v in raw_boxes[int(idx), :4]]
                f_sharp = self._crop_face_sharpness(gray_full, b)
                comp = score_face_frame(c, b, f_sharp)
                if comp > best_composite or (
                    comp == best_composite and c > best_conf
                ):
                    best_composite = comp
                    best_conf = c
                    best_box = b
                    best_face_sharp = f_sharp
            return best_conf, best_box, best_face_sharp, full_sharpness

        best_idx = int(np.argmax(face_probs))
        best_conf = float(face_probs[best_idx])
        best_box = [float(v) for v in raw_boxes[best_idx, :4]]
        best_face_sharp = self._crop_face_sharpness(gray_full, best_box)
        return best_conf, best_box, best_face_sharp, full_sharpness

    analyze_frame = detect
    __call__ = detect


def select_best_frame(
    frame_paths: list[str],
    detector: UltraFaceDetector,
) -> tuple[str, dict[str, Any]]:
    """Select the highest-scoring face frame, or fall back to the sharpest frame in the middle 60%."""
    if not frame_paths:
        raise ValueError("frame_paths must not be empty")

    records: list[dict[str, Any]] = []
    for idx, path in enumerate(frame_paths):
        if os.path.exists(path):
            with Image.open(path) as img:
                img.load()
                conf, box, face_sharpness, full_sharpness = detector.detect(img)
        else:
            conf, box, face_sharpness, full_sharpness = detector.detect(path)

        face_score = score_face_frame(conf, box, face_sharpness)
        records.append(
            {
                "path": path,
                "frame_index": idx,
                "conf": float(conf),
                "box": [float(v) for v in box] if box else [0.0, 0.0, 0.0, 0.0],
                "face_sharpness": float(face_sharpness),
                "full_sharpness": float(full_sharpness),
                "face_score": float(face_score),
            }
        )

    best_face = max(records, key=lambda r: (r["face_score"], r["full_sharpness"]))
    if best_face["face_score"] > 0.0:
        meta = {
            **best_face,
            "mode": "face",
            "face_detected": True,
            "score": best_face["face_score"],
            "total_frames": len(frame_paths),
        }
        return str(best_face["path"]), meta

    n = len(records)
    if n < 3:
        candidates = records
    else:
        trim = max(1, round(n * 0.2))
        candidates = records[trim : n - trim]

    best_sharp = max(candidates, key=lambda r: r["full_sharpness"])
    meta = {
        **best_sharp,
        "mode": "sharpness_fallback",
        "face_detected": False,
        "score": 0.0,
        "total_frames": len(frame_paths),
    }
    return str(best_sharp["path"]), meta


def render_notification_jpeg(
    frame_path: str,
    meta: dict[str, Any],
    quality: int = 90,
) -> bytes:
    """Smart-crop the winning frame to 16:9 anchored on the face for Android BigPictureStyle."""
    raw_bytes = Path(frame_path).read_bytes()
    try:
        with Image.open(io.BytesIO(raw_bytes)) as loaded:
            img = loaded.convert("RGB")
    except Exception:  # noqa: BLE001
        return raw_bytes

    w, h = img.size
    target_h = round(w * 9 / 16)
    if target_h >= h or target_h < 16:
        return raw_bytes

    box = meta.get("box")
    if meta.get("face_detected") and isinstance(box, Sequence) and len(box) >= 4:
        face_cy = ((float(box[1]) + float(box[3])) / 2.0) * h
        top = round(face_cy - target_h * 0.42)
    else:
        top = round((h - target_h) * 0.35)
    top = max(0, min(top, h - target_h))

    cropped = img.crop((0, top, w, top + target_h))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


async def resolve_doorbell_device_id(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    entity_id: str = DOORBELL_ENTITY_ID,
) -> str:
    """Resolve the Home Assistant device_id for event.doorbell_chime via POST /api/template."""
    url = f"{ha_url.rstrip('/')}/api/template"
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    payload = {"template": f"{{{{ device_id('{entity_id}') }}}}"}
    async with session.post(url, headers=headers, json=payload) as resp:
        body = (await resp.text()).strip()
        if resp.status != 200 or not body or body.lower() in ("none", "null", "unknown"):
            raise RuntimeError(
                f"Failed to resolve device_id for {entity_id}: HTTP {resp.status} ({body})"
            )
        return body


async def fetch_nest_clip_mp4(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    device_id: str,
    event_id: str,
    initial_delay: float = 1.0,
    poll_interval: float = 0.4,
    max_wait: float = 6.0,
) -> bytes:
    """Poll GET /api/nest/event_media/{device_id}/{event_id} after 1.0s every 0.4s up to 6.0s."""
    url = f"{ha_url.rstrip('/')}/api/nest/event_media/{device_id}/{event_id}"
    headers = {"Authorization": f"Bearer {ha_token}"}

    elapsed = 0.0
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
        elapsed += initial_delay

    while True:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.read()
                if data:
                    return data

        if elapsed + poll_interval > max_wait + 1e-6:
            break
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"Timed out after {max_wait:.1f}s polling Nest MP4 clip for event {event_id}"
    )


def extract_frames_from_mp4(mp4_bytes: bytes, output_dir: str) -> list[str]:
    """Write clip.mp4 into output_dir and extract frame_%02d.jpg via ffmpeg."""
    clip_path = os.path.join(output_dir, "clip.mp4")
    with open(clip_path, "wb") as f:
        f.write(mp4_bytes)

    frame_pattern = os.path.join(output_dir, "frame_%02d.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        clip_path,
        "-q:v",
        "2",
        frame_pattern,
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    if res.returncode != 0:
        stderr_text = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg frame extraction failed (rc={res.returncode}): {stderr_text}")

    frames = sorted(
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.startswith("frame_") and name.endswith(".jpg")
    )
    if not frames:
        raise RuntimeError("ffmpeg completed but extracted 0 frame_*.jpg files")
    return frames


def upload_best_frame_to_ha(
    jpeg_bytes: bytes,
    host: str | None = None,
    port: str | int | None = None,
    user: str | None = None,
    timeout: int = 15,
) -> bool:
    """Pipe winning JPEG bytes over SSH stdin into home-assistant /config/www/doorbell_best.jpg."""
    if not jpeg_bytes:
        raise ValueError("jpeg_bytes must not be empty")

    target_host = host or nas_config.HOST_1_IP
    target_port = str(port or nas_config.SSH_PORT)
    target_user = user or nas_config.SSH_USER

    cmd = [
        "ssh",
        "-p",
        target_port,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        f"{target_user}@{target_host}",
        REMOTE_BEST_JPG_CMD,
    ]
    res = subprocess.run(
        cmd,
        input=jpeg_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        stderr_text = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"SSH upload to Home Assistant failed (rc={res.returncode}): {stderr_text}"
        )
    return True


def build_notification_payload(
    image_url: str,
    title: str = "\U0001F514 Doorbell Ringing",
    message: str | None = None,
) -> dict[str, Any]:
    """Build the silent in-place Home Assistant mobile_app notification payload."""
    if message is None:
        ring_time = datetime.now().strftime("%-I:%M %p")
        message = f"Someone is at the front door ({ring_time})."
    return {
        "title": title,
        "message": message,
        "data": {
            "tag": NOTIFICATION_TAG,
            "alert_once": True,
            "ttl": 0,
            "priority": "high",
            "channel": NOTIFICATION_CHANNEL,
            "clickAction": NOTIFICATION_CLICK_ACTION,
            "image": image_url,
        },
    }


async def send_doorbell_notifications(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    image_url: str,
    targets: Sequence[str] = DEFAULT_NOTIFY_TARGETS,
    title: str = "\U0001F514 Doorbell Ringing",
    message: str | None = None,
) -> list[str]:
    """Send silent in-place doorbell notification updates to the configured mobile_app targets."""
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    payload = build_notification_payload(image_url, title=title, message=message)
    delivered: list[str] = []

    for target in targets:
        service_name = (
            target.split(".", 1)[1] if target.startswith("notify.") else target
        )
        url = f"{ha_url.rstrip('/')}/api/services/notify/{service_name}"
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    delivered.append(target)
                else:
                    body = await resp.text()
                    log(
                        f"Warning: notify.{service_name} returned HTTP {resp.status}: {body[:200]}"
                    )
        except Exception as exc:  # noqa: BLE001
            log(f"Warning: failed calling notify.{service_name}: {exc}")

    return delivered


async def process_doorbell_event(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    device_id: str,
    event_id: str,
    detector: UltraFaceDetector,
    targets: Sequence[str] = DEFAULT_NOTIFY_TARGETS,
    epoch_fn: Any = time.time,
) -> dict[str, Any]:
    """Fetch Nest MP4 clip, score frames, upload winning JPEG, and update notifications.

    Falls back cleanly to `/api/nest/event_media/<dev_id>/<event_id>/thumbnail` on any error.
    """
    fallback_url = f"/api/nest/event_media/{device_id}/{event_id}/thumbnail"
    image_url = fallback_url
    used_fallback = False
    meta: dict[str, Any] = {}

    t0 = time.monotonic()
    try:
        mp4_bytes = await fetch_nest_clip_mp4(
            session=session,
            ha_url=ha_url,
            ha_token=ha_token,
            device_id=device_id,
            event_id=event_id,
        )
        with tempfile.TemporaryDirectory(prefix="doorbell_face_") as tmpdir:
            frame_paths = extract_frames_from_mp4(mp4_bytes, tmpdir)
            best_path, meta = select_best_frame(frame_paths, detector)
            jpeg_bytes = render_notification_jpeg(best_path, meta)

        upload_best_frame_to_ha(jpeg_bytes)
        epoch = int(epoch_fn())
        image_url = f"/local/doorbell_best.jpg?t={epoch}"
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        log(
            f"Processed doorbell event {event_id} in {elapsed_ms:.0f}ms "
            f"(mode={meta.get('mode')}, frame={meta.get('frame_index')}/{meta.get('total_frames')}, "
            f"conf={meta.get('conf', 0.0):.2f}, score={meta.get('score', 0.0):.3f})"
        )
    except Exception as exc:  # noqa: BLE001
        used_fallback = True
        image_url = fallback_url
        log(
            f"Error processing clip for event {event_id}; falling back to thumbnail ({fallback_url}): {exc}"
        )

    delivered = await send_doorbell_notifications(
        session=session,
        ha_url=ha_url,
        ha_token=ha_token,
        image_url=image_url,
        targets=targets,
    )
    return {
        "event_id": event_id,
        "device_id": device_id,
        "image_url": image_url,
        "fallback": used_fallback,
        "notified": delivered,
        "meta": meta,
    }


def extract_doorbell_event_id(
    msg: dict[str, Any],
    seen_event_ids: set[str] | None = None,
) -> str | None:
    """Validate a WebSocket state_changed message for event.doorbell_chime and extract nest_event_id.

    Ignores events where old_state is missing or in ('unknown', 'unavailable') or where
    nest_event_id is missing/duplicate.
    """
    if not isinstance(msg, dict) or msg.get("type") != "event":
        return None

    event_obj = msg.get("event")
    if not isinstance(event_obj, dict):
        return None
    if event_obj.get("event_type") not in (None, "state_changed"):
        return None

    data = event_obj.get("data")
    if not isinstance(data, dict) or data.get("entity_id") != DOORBELL_ENTITY_ID:
        return None

    old_state = data.get("old_state")
    if old_state is None:
        return None
    if isinstance(old_state, dict):
        old_state_val = str(old_state.get("state") or "").strip().lower()
        old_attrs = old_state.get("attributes") or {}
        old_event_id = str(old_attrs.get("nest_event_id") or "").strip()
    else:
        old_state_val = str(old_state).strip().lower()
        old_event_id = ""

    if not old_state_val or old_state_val in ("unknown", "unavailable"):
        return None

    new_state = data.get("new_state")
    if not isinstance(new_state, dict):
        return None
    new_state_val = str(new_state.get("state") or "").strip().lower()
    if not new_state_val or new_state_val in ("unknown", "unavailable"):
        return None

    new_attrs = new_state.get("attributes") or {}
    nest_event_id = str(new_attrs.get("nest_event_id") or "").strip()
    if not nest_event_id:
        return None

    if old_event_id and old_event_id == nest_event_id:
        return None

    if seen_event_ids is not None:
        if nest_event_id in seen_event_ids:
            return None
        seen_event_ids.add(nest_event_id)

    return nest_event_id


async def listen_websocket_once(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    detector: UltraFaceDetector,
    seen_event_ids: set[str],
    device_id: str | None = None,
    targets: Sequence[str] = DEFAULT_NOTIFY_TARGETS,
) -> str | None:
    """Connect to HA /api/websocket, authenticate, subscribe to state_changed, and process events."""
    ws_url = build_ws_url(ha_url)
    resolved_device_id = device_id
    if not resolved_device_id:
        try:
            resolved_device_id = await resolve_doorbell_device_id(
                session, ha_url, ha_token
            )
            log(f"Resolved {DOORBELL_ENTITY_ID} device_id: {resolved_device_id}")
        except Exception as exc:  # noqa: BLE001
            log(f"Deferred device_id resolution until chime event: {exc}")

    async with session.ws_connect(ws_url, heartbeat=30) as ws:
        first_msg = await ws.receive_json()
        if first_msg.get("type") == "auth_required":
            await ws.send_json({"type": "auth", "access_token": ha_token})
            auth_resp = await ws.receive_json()
        else:
            auth_resp = first_msg

        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant WebSocket authentication failed: {auth_resp}")

        await ws.send_json(
            {
                "id": 1,
                "type": "subscribe_events",
                "event_type": "state_changed",
            }
        )
        log(f"Connected and subscribed to state_changed on {ws_url}")

        async for ws_msg in ws:
            if not _running:
                break
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(ws_msg.data)
                except json.JSONDecodeError:
                    continue

                event_id = extract_doorbell_event_id(
                    payload, seen_event_ids=seen_event_ids
                )
                if not event_id:
                    continue

                if not resolved_device_id:
                    resolved_device_id = await resolve_doorbell_device_id(
                        session, ha_url, ha_token
                    )

                log(f"Intercepted doorbell chime nest_event_id={event_id}")
                await process_doorbell_event(
                    session=session,
                    ha_url=ha_url,
                    ha_token=ha_token,
                    device_id=resolved_device_id,
                    event_id=event_id,
                    detector=detector,
                    targets=targets,
                )
            elif ws_msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                break

    return resolved_device_id


async def async_daemon_loop() -> None:
    """Persistent WebSocket reconnect loop with exponential backoff."""
    ha_url, ha_token = load_ha_config()
    if not ha_url or not ha_token:
        log("ERROR: HA_BASE_URL or HA_ACCESS_TOKEN missing from environment / /secrets/env.json.")
        return

    detector = UltraFaceDetector()
    seen_event_ids: set[str] = set()
    cached_device_id: str | None = None
    backoff = 1.0

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while _running:
            conn_start = time.monotonic()
            try:
                cached_device_id = await listen_websocket_once(
                    session=session,
                    ha_url=ha_url,
                    ha_token=ha_token,
                    detector=detector,
                    seen_event_ids=seen_event_ids,
                    device_id=cached_device_id,
                )
                if time.monotonic() - conn_start > 10.0:
                    backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if not _running:
                    break
                log(f"WebSocket connection error (reconnecting in {backoff:.1f}s): {exc}")

            if not _running:
                break
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2.0)


def _handle_exit_signal(signum: int, _frame: Any) -> None:
    global _running
    log(f"Received signal {signum}, stopping Doorbell Face-Picker daemon cleanly...")
    _running = False


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        cmdline_path = f"/proc/{pid}/cmdline"
        status_path = f"/proc/{pid}/status"
        if os.path.exists(cmdline_path) and os.path.exists(status_path):
            with open(cmdline_path, "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            with open(status_path, encoding="utf-8", errors="ignore") as f:
                tgid_lines = [line.split()[1] for line in f if line.startswith("Tgid:")]
            if (tgid_lines and int(tgid_lines[0]) != pid) or "doorbell_face_picker" not in cmdline:
                return False
        return True
    except OSError:
        return False


def get_listener_pid() -> int | None:
    return reconcile_live_daemon_pid(
        pid_file=PID_FILE,
        lock_path=LOCK_FILE,
        script_name=SCRIPT_NAME,
        subcommands=("run",),
    )


def get_status() -> dict[str, Any]:
    pid = get_listener_pid()
    return {
        "running": pid is not None,
        "pid": pid,
        "model_path": DEFAULT_MODEL_PATH,
        "log_file": LOG_FILE,
        "targets": list(DEFAULT_NOTIFY_TARGETS),
    }


def run_loop() -> None:
    """Entry point for the foreground singleton daemon worker (`run` subcommand)."""
    global _running
    _running = True
    lock_handle = acquire_singleton_lock(LOCK_FILE)
    if lock_handle is None:
        log("Doorbell face-picker daemon already running (lock held). Exiting.")
        return

    signal.signal(signal.SIGINT, _handle_exit_signal)
    signal.signal(signal.SIGTERM, _handle_exit_signal)

    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        log(f"Failed writing PID file {PID_FILE}: {exc}")
        release_singleton_lock(lock_handle)
        return

    log(f"Starting Doorbell Face-Picker daemon (PID: {os.getpid()})...")
    try:
        asyncio.run(async_daemon_loop())
    finally:
        try:
            if os.path.exists(PID_FILE):
                os.unlink(PID_FILE)
        except OSError:
            pass
        release_singleton_lock(lock_handle)
        log("Zero Doorbell Face-Picker daemon stopped cleanly.")


def start_daemon_background() -> bool:
    pid = get_listener_pid()
    if pid or is_lock_held(LOCK_FILE):
        terminate_duplicate_daemons(SCRIPT_NAME, ("run",), keep_pid=pid)
        log(f"Doorbell face-picker daemon already running (PID {pid}).")
        return True

    terminate_duplicate_daemons(SCRIPT_NAME, ("run",))
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "run"],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=WORKSPACE if os.path.exists(WORKSPACE) else None,
        )

    for _ in range(20):
        if proc.poll() is not None:
            print(
                f"[Doorbell Face-Picker] ERROR: Process exited prematurely with code {proc.returncode}.",
                file=sys.stderr,
            )
            return False
        if get_listener_pid() is not None:
            print(
                f"[Doorbell Face-Picker] Successfully started daemon (PID {proc.pid})."
            )
            return True
        time.sleep(0.1)

    print(f"[Doorbell Face-Picker] Spawned process (PID {proc.pid}).")
    return True


def stop_daemon() -> bool:
    pid = get_listener_pid()
    if not pid:
        terminate_duplicate_daemons(SCRIPT_NAME, ("run",))
        print("[Doorbell Face-Picker] Daemon is not running.")
        return True

    print(f"[Doorbell Face-Picker] Stopping daemon (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            if not is_process_running(pid):
                break
            time.sleep(0.1)
        if is_process_running(pid):
            print(
                f"[Doorbell Face-Picker] Process {pid} did not exit gracefully, sending SIGKILL..."
            )
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
    except OSError as exc:
        print(f"[Doorbell Face-Picker] Error while stopping PID {pid}: {exc}")

    terminate_duplicate_daemons(SCRIPT_NAME, ("run",), keep_pid=None)
    try:
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
    except OSError:
        pass
    print("[Doorbell Face-Picker] Stopped.")
    return True


async def run_test_clip_async(
    event_id: str,
    target: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Execute a one-shot clip test for a given Nest event_id and target notify service."""
    ha_url, ha_token = load_ha_config()
    if not ha_url or not ha_token:
        raise RuntimeError("HA_BASE_URL and HA_ACCESS_TOKEN are required for test-clip")

    targets: Sequence[str] = (target,) if target else DEFAULT_NOTIFY_TARGETS
    detector = UltraFaceDetector()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        resolved_dev_id = device_id or await resolve_doorbell_device_id(
            session, ha_url, ha_token
        )
        return await process_doorbell_event(
            session=session,
            ha_url=ha_url,
            ha_token=ha_token,
            device_id=resolved_dev_id,
            event_id=event_id,
            detector=detector,
            targets=targets,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv if argv is not None else sys.argv[1:])
    cmd = args_list[0].lower() if args_list else "status"

    if cmd == "run":
        run_loop()
        return 0
    if cmd == "start":
        return 0 if start_daemon_background() else 1
    if cmd == "stop":
        return 0 if stop_daemon() else 1
    if cmd == "restart":
        stop_daemon()
        time.sleep(0.5)
        return 0 if start_daemon_background() else 1
    if cmd == "status":
        st = get_status()
        status_str = "🟢 RUNNING" if st["running"] else "🔴 STOPPED"
        print(f"**Zero Doorbell Face-Picker**: {status_str}")
        if st["pid"]:
            print(f"- **PID**: `{st['pid']}`")
        print(f"- **Model Path**: `{st['model_path']}`")
        print(f"- **Log File**: `{st['log_file']}`")
        return 0
    if cmd == "json":
        print(json.dumps(get_status(), indent=2))
        return 0
    if cmd == "test-clip":
        parser = argparse.ArgumentParser(prog="doorbell_face_picker.py test-clip")
        parser.add_argument("--event-id", required=True, help="Nest doorbell event_id")
        parser.add_argument(
            "--target",
            default=None,
            help="Optional single notify service (e.g. notify.mobile_app_rqb_pixel_11_xl)",
        )
        parser.add_argument(
            "--device-id",
            default=None,
            help="Optional Home Assistant device_id override",
        )
        parsed = parser.parse_args(args_list[1:])
        result = asyncio.run(
            run_test_clip_async(
                event_id=parsed.event_id,
                target=parsed.target,
                device_id=parsed.device_id,
            )
        )
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"Usage: {sys.argv[0]} [run|start|stop|restart|status|json|test-clip --event-id <id> --target <service>]"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
