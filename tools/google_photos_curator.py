#!/usr/bin/env python3
"""Google Photos Shared Album Scraper, Heart/Date Metadata Parser & Calibration Dataset Summary."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import json
from pathlib import Path
import re
import time
from typing import Any
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BATCHEXECUTE_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=snAcKc"
DEFAULT_MANIFEST_PATH = Path("/workspace/data/photos_curation/manifest.json")

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Photos Shared Album Pagination Indexer & Calibration Metadata Extractor"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Output path for the indexed manifest JSON",
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
        default=6,
        help="Concurrent album scraper threads",
    )
    args = parser.parse_args()

    t0 = time.time()
    manifest = build_curation_manifest(
        output_path=args.output,
        max_workers=args.workers,
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


if __name__ == "__main__":
    main()
