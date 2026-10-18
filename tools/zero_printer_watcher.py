#!/usr/bin/env python3
"""
Zero 3D Printer Watcher & Teardown Daemon
Monitors Ender-3 V2 print jobs via OctoPrint, executes periodic AI vision
failure evaluations (spaghetti/detachment), auto-pauses on catastrophic failures,
and handles automated post-print cooldown and mains teardown.

Workflow:
  1. Active Print (20m cadence): Captures snapshot, runs Gemini vision eval.
     If failure detected -> auto-pauses print via OctoPrint API & alerts #projects.
  2. Completion (PrintDone / Cancelled / Failed): Downshifts to 60s cooldown polling.
  3. Safe Teardown: Once tool0 <= 50.0°C, stops IP Webcam via ADB, cuts
     switch.3d_printer_power via Home Assistant, posts receipt to #projects, and exits.
"""

import os
import sys
import time
import json
import signal
import base64
import argparse
import urllib.request
import urllib.error
import subprocess
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

PT_TZ = ZoneInfo("America/Los_Angeles")

import requests

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import nas_config
OCTOPRINT_DEFAULT_URL = f"http://{nas_config.HOST_2_IP}:8888"
OCTOPRINT_DEFAULT_KEY = "8FC0C5F37CD14D4384100ED682682A80"
DEFAULT_DISCORD_CHANNEL = "3d-print"  # 1555973316752449576

DATA_DIR = Path("/workspace/data/3dprint")
PID_FILE = DATA_DIR / "watcher.pid"
STATUS_FILE = DATA_DIR / "watcher_status.json"
LOG_FILE = DATA_DIR / "watcher.log"
LATEST_SNAPSHOT = DATA_DIR / "watcher_snapshot.jpg"

TOOLS_DIR = Path("/workspace/tools")
WEBCAM_SCRIPT = TOOLS_DIR / "webcam_control.py"
DELIVER_IMAGE_SCRIPT = TOOLS_DIR / "deliver_image.py"
SPEECH_SCRIPT = TOOLS_DIR / "phone_speech.py"


def parse_time_string_to_seconds(time_str: str) -> int:
    """Convert human-readable time strings like '4h 53m 2s' or '35m 12s' to total seconds."""
    secs = 0
    m_h = re.search(r"(\d+)\s*h", time_str)
    m_m = re.search(r"(\d+)\s*m", time_str)
    m_s = re.search(r"(\d+)\s*s", time_str)
    if m_h:
        secs += int(m_h.group(1)) * 3600
    if m_m:
        secs += int(m_m.group(1)) * 60
    if m_s:
        secs += int(m_s.group(1))
    return secs


def extract_gcode_sliced_seconds(gcode_path: Path) -> int:
    """Parse OrcaSlicer/PrusaSlicer estimated printing time directly from G-code comments or M73 tags."""
    if not gcode_path.exists():
        return 0
    try:
        # Read the first 16KB (header comments and M73 tags)
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(16384)

        m73 = re.search(r"M73\s+P\d+\s+R(\d+)", head)
        if m73:
            return int(m73.group(1)) * 60

        m_time = re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*(.*)", head)
        if m_time:
            return parse_time_string_to_seconds(m_time.group(1))

        # Check tail if not found in header
        file_size = gcode_path.stat().st_size
        if file_size > 16384:
            with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(max(0, file_size - 65536))
                tail = f.read()
                m_time = re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*(.*)", tail)
                if m_time:
                    return parse_time_string_to_seconds(m_time.group(1))
    except Exception as e:
        print(f"[Watcher] Warning extracting G-code time from {gcode_path}: {e}")
    return 0


def resolve_gcode_path(filename: str) -> Optional[Path]:
    """Locate G-code file across DATA_DIR and any subdirectories."""
    cand = DATA_DIR / filename
    if cand.exists():
        return cand
    for sub in DATA_DIR.iterdir():
        if sub.is_dir():
            nested = sub / filename
            if nested.exists():
                return nested
    matches = list(DATA_DIR.rglob(filename))
    return matches[0] if matches else None


def extract_m73_remaining_minutes(gcode_path: Path, filepos: int = 0) -> Optional[int]:
    """Extract remaining minutes from the nearest M73 tag preceding current filepos, or from the header."""
    if not gcode_path or not gcode_path.exists():
        return None
    try:
        with open(gcode_path, "rb") as f:
            if filepos > 0:
                start = max(0, filepos - 131072)
                f.seek(start)
                chunk = f.read(min(131072, filepos - start)).decode("utf-8", errors="ignore")
                matches = re.findall(r"M73\s+P\d+\s+R(\d+)", chunk)
                if matches:
                    return int(matches[-1])
            f.seek(0)
            head = f.read(32768).decode("utf-8", errors="ignore")
            m = re.search(r"M73\s+P\d+\s+R(\d+)", head)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def estimate_eta_str(filename: str, completion: float, print_time: float, octo_time_left: float = 0.0, filepos: int = 0) -> str:
    """
    Calculate an accurate remaining print duration and Pacific Time clock ETA.
    Prioritizes OrcaSlicer motion planner metadata (M73 tags & kinematic estimates)
    over OctoPrint's naive static/linear estimator.
    """
    gcode_file = resolve_gcode_path(filename)
    rem_secs = None

    if gcode_file:
        # 1. Prefer ground-truth M73 remaining minutes from slicer motion planner
        m73_mins = extract_m73_remaining_minutes(gcode_file, filepos)
        if m73_mins is not None and m73_mins >= 0:
            rem_secs = m73_mins * 60
        else:
            # 2. Kinematic total expected duration minus actual elapsed print time
            sliced_secs = extract_gcode_sliced_seconds(gcode_file)
            if sliced_secs > 0:
                total_expected = sliced_secs + 300  # 300s baseline overhead (bed/nozzle heating, homing, probing)
                rem_secs = max(0.0, total_expected - print_time)

    # 3. Fallback only if no gcode metadata exists anywhere
    if rem_secs is None:
        if octo_time_left and 0 < octo_time_left < 86400:
            rem_secs = octo_time_left
        elif completion >= 10.0 and print_time > 0:
            rem_secs = max(0.0, print_time * (100.0 - completion) / completion)

    if rem_secs is None or rem_secs <= 0:
        return "Calculating..."

    rem_secs = int(rem_secs)
    hours = rem_secs // 3600
    mins = (rem_secs % 3600) // 60
    if hours > 0:
        dur_str = f"~{hours}h {mins:02d}m remaining"
    else:
        dur_str = f"~{max(1, mins)}m remaining"

    eta_dt = datetime.now(PT_TZ) + timedelta(seconds=rem_secs)
    clock_str = eta_dt.strftime("%-I:%M %p PT")
    return f"{dur_str} ({clock_str})"


def clean_job_name(filename: str) -> str:
    """Format raw gcode filename into clean spoken words (e.g. 'zero casual robot 6in')."""
    base = filename.rsplit(".", 1)[0]
    return base.replace("_", " ").replace("-", " ")


def speak_announcement(text: str, wait_home: bool = True, synchronous: bool = False, timeout: float = 10.0) -> bool:
    """Broadcast an audible announcement via tools/phone_speech.py using locked Stefan profile."""
    if not SPEECH_SCRIPT.exists():
        print(f"[Watcher Audio] Speech script {SPEECH_SCRIPT} not found, skipping.")
        return False
    try:
        cmd = ["python3", str(SPEECH_SCRIPT), text]
        if not wait_home:
            cmd.append("--no-home")
        if synchronous:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(f"[Watcher Audio] Dispatched Stefan announcement: {text!r}")
        return True
    except Exception as e:
        print(f"[Watcher Audio] Failed to dispatch announcement: {e}")
        return False


def get_ha_credentials() -> Tuple[str, str]:
    """Retrieve Home Assistant URL and Token from secrets/env.json."""
    secrets_file = Path("/secrets/env.json")
    if secrets_file.exists():
        try:
            with open(secrets_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data["HA_BASE_URL"].rstrip("/"), data["HA_ACCESS_TOKEN"]
        except Exception as e:
            raise RuntimeError(f"Failed to read Home Assistant credentials from secrets: {e}")
    raise RuntimeError("HA credentials not found in /secrets/env.json")


def get_gemini_api_key() -> str:
    """Retrieve Gemini API key from environment, secrets, or .env."""
    key = os.getenv("GEMINI_API_KEY")
    if key and key.strip():
        return key.strip()

    secrets_file = Path("/secrets/env.json")
    if secrets_file.exists():
        try:
            with open(secrets_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "GEMINI_API_KEY" in data and data["GEMINI_API_KEY"].strip():
                    return data["GEMINI_API_KEY"].strip()
        except Exception:
            pass

    raise RuntimeError("GEMINI_API_KEY not found in environment or /secrets/env.json")


class OctoPrintClient:
    def __init__(self, base_url: str = OCTOPRINT_DEFAULT_URL, api_key: str = OCTOPRINT_DEFAULT_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def get_job(self) -> Dict[str, Any]:
        url = f"{self.base_url}/api/job"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def get_printer(self) -> Dict[str, Any]:
        url = f"{self.base_url}/api/printer"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def pause_job(self) -> bool:
        url = f"{self.base_url}/api/job"
        payload = json.dumps({"command": "pause", "action": "pause"}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print(f"[OctoPrintClient] Error pausing print: {e}", file=sys.stderr)
            return False

    def reconnect(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200) -> bool:
        url = f"{self.base_url}/api/connection"
        payload = json.dumps({"command": "connect", "port": port, "baudrate": baudrate}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print(f"[OctoPrintClient] Error reconnecting serial: {e}", file=sys.stderr)
            return False

    def send_commands(self, commands: list[str]) -> bool:
        url = f"{self.base_url}/api/printer/command"
        payload = json.dumps({"commands": commands}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print(f"[OctoPrintClient] Error sending commands: {e}", file=sys.stderr)
            return False

    def emergency_park_and_cool(self) -> bool:
        return self.send_commands([
            "M104 S0",
            "M140 S0",
            "G91",
            "G1 Z15 F1200",
            "G90",
            "G1 X10 Y200 F6000",
        ])

    def upload_and_start_gcode(self, gcode_path: Path) -> bool:
        """Upload a local G-code file to OctoPrint and immediately select & start printing."""
        url = f"{self.base_url}/api/files/local"
        headers = {"X-Api-Key": self.api_key}
        try:
            with open(gcode_path, "rb") as f:
                files = {"file": (gcode_path.name, f, "application/octet-stream")}
                data = {"select": "true", "print": "true"}
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            return resp.status_code in (200, 201, 204)
        except Exception as e:
            print(f"[OctoPrintClient] Error uploading/starting resume G-code {gcode_path}: {e}", file=sys.stderr)
            return False


def build_zero_homing_resume_gcode(filename: str, filepos: int, attempt: int = 1) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """
    Build an in-place zero-homing continuation G-code file starting from `filepos`.
    Preserves bed/nozzle heat and Marlin stepper coordinate state without re-homing into tall parts.
    """
    src_path = resolve_gcode_path(filename)
    if not src_path or not src_path.exists():
        print(f"[AutoResume] Source G-code not found for {filename}", file=sys.stderr)
        return None

    file_size = src_path.stat().st_size
    if filepos <= 1000 or filepos >= file_size - 500:
        print(f"[AutoResume] Invalid filepos {filepos} for file size {file_size}", file=sys.stderr)
        return None

    try:
        with open(src_path, "rb") as f:
            # Read up to 256KB preceding filepos to extract Z height, fan state, and XY position
            lookback_start = max(0, filepos - 262144)
            f.seek(lookback_start)
            preceding_text = f.read(filepos - lookback_start).decode("utf-8", errors="ignore")

            # Advance to start of next complete G-code line at or after filepos
            f.seek(filepos)
            partial_line = f.readline()
            actual_offset = f.tell()
            remaining_bytes = f.read()

        # Extract current layer Z height
        z_matches = re.findall(r";Z:([0-9]+\.?[0-9]*)", preceding_text)
        if not z_matches:
            z_matches = re.findall(r"G[01]\s+[^;\n]*Z([0-9]+\.?[0-9]*)", preceding_text)
        if not z_matches:
            print("[AutoResume] Could not determine Z height from preceding G-code", file=sys.stderr)
            return None
        z_height = float(z_matches[-1])

        # Extract last fan command
        fan_matches = re.findall(r"(M106\s+S\d+|M107)", preceding_text)
        fan_cmd = fan_matches[-1] if fan_matches else "M106 S255"

        # Extract last XY travel/extrusion coordinates before filepos
        xy_matches = re.findall(r"G[01]\s+[^;\n]*X([0-9]+\.?[0-9]*)\s+Y([0-9]+\.?[0-9]*)", preceding_text)
        resume_x, resume_y = (float(xy_matches[-1][0]), float(xy_matches[-1][1])) if xy_matches else (None, None)

        base_stem = re.sub(r"_autoresume_\d+$", "", src_path.stem)
        out_name = f"{base_stem}_autoresume_{attempt}.gcode"
        out_path = DATA_DIR / out_name

        xy_travel_line = (
            f"G1 X{resume_x:.3f} Y{resume_y:.3f} F6000 ; return to last XY coordinate\n"
            if resume_x is not None and resume_y is not None
            else ""
        )

        header = (
            f"; --- ZERO WATCHER AUTO-RESUME FROM SERIAL DROP (ATTEMPT {attempt}) ---\n"
            f"; Source: {filename} | Offset: {actual_offset} | Z: {z_height:.2f}mm\n"
            f"M140 S60 ; maintain bed at 60C\n"
            f"M109 S200 T0 ; ensure nozzle at 200C\n"
            f"G90 ; absolute positioning\n"
            f"G21 ; millimeter units\n"
            f"M83 ; relative extrusion mode\n"
            f"M420 S1 Z0 ; ensure bilinear mesh remains active\n"
            f"{fan_cmd} ; restore part cooling fan\n"
            f"G1 Z{z_height + 2.0:.2f} F1200 ; safe clearance above layer\n"
            f"{xy_travel_line}"
            f"G1 Z{z_height:.2f} F1200 ; descend to exact layer Z={z_height:.2f}mm\n"
            f"G1 E0.6 F1800 ; micro-prime nozzle pressure\n"
            f"; --- RESUMING SLICED GCODE ---\n"
        ).encode("utf-8")

        with open(out_path, "wb") as out_f:
            out_f.write(header)
            out_f.write(remaining_bytes)

        meta = {
            "source_file": filename,
            "resume_file": out_name,
            "filepos": actual_offset,
            "z_height": z_height,
            "resume_x": resume_x,
            "resume_y": resume_y,
            "fan_cmd": fan_cmd,
        }
        return out_path, meta
    except Exception as e:
        print(f"[AutoResume] Failed to build resume G-code: {e}", file=sys.stderr)
        return None



def get_nozzle_temp(client: OctoPrintClient) -> float:
    """Retrieve tool0 actual temperature from OctoPrint."""
    try:
        printer_data = client.get_printer()
        return float(printer_data.get("temperature", {}).get("tool0", {}).get("actual", 0.0))
    except Exception:
        # Fallback to Home Assistant sensor if OctoPrint disconnects
        try:
            ha_url, token = get_ha_credentials()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            req = urllib.request.Request(f"{ha_url}/api/states/sensor.octoprint_actual_tool0_temp", headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                val = float(data.get("state", 0.0))
                unit = data.get("attributes", {}).get("unit_of_measurement", "°C")
                if unit == "°F":
                    val = (val - 32.0) * 5.0 / 9.0
                return val
        except Exception:
            return 0.0


def capture_webcam_snapshot(output_path: Path, burst: int = 6, interval: float = 4.0, max_window: float = 25.0) -> bool:
    """Invoke tools/webcam_control.py to capture a rotated snapshot with adaptive early-exit burst."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            output_path.unlink()
        except OSError:
            pass
    cmd = [
        "python3",
        str(WEBCAM_SCRIPT),
        "snapshot",
        "--burst",
        str(burst),
        "--interval",
        str(interval),
        "--max-window",
        str(max_window),
        "--sleep-after",
        "--output",
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return True
    print(f"[Webcam] Snapshot capture failed: {res.stderr or res.stdout}", file=sys.stderr)
    return False


def stop_webcam() -> bool:
    """Invoke tools/webcam_control.py to cleanly stop camera server and sleep screen."""
    cmd = ["python3", str(WEBCAM_SCRIPT), "stop"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


def cut_printer_power() -> bool:
    """Turn off switch.3d_printer_power via Home Assistant REST API."""
    try:
        ha_url, token = get_ha_credentials()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req = urllib.request.Request(
            f"{ha_url}/api/services/switch/turn_off",
            data=json.dumps({"entity_id": "switch.3d_printer_power"}).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        print(f"[HomeAssistant] Failed to turn off printer switch: {e}", file=sys.stderr)
        return False


def evaluate_print_image(image_path: Path, timeout: float = 75.0) -> Dict[str, Any]:
    """
    Perform AI vision evaluation for catastrophic print failures
    using native agy CLI (Google Antigravity bundled quota).
    """
    if not image_path.exists() or image_path.stat().st_size == 0:
        return {"failure": False, "confidence": 0.0, "error": "Image file does not exist or is empty"}

    prompt = (
        f"Inspect the image file {image_path.resolve()}. "
        "You are an expert 3D printing QA inspector analyzing a snapshot from an Ender-3 V2. "
        "Detect if there is a CATASTROPHIC print failure in progress: "
        "1. Spaghetti failure (filament extrusion bird nesting into free air). "
        "2. Print detached from the print bed and being dragged around. "
        "3. Massive layer shift destroying the part. "
        "4. Filament blob encasing the hotend / nozzle. "
        "Normal active printing, skirt lines, supports, infill, or completed prints on the bed are NOT failures. "
        "Respond ONLY with a valid JSON object with keys: "
        "failure (boolean), confidence (float between 0.0 and 1.0), issue (short summary string), summary (detailed string)."
    )

    for attempt in range(2):
        try:
            res = subprocess.run(
                [
                    "agy",
                    "--model=gemini-3.8-flash-low",
                    "--disable-slash-commands",
                    f"-p={prompt}"
                ],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if res.returncode == 0 and res.stdout.strip():
                raw_text = res.stdout.strip()
                if "```" in raw_text:
                    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
                    if match:
                        raw_text = match.group(1).strip()
                elif "{" in raw_text and "}" in raw_text:
                    match = re.search(r"(\{[\s\S]*\})", raw_text)
                    if match:
                        raw_text = match.group(1).strip()
                return json.loads(raw_text)
            else:
                last_err = f"agy CLI error {res.returncode}: {res.stderr[:200]}"
        except Exception as e:
            last_err = f"Evaluation exception: {e}"
        if attempt == 0:
            time.sleep(2)

    return {
        "failure": False,
        "confidence": 0.0,
        "error": last_err
    }


def deliver_discord_photo(image_path: Path, caption: str, channel: str = DEFAULT_DISCORD_CHANNEL) -> bool:
    """Deliver a snapshot with caption to Discord via deliver_image.py."""
    cmd = [
        "python3",
        str(DELIVER_IMAGE_SCRIPT),
        "--upload",
        str(image_path),
        "--channel",
        channel,
        "--caption",
        caption,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


def update_status(status_dict: Dict[str, Any]):
    """Record current state to STATUS_FILE atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = STATUS_FILE.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(status_dict, f, indent=2)
        tmp_file.replace(STATUS_FILE)
    except Exception as e:
        print(f"[Watcher] Warning: Failed to write status file: {e}", file=sys.stderr)


class PrinterWatcherDaemon:
    def __init__(
        self,
        octoprint_url: str = OCTOPRINT_DEFAULT_URL,
        api_key: str = OCTOPRINT_DEFAULT_KEY,
        interval_print: int = 1200,      # 20 minutes
        interval_cooldown: int = 60,     # 60 seconds
        cooldown_temp: float = 50.0,     # 50°C
        channel: str = DEFAULT_DISCORD_CHANNEL,
        post_progress: bool = True,
        enable_voice: bool = True,
        burst: int = 6,
    ):
        self.client = OctoPrintClient(octoprint_url, api_key)
        self.interval_print = interval_print
        self.interval_cooldown = interval_cooldown
        self.cooldown_temp = cooldown_temp
        self.channel = channel
        self.post_progress = post_progress
        self.enable_voice = enable_voice
        self.burst = burst
        self.running = True
        self.was_printing = False
        self.last_known_filename = "unknown_job"
        self.last_known_completion = 0.0
        self.last_known_filepos = 0
        self.auto_resume_count = 0
        self.max_auto_resumes = 3

    def setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, signum, frame):
        print(f"\n[Watcher] Signal {signum} received. Cleaning up...")
        self.running = False

    def write_pid(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    def remove_pid(self):
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass

    def attempt_serial_auto_resume(self, state: str, filename: str, completion: float) -> bool:
        """
        Attempt an immediate in-place zero-homing resume after a mid-print USB serial drop.
        Returns True if the spliced G-code was generated, uploaded, and resumed printing.
        """
        attempt = self.auto_resume_count + 1
        filepos = self.last_known_filepos
        print(
            f"🔄 [Watcher AutoResume] Serial drop detected ({state}) on {filename} "
            f"at {completion:.2f}% (filepos={filepos}). Attempting auto-resume #{attempt}/{self.max_auto_resumes}..."
        )

        # 1. Reconnect serial port and wait for Operational state
        self.client.reconnect()
        operational = False
        for _ in range(6):
            time.sleep(2)
            try:
                j = self.client.get_job()
                st = j.get("state", "")
                if st == "Operational":
                    operational = True
                    break
                if "Offline" in st or "Closed" in st:
                    self.client.reconnect()
            except Exception:
                pass

        if not operational:
            print("[Watcher AutoResume] Could not restore OctoPrint Operational state after serial reconnect.", file=sys.stderr)
            return False

        # 2. Immediately keep bed & nozzle hot and lift Z +5mm clear of part
        self.client.send_commands([
            "M140 S60",
            "M104 S200",
            "G91",
            "G1 Z5 F1200",
            "G90",
        ])

        # 3. Build zero-homing continuation G-code from last_known_filepos
        built = build_zero_homing_resume_gcode(filename, filepos, attempt=attempt)
        if not built:
            print("[Watcher AutoResume] Failed to build zero-homing continuation G-code.", file=sys.stderr)
            return False

        resume_path, meta = built
        print(
            f"[Watcher AutoResume] Built {resume_path.name} "
            f"(offset={meta['filepos']}, Z={meta['z_height']:.2f}mm, XY=({meta['resume_x']}, {meta['resume_y']})). Uploading & starting..."
        )

        # 4. Upload and immediately start print
        if not self.client.upload_and_start_gcode(resume_path):
            print("[Watcher AutoResume] Upload/start API call failed.", file=sys.stderr)
            return False

        # 5. Confirm state transitions to Printing
        resumed_ok = False
        for _ in range(6):
            time.sleep(2)
            try:
                j = self.client.get_job()
                if j.get("state") == "Printing":
                    resumed_ok = True
                    break
            except Exception:
                pass

        if not resumed_ok:
            print("[Watcher AutoResume] Job did not enter Printing state after upload.", file=sys.stderr)
            return False

        self.auto_resume_count = attempt
        self.last_known_filename = resume_path.name
        self.last_known_filepos = 0
        self.last_known_completion = 0.0
        self.was_printing = True

        clean_name = clean_job_name(filename)
        if self.enable_voice:
            speak_announcement(f"USB serial glitch recovered on {clean_name}. Auto resuming print at {meta['z_height']:.1f} millimeters.")

        caption = (
            f"🔄 **Zero Watcher: Auto-Resumed from USB Serial Drop!**\n"
            f"- **Interrupted Job:** `{filename}` ({completion:.1f}%, byte `{meta['filepos']:,}`)\n"
            f"- **Continuation Job:** `{resume_path.name}` (Attempt `{attempt}/{self.max_auto_resumes}`)\n"
            f"- **Resume Coordinates:** `Z = {meta['z_height']:.2f} mm`"
            + (f", `X = {meta['resume_x']:.2f}`, `Y = {meta['resume_y']:.2f}`\n" if meta["resume_x"] is not None else "\n")
            + f"- **Action:** Reconnected `/dev/ttyUSB0`, maintained `60°C` bed / `200°C` hotend, and spliced zero-homing continuation G-code in place."
        )
        if capture_webcam_snapshot(LATEST_SNAPSHOT, burst=self.burst):
            deliver_discord_photo(LATEST_SNAPSHOT, caption, self.channel)
        else:
            try:
                from tools.outbox import deliver_message
                deliver_message(caption, self.channel)
            except Exception:
                pass

        return True

    def run(self):
        self.setup_signals()
        self.write_pid()
        print(f"[Watcher] Started Zero Printer Watcher (PID {os.getpid()})")
        print(f"          Print Cadence: {self.interval_print}s | Cooldown Cadence: {self.interval_cooldown}s")

        last_eval_time = 0
        terminal_triggered = False
        terminal_reason = "DONE"

        try:
            while self.running:
                try:
                    job = self.client.get_job()
                    state = job.get("state", "Unknown")
                    progress = job.get("progress", {})
                    completion = progress.get("completion") or 0.0
                    filepos = int(progress.get("filepos") or 0)
                    file_info = job.get("job", {}).get("file", {})
                    filename = file_info.get("name") or self.last_known_filename
                    if state == "Printing":
                        if file_info.get("name"):
                            self.last_known_filename = file_info.get("name")
                        if completion > 0.0:
                            self.last_known_completion = completion
                        if filepos > 0:
                            self.last_known_filepos = filepos
                    else:
                        if completion == 0.0 and self.last_known_completion > 0.0:
                            completion = self.last_known_completion
                        if filepos == 0 and self.last_known_filepos > 0:
                            filepos = self.last_known_filepos
                except Exception as e:
                    print(f"[Watcher] Warning: Failed to query OctoPrint job: {e}")
                    time.sleep(10)
                    continue

                nozzle_temp = get_nozzle_temp(self.client)
                print_time = float(progress.get("printTime") or 0.0)
                octo_time_left = float(progress.get("printTimeLeft") or 0.0)
                eta_str = estimate_eta_str(filename, completion, print_time, octo_time_left, filepos=filepos)

                # Record status snapshot
                update_status({
                    "pid": os.getpid(),
                    "state": state,
                    "filename": filename,
                    "completion": round(completion, 1),
                    "filepos": filepos,
                    "auto_resume_count": self.auto_resume_count,
                    "eta": eta_str,
                    "nozzle_temp": nozzle_temp,
                    "terminal_triggered": terminal_triggered,
                    "last_updated": time.time(),
                })

                # Check if print is actively running
                if state == "Printing":
                    if not self.was_printing:
                        self.was_printing = True
                        clean_name = clean_job_name(filename)
                        if completion < 2.0 and self.enable_voice:
                            speak_announcement(f"Extrusion initialized for {clean_name}. Keep your hands off the build plate.")
                        else:
                            print(f"[Watcher Audio] Monitoring active print for {clean_name} ({completion:.1f}%). Initial start announcement bypassed.")

                    now = time.time()
                    if (now - last_eval_time) >= self.interval_print:
                        print(f"[Watcher] Active print at {completion:.1f}%. Capturing snapshot for vision eval...")
                        if capture_webcam_snapshot(LATEST_SNAPSHOT, burst=self.burst):
                            eval_result = evaluate_print_image(LATEST_SNAPSHOT)
                            if "error" in eval_result:
                                print(f"[Watcher] Vision Eval warning (skipping bogus 0% fallback): {eval_result['error']}")
                                last_eval_time = now
                                time.sleep(15)
                                continue

                            is_failure = eval_result.get("failure", False)
                            confidence = eval_result.get("confidence", 0.0)
                            issue = eval_result.get("issue", "Unknown failure")

                            print(f"[Watcher] Vision Eval: failure={is_failure}, conf={confidence:.2f}, issue={issue}")

                            if is_failure and confidence >= 0.70:
                                print(f"🚨 [Watcher] Catastrophic failure detected! Executing Option B (Auto-Pause)...")
                                self.client.pause_job()
                                clean_name = clean_job_name(filename)
                                if self.enable_voice:
                                    speak_announcement(f"Catastrophic failure detected on {clean_name}. Print paused to prevent damage.")
                                caption = (
                                    f"🚨 **Zero Watcher: Catastrophic Print Failure Detected!**\n"
                                    f"- **Job:** `{filename}` ({completion:.1f}%)\n"
                                    f"- **Action:** ⏸️ Print automatically PAUSED to prevent damage and filament waste.\n"
                                    f"- **Detection:** {issue} (confidence: {confidence:.0%})\n"
                                    f"- **Summary:** {eval_result.get('summary', '')}"
                                )
                                deliver_discord_photo(LATEST_SNAPSHOT, caption, self.channel)
                                update_status({
                                    "pid": os.getpid(),
                                    "state": "Paused_Due_To_Failure",
                                    "issue": issue,
                                    "confidence": confidence,
                                    "last_updated": time.time(),
                                })
                                # Pause watcher loop until user resumes or aborts
                                time.sleep(60)
                                continue
                            elif self.post_progress:
                                print(f"[Watcher] Posting nominal progress snapshot to #{self.channel}...")
                                caption = (
                                    f"📸 **Zero Watcher Progress Check: Nominal**\n"
                                    f"- **Job:** `{filename}` ({completion:.1f}%)\n"
                                    f"- **ETA:** {eta_str}\n"
                                    f"- **Hotend:** {nozzle_temp:.1f}°C\n"
                                    f"- **Status:** Geometry & bed adhesion clean (confidence: {confidence:.0%})\n"
                                    f"- **Analysis:** {eval_result.get('summary', 'Active extrusion nominal').strip()}"
                                )
                                deliver_discord_photo(LATEST_SNAPSHOT, caption, self.channel)

                        last_eval_time = now

                    # Sleep 15s between status loops while printing
                    time.sleep(15)
                    continue

                # Check for Terminal States: Done, Cancelled, or Failed (including serial drops mid-print)
                is_serial_drop = (
                    self.was_printing
                    and ("Offline" in state or "Error" in state or "Closed" in state)
                )
                if is_serial_drop and not terminal_triggered:
                    if self.auto_resume_count < self.max_auto_resumes and self.last_known_filepos > 1000:
                        if self.attempt_serial_auto_resume(state, filename, completion):
                            last_eval_time = time.time()
                            time.sleep(15)
                            continue
                    print("[Watcher] Auto-resume unavailable or exhausted; falling back to emergency park & cool.")

                is_done = (completion >= 99.9 and state in ("Operational", "Finishing"))
                is_cancelled = (state in ("Cancelling", "Cancelled"))
                is_failed = (
                    state in ("Failed", "Error")
                    or is_serial_drop
                )

                if (is_done or is_cancelled or is_failed) and not terminal_triggered:
                    terminal_triggered = True
                    self.was_printing = False
                    clean_name = clean_job_name(filename)
                    if is_cancelled:
                        terminal_reason = "CANCELLED"
                        caption_header = f"⚠️ **Zero Watcher: Print Cancelled** (`{filename}` at {completion:.1f}%)"
                        if self.enable_voice:
                            speak_announcement(f"Print cancelled for {clean_name}. Parking toolhead.")
                    elif is_failed:
                        terminal_reason = "FAILED"
                        if "Offline" in state or "Error" in state or "Closed" in state:
                            print(f"[Watcher] Serial disconnect detected ({state}). Reconnecting to park & cool hotend...")
                            self.client.reconnect()
                            time.sleep(4)
                            self.client.emergency_park_and_cool()
                            nozzle_temp = get_nozzle_temp(self.client)
                        caption_header = f"❌ **Zero Watcher: Print Interrupted ({state})** (`{filename}` at {completion:.1f}%)"
                        if self.enable_voice:
                            speak_announcement(f"Print interrupted for {clean_name}. Toolhead parked and cooling.")
                    else:
                        terminal_reason = "DONE"
                        print_time_mins = round((progress.get("printTime") or 0) / 60, 1)
                        caption_header = f"🎉 **Zero Watcher: Print Complete (100%)!** (`{filename}`, {print_time_mins} min)"
                        if self.enable_voice:
                            speak_announcement(f"Print complete for {clean_name}. Part cooling on bed. Do not touch.")

                    print(f"[Watcher] Terminal state hit ({terminal_reason}). Capturing completion photo...")
                    if capture_webcam_snapshot(LATEST_SNAPSHOT):
                        deliver_discord_photo(
                            LATEST_SNAPSHOT,
                            f"{caption_header}\nCooling hotend below {self.cooldown_temp:.0f}°C before automated power-off...",
                            self.channel
                        )

                # Cooldown phase
                if terminal_triggered:
                    print(f"[Watcher] Cooldown phase active. Nozzle temp: {nozzle_temp:.1f}°C (target: <= {self.cooldown_temp:.0f}°C)")
                    if nozzle_temp <= self.cooldown_temp:
                        print(f"[Watcher] Hotend safe threshold reached ({nozzle_temp:.1f}°C <= {self.cooldown_temp:.0f}°C). Executing teardown...")
                        if self.enable_voice:
                            speak_announcement("Cooldown threshold satisfied. Cutting main power now.", synchronous=True, timeout=8.0)
                        stop_webcam()
                        cut_printer_power()
                        receipt = (
                            f"✅ **Zero Watcher: Teardown Complete**\n"
                            f"- **Hotend:** Safe at {nozzle_temp:.1f}°C (cooling fan duty complete)\n"
                            f"- **Webcam:** IP Webcam stopped & Pixel 8 Pro asleep\n"
                            f"- **Mains Power:** `switch.3d_printer_power` is OFF via Home Assistant"
                        )
                        # Post text receipt via outbox or python deliver_image
                        try:
                            from tools.outbox import deliver_message
                            deliver_message(receipt, self.channel)
                        except Exception:
                            subprocess.run([
                                "python3", "/workspace/tools/outbox.py",
                                "--channel", self.channel,
                                "--message", receipt,
                                "--flush"
                            ])
                        print("[Watcher] Teardown sequence finished cleanly. Exiting.")
                        break

                    time.sleep(self.interval_cooldown)
                    continue

                # If idle / not printing and not terminal
                time.sleep(10)

        finally:
            self.remove_pid()
            update_status({
                "pid": None,
                "state": "Stopped",
                "last_updated": time.time(),
            })


def is_running() -> Tuple[bool, Optional[int]]:
    """Check if a watcher process is currently running."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (OSError, ValueError):
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        return False, None


def stop_daemon(timeout: float = 10.0) -> bool:
    """Stop running watcher daemon and wait for it to exit completely."""
    running, pid = is_running()
    if not running or pid is None:
        print("No Zero Watcher daemon is running.")
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        return True
    print(f"Stopping Zero Watcher daemon (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            if not is_running()[0]:
                print("Zero Watcher stopped cleanly.")
                if PID_FILE.exists():
                    try:
                        PID_FILE.unlink()
                    except Exception:
                        pass
                return True
        print(f"Zero Watcher (PID {pid}) did not stop within {timeout}s; escalating to SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.5)
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        print("Zero Watcher killed.")
        return True
    except Exception as e:
        print(f"Error stopping watcher: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Zero 3D Printer Watcher & Teardown Daemon")
    parser.add_argument("--daemon", action="store_true", help="Run watcher daemon continuously")
    parser.add_argument("--stop", action="store_true", help="Stop running watcher daemon")
    parser.add_argument("--restart", action="store_true", help="Stop existing daemon (if running) and start a new daemon")
    parser.add_argument("--replace", action="store_true", help="Alias for --restart")
    parser.add_argument("--status", action="store_true", help="Display watcher status")
    parser.add_argument("--evaluate", help="Run vision evaluation on an image file directly and print JSON")
    parser.add_argument("--internal-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-post-progress", action="store_true", help="Disable posting periodic nominal progress photos to Discord")
    parser.add_argument("--no-voice", action="store_true", help="Disable audible speech announcements via Pixel 8 Pro")
    parser.add_argument("--interval-print", type=int, default=1200, help="Print evaluation cadence in seconds (default: 1200 / 20m)")
    parser.add_argument("--interval-cooldown", type=int, default=60, help="Cooldown polling cadence in seconds (default: 60)")
    parser.add_argument("--cooldown-temp", type=float, default=50.0, help="Safe hotend power-off temperature (default: 50.0°C)")
    parser.add_argument("--channel", default=DEFAULT_DISCORD_CHANNEL, help="Target Discord channel for photos and alerts")
    parser.add_argument("--burst", type=int, default=6, help="Number of frames in burst capture for occlusion scoring (default: 6)")

    args = parser.parse_args()

    if args.evaluate:
        res = evaluate_print_image(Path(args.evaluate))
        print(json.dumps(res, indent=2))
        return

    if args.status:
        running, pid = is_running()
        status_data = {}
        if STATUS_FILE.exists():
            try:
                status_data = json.loads(STATUS_FILE.read_text())
            except Exception:
                pass
        print(f"Watcher Running: {running} (PID: {pid})")
        print("Status Info:", json.dumps(status_data, indent=2))
        return

    if args.stop:
        stop_daemon()
        return

    if args.restart or args.replace:
        print("Restarting Zero Watcher daemon...")
        stop_daemon()
        args.daemon = True

    if args.daemon:
        running, pid = is_running()
        if running:
            # Check if process is actively shutting down
            for _ in range(4):
                time.sleep(0.5)
                running, pid = is_running()
                if not running:
                    break
        if running:
            print(f"Zero Watcher is already running (PID {pid}). Use --restart to replace it.")
            sys.exit(0)
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--internal-run",
            f"--interval-print={args.interval_print}",
            f"--interval-cooldown={args.interval_cooldown}",
            f"--cooldown-temp={args.cooldown_temp}",
            f"--channel={args.channel}",
            f"--burst={args.burst}",
        ]
        if args.no_post_progress:
            cmd.append("--no-post-progress")
        if args.no_voice:
            cmd.append("--no-voice")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_f = open(LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(6):
            time.sleep(0.5)
            r, active_pid = is_running()
            if r:
                print(f"Started Zero Watcher daemon detached (PID {active_pid}). Logs: {LOG_FILE}")
                return
        print(f"Started Zero Watcher process (PID {proc.pid}). Logs: {LOG_FILE}")
        return

    if args.internal_run:
        watcher = PrinterWatcherDaemon(
            interval_print=args.interval_print,
            interval_cooldown=args.interval_cooldown,
            cooldown_temp=args.cooldown_temp,
            channel=args.channel,
            post_progress=(not args.no_post_progress),
            enable_voice=(not args.no_voice),
            burst=args.burst,
        )
        watcher.run()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
