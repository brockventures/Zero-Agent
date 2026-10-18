#!/usr/bin/env python3
"""Pixel 8 Pro Real-Device Browser Automation CLI (over ADB CDP).

Enables headless browser automation on a physical Pixel 8 Pro (husky)
using Chrome DevTools Protocol (CDP) forwarded over ADB.

Serves as an emergency fallback when headless server browsers (Playwright/Puppeteer)
encounter aggressive anti-bot protections (Cloudflare Turnstile, DataDome, Akamai)
or CAPTCHAs that require genuine mobile hardware signatures (Tensor G3, genuine GPU,
real touch event dispatch, residential/mobile networking).

Safety Guard:
Checks for active OctoPrint 3D print jobs and IP Webcam streaming to prevent
disrupting print bed monitoring.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

sys.path.insert(0, "/workspace")

ADB_BIN = "/workspace/tools/bin/adb"
if not os.path.exists(ADB_BIN):
    ADB_BIN = "/workspace/scratch/platform-tools/platform-tools/adb"

import nas_config
HOST2_IP = nas_config.HOST_2_IP
ADB_PORT = "5037"
USB_SERIAL = "36261FDJG0003L"
WIFI_DEVICE = os.environ.get("PIXEL_WIFI_ADB", "")
CDP_PORT = 9223
IP_WEBCAM_URL = f"http://{HOST2_IP}:3333/status.json"


def get_adb_command_prefix() -> Tuple[List[str], str]:
    """Resolve ADB command prefix, checking local connected devices first then Host 2."""
    # 1. Check local connected devices
    try:
        res = subprocess.run([ADB_BIN, "devices"], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            if "	device" in line and not line.startswith("List"):
                dev = line.split("	")[0].strip()
                return [ADB_BIN, "-s", dev], "local"
    except Exception:
        pass

    # 2. Try connecting to WiFi device locally if offline
    try:
        subprocess.run([ADB_BIN, "connect", WIFI_DEVICE], capture_output=True, text=True, timeout=3)
        res = subprocess.run([ADB_BIN, "devices"], capture_output=True, text=True, timeout=2)
        if f"{WIFI_DEVICE}	device" in res.stdout:
            return [ADB_BIN, "-s", WIFI_DEVICE], "local"
    except Exception:
        pass

    # 3. Try USB via Host 2 ADB server
    try:
        res = subprocess.run(
            [ADB_BIN, "-H", HOST2_IP, "-P", ADB_PORT, "devices"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if f"{USB_SERIAL}	device" in res.stdout:
            return [ADB_BIN, "-H", HOST2_IP, "-P", ADB_PORT, "-s", USB_SERIAL], "usb"
    except Exception:
        pass

    # Fallback to local wifi device
    return [ADB_BIN, "-s", WIFI_DEVICE], "local"


def run_adb_shell(cmd_args: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    prefix, _ = get_adb_command_prefix()
    full_cmd = prefix + ["shell"] + cmd_args
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)


def check_printer_conflict() -> Tuple[bool, str]:
    """Check if the phone is currently actively serving an OctoPrint 3D print."""
    try:
        req = urllib.request.Request(IP_WEBCAM_URL)
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return True, "IP Webcam is currently active on port 3333 (possible 3D print in progress)"
    except Exception:
        pass
    return False, "Clear (IP Webcam offline)"


def get_device_status() -> Dict[str, Any]:
    """Retrieve hardware and battery telemetry from the Pixel 8 Pro."""
    prefix, transport = get_adb_command_prefix()
    conflict, conflict_msg = check_printer_conflict()

    status_data = {
        "device": "Pixel 8 Pro (husky)",
        "transport": transport,
        "serial": USB_SERIAL,
        "conflict": conflict,
        "conflict_message": conflict_msg,
        "battery_pct": None,
        "temperature_c": None,
        "charging": False,
        "screen_awake": False,
    }

    try:
        res_bat = run_adb_shell(["dumpsys", "battery"])
        if res_bat.returncode == 0:
            for line in res_bat.stdout.splitlines():
                line = line.strip()
                if line.startswith("level:"):
                    status_data["battery_pct"] = int(line.split(":")[1].strip())
                elif line.startswith("temperature:"):
                    status_data["temperature_c"] = float(line.split(":")[1].strip()) / 10.0
                elif line.startswith("status:"):
                    status_data["charging"] = line.split(":")[1].strip() == "2"

        res_pwr = run_adb_shell(["dumpsys", "power"])
        if res_pwr.returncode == 0:
            for line in res_pwr.stdout.splitlines():
                if "mWakefulness=" in line:
                    status_data["screen_awake"] = "Awake" in line
                    break
    except Exception as e:
        status_data["error"] = str(e)

    return status_data


def wake_and_unlock():
    """Wake screen and unlock keyguard."""
    prefix, _ = get_adb_command_prefix()
    # 224 = KEYCODE_WAKEUP, 82 = KEYCODE_MENU (unlocks standard swipe lock)
    subprocess.run(prefix + ["shell", "input", "keyevent", "224"], capture_output=True, timeout=3)
    subprocess.run(prefix + ["shell", "input", "keyevent", "82"], capture_output=True, timeout=3)
    time.sleep(0.5)


def sleep_screen():
    """Put display to sleep (KEYCODE_POWER = 26) to preserve OLED panel and battery."""
    prefix, _ = get_adb_command_prefix()
    subprocess.run(prefix + ["shell", "input", "keyevent", "26"], capture_output=True, timeout=3)


def setup_cdp_forwarding() -> bool:
    """Ensure ADB port forwarding for Chrome DevTools Protocol."""
    prefix, _ = get_adb_command_prefix()
    fwd_cmd = prefix + ["forward", f"tcp:{CDP_PORT}", "localabstract:chrome_devtools_remote"]
    res = subprocess.run(fwd_cmd, capture_output=True, text=True, timeout=5)
    return res.returncode == 0


def launch_chrome():
    """Ensure mobile Chrome is launched and in foreground without creating new tabs."""
    prefix, _ = get_adb_command_prefix()
    am_cmd = ["am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"]
    subprocess.run(prefix + ["shell"] + am_cmd, capture_output=True, timeout=5)
    time.sleep(1.0)


@contextmanager
def pixel_session(force: bool = False):
    """Context manager guaranteeing screen sleep and CDP cleanup on exit."""
    conflict, msg = check_printer_conflict()
    if conflict and not force:
        raise RuntimeError(f"Printer conflict guard active: {msg}. Pass --force to override.")

    prefix, transport = get_adb_command_prefix()
    wake_and_unlock()
    setup_cdp_forwarding()
    launch_chrome()

    try:
        cdp_host = "127.0.0.1" if transport == "local" else HOST2_IP
        yield f"http://{cdp_host}:{CDP_PORT}"
    finally:
        sleep_screen()


def close_extra_tabs(context, keep_page=None) -> int:
    """Close all extraneous open tabs in a browser context, keeping only one."""
    closed = 0
    try:
        pages = context.pages
        target = keep_page if keep_page is not None else (pages[0] if pages else None)
        for p in pages:
            if p != target:
                try:
                    p.close()
                    closed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return closed


def cleanup_tabs(keep_url: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Clean up open Chrome tabs over CDP, leaving at most one tab."""
    from playwright.sync_api import sync_playwright

    closed_count = 0
    remaining_url = None

    with pixel_session(force=force) as cdp_url:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                pages = context.pages
                if not pages:
                    return {"closed_tabs": 0, "remaining_tab": None}

                keep_page = pages[0]
                if keep_url:
                    for pg in pages:
                        if keep_url.lower() in pg.url.lower():
                            keep_page = pg
                            break

                closed_count = close_extra_tabs(context, keep_page=keep_page)
                remaining_url = keep_page.url
            finally:
                browser.close()

    return {"closed_tabs": closed_count, "remaining_tab": remaining_url}


def browse_url(
    url: str,
    action: str = "text",
    output_path: Optional[str] = None,
    timeout_ms: int = 25000,
    force: bool = False,
) -> Dict[str, Any]:
    """Execute browsing action on Pixel 8 Pro Chrome over CDP."""
    from playwright.sync_api import sync_playwright

    result: Dict[str, Any] = {
        "url": url,
        "action": action,
        "success": False,
        "title": None,
        "content": None,
        "cookies": None,
        "screenshot_path": None,
    }

    with pixel_session(force=force) as cdp_url:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                
                # Pre-clean: close any extraneous background tabs before navigating
                close_extra_tabs(context)
                page = context.pages[0] if context.pages else context.new_page()

                # Navigate existing single page
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                time.sleep(1.5)  # Allow mobile JS bundles to settle

                result["title"] = page.title()
                result["current_url"] = page.url

                if action == "text":
                    result["content"] = page.inner_text("body")
                elif action == "html":
                    result["content"] = page.content()
                elif action == "cookies":
                    result["cookies"] = context.cookies()
                elif action == "screenshot":
                    out = output_path or "/workspace/data/pixel_browser_screenshot.png"
                    Path(out).parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=out)
                    result["screenshot_path"] = out

                result["success"] = True
            finally:
                # Post-clean: ensure any popups or newly spawned tabs during session are closed
                try:
                    close_extra_tabs(context, keep_page=page)
                except Exception:
                    pass
                browser.close()

    return result


def main():
    parser = argparse.ArgumentParser(description="Pixel 8 Pro Mobile Browser Automation (ADB CDP)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Check Pixel 8 Pro hardware, battery, and camera status")

    # browse
    browse_p = subparsers.add_parser("browse", help="Navigate to URL and extract page text or DOM")
    browse_p.add_argument("url", help="Target URL to browse")
    browse_p.add_argument("--extract", choices=["text", "html", "cookies"], default="text", help="Content to extract")
    browse_p.add_argument("--timeout", type=int, default=25, help="Navigation timeout in seconds")
    browse_p.add_argument("--force", action="store_true", help="Override active 3D printer camera conflict guard")

    # screenshot
    snap_p = subparsers.add_parser("screenshot", help="Capture full page screenshot on mobile Chrome")
    snap_p.add_argument("url", help="Target URL to capture")
    snap_p.add_argument("--output", "-o", default="/workspace/data/pixel_browser_screenshot.png", help="Output PNG path")
    snap_p.add_argument("--timeout", type=int, default=25, help="Navigation timeout in seconds")
    snap_p.add_argument("--force", action="store_true", help="Override active 3D printer camera conflict guard")

    # cleanup-tabs
    clean_p = subparsers.add_parser("cleanup-tabs", help="Close all open tabs except the primary tab")
    clean_p.add_argument("--keep-url", default=None, help="Substring of URL to preserve if present")
    clean_p.add_argument("--force", action="store_true", help="Override active 3D printer camera conflict guard")

    args = parser.parse_args()

    if args.command == "status":
        st = get_device_status()
        print(json.dumps(st, indent=2))
        sys.exit(0)

    elif args.command == "cleanup-tabs":
        res = cleanup_tabs(keep_url=args.keep_url, force=args.force)
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.command == "browse":
        res = browse_url(
            url=args.url,
            action=args.extract,
            timeout_ms=args.timeout * 1000,
            force=args.force,
        )
        if not res["success"]:
            print(json.dumps(res, indent=2), file=sys.stderr)
            sys.exit(1)
        print(f"Title: {res.get('title')}")
        print(f"URL: {res.get('current_url')}")
        if args.extract == "cookies":
            print(json.dumps(res.get("cookies", []), indent=2))
        else:
            print("\n--- Extracted Content ---")
            print(res.get("content", ""))

    elif args.command == "screenshot":
        res = browse_url(
            url=args.url,
            action="screenshot",
            output_path=args.output,
            timeout_ms=args.timeout * 1000,
            force=args.force,
        )
        if not res["success"]:
            print(json.dumps(res, indent=2), file=sys.stderr)
            sys.exit(1)
        print(f"Screenshot captured: {res.get('screenshot_path')} (Page Title: {res.get('title')})")


if __name__ == "__main__":
    main()
