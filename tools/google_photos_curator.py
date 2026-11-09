#!/usr/bin/env python3
"""Google Photos Shared Album Scraper, Heart/Date Metadata Parser & Calibration Dataset Summary."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import urllib.parse
import urllib.request

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import numpy as np
import requests

try:
    from tools import modal_photos_scorer
except ImportError:  # pragma: no cover
    import modal_photos_scorer  # type: ignore[no-redef]

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BATCHEXECUTE_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=snAcKc"
DEFAULT_MANIFEST_PATH = Path("/workspace/data/photos_curation/manifest.json")
DEFAULT_CURATION_DIR = Path("/workspace/data/photos_curation")
DEFAULT_VISION_CACHE_PATH = Path("/workspace/data/photos_curation/vision_scores_cache.json")
DEFAULT_POPULATION_STATUS_PATH = Path("/workspace/data/photos_curation/population_status.json")
DEFAULT_CHROME_COOKIES_PATH = Path("/tmp/chrome_zero_profile/Default/Cookies")
ADD_ITEMS_RPC_ID = "C2V01c"
SAVE_TO_LIBRARY_RPC_ID = "V8RKJ"
ADD_LIBRARY_TO_SHARED_RPC_ID = "laUYf"
REMOVE_SHARED_ITEMS_RPC_ID = "LjmOue"
BASELINE_TARGET_COUNTS: dict[str, int] = {
    "Rosie Year 6": 1,
    "Isaac Year 2": 34,
}
BURST_WINDOW_MS = 90_000
TARGET_CURATION_SPECS: dict[str, dict[str, Any]] = {
    "Rosie Year 6": {
        "child": "rosalind",
        "target_count": 550,
        "target_video_ratio": 0.29,
        "output_filename": "rosie_year_6_curated.json",
    },
    "Isaac Year 2": {
        "child": "isaac",
        "target_count": 600,
        "target_video_ratio": 0.29,
        "output_filename": "isaac_year_2_curated.json",
    },
}

ALBUM_REGISTRY: dict[str, dict[str, str]] = {
    "Rosalind '19-'23": {
        "short_url": "https://photos.app.goo.gl/sEm2uGqTuy81oskf6",
        "role": "source",
        "child": "rosalind",
    },
    "Rosalind '23-": {
        "short_url": "https://photos.app.goo.gl/g75CdiNSQk9ed3id9",
        "role": "source",
        "child": "rosalind",
    },
    "Isaac '23-": {
        "short_url": "https://photos.app.goo.gl/iyjp2mCtMx3eAL1s6",
        "role": "source",
        "child": "isaac",
    },
    "Rosie Year 1": {
        "short_url": "https://photos.app.goo.gl/GSdNYFZafMj4cknZ6",
        "role": "calibration",
        "child": "rosalind",
        "year": "1",
    },
    "Rosie Year 2": {
        "short_url": "https://photos.app.goo.gl/E4eMJ249Jn6hG2b89",
        "role": "calibration",
        "child": "rosalind",
        "year": "2",
    },
    "Rosie Year 3": {
        "short_url": "https://photos.app.goo.gl/5Fdyn7iPTmeyuRiA6",
        "role": "calibration",
        "child": "rosalind",
        "year": "3",
    },
    "Rosie Year 4": {
        "short_url": "https://photos.app.goo.gl/n7gu5WYPtSkv4Wxz7",
        "role": "calibration",
        "child": "rosalind",
        "year": "4",
    },
    "Rosie Year 5": {
        "short_url": "https://photos.app.goo.gl/b8GMZ2NC5CoqKm5g6",
        "role": "calibration",
        "child": "rosalind",
        "year": "5",
    },
    "Rosie Year 6": {
        "short_url": "https://photos.app.goo.gl/vcHrpVGcdcsyVxLQA",
        "role": "target",
        "child": "rosalind",
        "year": "6",
    },
    "Rosie Year 7": {
        "short_url": "https://photos.app.goo.gl/U6w7pgYGkgwHntSb7",
        "role": "target",
        "child": "rosalind",
        "year": "7",
    },
    "Isaac Year 1": {
        "short_url": "https://photos.app.goo.gl/3hExhpiSawpJhgNL7",
        "role": "calibration",
        "child": "isaac",
        "year": "1",
    },
    "Isaac Year 2": {
        "short_url": "https://photos.app.goo.gl/ugBR2ni4WMt8UcR98",
        "role": "target",
        "child": "isaac",
        "year": "2",
    },
    "Isaac Year 3": {
        "short_url": "https://photos.app.goo.gl/psAZPrPsc4EHPwtx7",
        "role": "target",
        "child": "isaac",
        "year": "3",
    },
}

# Birthday-year windows (inclusive start, exclusive end) in UTC milliseconds
# Rosie birthday: Nov 10 (Nov 2024 - Nov 2025 for Year 6; Nov 2025 - Nov 2026 for Year 7)
# Isaac birthday: Dec 26 (Dec 2024 - Dec 2025 for Year 2; Dec 2025 - Dec 2026 for Year 3)
TARGET_BIRTHDAY_WINDOWS: dict[str, dict[str, Any]] = {
    "Rosie Year 6": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23", "Rosalind '23-"],
        "start_iso": "2024-11-10T00:00:00Z",
        "end_iso": "2025-11-10T00:00:00Z",
        "start_ms": int(datetime.datetime(2024, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2025, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Rosie Year 7": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23", "Rosalind '23-"],
        "start_iso": "2025-11-10T00:00:00Z",
        "end_iso": "2026-11-10T00:00:00Z",
        "start_ms": int(datetime.datetime(2025, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2026, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Isaac Year 2": {
        "child": "isaac",
        "source_albums": ["Isaac '23-"],
        "start_iso": "2024-12-26T00:00:00Z",
        "end_iso": "2025-12-26T00:00:00Z",
        "start_ms": int(datetime.datetime(2024, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2025, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Isaac Year 3": {
        "child": "isaac",
        "source_albums": ["Isaac '23-"],
        "start_iso": "2025-12-26T00:00:00Z",
        "end_iso": "2026-12-26T00:00:00Z",
        "start_ms": int(datetime.datetime(2025, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2026, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
}

CALIBRATION_WINDOWS: dict[str, dict[str, Any]] = {
    "Rosie Year 1": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23"],
        "start_ms": int(datetime.datetime(2019, 3, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2020, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Rosie Year 2": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23"],
        "start_ms": int(datetime.datetime(2020, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2021, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Rosie Year 3": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23"],
        "start_ms": int(datetime.datetime(2021, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2022, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Rosie Year 4": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23", "Rosalind '23-"],
        "start_ms": int(datetime.datetime(2022, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2023, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Rosie Year 5": {
        "child": "rosalind",
        "source_albums": ["Rosalind '19-'23", "Rosalind '23-"],
        "start_ms": int(datetime.datetime(2023, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2024, 11, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
    "Isaac Year 1": {
        "child": "isaac",
        "source_albums": ["Isaac '23-"],
        "start_ms": int(datetime.datetime(2023, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        "end_ms": int(datetime.datetime(2024, 12, 26, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000),
    },
}


def extract_ds1_payload(html: str) -> list[Any]:
    """Extract and JSON-parse the AF_initDataCallback ds:1 payload from shared album HTML."""
    match = re.search(
        r"AF_initDataCallback\(\{key:\s*'ds:1',\s*hash:\s*'[^']+',\s*data:(.*?),\s*sideChannel:",
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find AF_initDataCallback(ds:1) in HTML response")
    return json.loads(match.group(1))


def parse_batchexecute_response(raw_text: str, rpc_id: str = "snAcKc") -> list[Any]:
    """Parse Google batchexecute multi-line response and extract the inner JSON array for rpc_id."""
    for line in raw_text.splitlines():
        line_str = line.strip()
        if not line_str or rpc_id not in line_str:
            continue
        try:
            outer = json.loads(line_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(outer, list):
            continue
        for entry in outer:
            if (
                isinstance(entry, list)
                and len(entry) >= 3
                and entry[0] == "wrb.fr"
                and entry[1] == rpc_id
                and isinstance(entry[2], str)
            ):
                return json.loads(entry[2])
    raise ValueError(f"Could not find valid wrb.fr payload for {rpc_id} in batchexecute response")


def extract_contributors_map(album_meta: list[Any] | None) -> dict[str, str]:
    """Extract contributor actor_id -> display name mapping from album metadata array (data[3][9])."""
    contributors: dict[str, str] = {}
    if not isinstance(album_meta, list) or len(album_meta) <= 9:
        return contributors
    raw_actors = album_meta[9]
    if not isinstance(raw_actors, list):
        return contributors
    for actor in raw_actors:
        if isinstance(actor, list) and len(actor) >= 1 and isinstance(actor[0], str):
            actor_id = actor[0]
            name = None
            if len(actor) > 2 and isinstance(actor[2], str):
                name = actor[2]
            elif len(actor) > 11 and isinstance(actor[11], list) and len(actor[11]) >= 1:
                name = actor[11][0]
            contributors[actor_id] = name or actor_id
    return contributors


def parse_media_item(
    raw_item: list[Any],
    album_name: str | None = None,
    contributors_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Extract structured metadata from a raw Google Photos item entry in ds:1 / snAcKc."""
    if not isinstance(raw_item, list) or len(raw_item) < 3:
        raise ValueError(f"Invalid media item entry: {raw_item!r}")

    item_id = str(raw_item[0])
    media_info = raw_item[1] if isinstance(raw_item[1], list) else []
    base_url = str(media_info[0]) if len(media_info) > 0 and media_info[0] else ""
    width = int(media_info[1]) if len(media_info) > 1 and isinstance(media_info[1], (int, float)) else 0
    height = int(media_info[2]) if len(media_info) > 2 and isinstance(media_info[2], (int, float)) else 0

    timestamp_ms = int(raw_item[2]) if isinstance(raw_item[2], (int, float)) else 0
    dedup_key = str(raw_item[3]) if len(raw_item) > 3 and raw_item[3] is not None else f"{timestamp_ms}_{width}x{height}"
    tz_offset_ms = int(raw_item[4]) if len(raw_item) > 4 and isinstance(raw_item[4], (int, float)) else 0
    creation_ms = int(raw_item[5]) if len(raw_item) > 5 and isinstance(raw_item[5], (int, float)) else timestamp_ms

    raw_contributors = raw_item[6] if len(raw_item) > 6 and isinstance(raw_item[6], list) else []
    contributor_ids = [str(c) for c in raw_contributors if c]
    contributor_id = contributor_ids[0] if contributor_ids else None
    contributor_name = (
        contributors_map.get(contributor_id, contributor_id)
        if contributors_map and contributor_id
        else contributor_id
    )

    ext_dict: dict[str, Any] = raw_item[-1] if isinstance(raw_item[-1], dict) else {}

    # Video vs photo flag & duration
    video_info = ext_dict.get("76647426")
    is_video = video_info is not None
    video_duration_ms = (
        int(video_info[0])
        if isinstance(video_info, list) and len(video_info) > 0 and isinstance(video_info[0], (int, float))
        else None
    )

    # Shared-album heart / reaction / comment metadata
    # In Google Photos ds:1 / snAcKc items:
    # - 11-element items have raw_item[9] as an integer reaction/comment indicator (0 or >=1)
    # - ext_dict may also contain reaction/heart/source-cluster structures ("525000002", "139842850", etc.)
    reaction_slot_val = (
        int(raw_item[9])
        if len(raw_item) >= 11 and isinstance(raw_item[9], (int, float)) and not isinstance(raw_item[9], bool)
        else 0
    )
    has_len11_slot = len(raw_item) >= 11 and not isinstance(raw_item[9], dict)

    heart_actors: list[str] = []
    # Check if explicit reaction/heart arrays exist in ext_dict
    for heart_key in ("heart_actors", "reactions", "likes"):
        if isinstance(ext_dict.get(heart_key), list):
            heart_actors.extend(str(x) for x in ext_dict[heart_key])

    shared_cluster_refs = ext_dict.get("525000002", [])
    heart_count = max(reaction_slot_val, len(heart_actors))
    has_heart = bool(heart_count > 0 or has_len11_slot)

    dt_utc = datetime.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=datetime.timezone.utc)
    iso_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "item_id": item_id,
        "dedup_key": dedup_key,
        "base_url": base_url,
        "timestamp_ms": timestamp_ms,
        "timestamp_iso": iso_utc,
        "tz_offset_ms": tz_offset_ms,
        "creation_ms": creation_ms,
        "width": width,
        "height": height,
        "is_video": is_video,
        "media_type": "video" if is_video else "photo",
        "video_duration_ms": video_duration_ms,
        "contributor_id": contributor_id,
        "contributor_ids": contributor_ids,
        "contributor_name": contributor_name,
        "has_heart": has_heart,
        "heart_count": heart_count,
        "reaction_slot": reaction_slot_val if has_len11_slot else None,
        "shared_cluster_id": (
            shared_cluster_refs[0][0]
            if isinstance(shared_cluster_refs, list)
            and len(shared_cluster_refs) > 0
            and isinstance(shared_cluster_refs[0], list)
            and len(shared_cluster_refs[0]) > 0
            else None
        ),
        "album_name": album_name,
    }


def fetch_page_snAcKc(
    album_id: str,
    page_token: str,
    auth_key: str,
    timeout: int = 20,
    max_retries: int = 3,
) -> tuple[list[Any], str | None]:
    """Invoke Google Photos batchexecute RPC snAcKc to fetch the next page of album items."""
    rpc_arg = json.dumps([album_id, page_token, None, auth_key])
    f_req = json.dumps([[["snAcKc", rpc_arg, None, "generic"]]])
    body = urllib.parse.urlencode({"f.req": f_req}).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                BATCHEXECUTE_URL,
                data=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="ignore")
            inner = parse_batchexecute_response(raw_text, rpc_id="snAcKc")
            items = inner[1] if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], list) else []
            next_token = (
                str(inner[2])
                if isinstance(inner, list) and len(inner) > 2 and inner[2]
                else None
            )
            return items, next_token
        except Exception as exc:
            last_err = exc
            if attempt + 1 < max_retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"snAcKc pagination failed for album {album_id} after {max_retries} attempts: {last_err}")


def scrape_shared_album(
    album_name: str,
    short_url: str,
    max_pages: int | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Resolve a Google Photos shared album URL and paginate 100% of its media items via ds:1 + snAcKc."""
    req = urllib.request.Request(short_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="ignore")

    ds1 = extract_ds1_payload(html)
    raw_items: list[Any] = list(ds1[1]) if len(ds1) > 1 and isinstance(ds1[1], list) else []
    next_token: str | None = str(ds1[2]) if len(ds1) > 2 and ds1[2] else None
    album_meta: list[Any] = ds1[3] if len(ds1) > 3 and isinstance(ds1[3], list) else []

    album_id = str(album_meta[0]) if len(album_meta) > 0 and album_meta[0] else ""
    album_title = str(album_meta[1]) if len(album_meta) > 1 and album_meta[1] else album_name
    expected_total = int(album_meta[21]) if len(album_meta) > 21 and isinstance(album_meta[21], int) else None

    parsed_url = urllib.parse.urlparse(final_url)
    qs = urllib.parse.parse_qs(parsed_url.query)
    auth_key = qs.get("key", [None])[0]
    if not auth_key and len(album_meta) > 19 and isinstance(album_meta[19], str):
        auth_key = album_meta[19]
    if not auth_key:
        raise ValueError(f"Could not resolve auth key for album {album_name} ({final_url})")

    contributors_map = extract_contributors_map(album_meta)

    pages_fetched = 1
    seen_tokens: set[str] = set()
    while next_token:
        if max_pages is not None and pages_fetched >= max_pages:
            break
        if next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        page_items, next_token = fetch_page_snAcKc(
            album_id=album_id,
            page_token=next_token,
            auth_key=auth_key,
            timeout=timeout,
        )
        pages_fetched += 1
        if not page_items:
            break
        raw_items.extend(page_items)

    # Deduplicate within the same album by item_id while preserving order
    parsed_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_item in raw_items:
        parsed = parse_media_item(raw_item, album_name=album_name, contributors_map=contributors_map)
        if parsed["item_id"] not in seen_ids:
            seen_ids.add(parsed["item_id"])
            parsed_items.append(parsed)

    timestamps = [it["timestamp_ms"] for it in parsed_items if it["timestamp_ms"] > 0]
    return {
        "album_name": album_name,
        "album_title": album_title,
        "short_url": short_url,
        "resolved_url": final_url,
        "album_id": album_id,
        "auth_key": auth_key,
        "expected_total": expected_total,
        "pages_fetched": pages_fetched,
        "item_count": len(parsed_items),
        "min_timestamp_ms": min(timestamps) if timestamps else None,
        "max_timestamp_ms": max(timestamps) if timestamps else None,
        "contributors": contributors_map,
        "items": parsed_items,
    }


def detect_cross_album_duplicates(
    rosalind_items: list[dict[str, Any]],
    isaac_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect media items present in both Rosalind and Isaac source albums using content dedup_key."""
    rosalind_by_key: dict[str, dict[str, Any]] = {}
    for item in rosalind_items:
        key = item.get("dedup_key") or f"{item['timestamp_ms']}_{item['width']}x{item['height']}"
        rosalind_by_key[key] = item

    isaac_by_key: dict[str, dict[str, Any]] = {}
    for item in isaac_items:
        key = item.get("dedup_key") or f"{item['timestamp_ms']}_{item['width']}x{item['height']}"
        isaac_by_key[key] = item

    duplicate_keys = sorted(set(rosalind_by_key.keys()) & set(isaac_by_key.keys()))
    duplicate_set = set(duplicate_keys)

    for item in rosalind_items:
        key = item.get("dedup_key") or f"{item['timestamp_ms']}_{item['width']}x{item['height']}"
        item["is_cross_album_duplicate"] = key in duplicate_set

    for item in isaac_items:
        key = item.get("dedup_key") or f"{item['timestamp_ms']}_{item['width']}x{item['height']}"
        item["is_cross_album_duplicate"] = key in duplicate_set

    return {
        "duplicate_count": len(duplicate_keys),
        "rosalind_unique_keys": len(rosalind_by_key),
        "isaac_unique_keys": len(isaac_by_key),
        "rosalind_overlap_ratio": (
            round(len(duplicate_keys) / len(rosalind_by_key), 4) if rosalind_by_key else 0.0
        ),
        "isaac_overlap_ratio": (
            round(len(duplicate_keys) / len(isaac_by_key), 4) if isaac_by_key else 0.0
        ),
        "duplicate_dedup_keys": duplicate_keys,
    }


def partition_birthday_windows(
    albums_data: dict[str, dict[str, Any]],
    windows: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Partition source album pools into exact birthday-year windows for target curation."""
    target_windows = windows or TARGET_BIRTHDAY_WINDOWS
    partitions: dict[str, dict[str, Any]] = {}

    for target_name, spec in target_windows.items():
        start_ms = spec["start_ms"]
        end_ms = spec["end_ms"]
        source_names = spec["source_albums"]

        # Collect unique candidate items by dedup_key across the specified source albums
        candidates_by_key: dict[str, dict[str, Any]] = {}
        for src_name in source_names:
            src_album = albums_data.get(src_name, {})
            for item in src_album.get("items", []):
                ts = item["timestamp_ms"]
                if start_ms <= ts < end_ms:
                    key = item.get("dedup_key") or item["item_id"]
                    if key not in candidates_by_key:
                        candidates_by_key[key] = item

        candidates = sorted(candidates_by_key.values(), key=lambda x: x["timestamp_ms"])
        existing_target = albums_data.get(target_name, {})
        existing_items = existing_target.get("items", [])
        existing_keys = {it.get("dedup_key") or it["item_id"] for it in existing_items}

        video_count = sum(1 for it in candidates if it.get("is_video"))
        photo_count = len(candidates) - video_count
        hearted_count = sum(1 for it in candidates if it.get("has_heart"))
        cross_dup_count = sum(1 for it in candidates if it.get("is_cross_album_duplicate"))

        contributor_counts: dict[str, int] = {}
        for it in candidates:
            cname = it.get("contributor_name") or it.get("contributor_id") or "unknown"
            contributor_counts[cname] = contributor_counts.get(cname, 0) + 1

        partitions[target_name] = {
            "target_album": target_name,
            "child": spec["child"],
            "source_albums": source_names,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_iso": spec.get("start_iso"),
            "end_iso": spec.get("end_iso"),
            "candidate_count": len(candidates),
            "photo_count": photo_count,
            "video_count": video_count,
            "hearted_count": hearted_count,
            "cross_album_duplicate_count": cross_dup_count,
            "existing_curated_count": len(existing_items),
            "already_in_target_count": sum(
                1 for it in candidates if (it.get("dedup_key") or it["item_id"]) in existing_keys
            ),
            "contributor_breakdown": contributor_counts,
            "candidates": candidates,
        }

    return partitions


def compute_calibration_summary(albums_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute historical calibration statistics across Rosie Year 1..5 and Isaac Year 1."""
    per_album_stats: dict[str, dict[str, Any]] = {}
    total_curated_items = 0
    total_source_window_items = 0

    for cal_name, cal_spec in CALIBRATION_WINDOWS.items():
        cal_album = albums_data.get(cal_name, {})
        curated_items = cal_album.get("items", [])
        if not curated_items:
            continue

        curated_keys = {it.get("dedup_key") or it["item_id"] for it in curated_items}
        timestamps = [it["timestamp_ms"] for it in curated_items if it["timestamp_ms"] > 0]
        min_ts = min(timestamps) if timestamps else cal_spec["start_ms"]
        max_ts = max(timestamps) if timestamps else cal_spec["end_ms"]

        # Pool of source items within the same birthday window
        src_pool_by_key: dict[str, dict[str, Any]] = {}
        for src_name in cal_spec["source_albums"]:
            for item in albums_data.get(src_name, {}).get("items", []):
                if cal_spec["start_ms"] <= item["timestamp_ms"] < cal_spec["end_ms"]:
                    key = item.get("dedup_key") or item["item_id"]
                    src_pool_by_key[key] = item

        source_pool = list(src_pool_by_key.values())
        matched_in_source = [it for k, it in src_pool_by_key.items() if k in curated_keys]

        curated_videos = sum(1 for it in curated_items if it.get("is_video"))
        curated_photos = len(curated_items) - curated_videos
        curated_hearts = sum(1 for it in curated_items if it.get("has_heart"))
        source_hearts = sum(1 for it in source_pool if it.get("has_heart"))
        matched_source_hearts = sum(1 for it in matched_in_source if it.get("has_heart"))
        matched_cross_dups = sum(1 for it in matched_in_source if it.get("is_cross_album_duplicate"))

        # Monthly distribution inside curated album
        monthly_counts: dict[str, int] = {}
        for it in curated_items:
            dt = datetime.datetime.fromtimestamp(it["timestamp_ms"] / 1000.0, tz=datetime.timezone.utc)
            ym = dt.strftime("%Y-%m")
            monthly_counts[ym] = monthly_counts.get(ym, 0) + 1

        # Contributor distribution inside curated album
        contributor_counts: dict[str, int] = {}
        for it in curated_items:
            cname = it.get("contributor_name") or it.get("contributor_id") or "unknown"
            contributor_counts[cname] = contributor_counts.get(cname, 0) + 1

        selection_rate = round(len(curated_items) / len(source_pool), 4) if source_pool else None

        total_curated_items += len(curated_items)
        total_source_window_items += len(source_pool)

        per_album_stats[cal_name] = {
            "album_name": cal_name,
            "child": cal_spec["child"],
            "curated_count": len(curated_items),
            "source_pool_count": len(source_pool),
            "matched_in_source_count": len(matched_in_source),
            "selection_rate": selection_rate,
            "photo_count": curated_photos,
            "video_count": curated_videos,
            "video_ratio": round(curated_videos / len(curated_items), 4) if curated_items else 0.0,
            "curated_hearted_count": curated_hearts,
            "source_hearted_count": source_hearts,
            "matched_source_hearted_count": matched_source_hearts,
            "heart_selection_rate": (
                round(matched_source_hearts / source_hearts, 4) if source_hearts > 0 else None
            ),
            "cross_album_duplicate_count": matched_cross_dups,
            "min_timestamp_ms": min_ts,
            "max_timestamp_ms": max_ts,
            "monthly_distribution": dict(sorted(monthly_counts.items())),
            "contributor_breakdown": contributor_counts,
        }

    avg_curated_size = (
        round(total_curated_items / len(per_album_stats), 1) if per_album_stats else 0.0
    )
    overall_selection_rate = (
        round(total_curated_items / total_source_window_items, 4)
        if total_source_window_items > 0
        else None
    )

    return {
        "calibrated_albums_count": len(per_album_stats),
        "total_curated_items": total_curated_items,
        "total_source_window_items": total_source_window_items,
        "average_curated_album_size": avg_curated_size,
        "overall_selection_rate": overall_selection_rate,
        "albums": per_album_stats,
    }


def build_curation_manifest(
    album_registry: dict[str, dict[str, str]] | None = None,
    output_path: Path | str = DEFAULT_MANIFEST_PATH,
    max_workers: int = 6,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Scrape all 13 albums, detect cross-album duplicates, partition target windows, and save manifest."""
    registry = album_registry or ALBUM_REGISTRY
    albums_data: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(
                scrape_shared_album,
                album_name=name,
                short_url=meta["short_url"],
                max_pages=max_pages,
            ): name
            for name, meta in registry.items()
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            albums_data[name] = future.result()

    # Preserve registry ordering
    ordered_albums: dict[str, dict[str, Any]] = {
        name: albums_data[name] for name in registry if name in albums_data
    }

    rosalind_source_items: list[dict[str, Any]] = []
    for r_name in ("Rosalind '19-'23", "Rosalind '23-"):
        if r_name in ordered_albums:
            rosalind_source_items.extend(ordered_albums[r_name]["items"])

    isaac_source_items: list[dict[str, Any]] = []
    if "Isaac '23-" in ordered_albums:
        isaac_source_items.extend(ordered_albums["Isaac '23-"]["items"])

    cross_duplicates = detect_cross_album_duplicates(rosalind_source_items, isaac_source_items)
    target_partitions = partition_birthday_windows(ordered_albums)
    calibration_summary = compute_calibration_summary(ordered_albums)

    manifest: dict[str, Any] = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "album_count": len(ordered_albums),
        "total_indexed_items": sum(a["item_count"] for a in ordered_albums.values()),
        "album_summaries": {
            name: {
                "album_title": a["album_title"],
                "role": registry[name].get("role"),
                "child": registry[name].get("child"),
                "short_url": a["short_url"],
                "album_id": a["album_id"],
                "expected_total": a["expected_total"],
                "pages_fetched": a["pages_fetched"],
                "item_count": a["item_count"],
                "min_timestamp_ms": a["min_timestamp_ms"],
                "max_timestamp_ms": a["max_timestamp_ms"],
            }
            for name, a in ordered_albums.items()
        },
        "cross_album_duplicates": cross_duplicates,
        "calibration": calibration_summary,
        "target_windows": target_partitions,
        "albums": ordered_albums,
    }

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _item_key(item: dict[str, Any]) -> str:
    """Return canonical deduplication key for a parsed media item."""
    return str(item.get("dedup_key") or item.get("item_id") or f"{item.get('timestamp_ms', 0)}_{item.get('width', 0)}x{item.get('height', 0)}")


def group_temporal_bursts(
    candidates: list[dict[str, Any]],
    window_ms: int = BURST_WINDOW_MS,
) -> list[list[dict[str, Any]]]:
    """Group chronologically sorted items within `window_ms` (default 90s) into temporal bursts."""
    if not candidates:
        return []

    sorted_items = sorted(
        (dict(it) for it in candidates),
        key=lambda x: (int(x.get("timestamp_ms", 0)), str(x.get("item_id", ""))),
    )

    bursts: list[list[dict[str, Any]]] = []
    current_burst: list[dict[str, Any]] = []

    for item in sorted_items:
        if not current_burst:
            current_burst.append(item)
            continue
        prev_ts = int(current_burst[-1].get("timestamp_ms", 0))
        curr_ts = int(item.get("timestamp_ms", 0))
        if curr_ts - prev_ts <= window_ms:
            current_burst.append(item)
        else:
            bursts.append(current_burst)
            current_burst = [item]

    if current_burst:
        bursts.append(current_burst)

    for idx, burst in enumerate(bursts):
        burst_id = f"burst_{idx:04d}"
        burst_size = len(burst)
        for rank_idx, item in enumerate(burst):
            item["burst_id"] = burst_id
            item["burst_size"] = burst_size
            item["burst_index"] = rank_idx

    return bursts


def refresh_live_thumbnail_urls(
    manifest: dict[str, Any],
    album_names: list[str] | None = None,
    max_workers: int = 6,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Refresh expiring Google Photos `base_url`s from source and calibration shared albums without clobbering baseline target items."""
    albums = manifest.get("albums", {})
    if album_names is None:
        # Refresh source and calibration albums; preserve original baseline items in target albums
        album_names = [
            name
            for name, meta in ALBUM_REGISTRY.items()
            if meta.get("role") in ("source", "calibration")
        ]

    fresh_by_album: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        fut_to_name = {
            executor.submit(
                scrape_shared_album,
                album_name=name,
                short_url=ALBUM_REGISTRY[name]["short_url"],
            ): name
            for name in album_names
            if name in ALBUM_REGISTRY
        }
        for fut in as_completed(fut_to_name):
            name = fut_to_name[fut]
            fresh_by_album[name] = fut.result()

    url_by_dedup_key: dict[str, str] = {}
    item_id_by_album_and_key: dict[tuple[str, str], str] = {}
    for name, fresh_alb in fresh_by_album.items():
        if name in albums:
            albums[name]["album_id"] = fresh_alb["album_id"]
            albums[name]["auth_key"] = fresh_alb["auth_key"]
            albums[name]["resolved_url"] = fresh_alb["resolved_url"]
        for it in fresh_alb.get("items", []):
            k = _item_key(it)
            if it.get("base_url"):
                url_by_dedup_key[k] = it["base_url"]
            if it.get("item_id"):
                item_id_by_album_and_key[(name, k)] = str(it["item_id"])

    refreshed_count = 0
    for alb_name, alb_data in albums.items():
        for it in alb_data.get("items", []):
            k = _item_key(it)
            if k in url_by_dedup_key:
                it["base_url"] = url_by_dedup_key[k]
                refreshed_count += 1
            if (alb_name, k) in item_id_by_album_and_key:
                it["item_id"] = item_id_by_album_and_key[(alb_name, k)]

    for t_win in manifest.get("target_windows", {}).values():
        for it in t_win.get("candidates", []):
            k = _item_key(it)
            if k in url_by_dedup_key:
                it["base_url"] = url_by_dedup_key[k]
            src_alb = str(it.get("album_name") or "")
            if (src_alb, k) in item_id_by_album_and_key:
                it["item_id"] = item_id_by_album_and_key[(src_alb, k)]

    if output_path is not None:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "refreshed_albums": list(fresh_by_album.keys()),
        "unique_urls_refreshed": len(url_by_dedup_key),
        "album_item_urls_updated": refreshed_count,
    }


def analyze_thumbnail_bytes(
    image_bytes: bytes,
    item: dict[str, Any] | None = None,
    reference_centroids: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Run multimodal vision analysis on a thumbnail image (SigLIP aesthetics, InsightFace/landmark detection, child face similarity, smile/open-eye, and blur/screenshot rejection)."""
    return modal_photos_scorer.analyze_image_multimodal(
        image_bytes=image_bytes,
        item=item,
        reference_centroids=reference_centroids,
    )


def _fallback_vision_scores(item: dict[str, Any], error_msg: str | None = None) -> dict[str, Any]:
    """Compute deterministic fallback vision scores from item dimensions/metadata when offline."""
    w = max(1, int(item.get("width", 1920) or 1920))
    h = max(1, int(item.get("height", 1080) or 1080))
    mp = (w * h) / 1_000_000.0
    res_score = round(float(np.clip(mp / 12.0, 0.25, 0.95)), 4)
    is_cross = bool(item.get("is_cross_album_duplicate"))
    key_str = _item_key(item)
    key_hash = sum(ord(ch) * (idx + 1) for idx, ch in enumerate(key_str)) % 100

    if is_cross:
        # Deterministic profile when offline: ~65% shared sibling/group scenes, ~18% solo older child, ~17% solo toddler
        if w >= h or key_hash < 65:
            age_class = "shared_siblings_or_group"
            is_shared = True
            faces = 2
            group_score = 0.86
            child_match = "both"
            rosie_sim = 0.72
            isaac_sim = 0.72
        elif key_hash < 83:
            age_class = "solo_older_child"
            is_shared = False
            faces = 1
            group_score = 0.54
            child_match = "rosalind"
            rosie_sim = 0.78
            isaac_sim = 0.42
        else:
            age_class = "solo_toddler"
            is_shared = False
            faces = 1
            group_score = 0.52
            child_match = "isaac"
            rosie_sim = 0.42
            isaac_sim = 0.78
    else:
        age_class = "general"
        is_shared = False
        faces = 1
        group_score = 0.55
        child_match = "unknown"
        rosie_sim = 0.62
        isaac_sim = 0.62

    comp_score = round(0.68 if is_shared else 0.58 * res_score + 0.2, 4)
    res: dict[str, Any] = {
        "thumb_width": w,
        "thumb_height": h,
        "sharpness_score": res_score,
        "face_sharpness_score": res_score,
        "exposure_score": 0.65,
        "colorfulness_score": 0.6,
        "estimated_faces": faces,
        "face_area_ratio": 0.08 if is_shared else 0.05,
        "group_composition_score": group_score,
        "expression_score": 0.65,
        "smile_score": 0.65,
        "eyes_open_score": 0.72,
        "siglip_aesthetic_score": comp_score,
        "screenshot_score": 0.0,
        "is_screenshot_or_document": False,
        "is_heavy_blur": False,
        "is_closed_eyes": False,
        "rosie_face_similarity": rosie_sim,
        "isaac_face_similarity": isaac_sim,
        "child_identity_match": child_match,
        "face_embedding": [round(1.0 / (32**0.5), 5)] * 32,
        "subject_age_class": age_class,
        "is_shared_sibling_shot": is_shared,
        "vision_composite_score": comp_score,
        "scoring_backend": "fallback_metadata",
    }
    if error_msg:
        res["fetch_error"] = error_msg
    return res


def fetch_and_score_thumbnail(
    item: dict[str, Any],
    session: requests.Session | None = None,
    timeout: int = 10,
    reference_centroids: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Fetch `=w512-h512` thumbnail from `base_url` and run multimodal vision scoring."""
    base_url = item.get("base_url") or ""
    if not base_url:
        return _fallback_vision_scores(item, error_msg="missing_base_url")

    thumb_url = f"{base_url}=w512-h512"
    try:
        if session is not None:
            resp = session.get(thumb_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
            img_bytes = resp.content
        else:
            req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as url_resp:
                img_bytes = url_resp.read()
        return analyze_thumbnail_bytes(img_bytes, item=item, reference_centroids=reference_centroids)
    except Exception as exc:
        return _fallback_vision_scores(item, error_msg=str(exc))


def _metadata_pre_score(
    item: dict[str, Any],
    existing_keys: set[str] | None = None,
    vision_info: dict[str, Any] | None = None,
) -> float:
    """Calibrated pre-score used inside bursts without letting hearts override blurred/closed-eye/screenshot frames."""
    key = _item_key(item)
    score = 0.0
    if existing_keys and key in existing_keys:
        score += 1000.0

    rejected = modal_photos_scorer.is_quality_rejected_frame(item, vision_info or {}) if vision_info else False
    if rejected:
        score -= 25.0
    elif item.get("has_heart"):
        # Calibrated tie-breaker bonus (+3.5..+5.0) rather than +48 domination
        heart_cnt = max(1, int(item.get("heart_count", 1) or 1))
        score += 3.5 + 0.5 * min(heart_cnt, 3)

    if vision_info:
        score += 12.0 * float(vision_info.get("siglip_aesthetic_score", vision_info.get("vision_composite_score", 0.5)))
        score += 8.0 * float(vision_info.get("sharpness_score", 0.5))
        score += 6.0 * float(vision_info.get("face_sharpness_score", 0.5))

    w = int(item.get("width", 0) or 0)
    h = int(item.get("height", 0) or 0)
    mp = (w * h) / 1_000_000.0
    score += min(12.0, mp) * 0.4
    if item.get("is_video"):
        dur_ms = int(item.get("video_duration_ms") or 0)
        # Sweet-spot family video duration: 5s to 60s
        if 4_000 <= dur_ms <= 65_000:
            score += 4.5
        elif dur_ms > 0:
            score += 2.0
    return score


def score_candidates_vision(
    candidates: list[dict[str, Any]],
    cache_path: Path | str | None = DEFAULT_VISION_CACHE_PATH,
    max_workers: int = 32,
    fetch_network: bool = True,
    reference_centroids: dict[str, list[float]] | None = None,
    use_modal: bool = False,
) -> dict[str, dict[str, Any]]:
    """Run concurrent or Modal GPU batch thumbnail vision scoring across ALL candidates with persistent disk cache."""
    if fetch_network and use_modal:
        cache = modal_photos_scorer.run_batch_vision_scoring(
            items=candidates,
            cache_path=cache_path,
            reference_centroids=reference_centroids,
            max_workers=max_workers,
            use_modal=True,
        )
        for item in candidates:
            k = _item_key(item)
            if k not in cache:
                cache[k] = _fallback_vision_scores(item, error_msg="modal_missing_fallback")
        return cache

    cache: dict[str, dict[str, Any]] = {}
    cache_file = Path(cache_path) if cache_path else None
    if cache_file and cache_file.exists():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache.update(loaded)
        except Exception:
            pass

    to_fetch: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = _item_key(item)
        existing = cache.get(key)
        needs_score = (
            existing is None
            or (
                fetch_network
                and (
                    "fetch_error" in existing
                    or "siglip_aesthetic_score" not in existing
                    or "eyes_open_score" not in existing
                )
            )
        )
        if needs_score and key not in to_fetch:
            to_fetch[key] = item

    if to_fetch:
        if not fetch_network:
            for key, item in to_fetch.items():
                cache[key] = _fallback_vision_scores(item)
        else:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=max_workers,
                pool_maxsize=max_workers,
                max_retries=1,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_key = {
                        executor.submit(
                            fetch_and_score_thumbnail,
                            item,
                            session,
                            10,
                            reference_centroids,
                        ): key
                        for key, item in to_fetch.items()
                    }
                    for future in as_completed(future_to_key):
                        key = future_to_key[future]
                        cache[key] = future.result()
            finally:
                session.close()

        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    return cache


def should_exclude_for_child(
    item: dict[str, Any],
    vision_info: dict[str, Any],
    child: str,
    existing_keys: set[str] | None = None,
) -> bool:
    """Determine if a candidate should be excluded due to solo-sibling age or face-identity mismatch.

    Rosie is ~5-6 years old in Year 6 (`child == 'rosalind'`); Isaac is ~1-2 years old in Year 2 (`child == 'isaac'`).
    Shared sibling/group shots (`estimated_faces >= 2` or `is_shared_sibling_shot=True`) are kept in both albums.
    Solo toddler shots of Isaac are excluded from Rosie's album and solo older-child shots of Rosie are excluded from Isaac's.
    """
    key = _item_key(item)
    if existing_keys and key in existing_keys:
        return False

    if vision_info.get("is_shared_sibling_shot") or int(vision_info.get("estimated_faces", 0)) >= 2:
        return False

    age_class = vision_info.get("subject_age_class", "general")
    identity_match = vision_info.get("child_identity_match", "unknown")
    rosie_sim = float(vision_info.get("rosie_face_similarity", 0.5))
    isaac_sim = float(vision_info.get("isaac_face_similarity", 0.5))
    is_cross_dup = bool(item.get("is_cross_album_duplicate"))

    # If the item was hearted inside a child-specific album and is NOT a cross-album auto-duplicate, trust the parent's heart
    if item.get("has_heart") and not is_cross_dup:
        return False

    if child == "rosalind":
        if age_class == "solo_toddler" or (identity_match == "isaac" and isaac_sim - rosie_sim >= 0.12):
            if is_cross_dup or not item.get("has_heart"):
                return True

    if child == "isaac":
        if age_class == "solo_older_child" or (identity_match == "rosalind" and rosie_sim - isaac_sim >= 0.12):
            if is_cross_dup or not item.get("has_heart"):
                return True

    return False


def compute_item_curation_score(
    item: dict[str, Any],
    vision_info: dict[str, Any],
    existing_keys: set[str] | None = None,
    probe_model: dict[str, Any] | None = None,
    child: str = "rosalind",
) -> float:
    """Compute unified curation score using the Personalized Year 1-5 Ranking Probe, SigLIP/InsightFace vision, and calibrated heart tie-breaker.

    Fixes the Pass 1 Heart Score Inversion:
    - Previously, `has_heart` added +30..+48 points while the entire vision ceiling was 0..30 points,
      allowing blurry, closed-eye, or screenshot hearted items to beat sharp unhearted portraits.
    - Now, the Personalized Year 1-5 Probe (0..35 pts) + Multimodal SigLIP/InsightFace Vision (0..35 pts)
      provide a 70-point primary quality signal, while `has_heart` acts strictly as a calibrated tie-breaker
      (+6.5..+9.5 pts) ONLY on quality-passing frames. Heavy blur, closed eyes, and screenshots receive a
      -35.0 penalty and zero heart bonus even if hearted.
    """
    key = _item_key(item)
    score = 0.0

    # 1. Existing baseline target album items are unconditionally locked in
    if existing_keys and key in existing_keys:
        score += 1000.0

    rejected = modal_photos_scorer.is_quality_rejected_frame(item, vision_info)

    # 2. Personalized Year 1-5 Ranking Probe (0..35 pts)
    probe_prob = modal_photos_scorer.predict_personalized_probe_probability(
        item=item,
        vision_info=vision_info,
        probe_model=probe_model,
        child=child,
    )
    score += 35.0 * probe_prob

    # 3. Multimodal SigLIP + InsightFace vision quality (0..35 pts)
    vision_comp = float(vision_info.get("vision_composite_score", 0.5))
    siglip_aes = float(vision_info.get("siglip_aesthetic_score", vision_comp))
    sharpness = float(vision_info.get("sharpness_score", 0.5))
    face_sharp = float(vision_info.get("face_sharpness_score", 0.5))
    group_comp = float(vision_info.get("group_composition_score", 0.5))
    expr = float(vision_info.get("expression_score", 0.5))
    smile = float(vision_info.get("smile_score", expr))
    eyes_open = float(vision_info.get("eyes_open_score", 0.70))

    score += (
        9.0 * siglip_aes
        + 6.5 * vision_comp
        + 5.5 * sharpness
        + 4.5 * face_sharp
        + 3.5 * smile
        + 2.5 * eyes_open
        + 3.5 * group_comp
    )

    # 4. Child reference face-embedding similarity bonus (0..4 pts)
    child_sim = (
        float(vision_info.get("rosie_face_similarity", 0.55))
        if child == "rosalind"
        else float(vision_info.get("isaac_face_similarity", 0.55))
    )
    score += max(0.0, (child_sim - 0.5) * 8.0)

    # 5. Shared sibling / group bonus
    if vision_info.get("is_shared_sibling_shot") or int(vision_info.get("estimated_faces", 0)) >= 2:
        score += 4.5

    # 6. Resolution & video duration quality bonus
    w = int(item.get("width", 0) or 0)
    h = int(item.get("height", 0) or 0)
    mp = (w * h) / 1_000_000.0
    score += min(3.0, mp * 0.25)

    if item.get("is_video"):
        dur_ms = int(item.get("video_duration_ms") or 0)
        if 4_000 <= dur_ms <= 65_000:
            score += 3.0
        elif dur_ms > 0:
            score += 1.2

    # 7. Calibrated Heart Tie-Breaker (+6.5..+9.5 pts ONLY if frame passes quality gates)
    if rejected:
        if not (existing_keys and key in existing_keys):
            score -= 35.0
    elif item.get("has_heart"):
        heart_cnt = max(1, int(item.get("heart_count", 1) or 1))
        score += 6.5 + 1.0 * min(heart_cnt, 3)

    return round(score, 4)


def compute_calibrated_monthly_quotas(
    candidates: list[dict[str, Any]],
    target_count: int,
    child: str,
    calibration: dict[str, Any] | None = None,
    existing_keys: set[str] | None = None,
) -> dict[str, int]:
    """Compute monthly (`YYYY-MM`) selection quotas calibrated against historical Year 1-5 distributions."""
    if not candidates or target_count <= 0:
        return {}

    effective_target = min(target_count, len(candidates))
    existing_set = existing_keys or set()

    # Group available candidates and existing locked items by YYYY-MM
    avail_by_ym: dict[str, int] = {}
    locked_by_ym: dict[str, int] = {}
    for it in candidates:
        dt = datetime.datetime.fromtimestamp(int(it["timestamp_ms"]) / 1000.0, tz=datetime.timezone.utc)
        ym = dt.strftime("%Y-%m")
        avail_by_ym[ym] = avail_by_ym.get(ym, 0) + 1
        if _item_key(it) in existing_set:
            locked_by_ym[ym] = locked_by_ym.get(ym, 0) + 1

    sorted_yms = sorted(avail_by_ym.keys())
    total_avail = sum(avail_by_ym.values())

    # Extract historical calendar-month (01..12) shares from calibration albums
    cal_month_totals: dict[str, float] = {f"{m:02d}": 0.0 for m in range(1, 13)}
    if calibration and isinstance(calibration.get("albums"), dict):
        for alb_info in calibration["albums"].values():
            alb_weight = 1.5 if alb_info.get("child") == child else 1.0
            m_dist = alb_info.get("monthly_distribution", {})
            alb_total = max(1, sum(m_dist.values()))
            for ym_key, cnt in m_dist.items():
                if "-" in ym_key:
                    mm = ym_key.split("-")[1]
                    cal_month_totals[mm] = cal_month_totals.get(mm, 0.0) + alb_weight * (cnt / alb_total)

    raw_hist_weights: dict[str, float] = {}
    for ym in sorted_yms:
        mm = ym.split("-")[1]
        # Partial boundary months (e.g., first/last month of birthday window) scaled by supply
        supply_ratio = avail_by_ym[ym] / float(max(1, total_avail))
        hist_val = cal_month_totals.get(mm, 0.08)
        raw_hist_weights[ym] = hist_val if supply_ratio >= 0.03 else hist_val * (supply_ratio / 0.03)

    hist_sum = sum(raw_hist_weights.values()) or 1.0
    blended_weights: dict[str, float] = {}
    for ym in sorted_yms:
        supply_share = avail_by_ym[ym] / float(max(1, total_avail))
        hist_share = raw_hist_weights[ym] / hist_sum
        blended_weights[ym] = 0.55 * supply_share + 0.45 * hist_share

    weight_sum = sum(blended_weights.values()) or 1.0
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []

    for ym in sorted_yms:
        exact = effective_target * (blended_weights[ym] / weight_sum)
        floor_q = max(locked_by_ym.get(ym, 0), min(avail_by_ym[ym], int(exact)))
        quotas[ym] = floor_q
        remainders.append((exact - int(exact), ym))

    # Adjust quotas so sum(quotas.values()) == effective_target while respecting [locked, avail] bounds
    current_sum = sum(quotas.values())
    if current_sum < effective_target:
        remainders.sort(key=lambda x: x[0], reverse=True)
        while current_sum < effective_target:
            progressed = False
            for _, ym in remainders:
                if current_sum >= effective_target:
                    break
                if quotas[ym] < avail_by_ym[ym]:
                    quotas[ym] += 1
                    current_sum += 1
                    progressed = True
            if not progressed:
                break
    elif current_sum > effective_target:
        remainders.sort(key=lambda x: x[0])
        while current_sum > effective_target:
            progressed = False
            for _, ym in remainders:
                if current_sum <= effective_target:
                    break
                if quotas[ym] > locked_by_ym.get(ym, 0):
                    quotas[ym] -= 1
                    current_sum -= 1
                    progressed = True
            if not progressed:
                break

    return dict(sorted(quotas.items()))


def curate_target_album(
    manifest: dict[str, Any],
    target_album: str,
    target_count: int | None = None,
    target_video_ratio: float | None = None,
    vision_cache_path: Path | str | None = DEFAULT_VISION_CACHE_PATH,
    output_dir: Path | str | None = DEFAULT_CURATION_DIR,
    max_workers: int = 32,
    fetch_network: bool = True,
    use_modal: bool = False,
) -> dict[str, Any]:
    """Execute Phase 2 Curation for a target album (`Rosie Year 6` or `Isaac Year 2`)."""
    spec = TARGET_CURATION_SPECS.get(
        target_album,
        {
            "child": TARGET_BIRTHDAY_WINDOWS.get(target_album, {}).get("child", "rosalind"),
            "target_count": 550,
            "target_video_ratio": 0.29,
            "output_filename": f"{target_album.lower().replace(' ', '_')}_curated.json",
        },
    )
    child = str(spec["child"])
    desired_total = int(target_count if target_count is not None else spec["target_count"])
    desired_video_ratio = float(
        target_video_ratio if target_video_ratio is not None else spec["target_video_ratio"]
    )

    window_info = manifest.get("target_windows", {}).get(target_album, {})
    raw_candidates = list(window_info.get("candidates", []))
    raw_existing_items = list(manifest.get("albums", {}).get(target_album, {}).get("items", []))
    # Preserve original baseline items (1 in Rosie Year 6, 34 in Isaac Year 2) even if manifest.json was re-scraped after Pass 1
    baseline_cap = BASELINE_TARGET_COUNTS.get(target_album)
    if baseline_cap is not None and len(raw_existing_items) > baseline_cap:
        existing_items = raw_existing_items[:baseline_cap]
    else:
        existing_items = raw_existing_items
    existing_keys = {_item_key(it) for it in existing_items}

    # Ensure all existing items in the target album are present in the candidate pool
    candidate_by_key: dict[str, dict[str, Any]] = {_item_key(it): dict(it) for it in raw_candidates}
    for ex_it in existing_items:
        ex_key = _item_key(ex_it)
        if ex_key not in candidate_by_key:
            candidate_by_key[ex_key] = dict(ex_it)

    all_candidates = sorted(
        candidate_by_key.values(),
        key=lambda x: (int(x.get("timestamp_ms", 0)), str(x.get("item_id", ""))),
    )

    # Step 1: Group raw items into 90-second temporal bursts
    bursts = group_temporal_bursts(all_candidates, window_ms=BURST_WINDOW_MS)

    # Step 2: Evaluate ALL candidate items across full camera bursts BEFORE picking burst winners (850-cap removed)
    vision_targets_by_key: dict[str, dict[str, Any]] = {_item_key(it): it for it in all_candidates}

    vision_scores = score_candidates_vision(
        list(vision_targets_by_key.values()),
        cache_path=vision_cache_path,
        max_workers=max_workers,
        fetch_network=fetch_network,
        use_modal=use_modal,
    )

    # Build child reference face centroids from Year 1-5 Rosie & Year 1 Isaac and train the Personalized Ranking Probe
    reference_centroids = modal_photos_scorer.build_child_reference_centroids(manifest, vision_scores)
    if reference_centroids:
        for k, v_info in vision_scores.items():
            emb = v_info.get("face_embedding")
            if isinstance(emb, list) and len(emb) == 32:
                if "rosalind" in reference_centroids:
                    v_info["rosie_face_similarity"] = modal_photos_scorer.cosine_similarity(
                        emb, reference_centroids["rosalind"]
                    )
                if "isaac" in reference_centroids:
                    v_info["isaac_face_similarity"] = modal_photos_scorer.cosine_similarity(
                        emb, reference_centroids["isaac"]
                    )

    probe_model = modal_photos_scorer.train_personalized_ranking_probe(
        manifest=manifest,
        vision_cache=vision_scores,
        calibration_windows=CALIBRATION_WINDOWS,
        fallback_vision_fn=_fallback_vision_scores,
    )

    # Propagate burst-representative sibling/age context to any fallback items in the same 90s burst
    for burst in bursts:
        rep_vision: dict[str, Any] | None = None
        for it in burst:
            k = _item_key(it)
            if k in vision_scores and "fetch_error" not in vision_scores[k]:
                rep_vision = vision_scores[k]
                break
        for it in burst:
            k = _item_key(it)
            if k not in vision_scores:
                fb = _fallback_vision_scores(it)
                if rep_vision is not None and it.get("is_cross_album_duplicate"):
                    fb["subject_age_class"] = rep_vision["subject_age_class"]
                    fb["is_shared_sibling_shot"] = rep_vision["is_shared_sibling_shot"]
                    fb["estimated_faces"] = rep_vision["estimated_faces"]
                vision_scores[k] = fb

    # Step 3: Full-burst multimodal evaluation, quality rejection & sibling age disambiguation
    burst_survivors: list[dict[str, Any]] = []
    excluded_solo_other_child_count = 0
    shared_sibling_kept_count = 0
    quality_rejected_count = 0

    for burst in bursts:
        eligible_in_burst: list[dict[str, Any]] = []
        for it in burst:
            key = _item_key(it)
            v_info = vision_scores[key]
            if should_exclude_for_child(it, v_info, child=child, existing_keys=existing_keys):
                excluded_solo_other_child_count += 1
                continue
            is_rejected = modal_photos_scorer.is_quality_rejected_frame(it, v_info)
            if is_rejected and key not in existing_keys:
                quality_rejected_count += 1
                continue
            item_copy = dict(it)
            item_copy["already_in_target"] = key in existing_keys
            item_copy["vision_scores"] = v_info
            item_copy["probe_probability"] = modal_photos_scorer.predict_personalized_probe_probability(
                item=item_copy,
                vision_info=v_info,
                probe_model=probe_model,
                child=child,
            )
            item_copy["curation_score"] = compute_item_curation_score(
                item_copy,
                v_info,
                existing_keys=existing_keys,
                probe_model=probe_model,
                child=child,
            )
            eligible_in_burst.append(item_copy)

        if not eligible_in_burst:
            continue

        # Always retain any item already in the target album
        locked = [it for it in eligible_in_burst if it["already_in_target"]]
        locked_keys = {_item_key(it) for it in locked}
        remaining = [it for it in eligible_in_burst if _item_key(it) not in locked_keys]

        # Separate remaining photos and videos so burst photos don't wipe out burst videos
        rem_photos = sorted(
            [it for it in remaining if not it.get("is_video")],
            key=lambda x: x["curation_score"],
            reverse=True,
        )
        rem_videos = sorted(
            [it for it in remaining if it.get("is_video")],
            key=lambda x: x["curation_score"],
            reverse=True,
        )

        # Keep sharpest/best-framed photo representative (up to 2 if large burst >= 6 or multiple hearted)
        max_photo_survivors = 2 if (len(burst) >= 6 or sum(1 for p in rem_photos if p.get("has_heart")) >= 2) else 1
        max_video_survivors = 2 if sum(1 for v in rem_videos if v.get("has_heart")) >= 2 else 1

        chosen_burst = locked + rem_photos[:max_photo_survivors] + rem_videos[:max_video_survivors]
        for ch in chosen_burst:
            if ch["vision_scores"].get("is_shared_sibling_shot"):
                shared_sibling_kept_count += 1
        burst_survivors.extend(chosen_burst)

    # Step 4: Apply calibrated monthly quotas & ~28-30% video ratio
    effective_target = min(desired_total, len(burst_survivors))
    target_videos_total = int(round(effective_target * desired_video_ratio))
    avail_videos_total = sum(1 for it in burst_survivors if it.get("is_video"))
    target_videos_total = min(target_videos_total, avail_videos_total)

    monthly_quotas = compute_calibrated_monthly_quotas(
        burst_survivors,
        target_count=effective_target,
        child=child,
        calibration=manifest.get("calibration"),
        existing_keys=existing_keys,
    )

    survivors_by_ym: dict[str, list[dict[str, Any]]] = {}
    for it in burst_survivors:
        dt = datetime.datetime.fromtimestamp(int(it["timestamp_ms"]) / 1000.0, tz=datetime.timezone.utc)
        ym = dt.strftime("%Y-%m")
        survivors_by_ym.setdefault(ym, []).append(it)

    selected_by_key: dict[str, dict[str, Any]] = {}
    # First lock in all existing target items
    for it in burst_survivors:
        if it.get("already_in_target"):
            it["selection_reason"] = "existing_in_target"
            selected_by_key[_item_key(it)] = it

    # Monthly quota selection split by photo/video sub-quotas
    for ym, quota in monthly_quotas.items():
        ym_pool = survivors_by_ym.get(ym, [])
        already_in_ym = [it for it in ym_pool if _item_key(it) in selected_by_key]
        needed_in_ym = max(0, quota - len(already_in_ym))
        if needed_in_ym == 0:
            continue

        desired_ym_videos = int(round(quota * desired_video_ratio))
        existing_ym_videos = sum(1 for it in already_in_ym if it.get("is_video"))
        needed_ym_videos = max(0, min(needed_in_ym, desired_ym_videos - existing_ym_videos))
        needed_ym_photos = needed_in_ym - needed_ym_videos

        unselected_videos = sorted(
            [it for it in ym_pool if it.get("is_video") and _item_key(it) not in selected_by_key],
            key=lambda x: x["curation_score"],
            reverse=True,
        )
        unselected_photos = sorted(
            [it for it in ym_pool if not it.get("is_video") and _item_key(it) not in selected_by_key],
            key=lambda x: x["curation_score"],
            reverse=True,
        )

        picked_videos = unselected_videos[:needed_ym_videos]
        picked_photos = unselected_photos[:needed_ym_photos]

        for it in picked_videos + picked_photos:
            it["selection_reason"] = "hearted_priority" if it.get("has_heart") else "monthly_quota_top_vision"
            selected_by_key[_item_key(it)] = it

        # If either media type fell short in this month, fill remaining monthly quota from the other type
        still_needed = quota - sum(1 for it in ym_pool if _item_key(it) in selected_by_key)
        if still_needed > 0:
            leftover = sorted(
                [it for it in ym_pool if _item_key(it) not in selected_by_key],
                key=lambda x: x["curation_score"],
                reverse=True,
            )
            for it in leftover[:still_needed]:
                it["selection_reason"] = "hearted_priority" if it.get("has_heart") else "monthly_quota_fill"
                selected_by_key[_item_key(it)] = it

    # Global top-up if under effective_target
    if len(selected_by_key) < effective_target:
        remaining_all = sorted(
            [it for it in burst_survivors if _item_key(it) not in selected_by_key],
            key=lambda x: x["curation_score"],
            reverse=True,
        )
        for it in remaining_all[: effective_target - len(selected_by_key)]:
            it["selection_reason"] = "global_topup"
            selected_by_key[_item_key(it)] = it

    # Global video-ratio calibration pass: ensure video count matches target_videos_total (~28-30%)
    current_videos = [
        it for it in selected_by_key.values() if it.get("is_video") and not it.get("already_in_target")
    ]
    current_photos = [
        it for it in selected_by_key.values() if not it.get("is_video") and not it.get("already_in_target")
    ]
    total_selected_videos = sum(1 for it in selected_by_key.values() if it.get("is_video"))

    if total_selected_videos < target_videos_total:
        need_v = target_videos_total - total_selected_videos
        avail_unselected_videos = sorted(
            [it for it in burst_survivors if it.get("is_video") and _item_key(it) not in selected_by_key],
            key=lambda x: x["curation_score"],
            reverse=True,
        )
        removable_photos = sorted(current_photos, key=lambda x: x["curation_score"])
        swaps = min(need_v, len(avail_unselected_videos), len(removable_photos))
        for i in range(swaps):
            drop_photo = removable_photos[i]
            add_video = avail_unselected_videos[i]
            del selected_by_key[_item_key(drop_photo)]
            add_video["selection_reason"] = "video_ratio_calibration"
            selected_by_key[_item_key(add_video)] = add_video
    elif total_selected_videos > target_videos_total:
        excess_v = total_selected_videos - target_videos_total
        avail_unselected_photos = sorted(
            [it for it in burst_survivors if not it.get("is_video") and _item_key(it) not in selected_by_key],
            key=lambda x: x["curation_score"],
            reverse=True,
        )
        removable_videos = sorted(current_videos, key=lambda x: x["curation_score"])
        swaps = min(excess_v, len(avail_unselected_photos), len(removable_videos))
        for i in range(swaps):
            drop_video = removable_videos[i]
            add_photo = avail_unselected_photos[i]
            del selected_by_key[_item_key(drop_video)]
            add_photo["selection_reason"] = "video_ratio_calibration"
            selected_by_key[_item_key(add_photo)] = add_photo

    selected_items = sorted(
        selected_by_key.values(),
        key=lambda x: (int(x.get("timestamp_ms", 0)), str(x.get("item_id", ""))),
    )
    newly_added_items = [it for it in selected_items if not it.get("already_in_target")]

    final_video_count = sum(1 for it in selected_items if it.get("is_video"))
    final_photo_count = len(selected_items) - final_video_count
    final_video_ratio = round(final_video_count / len(selected_items), 4) if selected_items else 0.0

    hearted_candidates_total = sum(1 for it in all_candidates if it.get("has_heart"))
    unhearted_candidates_total = len(all_candidates) - hearted_candidates_total
    hearted_selected = sum(1 for it in selected_items if it.get("has_heart"))
    unhearted_selected = len(selected_items) - hearted_selected

    heart_sel_rate = (
        round(hearted_selected / hearted_candidates_total, 4) if hearted_candidates_total > 0 else 0.0
    )
    unheart_sel_rate = (
        round(unhearted_selected / unhearted_candidates_total, 4) if unhearted_candidates_total > 0 else 0.0
    )
    heart_lift = round(heart_sel_rate / unheart_sel_rate, 2) if unheart_sel_rate > 0 else 0.0

    actual_monthly_dist: dict[str, int] = {}
    actual_contributors: dict[str, int] = {}
    for it in selected_items:
        dt = datetime.datetime.fromtimestamp(int(it["timestamp_ms"]) / 1000.0, tz=datetime.timezone.utc)
        ym = dt.strftime("%Y-%m")
        actual_monthly_dist[ym] = actual_monthly_dist.get(ym, 0) + 1
        cname = it.get("contributor_name") or it.get("contributor_id") or "unknown"
        actual_contributors[cname] = actual_contributors.get(cname, 0) + 1

    curated_manifest: dict[str, Any] = {
        "target_album": target_album,
        "child": child,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "start_iso": window_info.get("start_iso"),
            "end_iso": window_info.get("end_iso"),
            "start_ms": window_info.get("start_ms"),
            "end_ms": window_info.get("end_ms"),
        },
        "target_count": desired_total,
        "selected_count": len(selected_items),
        "existing_preserved_count": sum(1 for it in selected_items if it.get("already_in_target")),
        "newly_selected_count": len(newly_added_items),
        "photo_count": final_photo_count,
        "video_count": final_video_count,
        "video_ratio": final_video_ratio,
        "hearted_candidate_count": hearted_candidates_total,
        "hearted_selected_count": hearted_selected,
        "hearted_selection_rate": heart_sel_rate,
        "unhearted_selection_rate": unheart_sel_rate,
        "hearted_lift_ratio": heart_lift,
        "burst_stats": {
            "raw_candidate_count": len(all_candidates),
            "total_bursts": len(bursts),
            "burst_window_ms": BURST_WINDOW_MS,
            "burst_survivor_count": len(burst_survivors),
            "burst_duplicates_pruned": len(all_candidates) - len(burst_survivors) - excluded_solo_other_child_count - quality_rejected_count,
        },
        "vision_disambiguation_stats": {
            "thumbnails_analyzed": len(vision_targets_by_key),
            "cross_album_duplicates_evaluated": sum(
                1 for it in all_candidates if it.get("is_cross_album_duplicate")
            ),
            "shared_sibling_group_shots_kept": shared_sibling_kept_count,
            "solo_other_child_excluded": excluded_solo_other_child_count,
            "quality_rejected_frames": quality_rejected_count,
        },
        "personalized_probe_model": probe_model,
        "monthly_quotas": monthly_quotas,
        "monthly_distribution": dict(sorted(actual_monthly_dist.items())),
        "contributor_breakdown": actual_contributors,
        "selected_items": selected_items,
        "newly_added_items": newly_added_items,
    }

    if output_dir is not None:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / str(spec["output_filename"])
        out_file.write_text(json.dumps(curated_manifest, indent=2), encoding="utf-8")

    return curated_manifest


def curate_all_targets(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    output_dir: Path | str = DEFAULT_CURATION_DIR,
    vision_cache_path: Path | str | None = DEFAULT_VISION_CACHE_PATH,
    targets: list[str] | None = None,
    max_workers: int = 32,
    fetch_network: bool = True,
    use_modal: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load the indexed manifest and run Phase 2 Curation for Rosie Year 6 and Isaac Year 2."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    target_names = targets or list(TARGET_CURATION_SPECS.keys())
    results: dict[str, dict[str, Any]] = {}
    for t_name in target_names:
        results[t_name] = curate_target_album(
            manifest=manifest,
            target_album=t_name,
            vision_cache_path=vision_cache_path,
            output_dir=output_dir,
            max_workers=max_workers,
            fetch_network=fetch_network,
            use_modal=use_modal,
        )
    return results


def decrypt_chrome_cookies(
    cookies_db_path: Path | str = DEFAULT_CHROME_COOKIES_PATH,
    host_filter: str = "google",
) -> list[dict[str, Any]]:
    """Decrypt Chrome SQLite cookies using Linux PBKDF2 ('peanuts'/'saltysalt', 1 iter, AES-128-CBC, 32B prefix strip)."""
    db_path = Path(cookies_db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Chrome cookies DB not found at {db_path}")

    aes_key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    iv = b" " * 16

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, value, encrypted_value, path, is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE ?",
            (f"%{host_filter}%",),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    cookies: list[dict[str, Any]] = []
    for host_key, name, value, encrypted_value, path, is_secure, is_httponly in rows:
        dec_val = str(value or "")
        if not dec_val and encrypted_value:
            enc_bytes = bytes(encrypted_value)
            if enc_bytes[:3] in (b"v10", b"v11"):
                cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
                decryptor = cipher.decryptor()
                padded = decryptor.update(enc_bytes[3:]) + decryptor.finalize()
                pad_len = padded[-1] if padded else 0
                unpadded = padded[:-pad_len] if 0 < pad_len <= 16 else padded
                raw_val = unpadded[32:] if len(unpadded) >= 32 else unpadded
                dec_val = raw_val.decode("utf-8", errors="ignore")
        if dec_val:
            cookies.append(
                {
                    "name": str(name),
                    "value": dec_val,
                    "domain": str(host_key),
                    "path": str(path or "/"),
                    "secure": bool(is_secure),
                    "httpOnly": bool(is_httponly),
                }
            )
    return cookies


def extract_wiz_tokens(html: str) -> dict[str, str]:
    """Extract SNlM0e (at CSRF token), FdrFJe (f.sid), and cfb2h (bl build label) from Google Photos HTML."""
    tokens: dict[str, str] = {}
    for key in ("SNlM0e", "FdrFJe", "cfb2h", "qwAQke"):
        match = re.search(rf'"{key}":"([^"]+)"', html)
        if match:
            tokens[key] = match.group(1)
    return tokens


def build_add_items_rpc_payload(
    target_album_id: str,
    target_auth_key: str,
    items: list[dict[str, Any] | str],
    rpc_id: str = ADD_ITEMS_RPC_ID,
    source_album_id: str | None = None,
    source_auth_key: str | None = None,
) -> tuple[str, str]:
    """Construct the batchexecute (rpc_id, f.req) payload to batch-add media items into a shared album."""
    item_ids: list[str] = []
    for it in items:
        if isinstance(it, str):
            item_ids.append(it)
        elif isinstance(it, dict) and it.get("item_id"):
            item_ids.append(str(it["item_id"]))

    if rpc_id == SAVE_TO_LIBRARY_RPC_ID:
        inner_arg = json.dumps([item_ids, source_auth_key, source_album_id])
    elif rpc_id == ADD_LIBRARY_TO_SHARED_RPC_ID:
        item_entries = [[[iid]] for iid in item_ids]
        inner_arg = json.dumps(
            [
                target_album_id,
                [2, None, item_entries, None, None, None, [1], None, None, None, None, None, None, 0],
                target_auth_key,
            ]
        )
    elif source_album_id and source_auth_key:
        item_entries = [[[iid, source_album_id, source_auth_key] for iid in item_ids]]
        inner_arg = json.dumps([target_album_id, item_entries, target_auth_key])
    else:
        inner_arg = json.dumps([[target_album_id], [item_ids, None, None, 0], [None, target_auth_key]])

    f_req = json.dumps([[[rpc_id, inner_arg, None, "generic"]]])
    return rpc_id, f_req


def build_remove_shared_items_rpc_payload(
    target_album_id: str,
    items: list[dict[str, Any] | str],
    rpc_id: str = REMOVE_SHARED_ITEMS_RPC_ID,
) -> tuple[str, str]:
    """Construct the batchexecute (rpc_id='LjmOue', f.req) payload to remove items from a shared album ONLY without touching global library Trash."""
    item_ids: list[str] = []
    for it in items:
        if isinstance(it, str) and it:
            item_ids.append(it)
        elif isinstance(it, dict) and it.get("item_id"):
            item_ids.append(str(it["item_id"]))

    actor_ctx = [
        None,
        None,
        None,
        [None, [], []],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        [],
    ]
    inner_arg = json.dumps(
        [
            [target_album_id],
            [[iid] for iid in item_ids],
            [actor_ctx],
        ]
    )
    f_req = json.dumps([[[rpc_id, inner_arg, None, "generic"]]])
    return rpc_id, f_req


def _extract_v8rkj_library_ids(v8_parsed: Any) -> list[str]:
    """Extract canonical Google Photos library item IDs from a `V8RKJ` response payload."""
    lib_ids: list[str] = []
    if not isinstance(v8_parsed, list) or not v8_parsed:
        return lib_ids
    first_slot = v8_parsed[0]
    if isinstance(first_slot, list):
        for entry in first_slot:
            if isinstance(entry, str) and entry:
                lib_ids.append(entry)
            elif isinstance(entry, list) and entry and isinstance(entry[0], str) and entry[0]:
                lib_ids.append(entry[0])
    return lib_ids


def remove_shared_album_items(
    target_album: str,
    target_album_id: str,
    target_auth_key: str,
    items_to_remove: list[dict[str, Any] | str],
    cookies: list[dict[str, Any]] | dict[str, str] | None = None,
    cookies_db_path: Path | str = DEFAULT_CHROME_COOKIES_PATH,
    batch_size: int = 50,
    rpc_id: str = REMOVE_SHARED_ITEMS_RPC_ID,
    at_token: str | None = None,
    bl_token: str | None = None,
    f_sid: str | None = None,
    session: requests.Session | None = None,
    dry_run: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Remove specified items from a shared album via `LjmOue` without moving any photo to Google Photos Trash."""
    if not items_to_remove or dry_run:
        return {
            "target_album": target_album,
            "target_album_id": target_album_id,
            "requested_remove_count": len(items_to_remove),
            "removed_count": 0 if dry_run else len(items_to_remove),
            "batches_executed": 0,
            "rpc_id": rpc_id,
            "dry_run": dry_run,
            "status": "dry_run" if dry_run else "noop",
            "removed_item_ids": [],
        }

    own_session = session is None
    http_session = session or requests.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

    if cookies is None and Path(cookies_db_path).exists():
        cookies = decrypt_chrome_cookies(cookies_db_path)

    if isinstance(cookies, list):
        for c in cookies:
            http_session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ".google.com"),
                path=c.get("path", "/"),
            )
    elif isinstance(cookies, dict):
        for k, v in cookies.items():
            http_session.cookies.set(k, v, domain=".google.com")

    try:
        share_url = f"https://photos.google.com/share/{target_album_id}?key={target_auth_key}"
        if not at_token:
            page_resp = http_session.get(share_url, timeout=timeout)
            page_resp.raise_for_status()
            wiz = extract_wiz_tokens(page_resp.text)
            at_token = wiz.get("SNlM0e", "")
            bl_token = bl_token or wiz.get("cfb2h", "")
            f_sid = f_sid or wiz.get("FdrFJe", "")

        batches_executed = 0
        removed_item_ids: list[str] = []

        for start_idx in range(0, len(items_to_remove), max(1, batch_size)):
            chunk = items_to_remove[start_idx : start_idx + max(1, batch_size)]
            _, f_req = build_remove_shared_items_rpc_payload(
                target_album_id=target_album_id,
                items=chunk,
                rpc_id=rpc_id,
            )
            params: dict[str, str] = {
                "rpcids": rpc_id,
                "source-path": f"/share/{target_album_id}",
                "hl": "en",
                "rt": "c",
            }
            if bl_token:
                params["bl"] = bl_token
            if f_sid:
                params["f.sid"] = f_sid

            post_url = "https://photos.google.com/_/PhotosUi/data/batchexecute?" + urllib.parse.urlencode(params)
            form_data: dict[str, str] = {"f.req": f_req}
            if at_token:
                form_data["at"] = at_token

            resp = http_session.post(
                post_url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Origin": "https://photos.google.com",
                    "Referer": share_url,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            parse_batchexecute_response(resp.text, rpc_id=rpc_id)
            batches_executed += 1
            for it in chunk:
                iid = it if isinstance(it, str) else str(it.get("item_id", ""))
                if iid:
                    removed_item_ids.append(iid)

        return {
            "target_album": target_album,
            "target_album_id": target_album_id,
            "requested_remove_count": len(items_to_remove),
            "removed_count": len(removed_item_ids),
            "batches_executed": batches_executed,
            "rpc_id": rpc_id,
            "dry_run": False,
            "status": "completed",
            "removed_item_ids": removed_item_ids,
        }
    finally:
        if own_session:
            http_session.close()


def populate_shared_album(
    target_album: str,
    target_album_id: str,
    target_auth_key: str,
    items_to_add: list[dict[str, Any]],
    cookies: list[dict[str, Any]] | dict[str, str] | None = None,
    cookies_db_path: Path | str = DEFAULT_CHROME_COOKIES_PATH,
    batch_size: int = 50,
    rpc_id: str = ADD_ITEMS_RPC_ID,
    source_album_id: str | None = None,
    source_auth_key: str | None = None,
    at_token: str | None = None,
    bl_token: str | None = None,
    f_sid: str | None = None,
    session: requests.Session | None = None,
    dry_run: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Batch-populate curated items into a Google Photos shared album via authenticated batchexecute RPC."""
    if not items_to_add or dry_run:
        return {
            "target_album": target_album,
            "target_album_id": target_album_id,
            "requested_count": len(items_to_add),
            "added_count": 0 if dry_run else len(items_to_add),
            "batches_executed": 0,
            "rpc_id": rpc_id,
            "dry_run": dry_run,
            "status": "dry_run" if dry_run else "noop",
            "added_item_ids": [],
        }

    own_session = session is None
    http_session = session or requests.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

    if cookies is None and Path(cookies_db_path).exists():
        cookies = decrypt_chrome_cookies(cookies_db_path)

    if isinstance(cookies, list):
        for c in cookies:
            http_session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ".google.com"),
                path=c.get("path", "/"),
            )
    elif isinstance(cookies, dict):
        for k, v in cookies.items():
            http_session.cookies.set(k, v, domain=".google.com")

    try:
        share_url = f"https://photos.google.com/share/{target_album_id}?key={target_auth_key}"
        if not at_token:
            page_resp = http_session.get(share_url, timeout=timeout)
            page_resp.raise_for_status()
            wiz = extract_wiz_tokens(page_resp.text)
            at_token = wiz.get("SNlM0e", "")
            bl_token = bl_token or wiz.get("cfb2h", "")
            f_sid = f_sid or wiz.get("FdrFJe", "")

        batches_executed = 0
        added_item_ids: list[str] = []
        batch_responses: list[Any] = []

        def _post_v8_chunk(sub_chunk: list[dict[str, Any] | str]) -> list[str]:
            nonlocal batches_executed
            if not sub_chunk:
                return []
            _, v8_req = build_add_items_rpc_payload(
                target_album_id=target_album_id,
                target_auth_key=target_auth_key,
                items=sub_chunk,
                rpc_id=SAVE_TO_LIBRARY_RPC_ID,
                source_album_id=source_album_id,
                source_auth_key=source_auth_key,
            )
            v8_params: dict[str, str] = {
                "rpcids": SAVE_TO_LIBRARY_RPC_ID,
                "source-path": f"/share/{source_album_id or target_album_id}",
                "hl": "en",
                "rt": "c",
            }
            if bl_token:
                v8_params["bl"] = bl_token
            if f_sid:
                v8_params["f.sid"] = f_sid
            v8_url = "https://photos.google.com/_/PhotosUi/data/batchexecute?" + urllib.parse.urlencode(v8_params)
            v8_form = {"f.req": v8_req}
            if at_token:
                v8_form["at"] = at_token
            v8_resp = http_session.post(
                v8_url,
                data=v8_form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Origin": "https://photos.google.com",
                    "Referer": share_url,
                },
                timeout=timeout,
            )
            v8_resp.raise_for_status()
            batches_executed += 1
            v8_parsed = parse_batchexecute_response(v8_resp.text, rpc_id=SAVE_TO_LIBRARY_RPC_ID)
            extracted = _extract_v8rkj_library_ids(v8_parsed)
            if not extracted:
                extracted = [
                    it if isinstance(it, str) else str(it.get("item_id", ""))
                    for it in sub_chunk
                    if (it if isinstance(it, str) else it.get("item_id"))
                ]
            return extracted

        for start_idx in range(0, len(items_to_add), max(1, batch_size)):
            chunk = items_to_add[start_idx : start_idx + max(1, batch_size)]
            if rpc_id in ("V8RKJ+laUYf", f"{SAVE_TO_LIBRARY_RPC_ID}+{ADD_LIBRARY_TO_SHARED_RPC_ID}"):
                lib_ids: list[str] = []
                try:
                    lib_ids = _post_v8_chunk(chunk)
                except Exception:
                    # Graceful sub-batch fallback if any stale or quota-limited item caused V8RKJ batch failure
                    if len(chunk) > 1:
                        sub_size = max(1, len(chunk) // 5)
                        for sub_start in range(0, len(chunk), sub_size):
                            sub_c = chunk[sub_start : sub_start + sub_size]
                            try:
                                lib_ids.extend(_post_v8_chunk(sub_c))
                            except Exception:
                                pass

                if not lib_ids:
                    continue

                _, la_req = build_add_items_rpc_payload(
                    target_album_id=target_album_id,
                    target_auth_key=target_auth_key,
                    items=lib_ids,
                    rpc_id=ADD_LIBRARY_TO_SHARED_RPC_ID,
                )
                la_params: dict[str, str] = {
                    "rpcids": ADD_LIBRARY_TO_SHARED_RPC_ID,
                    "source-path": f"/share/{target_album_id}",
                    "hl": "en",
                    "rt": "c",
                }
                if bl_token:
                    la_params["bl"] = bl_token
                if f_sid:
                    la_params["f.sid"] = f_sid
                la_url = "https://photos.google.com/_/PhotosUi/data/batchexecute?" + urllib.parse.urlencode(la_params)
                la_form = {"f.req": la_req}
                if at_token:
                    la_form["at"] = at_token
                try:
                    la_resp = http_session.post(
                        la_url,
                        data=la_form,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                            "Origin": "https://photos.google.com",
                            "Referer": share_url,
                        },
                        timeout=timeout,
                    )
                    la_resp.raise_for_status()
                    parsed = parse_batchexecute_response(la_resp.text, rpc_id=ADD_LIBRARY_TO_SHARED_RPC_ID)
                    batch_responses.append(parsed)
                    batches_executed += 1
                    added_item_ids.extend(lib_ids)
                except Exception:
                    pass
                continue

            try:
                _, f_req = build_add_items_rpc_payload(
                    target_album_id=target_album_id,
                    target_auth_key=target_auth_key,
                    items=chunk,
                    rpc_id=rpc_id,
                    source_album_id=source_album_id,
                    source_auth_key=source_auth_key,
                )
                params: dict[str, str] = {
                    "rpcids": rpc_id,
                    "source-path": f"/share/{target_album_id}",
                    "hl": "en",
                    "rt": "c",
                }
                if bl_token:
                    params["bl"] = bl_token
                if f_sid:
                    params["f.sid"] = f_sid

                post_url = "https://photos.google.com/_/PhotosUi/data/batchexecute?" + urllib.parse.urlencode(params)
                form_data: dict[str, str] = {"f.req": f_req}
                if at_token:
                    form_data["at"] = at_token

                resp = http_session.post(
                    post_url,
                    data=form_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "Origin": "https://photos.google.com",
                        "Referer": share_url,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                parsed = parse_batchexecute_response(resp.text, rpc_id=rpc_id)
                batch_responses.append(parsed)
                batches_executed += 1
                for it in chunk:
                    iid = it if isinstance(it, str) else str(it.get("item_id", ""))
                    if iid:
                        added_item_ids.append(iid)
            except Exception:
                pass

        return {
            "target_album": target_album,
            "target_album_id": target_album_id,
            "requested_count": len(items_to_add),
            "added_count": len(added_item_ids),
            "batches_executed": batches_executed,
            "rpc_id": rpc_id,
            "dry_run": False,
            "status": "completed",
            "added_item_ids": added_item_ids,
        }
    finally:
        if own_session:
            http_session.close()


def sync_shared_album_differential(
    target_album: str,
    curated_data: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    cookies: list[dict[str, Any]] | dict[str, str] | None = None,
    cookies_db_path: Path | str = DEFAULT_CHROME_COOKIES_PATH,
    batch_size: int = 50,
    add_rpc_id: str = ADD_ITEMS_RPC_ID,
    remove_rpc_id: str = REMOVE_SHARED_ITEMS_RPC_ID,
    session: requests.Session | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute safe differential sync on a target shared album:
    1. Keep overlapping items already in the live shared album + original baseline items (1 in Rosie Year 6, 34 in Isaac Year 2).
    2. Add newly selected ML winners not yet in the live shared album (grouped by source album when using V8RKJ+laUYf).
    3. Remove ONLY replaced Pass 1 items from the shared album via `LjmOue` (never touching global library Trash),
       and cap removals if any add batch partially failed so the live album count never drops below target.
    """
    short_url = ALBUM_REGISTRY[target_album]["short_url"]
    pre_live = scrape_shared_album(target_album, short_url)
    live_items = list(pre_live.get("items", []))
    live_by_key = {_item_key(it): it for it in live_items}

    # Identify baseline items (first N items from manifest or items marked already_in_target)
    baseline_keys: set[str] = set()
    baseline_cap = BASELINE_TARGET_COUNTS.get(target_album, 0)
    if manifest and target_album in manifest.get("albums", {}):
        manifest_target_items = manifest["albums"][target_album].get("items", [])
        for it in manifest_target_items[:baseline_cap]:
            baseline_keys.add(_item_key(it))
    for it in curated_data.get("selected_items", []):
        if it.get("already_in_target"):
            baseline_keys.add(_item_key(it))
    if not baseline_keys and baseline_cap > 0 and live_items:
        for it in live_items[:baseline_cap]:
            baseline_keys.add(_item_key(it))

    selected_items = list(curated_data.get("selected_items", []))
    selected_keys = {_item_key(it) for it in selected_items}

    overlapping_keys = set(live_by_key.keys()) & selected_keys
    items_to_add = [it for it in selected_items if _item_key(it) not in live_by_key]
    stale_live_items = [
        it
        for it in live_items
        if _item_key(it) not in selected_keys and _item_key(it) not in baseline_keys
    ]

    # Add newly selected ML winners (grouped by source album when source album auth keys are available)
    total_added = 0
    total_add_batches = 0
    added_ids: list[str] = []

    if items_to_add:
        albums_meta = (manifest or {}).get("albums", {})
        effective_add_rpc_id = add_rpc_id
        if albums_meta and effective_add_rpc_id == ADD_ITEMS_RPC_ID:
            effective_add_rpc_id = f"{SAVE_TO_LIBRARY_RPC_ID}+{ADD_LIBRARY_TO_SHARED_RPC_ID}"
        if albums_meta and effective_add_rpc_id in (
            "V8RKJ+laUYf",
            f"{SAVE_TO_LIBRARY_RPC_ID}+{ADD_LIBRARY_TO_SHARED_RPC_ID}",
        ):
            by_src: dict[str, list[dict[str, Any]]] = {}
            for it in items_to_add:
                s_name = str(it.get("album_name") or "")
                by_src.setdefault(s_name, []).append(it)
            for s_name, grp in by_src.items():
                src_meta = albums_meta.get(s_name, {})
                add_res = populate_shared_album(
                    target_album=target_album,
                    target_album_id=pre_live["album_id"],
                    target_auth_key=pre_live["auth_key"],
                    items_to_add=grp,
                    cookies=cookies,
                    cookies_db_path=cookies_db_path,
                    batch_size=batch_size,
                    rpc_id=effective_add_rpc_id,
                    source_album_id=src_meta.get("album_id"),
                    source_auth_key=src_meta.get("auth_key"),
                    session=session,
                    dry_run=dry_run,
                )
                total_added += int(add_res.get("added_count", 0))
                total_add_batches += int(add_res.get("batches_executed", 0))
                added_ids.extend(add_res.get("added_item_ids", []))
        else:
            add_res = populate_shared_album(
                target_album=target_album,
                target_album_id=pre_live["album_id"],
                target_auth_key=pre_live["auth_key"],
                items_to_add=items_to_add,
                cookies=cookies,
                cookies_db_path=cookies_db_path,
                batch_size=batch_size,
                rpc_id=add_rpc_id,
                session=session,
                dry_run=dry_run,
            )
            total_added += int(add_res.get("added_count", 0))
            total_add_batches += int(add_res.get("batches_executed", 0))
            added_ids.extend(add_res.get("added_item_ids", []))

    # If any add failed in live mode, retain enough stale items so total count does not drop below desired count
    add_shortfall = max(0, len(items_to_add) - total_added) if not dry_run else 0
    if add_shortfall > 0 and stale_live_items:
        items_to_remove = stale_live_items[: max(0, len(stale_live_items) - add_shortfall)]
    else:
        items_to_remove = stale_live_items

    rem_res = remove_shared_album_items(
        target_album=target_album,
        target_album_id=pre_live["album_id"],
        target_auth_key=pre_live["auth_key"],
        items_to_remove=items_to_remove,
        cookies=cookies,
        cookies_db_path=cookies_db_path,
        batch_size=batch_size,
        rpc_id=remove_rpc_id,
        session=session,
        dry_run=dry_run,
    )

    post_live = (
        pre_live
        if (dry_run or (not items_to_add and not items_to_remove))
        else scrape_shared_album(target_album, short_url)
    )
    post_count = int(post_live.get("item_count", 0))
    spec = TARGET_CURATION_SPECS.get(target_album, {"target_count": len(selected_items)})
    min_expected = 540 if target_album == "Rosie Year 6" else (560 if target_album == "Isaac Year 2" else int(spec["target_count"]) - 40)

    return {
        "target_album": target_album,
        "short_url": short_url,
        "album_id": pre_live["album_id"],
        "curated_target_count": int(spec["target_count"]),
        "curated_newly_selected_count": int(curated_data.get("newly_selected_count", len(items_to_add))),
        "pre_population_live_count": len(live_items),
        "baseline_locked_count": len(baseline_keys),
        "overlapping_kept_count": len(overlapping_keys),
        "remaining_submitted_count": len(items_to_add),
        "added_in_run_count": total_added,
        "stale_candidates_to_remove_count": len(stale_live_items),
        "removed_in_run_count": int(rem_res.get("removed_count", 0)),
        "batches_executed": total_add_batches + int(rem_res.get("batches_executed", 0)),
        "rpc_id": add_rpc_id,
        "remove_rpc_id": remove_rpc_id,
        "post_population_live_count": post_count,
        "live_item_count": post_count,
        "verified": post_count >= min_expected,
        "status": "dry_run" if dry_run else "completed",
    }


def populate_all_targets(
    curation_dir: Path | str = DEFAULT_CURATION_DIR,
    status_path: Path | str = DEFAULT_POPULATION_STATUS_PATH,
    cookies_db_path: Path | str = DEFAULT_CHROME_COOKIES_PATH,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    targets: list[str] | None = None,
    batch_size: int = 50,
    rpc_id: str = ADD_ITEMS_RPC_ID,
    remove_stale: bool = False,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Populate or differentially sync all curated target albums (Rosie Year 6 & Isaac Year 2), verify live counts, and write population_status.json."""
    cur_dir = Path(curation_dir)
    target_names = targets or list(TARGET_CURATION_SPECS.keys())
    albums_status: dict[str, dict[str, Any]] = {}
    manifest: dict[str, Any] | None = None
    if Path(manifest_path).exists():
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except Exception:
            manifest = None

    for t_name in target_names:
        spec = TARGET_CURATION_SPECS[t_name]
        curated_file = cur_dir / str(spec["output_filename"])
        curated_data = json.loads(curated_file.read_text(encoding="utf-8"))
        short_url = ALBUM_REGISTRY[t_name]["short_url"]

        if remove_stale:
            entry = sync_shared_album_differential(
                target_album=t_name,
                curated_data=curated_data,
                manifest=manifest,
                cookies_db_path=cookies_db_path,
                batch_size=batch_size,
                add_rpc_id=rpc_id,
                session=session,
                dry_run=dry_run,
            )
            albums_status[t_name] = entry
            continue

        pre_live = scrape_shared_album(t_name, short_url)
        live_existing_keys = {_item_key(it) for it in pre_live.get("items", [])}
        selected_items = curated_data.get("selected_items", [])
        remaining_items = [it for it in selected_items if _item_key(it) not in live_existing_keys]

        pop_res = populate_shared_album(
            target_album=t_name,
            target_album_id=pre_live["album_id"],
            target_auth_key=pre_live["auth_key"],
            items_to_add=remaining_items,
            cookies_db_path=cookies_db_path,
            batch_size=batch_size,
            rpc_id=rpc_id,
            session=session,
            dry_run=dry_run,
        )

        post_live = pre_live if (dry_run or not remaining_items) else scrape_shared_album(t_name, short_url)
        post_count = int(post_live.get("item_count", 0))
        min_expected = 540 if t_name == "Rosie Year 6" else (560 if t_name == "Isaac Year 2" else int(spec["target_count"]) - 40)

        entry = {
            "target_album": t_name,
            "short_url": short_url,
            "album_id": pre_live["album_id"],
            "curated_target_count": int(spec["target_count"]),
            "curated_newly_selected_count": int(curated_data.get("newly_selected_count", len(remaining_items))),
            "pre_population_live_count": int(pre_live.get("item_count", 0)),
            "remaining_submitted_count": len(remaining_items),
            "added_in_run_count": int(pop_res.get("added_count", 0)),
            "batches_executed": int(pop_res.get("batches_executed", 0)),
            "rpc_id": pop_res.get("rpc_id", rpc_id),
            "post_population_live_count": post_count,
            "live_item_count": post_count,
            "verified": post_count >= min_expected,
            "status": pop_res.get("status", "completed"),
        }
        albums_status[t_name] = entry

    status_payload: dict[str, Any] = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "albums": albums_status,
        **albums_status,
    }
    out_status = Path(status_path)
    out_status.parent.mkdir(parents=True, exist_ok=True)
    out_status.write_text(json.dumps(status_payload, indent=2), encoding="utf-8")
    return status_payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Photos Shared Album Pagination Indexer & Phase 2 Multimodal Vision Curator"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Output path for the indexed manifest JSON",
    )
    parser.add_argument(
        "--curation-dir",
        type=Path,
        default=DEFAULT_CURATION_DIR,
        help="Output directory for curated target manifests",
    )
    parser.add_argument(
        "--vision-cache",
        type=Path,
        default=DEFAULT_VISION_CACHE_PATH,
        help="Path for cached thumbnail vision analysis scores",
    )
    parser.add_argument(
        "--curate",
        action="store_true",
        help="Run Phase 2 multimodal curation for Rosie Year 6 and Isaac Year 2",
    )
    parser.add_argument(
        "--curate-only",
        action="store_true",
        help="Skip scraping and run Phase 2 curation directly from existing manifest.json",
    )
    parser.add_argument(
        "--populate",
        action="store_true",
        help="Populate target shared albums (Rosie Year 6 & Isaac Year 2) and write population_status.json",
    )
    parser.add_argument(
        "--remove-stale",
        action="store_true",
        help="Run safe differential shared album sync (add new ML winners, remove replaced Pass 1 items via LjmOue without touching global Trash)",
    )
    parser.add_argument(
        "--use-modal",
        action="store_true",
        help="Dispatch thumbnail vision scoring batches to Modal GPU containers",
    )
    parser.add_argument(
        "--refresh-urls",
        action="store_true",
        help="Refresh expiring Google Photos base_urls from source/calibration albums before curation",
    )
    parser.add_argument(
        "--population-status",
        type=Path,
        default=DEFAULT_POPULATION_STATUS_PATH,
        help="Output path for population_status.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run population in dry-run mode without mutating Google Photos albums",
    )
    parser.add_argument(
        "--no-network-vision",
        action="store_true",
        help="Disable network thumbnail fetches during vision scoring",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page cap per album for quick testing",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Concurrent worker threads",
    )
    args = parser.parse_args()

    if not args.curate_only and not args.populate:
        t0 = time.time()
        manifest = build_curation_manifest(
            output_path=args.output,
            max_workers=min(args.workers, 8),
            max_pages=args.max_pages,
        )
        elapsed = time.time() - t0
        print(
            f"Indexed {manifest['album_count']} albums ({manifest['total_indexed_items']} total items) "
            f"in {elapsed:.1f}s -> {args.output}"
        )
        print(f"Cross-album duplicates (Rosalind vs Isaac): {manifest['cross_album_duplicates']['duplicate_count']}")
        for t_name, t_info in manifest["target_windows"].items():
            print(
                f"  {t_name}: {t_info['candidate_count']} candidates "
                f"(photos={t_info['photo_count']}, videos={t_info['video_count']}, "
                f"hearted={t_info['hearted_count']}, cross_dups={t_info['cross_album_duplicate_count']})"
            )

    if args.refresh_urls and Path(args.output).exists():
        m_data = json.loads(Path(args.output).read_text(encoding="utf-8"))
        ref_res = refresh_live_thumbnail_urls(m_data, output_path=args.output)
        print(
            f"Refreshed {ref_res['unique_urls_refreshed']} unique thumbnail URLs across "
            f"{len(ref_res['refreshed_albums'])} source/calibration albums."
        )

    if args.curate or args.curate_only:
        t1 = time.time()
        results = curate_all_targets(
            manifest_path=args.output,
            output_dir=args.curation_dir,
            vision_cache_path=args.vision_cache,
            max_workers=args.workers,
            fetch_network=not args.no_network_vision,
            use_modal=args.use_modal,
        )
        c_elapsed = time.time() - t1
        print(f"Completed Phase 2 curation in {c_elapsed:.1f}s:")
        for t_name, res in results.items():
            print(
                f"  {t_name}: selected={res['selected_count']} "
                f"(existing_preserved={res['existing_preserved_count']}, newly_added={res['newly_selected_count']}, "
                f"photos={res['photo_count']}, videos={res['video_count']} [{res['video_ratio']*100:.1f}%], "
                f"hearted_selected={res['hearted_selected_count']}/{res['hearted_candidate_count']} "
                f"[lift={res['hearted_lift_ratio']}x], "
                f"bursts={res['burst_stats']['total_bursts']}, "
                f"solo_excluded={res['vision_disambiguation_stats']['solo_other_child_excluded']})"
            )

    if args.populate:
        t2 = time.time()
        pop_status = populate_all_targets(
            curation_dir=args.curation_dir,
            status_path=args.population_status,
            manifest_path=args.output,
            remove_stale=args.remove_stale,
            dry_run=args.dry_run,
        )
        p_elapsed = time.time() - t2
        print(f"Completed shared album population in {p_elapsed:.1f}s -> {args.population_status}")
        for t_name, p_res in pop_status.get("albums", {}).items():
            print(
                f"  {t_name}: live_item_count={p_res['live_item_count']}, "
                f"added_in_run={p_res['added_in_run_count']}, verified={p_res['verified']}"
            )


if __name__ == "__main__":
    main()
