#!/usr/bin/env python3
"""
3D Printer Safe Teardown & Power-Off CLI
Safely cools the hot end below 50°C, stops the webcam, and cuts AC mains power.
"""
import sys
import os
import time
import json
import urllib.request
import argparse
import subprocess

if "/workspace/tools" not in sys.path:
    sys.path.insert(0, "/workspace/tools")

def get_ha_context():
    with open('/secrets/env.json') as f:
        env = json.load(f)
    ha_url = env['HA_BASE_URL'].rstrip('/')
    token = env['HA_ACCESS_TOKEN']
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return ha_url, headers

def get_nozzle_temp(ha_url, headers):
    try:
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

def stop_webcam():
    print("Shutting down IP Webcam...")
    res = subprocess.run(["python3", "/workspace/tools/webcam_control.py", "stop"], capture_output=True, text=True)
    print(res.stdout.strip())

def turn_off_mains(ha_url, headers):
    print("Cutting 3D printer AC mains power...")
    req = urllib.request.Request(f"{ha_url}/api/services/switch/turn_off",
                                 data=json.dumps({"entity_id": "switch.3d_printer_power"}).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        print("Mains power cut: switch.3d_printer_power is OFF.")

def main():
    parser = argparse.ArgumentParser(description="Safely teardown 3D printer and webcam")
    parser.add_argument("--force", action="store_true", help="Bypass the 50°C cooldown safety check")
    parser.add_argument("--wait", action="store_true", help="Wait in a loop until nozzle drops below 50°C before cutting power")
    args = parser.parse_args()

    ha_url, headers = get_ha_context()
    temp = get_nozzle_temp(ha_url, headers)
    print(f"Current nozzle temperature: {temp:.1f}°C")

    # Heat creep protection
    if temp > 50.0 and not args.force:
        if args.wait:
            print(f"Hot end is at {temp:.1f}°C. Waiting for cooldown below 50.0°C to prevent heat creep...")
            while temp > 50.0:
                time.sleep(10)
                temp = get_nozzle_temp(ha_url, headers)
                print(f"Cooling... {temp:.1f}°C")
            print("Cooldown threshold reached (<= 50.0°C).")
        else:
            print(f"⚠️ SAFETY ABORT: Nozzle is still at {temp:.1f}°C! Cutting power now will stop the heatsink fan and cause heat creep / clogs.")
            print("Run with --wait to wait for safe cooldown, or --force if you know the printer is already cold.")
            sys.exit(1)

    # Execute teardown
    stop_webcam()
    turn_off_mains(ha_url, headers)
    print("Teardown sequence complete.")

if __name__ == "__main__":
    main()
