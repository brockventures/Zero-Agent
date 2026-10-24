#!/usr/bin/env python3
"""
Remote IP Webcam Control CLI (Pixel 8 Pro / Android 14)
Controls IP Webcam server headlessly over ADB (Host 2 :5037 -> 36261FDJG0003L).
Supports single snapshots and multi-frame burst capture with automatic
toolhead occlusion scoring and XY phase-locked bed/toolhead alignment.
"""
import sys
import os
import time
import math
import subprocess
import urllib.request
import json
import argparse
from typing import Tuple, Optional, Dict, Any
from PIL import Image
import io
import numpy as np

ADB_BIN = "/workspace/tools/bin/adb"
if not os.path.exists(ADB_BIN):
    ADB_BIN = "/workspace/scratch/platform-tools/platform-tools/adb"

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

USB_SERIAL = "36261FDJG0003L"
import nas_config
HOST2_IP = nas_config.HOST_2_IP
WIFI_DEVICE = os.environ.get("WEBCAM_WIFI_ADB", "").strip()
if not WIFI_DEVICE and os.path.exists("/secrets/env.json"):
    try:
        with open("/secrets/env.json", encoding="utf-8") as _f:
            WIFI_DEVICE = str(json.load(_f).get("WEBCAM_WIFI_ADB", "")).strip()
    except Exception:
        pass

# Model and bed coordinate anchors for 1080x1920 portrait frames
MODEL_CENTER_X = 540
MODEL_CENTER_Y = 1120
DEFAULT_TARGET_BED_Y = 1385.0
DEFAULT_TARGET_HEAD_X = 370.0
BED_RAIL_DX_DY = -0.35


def get_adb_context(ensure_forward: bool = True):
    """
    Auto-detect active transport:
    1. Direct USB via Host 2 ADB server (36261FDJG0003L on Host 2 :5037)
    2. Any currently connected Wi-Fi ADB device from `adb devices`
    3. Fallback to Wi-Fi ADB (WEBCAM_WIFI_ADB)
    """
    try:
        res = subprocess.run([ADB_BIN, "-H", HOST2_IP, "-P", "5037", "devices"], capture_output=True, text=True, timeout=2)
        if f"{USB_SERIAL}\tdevice" in res.stdout:
            if ensure_forward:
                subprocess.run([ADB_BIN, "-H", HOST2_IP, "-P", "5037", "-s", USB_SERIAL, "forward", "tcp:3333", "tcp:3333"], capture_output=True, timeout=2)
            return [ADB_BIN, "-H", HOST2_IP, "-P", "5037", "-s", USB_SERIAL], f"http://{HOST2_IP}:3333", "usb"
    except Exception:
        pass

    # Check already connected adb devices
    try:
        res = subprocess.run([ADB_BIN, "devices"], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            if "\tdevice" in line:
                dev = line.split("\t")[0].strip()
                dev_ip = dev.split(":")[0] if ":" in dev else dev
                if ensure_forward:
                    subprocess.run([ADB_BIN, "-s", dev, "forward", "tcp:3333", "tcp:3333"], capture_output=True, timeout=2)
                return [ADB_BIN, "-s", dev], f"http://{dev_ip}:3333", "wifi"
    except Exception:
        pass

    # Fallback to Wi-Fi if configured, otherwise Host 2 USB
    if WIFI_DEVICE:
        try:
            subprocess.run([ADB_BIN, "connect", WIFI_DEVICE], capture_output=True, timeout=3)
            if ensure_forward:
                subprocess.run([ADB_BIN, "-s", WIFI_DEVICE, "forward", "tcp:3333", "tcp:3333"], capture_output=True, timeout=3)
        except Exception:
            pass
        dev_ip = WIFI_DEVICE.split(":")[0]
        return [ADB_BIN, "-s", WIFI_DEVICE], f"http://{dev_ip}:3333", "wifi"

    return [ADB_BIN, "-H", HOST2_IP, "-P", "5037", "-s", USB_SERIAL], f"http://{HOST2_IP}:3333", "usb"


def is_port_open():
    """Verify if IP Webcam HTTP endpoint is actively responding."""
    _, web_url, _ = get_adb_context(ensure_forward=False)
    try:
        req = urllib.request.Request(f"{web_url}/status.json")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
            # Check if video acquisition is active
            curvals = data.get("curvals", {})
            return curvals.get("video_acquisition") == "on" or resp.status == 200
    except Exception:
        return False


DEFAULT_FOCUS_DISTANCE = 2.30  # Diopters (~43.5 cm from lens to PEI build plate)


def configure_camera_focus(focus_distance: float = DEFAULT_FOCUS_DISTANCE, settle_time: float = 2.0) -> bool:
    """Lock manual focus to the 3D printer build plate distance (~43.5 cm / 2.30 diopters)."""
    _, web_url, _ = get_adb_context(ensure_forward=False)
    try:
        # Check current status first to avoid redundant settling sleep if already configured
        try:
            req_stat = urllib.request.Request(f"{web_url}/status.json")
            with urllib.request.urlopen(req_stat, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                cur = data.get("curvals", {})
                cur_mode = cur.get("focusmode")
                cur_dist = float(cur.get("focus_distance", -1.0))
                if cur_mode == "off" and abs(cur_dist - focus_distance) < 0.05:
                    return True
        except Exception:
            pass

        # Disable continuous video auto-focus
        req1 = urllib.request.Request(f"{web_url}/settings/focusmode?set=off", data=b"", method="POST")
        urllib.request.urlopen(req1, timeout=3)
        # Lock calibrated focal distance
        req2 = urllib.request.Request(f"{web_url}/settings/focus_distance?set={focus_distance:.2f}", data=b"", method="POST")
        urllib.request.urlopen(req2, timeout=3)
        if settle_time > 0:
            time.sleep(settle_time)
        return True
    except Exception as e:
        print(f"Warning: Failed to lock camera focus: {e}")
        return False


def start_camera():
    adb_cmd, web_url, transport = get_adb_context(ensure_forward=True)
    if is_port_open():
        configure_camera_focus()
        print(f"Webcam is already running on {web_url} (transport: {transport}, focus locked to build plate).")
        return True

    # Wake and unlock
    subprocess.run(adb_cmd + ["shell", "input keyevent KEYCODE_WAKEUP"], capture_output=True)
    subprocess.run(adb_cmd + ["shell", "wm dismiss-keyguard"], capture_output=True)

    # Launch Configuration & tap Start Server
    subprocess.run(adb_cmd + ["shell", "am start -n com.pas.webcam/.Configuration"], capture_output=True)
    time.sleep(1.2)
    # Scroll and tap bottom button (Start Server)
    subprocess.run(adb_cmd + ["shell", "input swipe 500 2000 500 200 300"], capture_output=True)
    time.sleep(0.8)
    subprocess.run(adb_cmd + ["shell", "input tap 254 1914"], capture_output=True)
    time.sleep(2.0)

    # Dim screen to 0 to preserve OLED display
    subprocess.run(adb_cmd + ["shell", "settings put system screen_brightness_mode 0"], capture_output=True)
    subprocess.run(adb_cmd + ["shell", "settings put system screen_brightness 0"], capture_output=True)

    # Re-verify port
    for _ in range(5):
        if is_port_open():
            configure_camera_focus()
            print(f"Webcam successfully started on {web_url} (transport: {transport}, focus locked to build plate)")
            return True
        time.sleep(0.6)

    print(f"Failed to verify {web_url} after start.")
    return False


def stop_camera():
    adb_cmd, _, transport = get_adb_context(ensure_forward=False)
    # Stop foreground service and force stop app
    subprocess.run(adb_cmd + ["shell", "am stopservice -n com.pas.webcam/.WebServer"], capture_output=True)
    subprocess.run(adb_cmd + ["shell", "am force-stop com.pas.webcam"], capture_output=True)
    # Remove port forward
    subprocess.run(adb_cmd + ["forward", "--remove", "tcp:3333"], capture_output=True)
    # Put screen to sleep
    subprocess.run(adb_cmd + ["shell", "input keyevent 26"], capture_output=True)
    time.sleep(0.5)
    print(f"Webcam stopped and device put to sleep (transport: {transport}). Port 3333 closed.")
    return True


def detect_bed_and_toolhead_pose(img: Image.Image) -> Dict[str, Any]:
    """
    Detect 2D bed and toolhead fiducials on a portrait camera frame.
    All coordinate slicing scales by sx = w / 1080.0 and sy = h / 1920.0,
    and returns positions normalized to the 1080x1920 reference space.
    """
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    sx = w / 1080.0
    sy = h / 1920.0

    # 1. Front-right PEI lip step-edge detection (x=520..850, y=1220..1760)
    # Golden PEI sheet (luma > 75) drops to black base shadow (luma < 75)
    x1_pei = max(0, min(w - 1, int(round(520 * sx))))
    x2_pei = max(x1_pei + 1, min(w, int(round(850 * sx))))
    y1_pei = max(0, min(h - 1, int(round(1220 * sy))))
    y2_pei = max(y1_pei + 1, min(h, int(round(1760 * sy))))

    edge_found = False
    edge_y = DEFAULT_TARGET_BED_Y

    if x2_pei > x1_pei and y2_pei > y1_pei:
        pei_roi = arr[y1_pei:y2_pei, x1_pei:x2_pei]
        luma = 0.299 * pei_roi[:, :, 0] + 0.587 * pei_roi[:, :, 1] + 0.114 * pei_roi[:, :, 2]

        column_edges = []
        for c in range(luma.shape[1]):
            col = luma[:, c]
            valid = (col[:-1] > 75) & (col[1:] < 75)
            if np.any(valid):
                drops = col[:-1] - col[1:]
                idx = np.where(valid)[0]
                best_r = idx[np.argmax(drops[idx])]
                column_edges.append(best_r + 1)

        min_cols = max(3, int(0.02 * (x2_pei - x1_pei)))
        if len(column_edges) >= min_cols:
            detected_y_px = y1_pei + float(np.median(column_edges))
            edge_y = detected_y_px / sy
            edge_found = True
        else:
            row_luma = luma.mean(axis=1)
            row_valid = (row_luma[:-1] > 75) & (row_luma[1:] < 75)
            if np.any(row_valid):
                row_drops = row_luma[:-1] - row_luma[1:]
                idx = np.where(row_valid)[0]
                best_r = idx[np.argmax(row_drops[idx])]
                edge_y = (y1_pei + best_r + 1) / sy
                edge_found = True

    # 2. White COMGROW silkscreen centroid detection in x=20..490 above the lip
    # Neutral white pixels: min(R,G,B) > 170 and max(R,G,B) - min(R,G,B) < 40
    # Mask out desk objects below edge_y
    x1_cg = max(0, min(w - 1, int(round(20 * sx))))
    x2_cg = max(x1_cg + 1, min(w, int(round(490 * sx))))
    edge_y_px = int(round(edge_y * sy))
    y2_cg = max(0, min(h, edge_y_px))
    y1_cg = max(0, min(int(round(900 * sy)), max(0, y2_cg - 1)))

    logo_px = 0
    logo_x: Optional[float] = None
    logo_y: Optional[float] = None

    if x2_cg > x1_cg and y2_cg > y1_cg:
        cg_roi = arr[y1_cg:y2_cg, x1_cg:x2_cg]
        r = cg_roi[:, :, 0].astype(int)
        g = cg_roi[:, :, 1].astype(int)
        b = cg_roi[:, :, 2].astype(int)
        min_rgb = np.minimum(np.minimum(r, g), b)
        max_rgb = np.maximum(np.maximum(r, g), b)
        logo_mask = (min_rgb > 170) & ((max_rgb - min_rgb) < 40)
        raw_logo_px = int(np.sum(logo_mask))
        if raw_logo_px > 0:
            coords = np.argwhere(logo_mask)
            logo_y_px = float(np.mean(coords[:, 0])) + y1_cg
            logo_x_px = float(np.mean(coords[:, 1])) + x1_cg
            logo_x = logo_x_px / sx
            logo_y = logo_y_px / sy
            scale_factor = sx * sy
            logo_px = int(round(raw_logo_px / scale_factor)) if scale_factor > 0 else raw_logo_px

    # Resolve bed_y and bed_x
    if edge_found:
        bed_y = edge_y
    elif logo_y is not None:
        bed_y = logo_y
    else:
        bed_y = DEFAULT_TARGET_BED_Y

    if logo_x is not None:
        bed_x = logo_x
    else:
        bed_x = 255.0 + BED_RAIL_DX_DY * (bed_y - DEFAULT_TARGET_BED_Y)

    # 3. Toolhead centroid detection in gantry motion zone (x=80..930, y=720..1250)
    th_x1 = max(0, min(w - 1, int(round(80 * sx))))
    th_x2 = max(th_x1 + 1, min(w, int(round(930 * sx))))
    th_y1 = max(0, min(h - 1, int(round(720 * sy))))
    th_y2 = max(th_y1 + 1, min(h, int(round(1250 * sy))))

    zone = arr[th_y1:th_y2, th_x1:th_x2]
    dark_mask = (zone[:, :, 0] < 50) & (zone[:, :, 1] < 50) & (zone[:, :, 2] < 50)
    th_coords = np.argwhere(dark_mask)
    min_dark_count = max(20, int(round(500 * (sx * sy))))

    if len(th_coords) >= min_dark_count:
        head_y_px = float(np.mean(th_coords[:, 0])) + th_y1
        head_x_px = float(np.mean(th_coords[:, 1])) + th_x1
        head_x = head_x_px / sx
        head_y = head_y_px / sy
        dist = math.hypot(head_x - MODEL_CENTER_X, head_y - MODEL_CENTER_Y)
        is_behind = head_y < MODEL_CENTER_Y
    else:
        head_x, head_y = 0.0, 0.0
        dist = 0.0
        is_behind = False

    head_pos = (round(head_x, 1), round(head_y, 1))

    # 4. Model zone: center-lower bed region where figurine base sits (x=250..850, y=900..1350)
    m_x1 = max(0, min(w - 1, int(round(250 * sx))))
    m_x2 = max(m_x1 + 1, min(w, int(round(850 * sx))))
    m_y1 = max(0, min(h - 1, int(round(900 * sy))))
    m_y2 = max(m_y1 + 1, min(h, int(round(1350 * sy))))

    roi_model = arr[m_y1:m_y2, m_x1:m_x2]
    raw_white_px = int(np.sum((roi_model[:, :, 0] > 160) & (roi_model[:, :, 1] > 160) & (roi_model[:, :, 2] > 160)))
    raw_shroud_px = int(np.sum((roi_model[:, :, 0] < 55) & (roi_model[:, :, 1] < 55) & (roi_model[:, :, 2] < 55)))
    scale_factor = sx * sy
    if scale_factor > 0 and abs(scale_factor - 1.0) > 0.01:
        white_px = int(round(raw_white_px / scale_factor))
        shroud_px = int(round(raw_shroud_px / scale_factor))
    else:
        white_px = raw_white_px
        shroud_px = raw_shroud_px

    return {
        "bed_x": float(bed_x),
        "bed_y": float(bed_y),
        "edge_y": float(edge_y),
        "logo_px": int(logo_px),
        "head_pos": head_pos,
        "white_px": int(white_px),
        "shroud_px": int(shroud_px),
        "dist": round(dist, 1),
        "is_behind": is_behind,
    }


def score_burst_frame(
    img: Image.Image,
    target_bed_y: float = DEFAULT_TARGET_BED_Y,
    target_head_x: float = DEFAULT_TARGET_HEAD_X,
) -> Tuple[float, dict]:
    """
    Score a portrait print bed frame for visibility of the printed model and XY phase alignment.
    Higher score = cleaner view with toolhead and bed in canonical presentation pose.
    """
    pose = detect_bed_and_toolhead_pose(img)

    white_px = pose["white_px"]
    shroud_px = pose["shroud_px"]
    dist = pose["dist"]
    is_behind = pose["is_behind"]
    head_x, head_y = pose["head_pos"]
    bed_x = pose["bed_x"]
    bed_y = pose["bed_y"]

    bed_y_err = abs(bed_y - target_bed_y)
    head_x_err = abs(head_x - target_head_x)
    xy_pose_penalty = (bed_y_err * 150.0) + (head_x_err * 55.0)

    # Composite score:
    # + maximize visible printed model (white_px)
    # - strongly penalize black shroud blocking model (shroud_px * 1.5)
    # + reward distance from center (dist * 40)
    # + bonus if toolhead is behind model center (further back on bed)
    # - penalize out-of-phase bed Y and toolhead X position
    behind_bonus = 2000.0 if is_behind else -2000.0
    score = float(white_px - (1.5 * shroud_px) + (dist * 40.0) + behind_bonus - xy_pose_penalty)

    details = {
        "score": round(score, 1),
        "white_px": white_px,
        "shroud_px": shroud_px,
        "head_pos": (round(head_x, 1), round(head_y, 1)),
        "dist": round(dist, 1),
        "is_behind": is_behind,
        "bed_pos": (round(bed_x, 1), round(bed_y, 1)),
        "bed_y_err": round(bed_y_err, 1),
        "head_x_err": round(head_x_err, 1),
    }
    return score, details


def is_frame_unoccluded(
    details: dict,
    clean_score_threshold: float = 25000.0,
    max_shroud_px: int = 18000,
    min_dist: float = 200.0,
    max_bed_y_err: float = 14.0,
    max_head_x_err: float = 45.0,
) -> bool:
    """
    Determine if a frame provides an unobstructed view of the print bed and is XY phase-locked.
    Returns False if bed Y or toolhead X error exceeds tolerances.
    """
    if details.get("bed_y_err", 999999.0) > max_bed_y_err:
        return False
    if details.get("head_x_err", 999999.0) > max_head_x_err:
        return False
    if details.get("score", -999999.0) >= clean_score_threshold:
        return True
    if details.get("shroud_px", 999999) <= max_shroud_px and details.get("dist", 0.0) >= min_dist:
        return True
    return False


def capture_snapshot(
    output_path: str,
    rotate: Optional[int] = None,
    burst: int = 1,
    burst_interval: float = 0.8,
    max_window: float = 12.0,
    early_exit: bool = True,
    target_bed_y: float = DEFAULT_TARGET_BED_Y,
    target_head_x: float = DEFAULT_TARGET_HEAD_X,
) -> bool:
    """
    Capture snapshot(s) from IP Webcam.
    If burst > 1, captures up to N frames over max_window seconds and selects
    the one with minimum toolhead occlusion and optimal XY phase-lock.
    If early_exit is True, short-circuits immediately when an unoccluded frame is detected.
    """
    adb_cmd, web_url, transport = get_adb_context(ensure_forward=True)

    if not is_port_open():
        print(f"Webcam is offline. Starting camera first (transport: {transport})...")
        if not start_camera():
            return False

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    configure_camera_focus()

    if burst <= 1:
        try:
            req = urllib.request.Request(f"{web_url}/shot.jpg")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
        except Exception as e:
            print(f"Failed to capture snapshot: {e}")
            return False

        img = Image.open(io.BytesIO(data))
        if rotate is None:
            effective_rotate = 270 if img.width > img.height else 0
        else:
            effective_rotate = rotate

        if effective_rotate:
            img = img.rotate(effective_rotate, expand=True)

        img.save(output_path, "JPEG", quality=95)
        print(f"Snapshot saved to {output_path} ({os.path.getsize(output_path)} bytes, dimensions={img.size}, rotated {effective_rotate}°, transport: {transport})")
        return True

    # Multi-frame burst capture with adaptive early-exit
    print(f"[Webcam Burst] Initiating adaptive burst (up to {burst} frames, {burst_interval}s interval, {max_window}s window ceiling, early_exit={early_exit})...")
    captured_frames = []
    window_start = time.time()

    for i in range(burst):
        t0 = time.time()
        try:
            req = urllib.request.Request(f"{web_url}/shot.jpg")
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data))
            if rotate is None:
                effective_rotate = 270 if img.width > img.height else 0
            else:
                effective_rotate = rotate
            if effective_rotate:
                img = img.rotate(effective_rotate, expand=True)

            score, details = score_burst_frame(img, target_bed_y=target_bed_y, target_head_x=target_head_x)
            unoccluded = is_frame_unoccluded(details)
            captured_frames.append((img, score, details, i, unoccluded))

            elapsed = time.time() - window_start
            status_tag = " [CLEAN - UNRESTRICTED]" if unoccluded else " [OCCLUDED]"
            print(f"  Frame {i} (t={elapsed:.1f}s): Score={details['score']} (ShroudInModel={details['shroud_px']}, WhitePx={details['white_px']}, Head={details['head_pos']}, Dist={details['dist']}, Bed={details['bed_pos']}, BedYErr={details['bed_y_err']}, HeadXErr={details['head_x_err']}){status_tag} [{time.time() - t0:.2f}s]")

            if early_exit and unoccluded:
                print(f"[Webcam Burst] Early-exit triggered on Frame {i} after {elapsed:.1f}s. Print zone is clear.")
                img.save(output_path, "JPEG", quality=95)
                print(f"Optimal snapshot saved to {output_path} ({os.path.getsize(output_path)} bytes, transport: {transport})")
                return True

        except Exception as e:
            print(f"  Frame {i} capture failed: {e}")

        # Check if another frame fits inside max_window
        time_elapsed = time.time() - window_start
        if i < burst - 1:
            if time_elapsed + burst_interval > max_window:
                print(f"[Webcam Burst] Reached {max_window}s window ceiling ({time_elapsed:.1f}s elapsed). Concluding burst.")
                break
            time.sleep(burst_interval)

    if not captured_frames:
        print("[Webcam Burst] Error: Zero valid frames captured.")
        return False

    # Pick frame with highest composite score
    best_img, best_score, best_details, best_idx, _ = max(captured_frames, key=lambda x: x[1])
    best_img.save(output_path, "JPEG", quality=95)
    print(f"[Webcam Burst] Selected Frame {best_idx}/{len(captured_frames)} (Score: {best_score}, ShroudOcclusion: {best_details['shroud_px']}px, Head: {best_details['head_pos']}, Bed: {best_details['bed_pos']})")
    print(f"Optimal snapshot saved to {output_path} ({os.path.getsize(output_path)} bytes, transport: {transport})")
    return True


def get_status():
    adb_cmd, web_url, transport = get_adb_context(ensure_forward=False)
    port_open = is_port_open()
    status_info = {
        "transport": transport,
        "adb_target": " ".join(adb_cmd),
        "web_url": web_url,
        "port_3333_open": port_open,
        "telemetry": None
    }
    if port_open:
        try:
            req = urllib.request.Request(f"{web_url}/status.json")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                status_info["telemetry"] = data.get("deviceInfo")
        except Exception as e:
            status_info["telemetry_error"] = str(e)

    print(json.dumps(status_info, indent=2))
    return status_info


def main():
    parser = argparse.ArgumentParser(description="Control IP Webcam on Pixel 8 Pro")
    parser.add_argument("action", choices=["start", "stop", "status", "snapshot", "focus"])
    parser.add_argument("--output", "-o", default="/workspace/data/3dprint/cam_snapshot.jpg", help="Output path for snapshot")
    parser.add_argument("--rotate", "-r", type=int, default=None, help="Rotation in degrees (default: auto-detect based on aspect ratio)")
    parser.add_argument("--burst", "-b", type=int, default=1, help="Capture N frames and auto-select frame with least toolhead occlusion")
    parser.add_argument("--interval", "-i", type=float, default=0.8, help="Interval between burst frames in seconds (default: 0.8)")
    parser.add_argument("--max-window", "-w", type=float, default=12.0, help="Maximum capture window ceiling in seconds (default: 12.0)")
    parser.add_argument("--no-early-exit", action="store_true", help="Disable early exit on clean unoccluded frame")
    parser.add_argument("--sleep-after", action="store_true", help="Immediately stop camera server and sleep phone after capture")
    parser.add_argument("--target-bed-y", type=float, default=DEFAULT_TARGET_BED_Y, help=f"Target bed Y position for phase lock (default: {DEFAULT_TARGET_BED_Y})")
    parser.add_argument("--target-head-x", type=float, default=DEFAULT_TARGET_HEAD_X, help=f"Target toolhead X position for phase lock (default: {DEFAULT_TARGET_HEAD_X})")
    args = parser.parse_args()

    if args.action == "start":
        start_camera()
    elif args.action == "stop":
        stop_camera()
    elif args.action == "status":
        get_status()
    elif args.action == "focus":
        success = configure_camera_focus(getattr(args, 'focus_distance', DEFAULT_FOCUS_DISTANCE))
        if success:
            print(f"Camera focus locked to build plate ({getattr(args, 'focus_distance', DEFAULT_FOCUS_DISTANCE):.2f} diopters).")
        else:
            sys.exit(1)
    elif args.action == "snapshot":
        success = capture_snapshot(
            args.output,
            rotate=args.rotate,
            burst=args.burst,
            burst_interval=args.interval,
            max_window=args.max_window,
            early_exit=not args.no_early_exit,
            target_bed_y=args.target_bed_y,
            target_head_x=args.target_head_x,
        )
        if args.sleep_after:
            stop_camera()
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
