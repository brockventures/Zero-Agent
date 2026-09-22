#!/usr/bin/env python3
"""capture_screenshot.py - Zero UI Preview & Screenshot Capture Tool.

Captures high-fidelity headless browser screenshots of local dev servers,
staged web apps, dashboards, or specific UI components via Playwright and
delivers them directly to Discord channels.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure workspace root is in sys.path
WORKSPACE_DIR = str(Path(__file__).resolve().parent.parent)
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

DATA_DIR = Path("/workspace/data")
SCREENSHOTS_DIR = DATA_DIR / "qa_screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
BRAIN_ROOT = Path("/root/.gemini/antigravity-cli/brain")

VIEWPORT_PRESETS = {
    "desktop": (1280, 800),
    "desktop-hd": (1920, 1080),
    "desktop-wide": (1440, 900),
    "mobile": (390, 844),
    "tablet": (820, 1180),
}


def parse_viewport(viewport_str: str) -> Tuple[int, int]:
    """Parse viewport preset name or WxH string into (width, height)."""
    preset = VIEWPORT_PRESETS.get(viewport_str.lower())
    if preset:
        return preset
    if "x" in viewport_str.lower():
        parts = viewport_str.lower().split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return (1280, 800)


def get_active_brain_dir() -> Optional[Path]:
    """Find the most recently modified conversation session directory in brain."""
    if not BRAIN_ROOT.exists():
        return None
    conv_dirs = [d for d in BRAIN_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not conv_dirs:
        return None
    return max(conv_dirs, key=lambda d: d.stat().st_mtime)


def capture_screenshot(
    url: str,
    output_name: Optional[str] = None,
    selector: Optional[str] = None,
    full_page: bool = False,
    viewport: str = "desktop",
    color_scheme: str = "dark",
    wait_selector: Optional[str] = None,
    wait_ms: int = 1500,
    timeout_ms: int = 30000,
    padding: int = 8,
) -> Dict[str, Any]:
    """Capture a screenshot of a target URL or element using Playwright."""
    from PIL import Image
    from playwright.sync_api import sync_playwright

    width, height = parse_viewport(viewport)
    start_time = time.time()

    ts = int(time.time())
    safe_name = (output_name or f"preview_{ts}").replace("/", "_").replace(":", "_")
    if not safe_name.lower().endswith(".png"):
        safe_name += ".png"
    target_file = SCREENSHOTS_DIR / safe_name

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ],
        )
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=color_scheme,  # type: ignore
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Zero-Preview/1.0",
        )
        page = context.new_page()

        try:
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        except Exception:
            # Fallback to load event if networkidle times out
            try:
                page.goto(url, timeout=timeout_ms, wait_until="load")
            except Exception as e:
                browser.close()
                return {
                    "success": False,
                    "url": url,
                    "error": f"Navigation failed: {e}",
                    "elapsed_seconds": round(time.time() - start_time, 2),
                }

        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            except Exception as e:
                browser.close()
                return {
                    "success": False,
                    "url": url,
                    "error": f"Timeout waiting for wait-selector '{wait_selector}': {e}",
                    "elapsed_seconds": round(time.time() - start_time, 2),
                }

        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        # Handle selector-based capture
        if selector:
            locators = page.locator(selector).all()
            if not locators:
                browser.close()
                return {
                    "success": False,
                    "url": url,
                    "error": f"Selector '{selector}' matched 0 elements.",
                    "elapsed_seconds": round(time.time() - start_time, 2),
                }

            if len(locators) == 1:
                locators[0].screenshot(path=str(target_file))
            else:
                # Union bounding boxes of matched elements
                boxes = [loc.bounding_box() for loc in locators]
                valid_boxes = [b for b in boxes if b and b["width"] > 0 and b["height"] > 0]
                if not valid_boxes:
                    browser.close()
                    return {
                        "success": False,
                        "url": url,
                        "error": f"Selector '{selector}' matched elements with no visible dimensions.",
                        "elapsed_seconds": round(time.time() - start_time, 2),
                    }
                min_x = min(b["x"] for b in valid_boxes) - padding
                min_y = min(b["y"] for b in valid_boxes) - padding
                max_x = max(b["x"] + b["width"] for b in valid_boxes) + padding
                max_y = max(b["y"] + b["height"] for b in valid_boxes) + padding

                clip_x = max(0, min_x)
                clip_y = max(0, min_y)
                clip_w = max_x - clip_x
                clip_h = max_y - clip_y

                page.screenshot(
                    path=str(target_file),
                    clip={"x": clip_x, "y": clip_y, "width": clip_w, "height": clip_h},
                )
        else:
            page.screenshot(path=str(target_file), full_page=full_page)

        page_title = page.title()
        browser.close()

    if not target_file.exists() or target_file.stat().st_size == 0:
        return {
            "success": False,
            "url": url,
            "error": "Screenshot file was not created or is empty.",
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

    # Verify image dimensions
    with Image.open(target_file) as img:
        img_w, img_h = img.size
        img_format = img.format

    size_bytes = target_file.stat().st_size
    size_kb = round(size_bytes / 1024.0, 2)

    # Sync to brain session directory if present
    brain_dir = get_active_brain_dir()
    brain_path = None
    if brain_dir and brain_dir.exists():
        try:
            dest_brain_file = brain_dir / target_file.name
            shutil.copy2(target_file, dest_brain_file)
            brain_path = str(dest_brain_file)
        except Exception:
            pass

    return {
        "success": True,
        "url": url,
        "title": page_title,
        "file_path": str(target_file),
        "brain_path": brain_path,
        "filename": target_file.name,
        "format": img_format,
        "width": img_w,
        "height": img_h,
        "dimensions": f"{img_w}x{img_h}",
        "size_kb": size_kb,
        "elapsed_seconds": round(time.time() - start_time, 2),
    }


def capture_and_deliver(
    url: str,
    channel: Optional[str] = None,
    caption: Optional[str] = None,
    output_name: Optional[str] = None,
    selector: Optional[str] = None,
    full_page: bool = False,
    viewport: str = "desktop",
    color_scheme: str = "dark",
    wait_selector: Optional[str] = None,
    wait_ms: int = 1500,
) -> Dict[str, Any]:
    """Capture screenshot and optionally deliver directly to Discord."""
    res = capture_screenshot(
        url=url,
        output_name=output_name,
        selector=selector,
        full_page=full_page,
        viewport=viewport,
        color_scheme=color_scheme,
        wait_selector=wait_selector,
        wait_ms=wait_ms,
    )

    if not res.get("success"):
        return res

    if channel:
        try:
            from tools.deliver_image import deliver_image

            delivery = deliver_image(
                image_path=res["file_path"],
                channel_input=channel,
                caption=caption,
            )
            res["delivered"] = delivery.get("success", False)
            res["delivery_info"] = delivery
            if not delivery.get("success"):
                res["delivery_error"] = delivery.get("error")
        except Exception as de:
            res["delivered"] = False
            res["delivery_error"] = str(de)
    else:
        res["delivered"] = False

    return res


def main():
    parser = argparse.ArgumentParser(
        description="Zero UI Preview & Screenshot Capture Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Capture full viewport of local or staging app
  python3 tools/capture_screenshot.py "http://localhost:8008" --name homepage_preview

  # Capture specific UI cards and deliver directly to Discord
  python3 tools/capture_screenshot.py "http://localhost:8008" \\
    --selector ".service:has-text('Zero'), .service:has-text('Ivy')" \\
    --channel zero-chat \\
    --caption "Live Homepage Telemetry Cards"

  # Capture mobile viewport
  python3 tools/capture_screenshot.py "http://localhost:3000" --viewport mobile --name mobile_view
"""
    )
    parser.add_argument("url", help="Target URL or local file path to capture")
    parser.add_argument("--name", "-n", help="Base filename for saved screenshot (e.g. homepage_cards)")
    parser.add_argument("--selector", "-s", help="CSS or text selector to capture specific element(s)")
    parser.add_argument("--full-page", "-f", action="store_true", help="Capture full scrollable page")
    parser.add_argument("--viewport", "-v", default="desktop", help="Viewport preset (desktop, mobile, tablet, desktop-hd) or WxH")
    parser.add_argument("--color-scheme", default="dark", choices=["dark", "light", "no-preference"], help="Color scheme (default: dark)")
    parser.add_argument("--wait-selector", help="Selector to wait for before capturing")
    parser.add_argument("--wait-ms", type=int, default=1500, help="Wait time in ms after navigation (default: 1500)")
    parser.add_argument("--channel", "-c", help="Target Discord channel to upload to (e.g. zero-chat, lounge)")
    parser.add_argument("--caption", "-m", help="Caption text to accompany Discord image upload")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    result = capture_and_deliver(
        url=args.url,
        channel=args.channel,
        caption=args.caption,
        output_name=args.name,
        selector=args.selector,
        full_page=args.full_page,
        viewport=args.viewport,
        color_scheme=args.color_scheme,
        wait_selector=args.wait_selector,
        wait_ms=args.wait_ms,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("success"):
            print("============================================================")
            print(f"📸 UI PREVIEW CAPTURED: ✅ SUCCESS | {result['dimensions']} ({result['size_kb']} KB)")
            print(f"URL:      {result['url']}")
            print(f"Path:     {result['file_path']}")
            if result.get("brain_path"):
                print(f"Artifact: {result['brain_path']}")
            if result.get("delivered"):
                d_info = result["delivery_info"]
                print(f"🚀 Delivered to #{d_info.get('channel')} ({d_info.get('message_id')})")
                if d_info.get("attachments"):
                    print(f"Discord URL: {d_info['attachments'][0].get('url')}")
            elif result.get("delivery_error"):
                print(f"⚠️ Delivery Failed: {result.get('delivery_error')}")
            print("============================================================")
        else:
            print("============================================================")
            print(f"📸 UI PREVIEW CAPTURED: ❌ FAILED")
            print(f"URL:   {result.get('url')}")
            print(f"Error: {result.get('error')}")
            print("============================================================")
            sys.exit(1)


if __name__ == "__main__":
    main()
