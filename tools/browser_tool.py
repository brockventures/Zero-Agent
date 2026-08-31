#!/usr/bin/env python3
"""Headless Browser Tool for Zero.

Interfaces with headless Browserless Chromium container (via BROWSERLESS_URL).
Supports:
- Rendered HTML content fetching (JS execution)
- Full-page or viewport screenshots (saved to file)
- Structured element scraping
- Health checks
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

def _resolve_host_2():
    host_2 = os.environ.get("NAS_HOST_2_IP")
    if not host_2 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_2_IP"):
                    return d["NAS_HOST_2_IP"]
                elif d.get("HA_BASE_URL"):
                    h1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
                    parts = h1.split(".")
                    if len(parts) == 4 and parts[-1] == "82":
                        return ".".join(parts[:3] + ["84"])
        except Exception:
            pass
    if not host_2 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    h1 = urllib.parse.urlparse(d["url"]).hostname
                    parts = h1.split(".")
                    if len(parts) == 4 and parts[-1] == "82":
                        return ".".join(parts[:3] + ["84"])
        except Exception:
            pass
    return host_2 or "127.0.0.1"

HOST_2_IP = _resolve_host_2()
BROWSERLESS_URL = os.environ.get("BROWSERLESS_URL", f"http://{HOST_2_IP}:3000")
TIMEOUT = 45

def get_content(url: str, wait_for_selector: str = None, wait_for_timeout: int = 1000) -> str:
    """Fetch fully rendered HTML after JavaScript execution."""
    payload = {"url": url}
    if wait_for_selector:
        payload["waitForSelector"] = wait_for_selector
    if wait_for_timeout:
        payload["waitForTimeout"] = wait_for_timeout

    req = urllib.request.Request(
        f"{BROWSERLESS_URL}/content",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")

def take_screenshot(url: str, output_path: str, full_page: bool = True, wait_for_selector: str = None, wait_for_timeout: int = 1000) -> str:
    """Capture a rendered screenshot of a web page and save to disk."""
    payload = {
        "url": url,
        "options": {
            "fullPage": full_page,
            "type": "png"
        }
    }
    if wait_for_selector:
        payload["waitForSelector"] = wait_for_selector
    if wait_for_timeout:
        payload["waitForTimeout"] = wait_for_timeout

    req = urllib.request.Request(
        f"{BROWSERLESS_URL}/screenshot",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        return output_path

def scrape_elements(url: str, selectors: list[str]) -> dict:
    """Scrape specific CSS selectors from a rendered page."""
    payload = {
        "url": url,
        "elements": [{"selector": s} for s in selectors]
    }
    req = urllib.request.Request(
        f"{BROWSERLESS_URL}/scrape",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))

def check_health() -> dict:
    """Check health and concurrency status of the browserless container."""
    req = urllib.request.Request(f"{BROWSERLESS_URL}/pressure")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zero Headless Browser Tool")
    subparsers = parser.add_subparsers(dest="command")

    # Content
    p_content = subparsers.add_parser("content")
    p_content.add_argument("url")
    p_content.add_argument("--wait-selector")
    p_content.add_argument("--wait-ms", type=int, default=1000)

    # Screenshot
    p_shot = subparsers.add_parser("screenshot")
    p_shot.add_argument("url")
    p_shot.add_argument("output")
    p_shot.add_argument("--viewport-only", action="store_true")
    p_shot.add_argument("--wait-selector")
    p_shot.add_argument("--wait-ms", type=int, default=1000)

    # Health
    p_health = subparsers.add_parser("health")

    args = parser.parse_args()

    if args.command == "health":
        print(json.dumps(check_health(), indent=2))
    elif args.command == "content":
        print(get_content(args.url, args.wait_selector, args.wait_ms))
    elif args.command == "screenshot":
        out = take_screenshot(args.url, args.output, full_page=not args.viewport_only, wait_for_selector=args.wait_selector, wait_for_timeout=args.wait_ms)
        print(f"Screenshot saved to: {out}")
    else:
        parser.print_help()
