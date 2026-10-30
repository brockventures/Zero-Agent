#!/usr/bin/env python3
"""Zero Amazon Order Listener Daemon.

Maintains a fast, lightweight polling loop against Gmail for Amazon order
confirmation emails (from:auto-confirm@amazon.com), extracts structured order
data (Order #, items, total, delivery estimate), updates local state, and drops
an alert card into #zero-chat.
"""

import json
import os
import sys
import time
import re
import signal
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from html import unescape
import base64

WORKSPACE = "/workspace"
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

try:
    from tools.zero_mail_listener import get_access_token, load_env
except ImportError:
    from zero_mail_listener import get_access_token, load_env

STATE_DIR = os.environ.get("DATA_DIR", "/workspace/data")
SEEN_FILE = os.path.join(STATE_DIR, "seen_amazon_orders.json")
ORDERS_FILE = os.path.join(STATE_DIR, "amazon_orders.json")
LOG_FILE = os.environ.get("AMAZON_ORDER_LOG", os.path.join(STATE_DIR, "amazon_order_listener.log"))
PID_FILE = os.path.join(STATE_DIR, "amazon_order_listener.pid")

POLL_INTERVAL = int(os.environ.get("AMAZON_POLL_INTERVAL", "60"))
NOTIFY_DISCORD = os.environ.get("AMAZON_NOTIFY_DISCORD", "false").lower() in ("true", "1", "yes")
TIMEOUT = 15

_running = True

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data) if isinstance(data, list) else set()
        except Exception as e:
            log(f"Warning reading seen file: {e}")
    return set()

def save_seen(seen: set):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen)), f, indent=2)
    except Exception as e:
        log(f"Warning saving seen file: {e}")

def load_orders() -> list[dict]:
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            log(f"Warning reading orders file: {e}")
    return []

def save_orders(orders: list[dict]):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(orders, f, indent=2)
    except Exception as e:
        log(f"Warning saving orders file: {e}")

def extract_mime_parts(payload: dict) -> dict[str, str]:
    out = {"plain": "", "html": ""}
    mime = payload.get("mimeType", "")
    if "parts" in payload:
        for p in payload["parts"]:
            sub = extract_mime_parts(p)
            out["plain"] += sub["plain"]
            out["html"] += sub["html"]
    elif "body" in payload and "data" in payload["body"]:
        raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
        if mime == "text/plain":
            out["plain"] += raw + "\n"
        elif mime == "text/html":
            out["html"] += raw + "\n"
        else:
            out["plain"] += raw + "\n"
    return out


def parse_amazon_order(mdata: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in mdata.get("payload", {}).get("headers", [])}
    parts = extract_mime_parts(mdata.get("payload", {}))
    plain_text = unescape(parts["plain"])
    html_raw = parts["html"]
    combined = plain_text + "\n" + unescape(html_raw)

    # 1. Order Number(s) (handle split-shipment checkouts with multiple Order #s in one email)
    order_nums = []
    for oid in re.findall(r"Order\s+#?\s*[\u200e\u200f\u202a-\u202e\s]*([0-9]{3}-[0-9]{7}-[0-9]{7})", combined, re.I):
        oid_clean = oid.strip()
        if oid_clean not in order_nums:
            order_nums.append(oid_clean)
    order_num = " / ".join(order_nums) if order_nums else None

    # 2. Items from text/plain ('* <Item Title>' blocks) with Quantity and Item Price
    items = []
    seen_items = set()
    item_prices_sum = 0.0
    if plain_text.strip():
        lines = plain_text.splitlines()
        for idx, line in enumerate(lines):
            m_star = re.match(r"^\s*\*\s+(.+)$", line)
            if m_star:
                title = re.sub(r"\s+", " ", m_star.group(1)).strip()
                if not title:
                    continue
                qty = 1
                item_price = None
                for look_ahead in lines[idx + 1 : idx + 6]:
                    m_q = re.search(r"Quantity:\s*(\d+)", look_ahead, re.I)
                    if m_q:
                        qty = int(m_q.group(1))
                    m_p = re.match(r"^\s*\$?([0-9,]+\.[0-9]{1,2})\s*USD\s*$", look_ahead.strip(), re.I)
                    if m_p:
                        item_price = float(m_p.group(1).replace(",", ""))
                        break
                if item_price is not None:
                    item_prices_sum += item_price * qty
                label = f"{qty}x {title}" if qty > 1 else title
                if label not in seen_items:
                    seen_items.add(label)
                    items.append(label)

    # Fallback to HTML alt tags only if text/plain yielded no items (run regex BEFORE unescape so &quot; doesn't truncate)
    if not items and html_raw:
        raw_alts = re.findall(r'alt="([^"]+)"', html_raw)
        exact_banned = {
            "amazon.com", "amazon", "completed", "pending", "shipped", "delivered",
            "prime", "your orders", "your account", "buy again",
        }
        for alt in raw_alts:
            alt_clean = re.sub(r"\s+", " ", unescape(alt)).strip()
            if len(alt_clean) < 6:
                continue
            alt_lower = alt_clean.lower()
            if alt_lower in exact_banned or alt_lower.startswith("transactional-"):
                continue
            if alt_clean not in seen_items:
                seen_items.add(alt_clean)
                items.append(alt_clean)

    # 3. Total (sum all Grand Total blocks across split shipments; support 1 or 2 decimal places like '82.4 USD')
    grand_totals = [
        float(x.replace(",", ""))
        for x in re.findall(
            r"(?:Grand\s+Total|Order\s+Total):[\s\r\n]*\$?([0-9,]+\.[0-9]{1,2})(?:\s*USD)?",
            plain_text or combined,
            re.I,
        )
    ]
    total_val = sum(grand_totals) if grand_totals else 0.0
    if total_val > 0:
        total = f"${total_val:,.2f}"
    elif item_prices_sum > 0:
        total = f"${item_prices_sum:,.2f} (Points/Gift Card)"
    else:
        total = "Unknown"

    # 4. Delivery Estimate
    estimate = None
    m = re.search(r"(Arriving\s+[A-Za-z]+(?:\s+\d+)?(?:\s*,\s*[A-Za-z]+\s+\d+)?|Estimated delivery[:\s]+[^\n\r<]+)", combined, re.I)
    if m:
        estimate = m.group(1).strip()

    det_result = {
        "order_id": order_num or "Unknown",
        "total": total,
        "estimate": estimate or "Pending",
        "items": items,
        "date": headers.get("date", datetime.now().isoformat()),
        "subject": headers.get("subject", "Amazon Order Confirmation"),
        "message_id": mdata.get("id"),
    }

    # 5. Lightweight LLM Synthesis / Extraction (gemini-3.8-flash-low) when not in test fast-mode
    try:
        from tools.llm_sidecar import _is_test_fast_mode, call_sidecar_llm_json
        if not _is_test_fast_mode():
            body_for_llm = plain_text.strip()
            if not body_for_llm and html_raw:
                stripped = re.sub(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", " ", html_raw, flags=re.I)
                stripped = re.sub(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^<>]*)?>", " ", stripped)
                body_for_llm = re.sub(r"\s+", " ", unescape(stripped)).strip()
            if body_for_llm:
                llm_prompt = (
                    "Extract structured Amazon order details from this confirmation email.\n"
                    f"Subject: {det_result['subject']}\n"
                    f"Email Text:\n{body_for_llm[:5000]}\n\n"
                    "Rules:\n"
                    "1. `order_id`: All Amazon Order IDs (format NNN-NNNNNNN-NNNNNNN), joined by ' / ' if split across multiple orders.\n"
                    "2. `items`: Complete list of ordered product titles (prefix with 'Nx ' if quantity > 1). Never include UI banners or buttons.\n"
                    "3. `total`: Sum of Grand Totals across all orders in the email formatted as '$X.XX'. If Grand Total is $0.00 (paid via points/gift card), sum the item prices and append ' (Points/Gift Card)'.\n"
                    "4. `estimate`: Delivery estimate string (e.g. 'Arriving Sunday', 'Arriving September 15').\n\n"
                    "Return ONLY valid JSON matching:\n"
                    '{"order_id": "...", "total": "$...", "estimate": "...", "items": ["..."]}'
                )
                parsed_llm = call_sidecar_llm_json(
                    job_key="amazon_order_listener",
                    prompt=llm_prompt,
                    model="gemini-3.8-flash-low",
                    timeout=30,
                )
                if isinstance(parsed_llm, dict):
                    llm_items = [str(i).strip() for i in (parsed_llm.get("items") or []) if str(i).strip()]
                    llm_oid = str(parsed_llm.get("order_id") or "").strip()
                    llm_tot = str(parsed_llm.get("total") or "").strip()
                    llm_est = str(parsed_llm.get("estimate") or "").strip()
                    if llm_items and len(llm_items) >= len(det_result["items"]):
                        det_result["items"] = llm_items
                    if llm_oid and llm_oid != "Unknown" and det_result["order_id"] == "Unknown":
                        det_result["order_id"] = llm_oid
                    if llm_tot and llm_tot != "Unknown" and det_result["total"] == "Unknown":
                        det_result["total"] = llm_tot
                    if llm_est and llm_est != "Pending":
                        det_result["estimate"] = llm_est
    except Exception as e:
        log(f"LLM extraction fallback to deterministic parser: {e}")

    return det_result

def format_discord_card(order: dict) -> str:
    lines = [
        "🛒 **New Amazon Order Detected**",
        f"- **Order #:** `{order.get('order_id')}`",
        f"- **Total:** `{order.get('total')}`",
        f"- **Delivery Estimate:** `{order.get('estimate')}`"
    ]
    items = order.get("items", [])
    if items:
        lines.append("- **Items:**")
        for item in items:
            lines.append(f"  - {item}")
    lines.append("\n-# 📦 *Logged to durable homelab order state.*")
    return "\n".join(lines)

def post_discord(bot_token: str, channel_id: str, content: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "ZeroAmazonListener/1.0"
        }
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status in (200, 201)

def _handle_exit_signal(signum, frame):
    global _running
    log(f"Received signal {signum}, stopping Amazon order listener cleanly...")
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
            with open(status_path, "r", encoding="utf-8", errors="ignore") as f:
                tgid_lines = [line.split()[1] for line in f if line.startswith("Tgid:")]
            if (tgid_lines and int(tgid_lines[0]) != pid) or "amazon_order_listener" not in cmdline:
                return False
        return True
    except OSError:
        return False

def get_listener_pid() -> int | None:
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        if is_process_running(pid):
            return pid
        os.unlink(PID_FILE)
    except Exception:
        pass
    return None

def get_status() -> dict:
    pid = get_listener_pid()
    return {
        "running": pid is not None,
        "pid": pid,
        "poll_interval": POLL_INTERVAL,
        "log_file": LOG_FILE,
        "orders_file": ORDERS_FILE
    }

def run_loop():
    global _running
    signal.signal(signal.SIGINT, _handle_exit_signal)
    signal.signal(signal.SIGTERM, _handle_exit_signal)

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        log(f"Failed writing PID file {PID_FILE}: {e}")
        return

    log(f"Starting Amazon order listener daemon (PID: {os.getpid()})...")

    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("ZERO_CHAT_CHANNEL", "1542081375287640084")

    if not bot_token:
        log("Warning: DISCORD_BOT_TOKEN not found in environment. Order logging will be local only.")

    seen_ids = load_seen()
    all_orders = load_orders()
    known_order_ids = {o.get("order_id") for o in all_orders if o.get("order_id")}

    log(f"Listener active (polling every {POLL_INTERVAL}s for 'from:auto-confirm@amazon.com')...")

    try:
        while _running:
            try:
                tok = get_access_token()
                query = urllib.parse.quote("from:auto-confirm@amazon.com is:unread")
                url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults=10"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})

                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    data = json.loads(resp.read().decode())
                    messages = data.get("messages", [])

                new_msgs = [m for m in messages if m["id"] not in seen_ids]

                if new_msgs:
                    log(f"Detected {len(new_msgs)} new unread Amazon confirmation email(s).")
                    for m in new_msgs:
                        mid = m["id"]
                        m_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=full"
                        m_req = urllib.request.Request(m_url, headers={"Authorization": f"Bearer {tok}"})
                        with urllib.request.urlopen(m_req, timeout=TIMEOUT) as m_resp:
                            m_data = json.loads(m_resp.read().decode())

                        order = parse_amazon_order(m_data)
                        log(f"Parsed Order #{order.get('order_id')}: {len(order.get('items', []))} item(s), Total: {order.get('total')}")

                        # Avoid duplicate entries in orders list
                        if order.get("order_id") not in known_order_ids:
                            all_orders.append(order)
                            known_order_ids.add(order.get("order_id"))
                            save_orders(all_orders)

                        # Post notification card to Discord only if explicitly enabled
                        if NOTIFY_DISCORD and bot_token and channel_id:
                            card = format_discord_card(order)
                            try:
                                post_discord(bot_token, channel_id, card)
                                log(f"Discord notification posted for order {order.get('order_id')}")
                            except Exception as de:
                                log(f"Failed to post order to Discord: {de}")

                        seen_ids.add(mid)
                        time.sleep(1.0)

                    save_seen(seen_ids)

            except Exception as e:
                log(f"Error in Amazon order poll loop: {e}")

            for _ in range(POLL_INTERVAL):
                if not _running:
                    break
                time.sleep(1)

    finally:
        try:
            if os.path.exists(PID_FILE):
                os.unlink(PID_FILE)
        except OSError:
            pass
        log("Zero Amazon Order Listener stopped cleanly.")

def start_daemon_background() -> bool:
    pid = get_listener_pid()
    if pid:
        log(f"Amazon order listener already running (PID {pid}).")
        return True

    os.makedirs(STATE_DIR, exist_ok=True)
    log_fp = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "run"],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=WORKSPACE
    )

    for _ in range(20):
        if proc.poll() is not None:
            print(f"[Amazon Order Listener] ERROR: Process exited prematurely with code {proc.returncode}.", file=sys.stderr)
            return False
        if get_listener_pid() is not None:
            print(f"[Amazon Order Listener] Successfully started listener daemon (PID {proc.pid}).")
            return True
        time.sleep(0.1)

    print(f"[Amazon Order Listener] Spawned process (PID {proc.pid}).")
    return True

def stop_daemon() -> bool:
    pid = get_listener_pid()
    if not pid:
        print("[Amazon Order Listener] Listener is not running.")
        return True

    print(f"[Amazon Order Listener] Stopping listener (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            if not is_process_running(pid):
                break
            time.sleep(0.1)
        if is_process_running(pid):
            print(f"[Amazon Order Listener] Process {pid} did not exit gracefully, sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
    except Exception as e:
        print(f"[Amazon Order Listener] Error while stopping PID {pid}: {e}")

    try:
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
    except OSError:
        pass
    print("[Amazon Order Listener] Stopped.")
    return True

def ensure_amazon_listener_running() -> bool:
    pid = get_listener_pid()
    if pid:
        return True
    return start_daemon_background()

def sync_recent(limit: int = 5, notify: bool = False):
    """One-shot sync to pull and record recent orders."""
    tok = get_access_token()
    query = urllib.parse.quote("from:auto-confirm@amazon.com")
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})

    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
        messages = data.get("messages", [])

    seen_ids = load_seen()
    all_orders = load_orders()
    known_order_ids = {o.get("order_id") for o in all_orders if o.get("order_id")}

    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("ZERO_CHAT_CHANNEL", "1542081375287640084")

    synced = []
    for m in messages:
        mid = m["id"]
        m_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=full"
        m_req = urllib.request.Request(m_url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(m_req, timeout=TIMEOUT) as m_resp:
            m_data = json.loads(m_resp.read().decode())

        order = parse_amazon_order(m_data)
        if order.get("order_id") not in known_order_ids:
            all_orders.append(order)
            known_order_ids.add(order.get("order_id"))
            synced.append(order)
            log(f"Synced Order #{order.get('order_id')}: {order.get('items')}")

            if notify and bot_token and channel_id:
                card = format_discord_card(order)
                post_discord(bot_token, channel_id, card)

        seen_ids.add(mid)

    save_orders(all_orders)
    save_seen(seen_ids)
    print(f"Synced {len(synced)} new order(s). Total known orders: {len(all_orders)}.")

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run_loop()
    elif cmd == "start":
        ok = start_daemon_background()
        sys.exit(0 if ok else 1)
    elif cmd == "stop":
        ok = stop_daemon()
        sys.exit(0 if ok else 1)
    elif cmd == "restart":
        stop_daemon()
        time.sleep(0.5)
        ok = start_daemon_background()
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        st = get_status()
        status_str = "🟢 RUNNING" if st["running"] else "🔴 STOPPED"
        print(f"**Zero Amazon Order Listener**: {status_str}")
        if st["pid"]:
            print(f"- **PID**: `{st['pid']}`")
        print(f"- **Poll Interval**: `{st['poll_interval']}s`")
        print(f"- **Log File**: `{st['log_file']}`")
        print(f"- **Orders File**: `{st['orders_file']}`")
    elif cmd == "sync":
        notify_flag = "--notify" in sys.argv
        sync_recent(limit=5, notify=notify_flag)
    elif cmd == "json":
        print(json.dumps(get_status(), indent=2))
    else:
        print(f"Usage: {sys.argv[0]} [run|start|stop|restart|status|sync|json]")
        sys.exit(1)
