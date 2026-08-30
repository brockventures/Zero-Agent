#!/usr/bin/env python3
"""Streaming downloader and extractor for Google Takeout archives from Google Drive."""

import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from workspace_mcp import _auth_headers

TAKEOUT_DIR = Path("/workspace/data/takeout")
EXTRACT_DIR = Path("/workspace/data/takeout_extracted")
PROGRESS_FILE = TAKEOUT_DIR / "progress.json"

FILES = [
    {
        "name": "takeout-20260829T234656Z-001.zip",
        "id": "1BzpB5m9uJHIShbYvlAqItGYWXRmjAdMD",
        "size_bytes": 180274,
        "size_mb": 0.17
    },
    {
        "name": "takeout-20260829T234657Z-1-001.zip",
        "id": "1CcdJGpLjuKogBeLuiSs5xBSu-yA2-okf",
        "size_bytes": 1278234572,
        "size_mb": 1219.02
    },
    {
        "name": "takeout-20260829T234657Z-2-001.zip",
        "id": "1azGsPZK3_WGMjpRzH3mrtrwQv7WWBzdw",
        "size_bytes": 6941297250,
        "size_mb": 6619.74
    }
]

def update_progress(stage: str, percent: float, msg: str):
    data = {
        "stage": stage,
        "percent": round(percent, 1),
        "message": msg,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        PROGRESS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
    print(f"[{data['updated_at']}] [{stage.upper()}] ({data['percent']}%) {msg}", flush=True)

def download_file(item: dict) -> Path:
    dest = TAKEOUT_DIR / item["name"]
    if dest.exists() and dest.stat().st_size == item["size_bytes"]:
        print(f"File {item['name']} already completely downloaded ({item['size_mb']} MB).", flush=True)
        return dest

    url = f"https://www.googleapis.com/drive/v3/files/{item['id']}?alt=media"
    headers = _auth_headers()
    req = urllib.request.Request(url, headers=headers)

    chunk_size = 16 * 1024 * 1024  # 16 MB chunks
    downloaded = 0
    start_time = time.time()
    last_log = time.time()

    print(f"Starting download of {item['name']} ({item['size_mb']:.1f} MB)...", flush=True)
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_log >= 3.0 or downloaded == item["size_bytes"]:
                speed_mb = (downloaded / (1024 * 1024)) / max(1.0, now - start_time)
                pct = (downloaded / item["size_bytes"]) * 100
                update_progress("downloading", pct, f"{item['name']}: {downloaded/(1024*1024):.1f}/{item['size_mb']:.1f} MB ({speed_mb:.1f} MB/s)")
                last_log = now

    return dest

def extract_archive(zip_path: Path):
    update_progress("extracting", 0, f"Extracting {zip_path.name} to {EXTRACT_DIR}...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        members = z.infolist()
        total = len(members)
        last_log = time.time()
        for idx, member in enumerate(members):
            z.extract(member, EXTRACT_DIR)
            now = time.time()
            if now - last_log >= 3.0 or idx == total - 1:
                pct = ((idx + 1) / total) * 100
                update_progress("extracting", pct, f"{zip_path.name}: {idx+1}/{total} files extracted")
                last_log = now

def main():
    TAKEOUT_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    
    update_progress("init", 0, "Starting Takeout download and extraction pipeline...")

    # 1. Download files in sequence
    downloaded_paths = []
    for idx, item in enumerate(FILES):
        p = download_file(item)
        downloaded_paths.append(p)

    # 2. Extract files
    for idx, p in enumerate(downloaded_paths):
        extract_archive(p)

    update_progress("complete", 100, "All 3 archives successfully downloaded and extracted!")
    print("Takeout extraction pipeline finished cleanly!", flush=True)

if __name__ == "__main__":
    main()
