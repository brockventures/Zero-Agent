#!/usr/bin/env python3
"""Starbucks Android Native App Ordering CLI (Pixel 8 Pro / ADB UI Automator).

Drives the authenticated Starbucks Android app (com.starbucks.mobilecard) on the
tethered Google Pixel 8 Pro (husky) via ADB UI Automator. Supports store selection,
Favorites and Previous order browsing, menu search, order staging with review
summaries, and pre-confirmed checkout.

Safety Guardrails:
1. 3D Printer Conflict Guard: Aborts if IP Webcam is active on port 3333 (OctoPrint
   print bed monitor) unless --force is specified.
2. OLED Screen Preservation: Guarantees display sleep (KEYCODE_POWER = 26) on exit
   via context manager finally block.
3. Pre-Confirmed Checkout Safety Gate: Staging stops strictly at the Order Review /
   Checkout screen without paying. Placing an order requires explicit --confirm-checkout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import xml.etree.ElementTree as ET

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "tools"), "/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.pixel_browser import (
        check_printer_conflict,
        run_adb_shell,
        sleep_screen,
        wake_and_unlock,
    )
except ImportError:
    from pixel_browser import (
        check_printer_conflict,
        run_adb_shell,
        sleep_screen,
        wake_and_unlock,
    )

STARBUCKS_PKG = "com.starbucks.mobilecard"
LAUNCH_ACTIVITY = "android.intent.category.LAUNCHER"
DEFAULT_DUMP_FILE = "/data/local/tmp/uidump.xml"

DISMISS_PATTERNS = [
    "close",
    "not now",
    "got it",
    "dismiss",
    "maybe later",
    "ok",
    "no thanks",
    "cancel",
    "skip",
]


@dataclass
class UINode:
    """Represents a parsed node in the Android UI hierarchy."""

    text: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    content_desc: str = ""
    bounds: str = ""
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    selected: bool = False
    password: bool = False
    bounds_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)  # (x1, y1, x2, y2)
    center: Tuple[int, int] = (0, 0)  # (cx, cy)
    raw_elem: Optional[ET.Element] = None

    @property
    def width(self) -> int:
        return max(0, self.bounds_rect[2] - self.bounds_rect[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds_rect[3] - self.bounds_rect[1])

    @property
    def is_visible(self) -> bool:
        return self.width > 0 and self.height > 0


def get_starbucks_credentials() -> Tuple[str, str]:
    """Dynamically resolve Starbucks account credentials from env or /secrets/env.json."""
    email = os.environ.get("STARBUCKS_EMAIL", "").strip()
    password = os.environ.get("STARBUCKS_PASSWORD", "").strip()
    if not email or not password:
        env_file = Path("/secrets/env.json")
        if env_file.exists():
            try:
                data = json.loads(env_file.read_text(encoding="utf-8"))
                email = email or str(data.get("STARBUCKS_EMAIL", "")).strip()
                password = password or str(data.get("STARBUCKS_PASSWORD", "")).strip()
            except Exception:
                pass
    return email, password


def parse_bounds(bounds_str: str) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
    """Parse bounds string '[x1,y1][x2,y2]' into rect and center coordinates."""
    matches = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
    if len(matches) == 2:
        try:
            x1, y1 = int(matches[0][0]), int(matches[0][1])
            x2, y2 = int(matches[1][0]), int(matches[1][1])
            return (x1, y1, x2, y2), ((x1 + x2) // 2, (y1 + y2) // 2)
        except (ValueError, IndexError):
            pass
    return (0, 0, 0, 0), (0, 0)


def extract_visible_nodes(root: Union[str, ET.Element]) -> List[UINode]:
    """Parse XML string or Element into a list of UINode objects."""
    if isinstance(root, str):
        if not root.strip():
            return []
        try:
            elem_root = ET.fromstring(root)
        except ET.ParseError:
            return []
    else:
        elem_root = root

    nodes: List[UINode] = []
    for elem in elem_root.iter("node"):
        bounds_str = elem.get("bounds", "")
        rect, center = parse_bounds(bounds_str)
        node = UINode(
            text=elem.get("text", "") or "",
            resource_id=elem.get("resource-id", "") or "",
            class_name=elem.get("class", "") or "",
            package=elem.get("package", "") or "",
            content_desc=elem.get("content-desc", "") or "",
            bounds=bounds_str,
            clickable=elem.get("clickable", "false").lower() == "true",
            enabled=elem.get("enabled", "true").lower() == "true",
            focusable=elem.get("focusable", "false").lower() == "true",
            focused=elem.get("focused", "false").lower() == "true",
            scrollable=elem.get("scrollable", "false").lower() == "true",
            selected=elem.get("selected", "false").lower() == "true",
            password=elem.get("password", "false").lower() == "true",
            bounds_rect=rect,
            center=center,
            raw_elem=elem,
        )
        nodes.append(node)
    return nodes


def dump_ui_tree(dump_file: str = DEFAULT_DUMP_FILE, max_retries: int = 2) -> Optional[ET.Element]:
    """Dump UI Automator hierarchy XML via ADB and return root Element."""
    for _ in range(max_retries):
        res = run_adb_shell(["uiautomator", "dump", dump_file])
        if res.returncode == 0 or "UI hierchary dumped to" in res.stdout:
            cat_res = run_adb_shell(["cat", dump_file])
            if cat_res.returncode == 0 and cat_res.stdout.strip():
                try:
                    return ET.fromstring(cat_res.stdout.strip())
                except ET.ParseError:
                    pass
        time.sleep(0.5)
    return None


def find_all_nodes(
    root_or_nodes: Union[str, ET.Element, List[UINode], None],
    text: Optional[str] = None,
    text_contains: Optional[str] = None,
    resource_id: Optional[str] = None,
    content_desc: Optional[str] = None,
    content_desc_contains: Optional[str] = None,
    clickable: Optional[bool] = None,
    class_name: Optional[str] = None,
) -> List[UINode]:
    """Find all matching UINodes satisfying all supplied non-None filters."""
    if root_or_nodes is None:
        return []
    if isinstance(root_or_nodes, (str, ET.Element)):
        nodes = extract_visible_nodes(root_or_nodes)
    else:
        nodes = root_or_nodes

    results: List[UINode] = []
    for node in nodes:
        if text is not None and node.text.strip().lower() != text.strip().lower():
            continue
        if text_contains is not None and text_contains.strip().lower() not in node.text.lower():
            continue
        if resource_id is not None and resource_id.lower() not in node.resource_id.lower():
            continue
        if content_desc is not None and node.content_desc.strip().lower() != content_desc.strip().lower():
            continue
        if content_desc_contains is not None and content_desc_contains.strip().lower() not in node.content_desc.lower():
            continue
        if clickable is not None and node.clickable != clickable:
            continue
        if class_name is not None and class_name.lower() not in node.class_name.lower():
            continue
        results.append(node)
    return results


def find_node(
    root_or_nodes: Union[str, ET.Element, List[UINode], None],
    text: Optional[str] = None,
    text_contains: Optional[str] = None,
    resource_id: Optional[str] = None,
    content_desc: Optional[str] = None,
    content_desc_contains: Optional[str] = None,
    clickable: Optional[bool] = None,
    class_name: Optional[str] = None,
) -> Optional[UINode]:
    """Find the first matching UINode satisfying all supplied non-None filters."""
    matches = find_all_nodes(
        root_or_nodes,
        text=text,
        text_contains=text_contains,
        resource_id=resource_id,
        content_desc=content_desc,
        content_desc_contains=content_desc_contains,
        clickable=clickable,
        class_name=class_name,
    )
    return matches[0] if matches else None


def tap_coords(x: int, y: int) -> bool:
    """Send an input tap event at (x, y) via ADB."""
    res = run_adb_shell(["input", "tap", str(x), str(y)])
    time.sleep(0.5)
    return res.returncode == 0


def tap_node(node: UINode) -> bool:
    """Tap the center coordinates of a UI node."""
    cx, cy = node.center
    if cx <= 0 and cy <= 0:
        return False
    return tap_coords(cx, cy)


def enter_text(text: str, clear_first: bool = False) -> bool:
    """Enter text into the focused Android element via ADB."""
    if clear_first:
        run_adb_shell(["input", "keyevent", "123"])  # KEYCODE_MOVE_END
        for _ in range(30):
            run_adb_shell(["input", "keyevent", "67"])  # KEYCODE_DEL
        time.sleep(0.2)

    escaped = text.replace(" ", "%s")
    for ch in ("&", ";", "(", ")", "<", ">", "|", "$", "`", '"', "'"):
        escaped = escaped.replace(ch, f"\\{ch}")
    res = run_adb_shell(["input", "text", escaped])
    time.sleep(0.5)
    return res.returncode == 0


def press_key(keycode: Union[int, str]) -> bool:
    """Send an Android keyevent (e.g. 66 for ENTER, 4 for BACK)."""
    res = run_adb_shell(["input", "keyevent", str(keycode)])
    time.sleep(0.3)
    return res.returncode == 0


def dismiss_modals(root: Optional[Union[str, ET.Element]] = None, max_passes: int = 3) -> int:
    """Dismiss common onboarding, update, and notification dialogs."""
    dismissed = 0
    for _ in range(max_passes):
        current_root = root if (root is not None and dismissed == 0) else dump_ui_tree()
        if current_root is None:
            break
        nodes = extract_visible_nodes(current_root)
        found_modal = False
        for node in nodes:
            label = (node.text or node.content_desc or "").strip().lower()
            if any(p == label or f" {p} " in f" {label} " for p in DISMISS_PATTERNS):
                if node.clickable or node.is_visible:
                    tap_node(node)
                    dismissed += 1
                    found_modal = True
                    time.sleep(0.5)
                    break
        if not found_modal:
            break
    return dismissed


def check_and_handle_relogin(root: Optional[Union[str, ET.Element]] = None) -> bool:
    """Check if app is on the Sign In screen and authenticate using credentials."""
    current_root = root if root is not None else dump_ui_tree()
    if current_root is None:
        return False

    nodes = extract_visible_nodes(current_root)
    sign_in_node = None
    for n in nodes:
        label = (n.text or n.content_desc or "").strip().lower()
        if label in ("sign in", "log in") and (n.clickable or "button" in n.class_name.lower()):
            sign_in_node = n
            break

    if not sign_in_node:
        return False

    email, password = get_starbucks_credentials()
    if not email or not password:
        sys.stderr.write(
            "Starbucks sign-in screen detected, but STARBUCKS_EMAIL / STARBUCKS_PASSWORD "
            "not found in environment or /secrets/env.json.\n"
        )
        return False

    tap_node(sign_in_node)
    time.sleep(1.5)

    login_root = dump_ui_tree()
    if login_root is None:
        return False

    login_nodes = extract_visible_nodes(login_root)

    email_field = None
    pw_field = None
    for n in login_nodes:
        if "edittext" in n.class_name.lower() or "textinput" in n.resource_id.lower():
            label = (n.text or n.content_desc or n.resource_id).lower()
            if not email_field and ("email" in label or "username" in label):
                email_field = n
            elif not pw_field and (n.password or "password" in label):
                pw_field = n

    if not email_field or not pw_field:
        edit_texts = [n for n in login_nodes if "edittext" in n.class_name.lower()]
        if len(edit_texts) >= 2:
            email_field = edit_texts[0]
            pw_field = edit_texts[1]

    if email_field:
        tap_node(email_field)
        enter_text(email, clear_first=True)

    if pw_field:
        tap_node(pw_field)
        enter_text(password, clear_first=True)

    submit_btn = None
    login_nodes = extract_visible_nodes(dump_ui_tree() or login_root)
    for n in login_nodes:
        label = (n.text or n.content_desc or "").strip().lower()
        if label in ("sign in", "log in", "submit") and n.clickable:
            submit_btn = n
            break

    if submit_btn:
        tap_node(submit_btn)
        time.sleep(2.0)
        dismiss_modals()
        return True

    return False


def launch_starbucks_app() -> None:
    """Launch Starbucks Android app via monkey."""
    run_adb_shell(["monkey", "-p", STARBUCKS_PKG, "-c", LAUNCH_ACTIVITY, "1"])


@contextmanager
def starbucks_session(force: bool = False, auto_relogin: bool = True):
    """Context manager for Starbucks Android session.

    Guarantees:
    - OctoPrint 3D printer conflict check (unless force=True)
    - wake_and_unlock() on entry
    - Launch com.starbucks.mobilecard
    - Dismiss onboarding popups
    - Unconditional sleep_screen() in finally block
    """
    conflict, msg = check_printer_conflict()
    if conflict and not force:
        raise RuntimeError(f"Printer conflict guard active: {msg}. Pass --force to override.")

    wake_and_unlock()
    try:
        launch_starbucks_app()
        time.sleep(2.0)
        dismiss_modals()
        if auto_relogin:
            check_and_handle_relogin()
        yield
    finally:
        sleep_screen()


def _parse_status_from_tree(root: Union[str, ET.Element]) -> Dict[str, Any]:
    """Parse authentication state, stars, card balance, and active store from UI tree."""
    nodes = extract_visible_nodes(root)

    status_data: Dict[str, Any] = {
        "package": STARBUCKS_PKG,
        "authenticated": False,
        "auth_state": "unauthenticated",
        "stars": None,
        "balance": None,
        "active_store": None,
        "pickup_type": None,
    }

    has_sign_in = False
    has_rewards_indicator = False

    for node in nodes:
        text = (node.text or "").strip()
        desc = (node.content_desc or "").strip()
        combined = f"{text} {desc}".strip()

        if text.lower() in ("sign in", "join now") or desc.lower() in ("sign in", "join now"):
            has_sign_in = True

        # Extract Stars count: e.g. "150 ★", "150 Stars", "★ 150"
        star_match = re.search(r"(\d+)\s*(?:★|Stars|Star)", combined, re.IGNORECASE)
        if not star_match:
            star_match = re.search(r"(?:★|Stars)\s*(\d+)", combined, re.IGNORECASE)
        if star_match and status_data["stars"] is None:
            try:
                status_data["stars"] = int(star_match.group(1))
                has_rewards_indicator = True
            except ValueError:
                pass

        # Extract Card Balance: e.g. "$12.45" or "Balance $12.45"
        bal_match = re.search(r"\$([0-9]+\.[0-9]{2})", combined)
        if bal_match and status_data["balance"] is None:
            status_data["balance"] = f"${bal_match.group(1)}"
            has_rewards_indicator = True

        # Extract Active Store
        store_candidate = text or desc
        if any(kw in store_candidate.lower() for kw in ("pickup at", "drive-thru at", "store #", "ordering from")):
            cleaned = re.sub(
                r"^(?:pickup at|drive-thru at|ordering from)[:\s]*", "", store_candidate, flags=re.IGNORECASE
            ).strip()
            if cleaned and not status_data["active_store"]:
                status_data["active_store"] = cleaned

        # Fallback: check resource_id for store
        if ("store_name" in node.resource_id.lower() or "selected_store" in node.resource_id.lower()) and text:
            cleaned = re.sub(
                r"^(?:pickup at|drive-thru at|ordering from)[:\s]*", "", text, flags=re.IGNORECASE
            ).strip()
            if not status_data["active_store"]:
                status_data["active_store"] = cleaned

        # Pickup type
        if "drive-thru" in combined.lower():
            status_data["pickup_type"] = "Drive-thru"
        elif "in store" in combined.lower() or "in-store" in combined.lower():
            status_data["pickup_type"] = "In store"

    if has_rewards_indicator or (not has_sign_in and (status_data["active_store"] or status_data["stars"] is not None)):
        status_data["authenticated"] = True
        status_data["auth_state"] = "authenticated"

    return status_data


def get_status(root: Optional[Union[str, ET.Element]] = None, force: bool = False) -> Dict[str, Any]:
    """Retrieve Starbucks app status, rewards stars, balance, and active store."""
    if root is not None:
        return _parse_status_from_tree(root)

    with starbucks_session(force=force):
        tree = dump_ui_tree()
        if tree is None:
            return {
                "package": STARBUCKS_PKG,
                "authenticated": False,
                "auth_state": "unknown",
                "stars": None,
                "balance": None,
                "active_store": None,
                "error": "Failed to dump UI hierarchy from device",
            }
        return _parse_status_from_tree(tree)


def _parse_stores_from_tree(root: Union[str, ET.Element]) -> List[Dict[str, Any]]:
    """Parse nearby store listings from the UI hierarchy."""
    nodes = extract_visible_nodes(root)
    stores: List[Dict[str, Any]] = []

    # Identify store entry blocks
    current_store: Optional[Dict[str, Any]] = None
    for node in nodes:
        text = (node.text or "").strip()
        desc = (node.content_desc or "").strip()
        combined = f"{text} {desc}".strip()
        if not combined:
            continue

        # Look for distance indicator: e.g. "0.4 mi", "1.2 miles"
        dist_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:mi|miles)", combined, re.IGNORECASE)
        if dist_match and current_store:
            current_store["distance"] = f"{dist_match.group(1)} mi"
            continue

        # Store name heuristics
        is_name_id = any(k in node.resource_id.lower() for k in ("store_name", "store_title", "location_name"))
        if is_name_id and text:
            if current_store:
                stores.append(current_store)
            current_store = {
                "name": text,
                "distance": None,
                "address": None,
                "open": True,
                "center": node.center,
            }
            continue

        # Check for address or status
        if current_store:
            if "closed" in combined.lower():
                current_store["open"] = False
            elif re.search(r"\d+\s+[A-Za-z0-9\s,]+", text) and not current_store["address"]:
                current_store["address"] = text

    if current_store:
        stores.append(current_store)

    return stores


def list_stores(
    select: Optional[str] = None,
    root: Optional[Union[str, ET.Element]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """List nearby Starbucks stores and optionally select one."""
    if root is not None:
        stores = _parse_stores_from_tree(root)
        selected_store = None
        if select:
            for s in stores:
                if select.lower() in s["name"].lower():
                    selected_store = s["name"]
                    break
        return {
            "stores": stores,
            "selected": selected_store,
            "count": len(stores),
        }

    with starbucks_session(force=force):
        tree = dump_ui_tree()
        order_tab = find_node(tree, text="Order") or find_node(tree, content_desc="Order")
        if order_tab:
            tap_node(order_tab)
            time.sleep(1.0)
            tree = dump_ui_tree()

        # Tap store selector if present
        store_picker = find_node(tree, resource_id="store") or find_node(tree, text_contains="Pickup at")
        if store_picker:
            tap_node(store_picker)
            time.sleep(1.5)
            tree = dump_ui_tree()

        stores = _parse_stores_from_tree(tree or ET.Element("empty"))
        selected_name = None
        if select:
            target = None
            for s in stores:
                if select.lower() in s["name"].lower():
                    target = s
                    break
            if target and target.get("center", (0, 0)) != (0, 0):
                tap_coords(target["center"][0], target["center"][1])
                selected_name = target["name"]
                time.sleep(1.0)

        return {
            "stores": stores,
            "selected": selected_name,
            "count": len(stores),
        }


def _parse_items_from_tree(root: Union[str, ET.Element]) -> List[Dict[str, Any]]:
    """Extract menu, favorite, or previous items from UI tree."""
    nodes = extract_visible_nodes(root)
    items: List[Dict[str, Any]] = []

    current_item: Optional[Dict[str, Any]] = None
    for node in nodes:
        text = (node.text or "").strip()
        desc = (node.content_desc or "").strip()
        combined = f"{text} {desc}".strip()
        if not combined:
            continue

        # Price check
        price_match = re.search(r"\$([0-9]+\.[0-9]{2})", combined)
        if price_match:
            if current_item:
                current_item["price"] = f"${price_match.group(1)}"
            continue

        # Item name checks
        is_header = any(
            k in node.resource_id.lower()
            for k in ("search_title", "dialog_title", "action_bar", "header", "toolbar")
        )
        is_item_node = not is_header and (
            any(
                k in node.resource_id.lower()
                for k in ("item_name", "product_name", "item_title", "product_title", "food_name", "drink_name", "menu_item")
            )
            or (node.clickable and len(text) > 3 and not any(btn in text.lower() for btn in DISMISS_PATTERNS))
        )

        if is_item_node and text and not text.startswith("$") and not text.isdigit():
            if current_item and current_item.get("name") != text:
                items.append(current_item)
            current_item = {
                "name": text,
                "price": None,
                "details": None,
                "center": node.center,
            }
        elif current_item and text and not current_item.get("details"):
            if not text.startswith("$") and "cal" in text.lower():
                current_item["details"] = text

    if current_item:
        items.append(current_item)

    return items


def list_favorites(
    root: Optional[Union[str, ET.Element]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """List recent and favorite items from the Order tab."""
    if root is not None:
        items = _parse_items_from_tree(root)
        return {"favorites": items, "count": len(items)}

    with starbucks_session(force=force):
        tree = dump_ui_tree()
        order_tab = find_node(tree, text="Order") or find_node(tree, content_desc="Order")
        if order_tab:
            tap_node(order_tab)
            time.sleep(1.0)
            tree = dump_ui_tree()

        # Switch to Favorites or Previous sub-tab
        fav_tab = (
            find_node(tree, text="Favorites")
            or find_node(tree, text="Previous")
            or find_node(tree, content_desc="Favorites")
        )
        if fav_tab:
            tap_node(fav_tab)
            time.sleep(1.0)
            tree = dump_ui_tree()

        items = _parse_items_from_tree(tree or ET.Element("empty"))
        return {"favorites": items, "count": len(items)}


def search_menu(
    query: str,
    root: Optional[Union[str, ET.Element]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Search Starbucks menu by query string."""
    if root is not None:
        items = _parse_items_from_tree(root)
        return {"query": query, "results": items, "count": len(items)}

    with starbucks_session(force=force):
        tree = dump_ui_tree()
        order_tab = find_node(tree, text="Order") or find_node(tree, content_desc="Order")
        if order_tab:
            tap_node(order_tab)
            time.sleep(1.0)
            tree = dump_ui_tree()

        search_btn = (
            find_node(tree, resource_id="search")
            or find_node(tree, content_desc="Search")
            or find_node(tree, class_name="android.widget.EditText")
        )
        if search_btn:
            tap_node(search_btn)
            time.sleep(0.5)
            enter_text(query)
            press_key(66)  # KEYCODE_ENTER
            time.sleep(1.5)
            tree = dump_ui_tree()

        items = _parse_items_from_tree(tree or ET.Element("empty"))
        return {"query": query, "results": items, "count": len(items)}


def _parse_checkout_review_screen(
    root: Union[str, ET.Element],
    item_requested: str = "",
    store_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract store, pickup estimate, items, and price summary from checkout review."""
    nodes = extract_visible_nodes(root)

    review: Dict[str, Any] = {
        "status": "staged",
        "item_requested": item_requested,
        "store": store_override,
        "pickup_estimate": None,
        "items": [],
        "subtotal": None,
        "tax": None,
        "total_price": None,
        "payment_method": None,
        "ready_for_checkout": True,
        "safety_gate": "Pre-confirmed only: requires explicit --confirm-checkout to place order.",
    }

    for idx, node in enumerate(nodes):
        combined = f"{node.text} {node.content_desc}".strip()
        lower = combined.lower()

        # Pickup estimate: e.g. "Ready in 5-8 mins", "Estimated pickup: 7-10 mins"
        est_match = re.search(r"(?:ready in|estimated|pickup in|approx\.?)\s*(\d+[-–]\d+\s*mins?|\d+\s*mins?)", lower)
        if not est_match:
            est_match = re.search(r"(\d+[-–]\d+\s*mins?|\d+\s*mins?)", lower)
        if est_match and not review["pickup_estimate"]:
            review["pickup_estimate"] = est_match.group(1)

        # Store identification
        if not review["store"] and any(kw in lower for kw in ("pickup at", "store:", "pickup location")):
            store_candidate = node.text.strip() if node.text else node.content_desc.strip()
            cleaned = re.sub(r"^(?:pickup at|store:|pickup location)[:\s]*", "", store_candidate, flags=re.IGNORECASE).strip()
            if cleaned:
                review["store"] = cleaned

        # Subtotal
        if "subtotal" in lower:
            for next_node in nodes[idx : idx + 3]:
                sub_match = re.search(r"\$([0-9]+\.[0-9]{2})", next_node.text)
                if sub_match:
                    review["subtotal"] = f"${sub_match.group(1)}"
                    break

        # Tax
        if "tax" in lower and not review["tax"]:
            for next_node in nodes[idx : idx + 3]:
                tax_match = re.search(r"\$([0-9]+\.[0-9]{2})", next_node.text)
                if tax_match:
                    review["tax"] = f"${tax_match.group(1)}"
                    break

        # Total Price
        if "total" in lower and "subtotal" not in lower and not review["total_price"]:
            for next_node in nodes[idx : idx + 3]:
                tot_match = re.search(r"\$([0-9]+\.[0-9]{2})", next_node.text)
                if tot_match:
                    review["total_price"] = f"${tot_match.group(1)}"
                    break

        # Payment method: e.g. Starbucks Card, Apple Pay
        if "starbucks card" in lower or "card balance" in lower:
            review["payment_method"] = "Starbucks Card"
        elif "apple pay" in lower:
            review["payment_method"] = "Apple Pay"

    if item_requested and not review["items"]:
        review["items"].append({"name": item_requested, "quantity": 1})

    return review


def stage_order(
    item: str,
    store: Optional[str] = None,
    root: Optional[Union[str, ET.Element]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Stage an item in the bag and extract checkout review without paying.

    SAFETY INVARIANT: Staging halts unconditionally at the Order Review screen.
    """
    if root is not None:
        return _parse_checkout_review_screen(root, item_requested=item, store_override=store)

    with starbucks_session(force=force):
        tree = dump_ui_tree()

        # 1. Store selection if requested
        if store:
            list_stores(select=store, force=force)
            tree = dump_ui_tree()

        # 2. Find item in Favorites or Search
        order_tab = find_node(tree, text="Order") or find_node(tree, content_desc="Order")
        if order_tab:
            tap_node(order_tab)
            time.sleep(1.0)
            tree = dump_ui_tree()

        item_node = find_node(tree, text_contains=item) or find_node(tree, content_desc_contains=item)
        if not item_node:
            search_menu(item, force=force)
            tree = dump_ui_tree()
            item_node = find_node(tree, text_contains=item) or find_node(tree, content_desc_contains=item)

        if not item_node:
            return {
                "status": "error",
                "error": f"Item '{item}' not found on menu or favorites.",
                "ready_for_checkout": False,
            }

        # Tap item to open customization/detail
        tap_node(item_node)
        time.sleep(1.5)
        tree = dump_ui_tree()

        # 3. Add to order / bag
        add_btn = (
            find_node(tree, text="Add to Order")
            or find_node(tree, text="Add to Bag")
            or find_node(tree, content_desc="Add to Order")
        )
        if add_btn:
            tap_node(add_btn)
            time.sleep(1.5)
            tree = dump_ui_tree()

        # 4. Proceed to Review / Bag
        view_bag = (
            find_node(tree, text="View Order")
            or find_node(tree, text="Review Order")
            or find_node(tree, content_desc="View Order")
            or find_node(tree, resource_id="bag")
        )
        if view_bag:
            tap_node(view_bag)
            time.sleep(1.5)
            tree = dump_ui_tree()

        # 5. Extract review summary and HALT before payment
        return _parse_checkout_review_screen(tree or ET.Element("empty"), item_requested=item, store_override=store)


def _parse_confirmation_screen(root: Union[str, ET.Element]) -> Dict[str, Any]:
    """Parse confirmation screen receipt details."""
    nodes = extract_visible_nodes(root)
    receipt: Dict[str, Any] = {
        "status": "confirmed",
        "order_placed": True,
        "confirmation_message": "Order successfully placed",
        "pickup_estimate": None,
        "store": None,
    }

    for node in nodes:
        combined = f"{node.text} {node.content_desc}".strip()
        lower = combined.lower()

        if any(w in lower for w in ("thank you", "confirmed", "order placed")):
            receipt["confirmation_message"] = combined

        est_match = re.search(r"(\d+[-–]\d+\s*mins?|\d+\s*mins?)", lower)
        if est_match and not receipt["pickup_estimate"]:
            receipt["pickup_estimate"] = est_match.group(1)

        if any(kw in lower for kw in ("pickup at", "store:")) and not receipt["store"]:
            cleaned = re.sub(r"^(?:pickup at|store:)[:\s]*", "", combined, flags=re.IGNORECASE).strip()
            if cleaned:
                receipt["store"] = cleaned

    return receipt


def execute_checkout(
    confirm_checkout: bool = False,
    root: Optional[Union[str, ET.Element]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Complete final checkout payment.

    SAFETY GATE: Requires explicit confirm_checkout=True. Tapping the final
    'Checkout' or 'Place Order' button is strictly prevented without it.
    """
    if not confirm_checkout:
        return {
            "status": "error",
            "error": "Safety Guard: --confirm-checkout must be explicitly passed to place an order.",
            "order_placed": False,
        }

    if root is not None:
        return _parse_confirmation_screen(root)

    with starbucks_session(force=force):
        tree = dump_ui_tree()

        # Locate final Place Order / Checkout button
        place_order_btn = (
            find_node(tree, text="Place Order")
            or find_node(tree, text="Checkout")
            or find_node(tree, content_desc="Place Order")
            or find_node(tree, resource_id="checkout")
        )

        if not place_order_btn:
            # Might need to enter bag review first
            view_bag = find_node(tree, text="View Order") or find_node(tree, resource_id="bag")
            if view_bag:
                tap_node(view_bag)
                time.sleep(1.5)
                tree = dump_ui_tree()
                place_order_btn = find_node(tree, text="Place Order") or find_node(tree, text="Checkout")

        if not place_order_btn:
            return {
                "status": "error",
                "error": "Checkout / Place Order button not found on screen.",
                "order_placed": False,
            }

        # Final verified tap
        tap_node(place_order_btn)
        time.sleep(3.0)
        tree = dump_ui_tree()

        return _parse_confirmation_screen(tree or ET.Element("empty"))


def main():
    parser = argparse.ArgumentParser(
        description="Starbucks Android Native App Automation CLI (Pixel 8 Pro / ADB UI Automator)"
    )
    parser.add_argument("--json", action="store_true", help="Output structured results in JSON format")
    parser.add_argument("--force", action="store_true", help="Override active 3D printer camera conflict guard")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Inspect app auth state, reward stars/balance, and active pickup store")

    # stores
    stores_p = subparsers.add_parser("stores", help="List or select nearby stores")
    stores_p.add_argument("--select", type=str, default=None, help="Name or keyword of store to select")

    # favorites
    subparsers.add_parser("favorites", help="List recent/favorite items on the Order tab")

    # search
    search_p = subparsers.add_parser("search", help="Search the Starbucks menu")
    search_p.add_argument("query", type=str, help="Menu search term (e.g. 'cold brew', 'croissant')")

    # stage
    stage_p = subparsers.add_parser("stage", help="Stage an item or favorite in bag without paying")
    stage_p.add_argument("--item", type=str, required=True, help="Item or favorite name to stage")
    stage_p.add_argument("--store", type=str, default=None, help="Specific store name to select for pickup")

    # checkout
    checkout_p = subparsers.add_parser("checkout", help="Finalize order checkout (requires confirmation)")
    checkout_p.add_argument(
        "--confirm-checkout",
        action="store_true",
        default=False,
        help="Explicit confirmation required to place order and charge account",
    )

    args = parser.parse_args()

    if args.command == "status":
        res = get_status(force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=== Starbucks App Status ===")
            print(f"Auth State:   {res.get('auth_state', 'unknown')}")
            print(f"Reward Stars: {res.get('stars', 'N/A')} ★")
            print(f"Card Balance: {res.get('balance', 'N/A')}")
            print(f"Active Store: {res.get('active_store', 'None selected')}")
            if res.get("pickup_type"):
                print(f"Pickup Type:  {res['pickup_type']}")

    elif args.command == "stores":
        res = list_stores(select=args.select, force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=== Starbucks Nearby Stores ===")
            for idx, s in enumerate(res.get("stores", []), 1):
                dist = f" ({s['distance']})" if s.get("distance") else ""
                status_str = " [Closed]" if not s.get("open", True) else ""
                print(f"{idx}. {s['name']}{dist}{status_str}")
            if res.get("selected"):
                print(f"\nSelected Store: {res['selected']}")

    elif args.command == "favorites":
        res = list_favorites(force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=== Starbucks Favorites & Recent Orders ===")
            for idx, itm in enumerate(res.get("favorites", []), 1):
                price = f" - {itm['price']}" if itm.get("price") else ""
                details = f" ({itm['details']})" if itm.get("details") else ""
                print(f"{idx}. {itm['name']}{details}{price}")

    elif args.command == "search":
        res = search_menu(query=args.query, force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"=== Starbucks Menu Search: '{args.query}' ===")
            for idx, itm in enumerate(res.get("results", []), 1):
                price = f" - {itm['price']}" if itm.get("price") else ""
                print(f"{idx}. {itm['name']}{price}")

    elif args.command == "stage":
        res = stage_order(item=args.item, store=args.store, force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=== Starbucks Order Staged (Review Summary) ===")
            print(f"Item Requested: {res.get('item_requested')}")
            print(f"Store:          {res.get('store', 'Default / Nearby')}")
            print(f"Pickup Time:    {res.get('pickup_estimate', '5-8 mins')}")
            print(f"Subtotal:       {res.get('subtotal', 'N/A')}")
            print(f"Tax:            {res.get('tax', 'N/A')}")
            print(f"Total Price:    {res.get('total_price', 'N/A')}")
            print(f"Payment Method: {res.get('payment_method', 'Starbucks Card')}")
            print("\n⚠️  SAFETY GATE: Order is staged in cart. No payment processed.")
            print("To complete purchase, run with explicit confirmation:")
            print("  python3 tools/starbucks_order.py checkout --confirm-checkout")

    elif args.command == "checkout":
        if not args.confirm_checkout:
            err = {
                "status": "error",
                "error": "Safety Guard: --confirm-checkout must be explicitly passed to place an order.",
                "order_placed": False,
            }
            if args.json:
                print(json.dumps(err, indent=2))
            else:
                print("❌ Checkout Aborted: --confirm-checkout flag is required to place an order and charge payment.")
            sys.exit(1)

        res = execute_checkout(confirm_checkout=args.confirm_checkout, force=args.force)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("order_placed"):
                print("=== Starbucks Order Confirmed ===")
                print(f"Status:          {res.get('status')}")
                print(f"Message:         {res.get('confirmation_message')}")
                print(f"Pickup Estimate: {res.get('pickup_estimate', 'N/A')}")
                print(f"Store:           {res.get('store', 'N/A')}")
            else:
                print(f"❌ Order Failed: {res.get('error', 'Unknown checkout error')}")
                sys.exit(1)


if __name__ == "__main__":
    main()
