#!/usr/bin/env python3
"""Unit tests for tools/google_photos_curator.py."""

from __future__ import annotations

import datetime
import io
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import google_photos_curator as gpc


def _make_raw_item(
    item_id: str,
    timestamp_ms: int,
    dedup_key: str,
    width: int = 3024,
    height: int = 4032,
    contributor_id: str = "actor_1",
    is_video: bool = False,
    reaction_slot: int | None = None,
    album_cluster_id: str = "cluster_1",
) -> list[Any]:
    ext: dict[str, Any] = {
        "15": 12345,
        "101428965": [0, "abc12345"],
        "525000002": [[album_cluster_id]],
    }
    if is_video:
        ext["76647426"] = [15400, None, width, height]

    base_entry: list[Any] = [
        item_id,
        [f"https://lh3.googleusercontent.com/pw/{item_id}", width, height, None, None, None, None, None, [None, None, 1]],
        timestamp_ms,
        dedup_key,
        -28800000,
        timestamp_ms + 1000,
        [contributor_id],
        [[2], [31, 0, 1]],
        2,
    ]
    if reaction_slot is not None:
        base_entry.append(reaction_slot)
    base_entry.append(ext)
    return base_entry


def _make_ds1_html(items: list[Any], next_token: str | None = None, album_id: str = "ALBUM_1", title: str = "Test Album") -> str:
    album_meta: list[Any] = [None] * 43
    album_meta[0] = album_id
    album_meta[1] = title
    album_meta[9] = [
        ["actor_1", "111", "Parent One", None, None, ["actor_1", "111"], None, None, None, None, None, ["Parent One", 1]],
        ["actor_2", "222", None, None, None, ["actor_2", "222"], None, None, None, None, None, ["Parent Two", 1]],
    ]
    album_meta[19] = "TEST_AUTH_KEY"
    album_meta[21] = len(items)
    ds1_data = [None, items, next_token, album_meta, None, 0]
    return (
        "<html><script>"
        f"AF_initDataCallback({{key: 'ds:1', hash: '1', data:{json.dumps(ds1_data)}, sideChannel: {{}}}});"
        "</script></html>"
    )


def _make_batchexecute_payload(items: list[Any], next_token: str | None = None) -> str:
    inner_json = json.dumps([None, items, next_token, None, None, 0])
    outer_line = json.dumps([["wrb.fr", "snAcKc", inner_json, None, None, None, "generic"]])
    return f")}}]'\n\n1234\n{outer_line}\n56\n[[\"di\",100]]\n"


def test_registry_has_all_13_albums() -> None:
    expected_names = {
        "Rosalind '19-'23",
        "Rosalind '23-",
        "Isaac '23-",
        "Rosie Year 1",
        "Rosie Year 2",
        "Rosie Year 3",
        "Rosie Year 4",
        "Rosie Year 5",
        "Rosie Year 6",
        "Rosie Year 7",
        "Isaac Year 1",
        "Isaac Year 2",
        "Isaac Year 3",
    }
    assert set(gpc.ALBUM_REGISTRY.keys()) == expected_names
    assert len(gpc.ALBUM_REGISTRY) == 13


def test_extract_ds1_and_parse_media_item() -> None:
    ts_ms = int(datetime.datetime(2025, 5, 1, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    raw_photo = _make_raw_item("AF1QipPhoto1", ts_ms, "dedup_photo_1", reaction_slot=1)
    raw_video = _make_raw_item("AF1QipVideo1", ts_ms + 5000, "dedup_video_1", is_video=True, contributor_id="actor_2")
    html = _make_ds1_html([raw_photo, raw_video], next_token="TOKEN_PAGE_2")

    ds1 = gpc.extract_ds1_payload(html)
    contributors = gpc.extract_contributors_map(ds1[3])
    assert contributors == {"actor_1": "Parent One", "actor_2": "Parent Two"}

    parsed_photo = gpc.parse_media_item(raw_photo, album_name="Rosalind '23-", contributors_map=contributors)
    assert parsed_photo["item_id"] == "AF1QipPhoto1"
    assert parsed_photo["dedup_key"] == "dedup_photo_1"
    assert parsed_photo["base_url"] == "https://lh3.googleusercontent.com/pw/AF1QipPhoto1"
    assert parsed_photo["width"] == 3024
    assert parsed_photo["height"] == 4032
    assert parsed_photo["is_video"] is False
    assert parsed_photo["media_type"] == "photo"
    assert parsed_photo["contributor_id"] == "actor_1"
    assert parsed_photo["contributor_name"] == "Parent One"
    assert parsed_photo["has_heart"] is True
    assert parsed_photo["heart_count"] == 1
    assert parsed_photo["reaction_slot"] == 1

    parsed_video = gpc.parse_media_item(raw_video, album_name="Rosalind '23-", contributors_map=contributors)
    assert parsed_video["is_video"] is True
    assert parsed_video["media_type"] == "video"
    assert parsed_video["video_duration_ms"] == 15400
    assert parsed_video["contributor_name"] == "Parent Two"
    assert parsed_video["has_heart"] is False
    assert parsed_video["heart_count"] == 0


def test_parse_batchexecute_and_scrape_pagination() -> None:
    ts1 = int(datetime.datetime(2025, 1, 15, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    ts2 = int(datetime.datetime(2025, 2, 15, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    item_p1 = _make_raw_item("AF1QipP1", ts1, "key_p1")
    item_p2 = _make_raw_item("AF1QipP2", ts2, "key_p2", reaction_slot=0)

    html = _make_ds1_html([item_p1], next_token="NEXT_TOK_1", album_id="ALBUM_XYZ", title="Rosalind '23-")
    batch_raw = _make_batchexecute_payload([item_p2], next_token=None)

    def fake_urlopen(req: Any, timeout: int = 20) -> Any:
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if "batchexecute" in req.full_url:
            resp.read.return_value = batch_raw.encode("utf-8")
            resp.geturl.return_value = req.full_url
        else:
            resp.read.return_value = html.encode("utf-8")
            resp.geturl.return_value = "https://photos.google.com/share/ALBUM_XYZ?key=TEST_AUTH_KEY"
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = gpc.scrape_shared_album("Rosalind '23-", "https://photos.app.goo.gl/fake")

    assert result["album_id"] == "ALBUM_XYZ"
    assert result["auth_key"] == "TEST_AUTH_KEY"
    assert result["pages_fetched"] == 2
    assert result["item_count"] == 2
    assert [it["item_id"] for it in result["items"]] == ["AF1QipP1", "AF1QipP2"]
    assert result["items"][1]["has_heart"] is True


def test_cross_album_duplicates_and_birthday_window_partitioning(tmp_path: Path) -> None:
    # Timestamps inside each target birthday window:
    # Rosie Year 6: Nov 2024 - Nov 2025 (e.g., 2025-03-10)
    # Rosie Year 7: Nov 2025 - Nov 2026 (e.g., 2026-04-10)
    # Isaac Year 2: Dec 2024 - Dec 2025 (e.g., 2025-03-10 -> shared duplicate + 2025-06-01 -> Isaac only)
    # Isaac Year 3: Dec 2025 - Dec 2026 (e.g., 2026-05-01)
    ts_ry1 = int(datetime.datetime(2020, 5, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    ts_shared_2025 = int(datetime.datetime(2025, 3, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    ts_rosie_y7 = int(datetime.datetime(2026, 4, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    ts_isaac_y2 = int(datetime.datetime(2025, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    ts_isaac_y3 = int(datetime.datetime(2026, 5, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)

    rosie_19_23_items = [
        gpc.parse_media_item(_make_raw_item("R_19_1", ts_ry1, "dedup_ry1", reaction_slot=1), "Rosalind '19-'23"),
    ]
    rosie_23_items = [
        gpc.parse_media_item(_make_raw_item("R_23_1", ts_shared_2025, "dedup_shared_2025", reaction_slot=1), "Rosalind '23-"),
        gpc.parse_media_item(_make_raw_item("R_23_2", ts_rosie_y7, "dedup_rosie_y7", is_video=True), "Rosalind '23-"),
    ]
    isaac_23_items = [
        # Same dedup_key "dedup_shared_2025" but different album item_id (simulating Google Photos auto-add to both albums)
        gpc.parse_media_item(_make_raw_item("I_23_1", ts_shared_2025, "dedup_shared_2025"), "Isaac '23-"),
        gpc.parse_media_item(_make_raw_item("I_23_2", ts_isaac_y2, "dedup_isaac_y2"), "Isaac '23-"),
        gpc.parse_media_item(_make_raw_item("I_23_3", ts_isaac_y3, "dedup_isaac_y3", reaction_slot=2), "Isaac '23-"),
    ]
    rosie_y1_curated = [
        gpc.parse_media_item(_make_raw_item("RY1_C1", ts_ry1, "dedup_ry1"), "Rosie Year 1"),
    ]

    dup_summary = gpc.detect_cross_album_duplicates(rosie_19_23_items + rosie_23_items, isaac_23_items)
    assert dup_summary["duplicate_count"] == 1
    assert dup_summary["duplicate_dedup_keys"] == ["dedup_shared_2025"]
    assert rosie_23_items[0]["is_cross_album_duplicate"] is True
    assert isaac_23_items[0]["is_cross_album_duplicate"] is True
    assert isaac_23_items[1]["is_cross_album_duplicate"] is False

    albums_data = {
        "Rosalind '19-'23": {"items": rosie_19_23_items},
        "Rosalind '23-": {"items": rosie_23_items},
        "Isaac '23-": {"items": isaac_23_items},
        "Rosie Year 1": {"items": rosie_y1_curated},
        "Rosie Year 6": {"items": []},
        "Rosie Year 7": {"items": []},
        "Isaac Year 2": {"items": []},
        "Isaac Year 3": {"items": []},
    }

    partitions = gpc.partition_birthday_windows(albums_data)
    assert partitions["Rosie Year 6"]["candidate_count"] == 1
    assert partitions["Rosie Year 6"]["candidates"][0]["dedup_key"] == "dedup_shared_2025"
    assert partitions["Rosie Year 6"]["cross_album_duplicate_count"] == 1

    assert partitions["Rosie Year 7"]["candidate_count"] == 1
    assert partitions["Rosie Year 7"]["video_count"] == 1
    assert partitions["Rosie Year 7"]["candidates"][0]["dedup_key"] == "dedup_rosie_y7"

    assert partitions["Isaac Year 2"]["candidate_count"] == 2
    assert partitions["Isaac Year 2"]["cross_album_duplicate_count"] == 1

    assert partitions["Isaac Year 3"]["candidate_count"] == 1
    assert partitions["Isaac Year 3"]["hearted_count"] == 1
    assert partitions["Isaac Year 3"]["candidates"][0]["dedup_key"] == "dedup_isaac_y3"

    cal_summary = gpc.compute_calibration_summary(albums_data)
    assert cal_summary["calibrated_albums_count"] == 1
    assert cal_summary["albums"]["Rosie Year 1"]["curated_count"] == 1
    assert cal_summary["albums"]["Rosie Year 1"]["source_pool_count"] == 1
    assert cal_summary["albums"]["Rosie Year 1"]["selection_rate"] == 1.0
    assert cal_summary["albums"]["Rosie Year 1"]["heart_selection_rate"] == 1.0


def test_build_curation_manifest_writes_output(tmp_path: Path) -> None:
    out_path = tmp_path / "manifest.json"
    mini_registry = {
        "Rosalind '23-": {
            "short_url": "https://photos.app.goo.gl/rosie23",
            "role": "source",
            "child": "rosalind",
        },
        "Isaac '23-": {
            "short_url": "https://photos.app.goo.gl/isaac23",
            "role": "source",
            "child": "isaac",
        },
    }
    ts_y6 = int(datetime.datetime(2025, 2, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)

    def fake_scrape(album_name: str, short_url: str, max_pages: int | None = None, timeout: int = 20) -> dict[str, Any]:
        item = gpc.parse_media_item(_make_raw_item(f"ID_{album_name}", ts_y6, "shared_key_1"), album_name)
        return {
            "album_name": album_name,
            "album_title": album_name,
            "short_url": short_url,
            "resolved_url": short_url,
            "album_id": f"ALB_{album_name}",
            "auth_key": "KEY",
            "expected_total": 1,
            "pages_fetched": 1,
            "item_count": 1,
            "min_timestamp_ms": ts_y6,
            "max_timestamp_ms": ts_y6,
            "contributors": {},
            "items": [item],
        }

    with patch("tools.google_photos_curator.scrape_shared_album", side_effect=fake_scrape):
        manifest = gpc.build_curation_manifest(album_registry=mini_registry, output_path=out_path, max_workers=2)

    assert out_path.exists()
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["album_count"] == 2
    assert loaded["cross_album_duplicates"]["duplicate_count"] == 1
    assert loaded["target_windows"]["Rosie Year 6"]["candidate_count"] == 1
    assert loaded["target_windows"]["Isaac Year 2"]["candidate_count"] == 1
    assert manifest["total_indexed_items"] == 2


def _make_synthetic_thumbnail_bytes(
    num_faces: int = 1,
    sharp: bool = True,
    toddler_proportions: bool = False,
) -> bytes:
    """Create a synthetic RGB JPEG thumbnail with 1 or 2 skin-tone regions and controllable sharpness."""
    img = Image.new("RGB", (256, 256), color=(65, 115, 165))
    draw = ImageDraw.Draw(img)

    # Skin-tone RGB (warm peach/tan within YCbCr skin bounds: R=215, G=162, B=132)
    skin_rgb = (215, 162, 132)
    detail_rgb = (60, 35, 30)

    if num_faces >= 2:
        # Two distinct sibling faces separated horizontally
        draw.ellipse((35, 65, 105, 160), fill=skin_rgb)
        draw.ellipse((150, 75, 220, 165), fill=skin_rgb)
        if sharp:
            for y in range(85, 145, 6):
                draw.line((50, y, 90, y), fill=detail_rgb, width=2)
                draw.line((165, y, 205, y), fill=detail_rgb, width=2)
    elif num_faces == 1:
        if toddler_proportions:
            # Rounder baby/toddler face lower in the frame
            draw.ellipse((88, 125, 168, 200), fill=skin_rgb)
            if sharp:
                draw.line((108, 155, 148, 155), fill=detail_rgb, width=1)
        else:
            # Taller older-child face/upper portrait higher in the frame with high-frequency detail
            draw.rectangle((95, 35, 160, 155), fill=skin_rgb)
            if sharp:
                for y in range(45, 145, 4):
                    draw.line((102, y, 152, y), fill=detail_rgb, width=2)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92 if sharp else 30)
    return buf.getvalue()


def test_group_temporal_bursts_90s_window() -> None:
    t0 = 1_740_000_000_000
    items = [
        {"item_id": "id_1", "dedup_key": "k1", "timestamp_ms": t0},
        {"item_id": "id_2", "dedup_key": "k2", "timestamp_ms": t0 + 30_000},
        {"item_id": "id_3", "dedup_key": "k3", "timestamp_ms": t0 + 115_000},  # 85s after id_2 -> same burst
        {"item_id": "id_4", "dedup_key": "k4", "timestamp_ms": t0 + 210_000},  # 95s after id_3 -> new burst
    ]
    bursts = gpc.group_temporal_bursts(items, window_ms=90_000)
    assert len(bursts) == 2
    assert len(bursts[0]) == 3
    assert len(bursts[1]) == 1
    assert bursts[0][0]["burst_id"] == "burst_0000"
    assert bursts[0][0]["burst_size"] == 3
    assert bursts[1][0]["burst_id"] == "burst_0001"


def test_analyze_thumbnail_and_child_age_disambiguation() -> None:
    sibling_bytes = _make_synthetic_thumbnail_bytes(num_faces=2, sharp=True)
    older_sharp_bytes = _make_synthetic_thumbnail_bytes(num_faces=1, sharp=True, toddler_proportions=False)
    toddler_blur_bytes = _make_synthetic_thumbnail_bytes(num_faces=1, sharp=False, toddler_proportions=True)

    sibling_scores = gpc.analyze_thumbnail_bytes(sibling_bytes)
    older_scores = gpc.analyze_thumbnail_bytes(older_sharp_bytes)
    toddler_scores = gpc.analyze_thumbnail_bytes(toddler_blur_bytes)

    assert sibling_scores["estimated_faces"] >= 2
    assert sibling_scores["is_shared_sibling_shot"] is True
    assert sibling_scores["subject_age_class"] == "shared_siblings_or_group"
    assert sibling_scores["group_composition_score"] > older_scores["group_composition_score"]

    assert older_scores["sharpness_score"] > toddler_scores["sharpness_score"]
    assert older_scores["subject_age_class"] == "solo_older_child"
    assert toddler_scores["subject_age_class"] == "solo_toddler"

    cross_dup_item = {"item_id": "dup1", "dedup_key": "dup1", "is_cross_album_duplicate": True, "has_heart": False}
    # Shared sibling shot is kept in BOTH Rosie and Isaac albums
    assert gpc.should_exclude_for_child(cross_dup_item, sibling_scores, child="rosalind") is False
    assert gpc.should_exclude_for_child(cross_dup_item, sibling_scores, child="isaac") is False

    # Solo toddler shot is excluded from Rosie's album but kept in Isaac's album
    assert gpc.should_exclude_for_child(cross_dup_item, toddler_scores, child="rosalind") is True
    assert gpc.should_exclude_for_child(cross_dup_item, toddler_scores, child="isaac") is False

    # Solo older-child shot is excluded from Isaac's album but kept in Rosie's album
    assert gpc.should_exclude_for_child(cross_dup_item, older_scores, child="isaac") is True
    assert gpc.should_exclude_for_child(cross_dup_item, older_scores, child="rosalind") is False


def test_curate_target_album_preserves_existing_boosts_hearts_and_calibrates_video_ratio(tmp_path: Path) -> None:
    base_dt = datetime.datetime(2025, 1, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    candidates: list[dict[str, Any]] = []
    existing_items: list[dict[str, Any]] = []

    # Generate 120 synthetic candidates across 6 months (photos + videos, bursts, hearted items, 5 existing)
    for m_idx in range(6):
        month_dt = base_dt + datetime.timedelta(days=30 * m_idx)
        month_ms = int(month_dt.timestamp() * 1000)
        for i in range(20):
            # Pair items 0&1, 2&3 into 30-second bursts, space bursts by 5 minutes
            burst_group = i // 2
            within_burst = i % 2
            ts = month_ms + burst_group * 300_000 + within_burst * 25_000
            is_vid = within_burst == 1 and (burst_group % 2 == 0)
            has_heart = burst_group in (0, 1) and within_burst == 0
            key = f"cand_{m_idx}_{i}"
            item = {
                "item_id": f"ID_{key}",
                "dedup_key": key,
                "base_url": f"https://lh3.googleusercontent.com/pw/{key}",
                "timestamp_ms": ts,
                "timestamp_iso": datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "width": 4032 if within_burst == 0 else 1920,
                "height": 3024 if within_burst == 0 else 1080,
                "is_video": is_vid,
                "media_type": "video" if is_vid else "photo",
                "video_duration_ms": 18_000 if is_vid else None,
                "contributor_id": "actor_1",
                "contributor_name": "Parent One",
                "has_heart": has_heart,
                "heart_count": 2 if has_heart else 0,
                "is_cross_album_duplicate": (i == 19),
            }
            candidates.append(item)
            if m_idx == 0 and i < 5:
                existing_items.append(dict(item))

    manifest = {
        "calibration": {
            "albums": {
                "Isaac Year 1": {
                    "child": "isaac",
                    "monthly_distribution": {f"2024-{m:02d}": 50 for m in range(1, 7)},
                }
            }
        },
        "target_windows": {
            "Isaac Year 2": {
                "child": "isaac",
                "start_iso": "2024-12-26T00:00:00Z",
                "end_iso": "2025-12-26T00:00:00Z",
                "start_ms": int(base_dt.timestamp() * 1000),
                "end_ms": int((base_dt + datetime.timedelta(days=365)).timestamp() * 1000),
                "candidates": candidates,
            }
        },
        "albums": {
            "Isaac Year 2": {
                "items": existing_items,
            }
        },
    }

    cache_file = tmp_path / "vision_cache.json"
    out_dir = tmp_path / "curation_out"

    curated = gpc.curate_target_album(
        manifest=manifest,
        target_album="Isaac Year 2",
        target_count=40,
        target_video_ratio=0.29,
        vision_cache_path=cache_file,
        output_dir=out_dir,
        fetch_network=False,
    )

    assert curated["selected_count"] == 40
    assert curated["existing_preserved_count"] == 5
    assert curated["newly_selected_count"] == 35
    # Video ratio should match ~28-30% target (12 / 40 = 0.30)
    assert 0.25 <= curated["video_ratio"] <= 0.33
    # Family-hearted items should have high selection rate
    assert curated["hearted_selection_rate"] >= 0.8
    assert curated["hearted_lift_ratio"] >= 2.0
    assert (out_dir / "isaac_year_2_curated.json").exists()


def test_extract_wiz_tokens_and_populate_shared_album() -> None:
    sample_html = (
        '<html><script>window.WIZ_global_data = '
        '{"SNlM0e":"AT_TOKEN_123:456","FdrFJe":"-987654321","cfb2h":"boq_photosuiserver_20261108"};'
        '</script></html>'
    )
    wiz = gpc.extract_wiz_tokens(sample_html)
    assert wiz["SNlM0e"] == "AT_TOKEN_123:456"
    assert wiz["FdrFJe"] == "-987654321"
    assert wiz["cfb2h"] == "boq_photosuiserver_20261108"

    mock_session = MagicMock()
    get_resp = MagicMock()
    get_resp.text = sample_html
    get_resp.raise_for_status.return_value = None
    mock_session.get.return_value = get_resp

    rpc_id = gpc.ADD_ITEMS_RPC_ID
    inner_json = json.dumps([["AF1QipAdded1", "AF1QipAdded2"]])
    outer_line = json.dumps([["wrb.fr", rpc_id, inner_json, None, None, None, "generic"]])
    post_resp = MagicMock()
    post_resp.text = f")}}]'\n\n120\n{outer_line}\n"
    post_resp.raise_for_status.return_value = None
    mock_session.post.return_value = post_resp

    items_to_add = [
        {"item_id": "AF1QipItem1", "dedup_key": "k1"},
        {"item_id": "AF1QipItem2", "dedup_key": "k2"},
        {"item_id": "AF1QipItem3", "dedup_key": "k3"},
    ]
    res = gpc.populate_shared_album(
        target_album="Rosie Year 6",
        target_album_id="ALB_TARGET_1",
        target_auth_key="AUTH_KEY_1",
        items_to_add=items_to_add,
        cookies={"SID": "fake_sid"},
        batch_size=2,
        session=mock_session,
    )
    assert res["status"] == "completed"
    assert res["requested_count"] == 3
    assert res["added_count"] == 3
    assert res["batches_executed"] == 2
    assert res["added_item_ids"] == ["AF1QipItem1", "AF1QipItem2", "AF1QipItem3"]
    assert mock_session.post.call_count == 2


