#!/usr/bin/env python3
"""Autonomous Web QA & Browser Evaluation Tool for Zero.

Provides on-demand headless Chromium execution via Playwright to:
- Navigate to local, staged, or deployed web applications
- Capture console logs, JS runtime exceptions, and HTTP network errors
- Extract rendered DOM elements (headings, buttons, inputs, links, forms)
- Capture full-page or viewport screenshots to disk
- Run assertions (expected text, expected selectors, zero console errors)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCREENSHOTS_DIR = Path("/workspace/data/qa_screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def run_web_qa(
    url: str,
    screenshot: bool = True,
    full_page: bool = True,
    screenshot_name: Optional[str] = None,
    wait_selector: Optional[str] = None,
    wait_ms: int = 1500,
    expect_selectors: Optional[List[str]] = None,
    expect_texts: Optional[List[str]] = None,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Execute autonomous browser QA on a target URL."""
    from playwright.sync_api import sync_playwright

    console_logs: List[Dict[str, str]] = []
    page_errors: List[str] = []
    network_errors: List[Dict[str, Any]] = []

    start_time = time.time()

    with sync_playwright() as p:
        # Launch Chromium headless
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
            viewport={"width": viewport_width, "height": viewport_height},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Zero-QA/1.0",
        )
        page = context.new_page()

        # Wire event listeners
        page.on("console", lambda msg: console_logs.append({"type": msg.type, "text": msg.text}))
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on(
            "response",
            lambda resp: network_errors.append({"url": resp.url, "status": resp.status, "statusText": resp.status_text})
            if resp.status >= 400
            else None,
        )

        # Navigate
        try:
            response = page.goto(url, timeout=timeout_ms, wait_until="load")
            http_status = response.status if response else 0
        except Exception as e:
            browser.close()
            return {
                "success": False,
                "url": url,
                "error": f"Navigation failed: {e}",
                "elapsed_seconds": round(time.time() - start_time, 2),
            }

        # Wait for selector or timeout
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            except Exception as e:
                page_errors.append(f"Timeout waiting for selector '{wait_selector}': {e}")

        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        # Capture metadata
        title = page.title()
        url_final = page.url

        # DOM inspection
        headings = [h.inner_text().strip() for h in page.locator("h1, h2, h3").all() if h.inner_text().strip()]
        buttons = [b.inner_text().strip() for b in page.locator("button, a.btn, input[type='submit']").all() if b.inner_text().strip()]
        links_count = page.locator("a[href]").count()
        inputs_count = page.locator("input, textarea, select").count()

        # Assertion evaluations
        assertion_results = []
        all_passed = True

        if expect_selectors:
            for sel in expect_selectors:
                count = page.locator(sel).count()
                passed = count > 0
                if not passed:
                    all_passed = False
                assertion_results.append({
                    "type": "selector",
                    "target": sel,
                    "passed": passed,
                    "matched_count": count,
                })

        if expect_texts:
            for text in expect_texts:
                count = page.get_by_text(text, exact=False).count()
                passed = count > 0
                if not passed:
                    all_passed = False
                assertion_results.append({
                    "type": "text",
                    "target": text,
                    "passed": passed,
                    "matched_count": count,
                })

        # Screenshot capture
        saved_screenshot_path = None
        if screenshot:
            ts = int(time.time())
            safe_name = (screenshot_name or f"qa_{ts}").replace("/", "_").replace(":", "_")
            if not safe_name.endswith(".png"):
                safe_name += ".png"
            dest_file = SCREENSHOTS_DIR / safe_name
            page.screenshot(path=str(dest_file), full_page=full_page)
            saved_screenshot_path = str(dest_file)

        browser.close()

    elapsed = round(time.time() - start_time, 2)
    has_critical_errors = len(page_errors) > 0 or any(l["type"] == "error" for l in console_logs)

    return {
        "success": all_passed and not has_critical_errors,
        "url": url,
        "final_url": url_final,
        "http_status": http_status,
        "title": title,
        "elapsed_seconds": elapsed,
        "dom_summary": {
            "headings": headings[:10],
            "sample_buttons": buttons[:10],
            "total_links": links_count,
            "total_inputs": inputs_count,
        },
        "assertions": assertion_results,
        "console_logs": console_logs,
        "page_errors": page_errors,
        "network_errors": network_errors,
        "screenshot_path": saved_screenshot_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Zero Autonomous Web QA Tool")
    parser.add_argument("url", help="Target URL to inspect (http/https)")
    parser.add_argument("--screenshot-name", "-s", help="Custom filename for screenshot")
    parser.add_argument("--viewport-only", action="store_true", help="Capture viewport only instead of full page")
    parser.add_argument("--no-screenshot", action="store_true", help="Skip capturing screenshot")
    parser.add_argument("--wait-selector", "-w", help="CSS selector to wait for before evaluation")
    parser.add_argument("--wait-ms", type=int, default=1000, help="Additional ms to wait after load (default: 1000)")
    parser.add_argument("--expect-selector", "-es", action="append", default=[], help="Assert CSS selector exists")
    parser.add_argument("--expect-text", "-et", action="append", default=[], help="Assert text appears on page")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")

    args = parser.parse_args()

    result = run_web_qa(
        url=args.url,
        screenshot=not args.no_screenshot,
        full_page=not args.viewport_only,
        screenshot_name=args.screenshot_name,
        wait_selector=args.wait_selector,
        wait_ms=args.wait_ms,
        expect_selectors=args.expect_selector,
        expect_texts=args.expect_text,
    )

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("success") else 1)

    print("=" * 60)
    status_emoji = "✅ PASS" if result.get("success") else "❌ FAIL"
    print(f"WEB QA AUDIT: {status_emoji} | HTTP {result.get('http_status')} | {result.get('elapsed_seconds')}s")
    print(f"URL:   {result.get('url')}")
    print(f"Title: {result.get('title')}")
    print("=" * 60)

    if result.get("dom_summary"):
        dom = result["dom_summary"]
        print("\n📋 DOM Summary:")
        if dom.get("headings"):
            print("  • Headings:", " | ".join(dom["headings"][:5]))
        if dom.get("sample_buttons"):
            print("  • Buttons:", " | ".join(dom["sample_buttons"][:5]))
        print(f"  • Total Links: {dom.get('total_links', 0)} | Total Inputs: {dom.get('total_inputs', 0)}")

    if result.get("assertions"):
        print("\n🔍 Assertions:")
        for a in result["assertions"]:
            mark = "✅" if a["passed"] else "❌"
            print(f"  {mark} [{a['type']}] {a['target']} (matches: {a['matched_count']})")

    if result.get("page_errors"):
        print("\n🚨 Page Exceptions:")
        for err in result["page_errors"]:
            print(f"  • {err}")

    if result.get("network_errors"):
        print("\n⚠️ Failed Network Requests (4xx/5xx):")
        for ne in result["network_errors"][:5]:
            print(f"  • {ne['status']} {ne['statusText']}: {ne['url']}")

    if result.get("console_logs"):
        err_logs = [l for l in result["console_logs"] if l["type"] == "error"]
        if err_logs:
            print(f"\n🪵 Console Errors ({len(err_logs)}):")
            for cl in err_logs[:5]:
                print(f"  • [{cl['type']}] {cl['text']}")

    if result.get("screenshot_path"):
        print(f"\n📸 Screenshot: {result['screenshot_path']}")

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
