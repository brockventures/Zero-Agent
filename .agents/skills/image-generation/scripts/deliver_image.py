#!/usr/bin/env python3
"""
deliver_image.py - Zero Discord Image Verification & Direct Delivery Tool
Part of the image-generation skill.

Enables Zero to:
1. Forensically verify generated images on disk (dimensions, format, size).
2. Upload images directly to any Discord channel (home or Crab Cavern) via Discord REST API v10 in <300ms.
3. Record delivered artifacts in /workspace/data/delivered_artifacts.json to prevent duplicate delivery by bridge runners.
"""

import os
import sys
import json
import time
import argparse
import mimetypes
from pathlib import Path
from PIL import Image
import requests

DATA_DIR = Path("/workspace/data")
DELIVERED_FILE = DATA_DIR / "delivered_artifacts.json"
BRAIN_ROOT = Path("/root/.gemini/antigravity-cli/brain")

KNOWN_CHANNELS = {
    "the-banana-stand": 1534436119888793750,
    "agent-chat": 1534436119888793750,
    "lounge": 1534452820995080192,
    "zero-chat": 1542081375287640084,
    "zero-ops": 1544953279664889888,
    "harness-management": 1544953279664889888,
    "steam-deck": 1544953277592899615,
    "home-assistant": 1544953275877556334,
    "finances": 1544955532765560924,
    "homelab": 1544955535722545253,
    "shopping": 1544955538033348618,
    "general": 1534452820995080192,
    "signals": 1534436119888793750,
    "staff-comms": 1534436119888793750,
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def resolve_channel(channel_input: str | int) -> tuple[str, int | None]:
    """Resolve channel name or ID to (clean_name, channel_id)."""
    ch_str = str(channel_input).strip().lstrip("#")
    if ch_str.isdigit():
        ch_id = int(ch_str)
        for name, cid in KNOWN_CHANNELS.items():
            if cid == ch_id:
                return name, ch_id
        return f"channel-{ch_id}", ch_id

    clean_name = ch_str.lower()
    ch_id = KNOWN_CHANNELS.get(clean_name)
    return clean_name, ch_id


def get_discord_token() -> str:
    """Retrieve Discord bot token securely from secrets or environment."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()
    token_file = Path("/secrets/discord_token")
    if token_file.exists():
        return token_file.read_text().strip()
    raise RuntimeError("Discord bot token not found in DISCORD_BOT_TOKEN or /secrets/discord_token")


def check_image(image_path: str | Path) -> dict:
    """
    Forensically verify an image file on disk.
    Checks existence, readability, dimensions, format, and file size.
    """
    p = Path(image_path).resolve()
    if not p.exists():
        return {"valid": False, "path": str(p), "error": f"File does not exist: {p}"}
    if not p.is_file():
        return {"valid": False, "path": str(p), "error": f"Path is not a regular file: {p}"}

    size_bytes = p.stat().st_size
    if size_bytes == 0:
        return {"valid": False, "path": str(p), "error": f"Image file is empty (0 bytes): {p}"}

    try:
        with Image.open(p) as img:
            img_format = img.format
            width, height = img.size
            mode = img.mode

        return {
            "valid": True,
            "path": str(p),
            "filename": p.name,
            "format": img_format,
            "width": width,
            "height": height,
            "mode": mode,
            "size_bytes": size_bytes,
            "size_kb": round(size_bytes / 1024.0, 2),
            "mtime": p.stat().st_mtime,
        }
    except Exception as e:
        return {"valid": False, "path": str(p), "error": f"Corrupt or invalid image file: {e}"}


def find_latest_image() -> Path | None:
    """Find the most recently generated image artifact in brain sessions or data dir."""
    candidate_files = []

    # Check brain directories
    if BRAIN_ROOT.exists():
        for item in BRAIN_ROOT.rglob("*"):
            if item.is_file() and not item.name.startswith("."):
                if item.suffix.lower() in IMAGE_EXTENSIONS:
                    candidate_files.append(item)

    # Check workspace data
    if DATA_DIR.exists():
        for item in DATA_DIR.glob("*"):
            if item.is_file() and not item.name.startswith("."):
                if item.suffix.lower() in IMAGE_EXTENSIONS:
                    candidate_files.append(item)

    if not candidate_files:
        return None

    return max(candidate_files, key=lambda f: f.stat().st_mtime)


def record_delivered_artifact(image_path: str | Path, channel_id: int, message_id: int | str, filename: str):
    """
    Record that an image was successfully uploaded to Discord.
    Prevents bridge runners from re-attaching the same artifact at turn completion.
    """
    p = Path(image_path).resolve()
    resolved_path = str(p)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    delivered_data = {}
    if DELIVERED_FILE.exists():
        try:
            with open(DELIVERED_FILE, "r", encoding="utf-8") as f:
                delivered_data = json.load(f)
        except Exception:
            delivered_data = {}

    entry = {
        "channel_id": channel_id,
        "message_id": str(message_id),
        "delivered_at": time.time(),
        "filename": filename,
    }
    delivered_data[resolved_path] = entry
    delivered_data[p.name] = entry

    # Prune entries older than 24 hours to keep file compact
    now = time.time()
    delivered_data = {
        k: v for k, v in delivered_data.items()
        if isinstance(v, dict) and (now - v.get("delivered_at", 0)) < 86400
    }

    try:
        temp_file = DELIVERED_FILE.with_suffix(f".tmp.{os.getpid()}")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(delivered_data, f, indent=2)
        temp_file.replace(DELIVERED_FILE)
    except Exception as e:
        print(f"[DeliverImage] Warning: Failed to record delivered artifact: {e}", file=sys.stderr)


def deliver_image(
    image_path: str | Path,
    channel_input: str | int,
    caption: str | None = None,
    reply_to: int | str | None = None,
) -> dict:
    """
    Directly upload an image to a Discord channel via REST API v10.
    Returns result dict with success state, message ID, and attachment details.
    """
    clean_name, channel_id = resolve_channel(channel_input)
    if not channel_id:
        return {
            "success": False,
            "error": f"Could not resolve channel '{channel_input}'. Provide a valid name or snowflake ID.",
        }

    verify_info = check_image(image_path)
    if not verify_info.get("valid"):
        return {
            "success": False,
            "error": f"Image check failed: {verify_info.get('error')}",
            "check": verify_info,
        }

    p = Path(image_path).resolve()
    token = get_discord_token()

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "ZeroDiscordBridge/1.0",
    }

    payload = {}
    if caption:
        payload["content"] = caption
    if reply_to:
        payload["message_reference"] = {"message_id": str(reply_to)}

    mime_type = mimetypes.guess_type(p.name)[0] or "image/jpeg"

    try:
        with open(p, "rb") as f:
            files = {
                "files[0]": (p.name, f.read(), mime_type),
                "payload_json": (None, json.dumps(payload), "application/json"),
            }
            res = requests.post(url, headers=headers, files=files, timeout=30)

        if res.status_code in (200, 201):
            res_json = res.json()
            msg_id = res_json.get("id")
            record_delivered_artifact(p, channel_id, msg_id, p.name)
            return {
                "success": True,
                "channel": clean_name,
                "channel_id": channel_id,
                "message_id": msg_id,
                "attachments": res_json.get("attachments", []),
                "image_path": str(p),
                "dimensions": f"{verify_info['width']}x{verify_info['height']}",
                "size_kb": verify_info["size_kb"],
            }
        else:
            return {
                "success": False,
                "error": f"Discord API error {res.status_code}: {res.text}",
                "status_code": res.status_code,
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to deliver image over HTTP: {e}",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Zero Discord Image Verification & Direct Delivery Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Check image validity and dimensions
  python3 tools/deliver_image.py --check /root/.gemini/antigravity-cli/brain/.../image.jpg

  # Deliver image directly to #lounge with caption
  python3 tools/deliver_image.py --upload /path/to/img.jpg --channel lounge --caption "Why can't I hold all these containers?"

  # Deliver most recent image generated in brain
  python3 tools/deliver_image.py --latest --channel zero-chat --caption "Latest render"
"""
    )
    parser.add_argument("--check", "-c", help="Path to image file to inspect and verify")
    parser.add_argument("--upload", "-u", help="Path to image file to upload to Discord")
    parser.add_argument("--latest", action="store_true", help="Automatically target the latest generated image")
    parser.add_argument("--channel", help="Target Discord channel name (e.g. lounge, the-banana-stand, zero-chat) or ID")
    parser.add_argument("--caption", "-m", help="Optional text caption or prompt summary to accompany the image")
    parser.add_argument("--reply-to", "-r", help="Optional Discord message ID to reply to")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    # 1. Verification only
    if args.check:
        res = check_image(args.check)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("valid"):
                print(f"✅ Valid Image: {res['filename']}")
                print(f"   • Path:       {res['path']}")
                print(f"   • Dimensions: {res['width']}x{res['height']} ({res['format']}, mode: {res['mode']})")
                print(f"   • Size:       {res['size_kb']} KB ({res['size_bytes']} bytes)")
            else:
                print(f"❌ Invalid Image: {res.get('error')}", file=sys.stderr)
                sys.exit(1)
        return

    # 2. Upload / Deliver
    target_path = args.upload
    if args.latest and not target_path:
        latest = find_latest_image()
        if not latest:
            print("❌ No candidate image artifacts found in brain or data directories.", file=sys.stderr)
            sys.exit(1)
        target_path = str(latest)
        if not args.json:
            print(f"🎯 Auto-detected latest image: {target_path}")

    if target_path:
        if not args.channel:
            print("❌ Error: --channel is required when uploading an image.", file=sys.stderr)
            sys.exit(1)

        result = deliver_image(
            image_path=target_path,
            channel_input=args.channel,
            caption=args.caption,
            reply_to=args.reply_to,
        )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("success"):
                print(f"🚀 Delivered Image to #{result['channel']} ({result['channel_id']})")
                print(f"   • Message ID:  {result['message_id']}")
                print(f"   • Dimensions:  {result['dimensions']}")
                print(f"   • Size:        {result['size_kb']} KB")
                if result.get("attachments"):
                    print(f"   • Discord URL: {result['attachments'][0].get('url')}")
            else:
                print(f"❌ Delivery Failed: {result.get('error')}", file=sys.stderr)
                sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
