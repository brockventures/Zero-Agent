#!/usr/bin/env python3
"""Unit tests for tools/doorbell_face_picker.py.

Uses RFC 5737 documentation addresses (198.51.100.x) exclusively and mocks all
network, WebSocket, ONNX session, ffmpeg, and SSH subprocess calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools import doorbell_face_picker as dfp

MOCK_HA_URL = "http://198.51.100.10:8123"
MOCK_HA_HOST = "198.51.100.10"
MOCK_HA_TOKEN = "test-ha-token-rfc5737"
MOCK_DEVICE_ID = "dev_nest_doorbell_01"
MOCK_EVENT_ID = "nest_evt_987654321"


class _FakeAsyncContextManager:
    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _make_fake_response(
    status: int = 200,
    body_bytes: bytes = b"",
    body_text: str = "",
) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body_bytes)
    resp.text = AsyncMock(
        return_value=body_text or body_bytes.decode("utf-8", errors="ignore")
    )
    return resp


def test_compute_laplacian_sharpness_flat_vs_checkerboard() -> None:
    """A flat uniform image should have 0 sharpness; a high-contrast checkerboard should be high."""
    flat = np.full((64, 64), 128.0, dtype=np.float32)
    checker = np.zeros((64, 64), dtype=np.float32)
    checker[::2, ::2] = 255.0
    checker[1::2, 1::2] = 255.0

    assert dfp.compute_laplacian_sharpness(flat) == pytest.approx(0.0)
    sharp_val = dfp.compute_laplacian_sharpness(checker)
    assert sharp_val > 10000.0

    # Also supports 3D RGB arrays and tiny degenerate arrays
    rgb_checker = np.stack([checker, checker, checker], axis=2)
    assert dfp.compute_laplacian_sharpness(rgb_checker) == pytest.approx(sharp_val)
    assert dfp.compute_laplacian_sharpness(np.zeros((2, 2), dtype=np.float32)) == 0.0


def test_score_face_frame_thresholds_and_edge_penalty() -> None:
    """Verify confidence (<0.65), box area (<0.005), and edge-clipping (0.65x) rules."""
    centered_box = [0.25, 0.20, 0.55, 0.60]  # area = 0.30 * 0.40 = 0.12

    # Below confidence threshold -> 0.0
    assert dfp.score_face_frame(0.64, centered_box, 200.0) == 0.0

    # Below area threshold (< 0.005) -> 0.0
    tiny_box = [0.40, 0.40, 0.45, 0.45]  # area = 0.0025
    assert dfp.score_face_frame(0.95, tiny_box, 200.0) == 0.0

    # Centered valid face vs edge-clipped face
    unclipped_score = dfp.score_face_frame(0.90, centered_box, 250.0)
    assert unclipped_score > 0.0

    left_clipped_box = [0.01, 0.20, 0.31, 0.60]  # same area (0.12), x1 < 0.02
    clipped_score = dfp.score_face_frame(0.90, left_clipped_box, 250.0)
    assert clipped_score == pytest.approx(unclipped_score * 0.65)

    right_clipped_box = [0.69, 0.20, 0.99, 0.60]  # same area (0.12), x2 > 0.98
    assert dfp.score_face_frame(0.90, right_clipped_box, 250.0) == pytest.approx(
        unclipped_score * 0.65
    )

    # Higher sharpness and larger face area increase score
    sharper_score = dfp.score_face_frame(0.90, centered_box, 800.0)
    assert sharper_score > unclipped_score


def test_select_best_frame_picks_highest_face_score() -> None:
    """When one or more frames contain valid faces, select_best_frame picks the highest score."""
    frames = [f"/tmp/fake_frame_{i:02d}.jpg" for i in range(5)]
    mock_detector = MagicMock()
    # (conf, box, face_sharpness, full_sharpness)
    responses = {
        frames[0]: (0.20, [0.0, 0.0, 0.0, 0.0], 0.0, 500.0),
        frames[1]: (0.85, [0.01, 0.20, 0.31, 0.60], 200.0, 300.0),  # edge clipped
        frames[2]: (0.88, [0.25, 0.20, 0.55, 0.60], 450.0, 320.0),  # centered & sharp
        frames[3]: (0.86, [0.25, 0.20, 0.55, 0.60], 120.0, 310.0),  # centered but blurry
        frames[4]: (0.40, [0.10, 0.10, 0.40, 0.40], 50.0, 600.0),
    }
    mock_detector.detect.side_effect = lambda p: responses[str(p)]

    best_path, meta = dfp.select_best_frame(frames, mock_detector)
    assert best_path == frames[2]
    assert meta["mode"] == "face"
    assert meta["face_detected"] is True
    assert meta["frame_index"] == 2
    assert meta["score"] > 0.0


def test_select_best_frame_fallback_to_middle_60_percent() -> None:
    """When no faces are detected, select_best_frame falls back to sharpest frame in middle 60%."""
    frames = [f"/tmp/no_face_{i:02d}.jpg" for i in range(10)]
    # Make outer frames (indices 0, 1, 8, 9) artificially sharpest to prove middle 60% (2..7) is used
    full_sharpnesses = [
        9999.0,  # idx 0 (excluded)
        8888.0,  # idx 1 (excluded)
        100.0,   # idx 2
        250.0,   # idx 3
        640.0,   # idx 4 (sharpest inside middle 60%)
        410.0,   # idx 5
        300.0,   # idx 6
        190.0,   # idx 7
        7777.0,  # idx 8 (excluded)
        9500.0,  # idx 9 (excluded)
    ]
    mock_detector = MagicMock()
    mock_detector.detect.side_effect = [
        (0.15, [0.0, 0.0, 0.0, 0.0], 0.0, s) for s in full_sharpnesses
    ]

    best_path, meta = dfp.select_best_frame(frames, mock_detector)
    assert best_path == frames[4]
    assert meta["mode"] == "sharpness_fallback"
    assert meta["face_detected"] is False
    assert meta["frame_index"] == 4
    assert meta["full_sharpness"] == pytest.approx(640.0)


def test_select_best_frame_fallback_under_three_frames() -> None:
    """With fewer than 3 frames and no faces, all frames are considered."""
    frames = ["/tmp/short_00.jpg", "/tmp/short_01.jpg"]
    mock_detector = MagicMock()
    mock_detector.detect.side_effect = [
        (0.10, [0.0, 0.0, 0.0, 0.0], 0.0, 820.0),
        (0.12, [0.0, 0.0, 0.0, 0.0], 0.0, 410.0),
    ]
    best_path, meta = dfp.select_best_frame(frames, mock_detector)
    assert best_path == frames[0]
    assert meta["mode"] == "sharpness_fallback"


def test_ultraface_detector_preprocessing_and_inference() -> None:
    """Verify UltraFaceDetector normalizes to (1, 3, 240, 320) float32 and scores face crop."""
    mock_session = MagicMock()
    mock_input = MagicMock()
    mock_input.name = "input"
    mock_session.get_inputs.return_value = [mock_input]

    # 3 anchors: anchor 1 has high face confidence (0.92) at centered box
    confs = np.array(
        [[[0.95, 0.05], [0.08, 0.92], [0.70, 0.30]]], dtype=np.float32
    )
    boxes = np.array(
        [
            [
                [0.0, 0.0, 0.1, 0.1],
                [0.25, 0.25, 0.75, 0.75],
                [0.1, 0.1, 0.5, 0.5],
            ]
        ],
        dtype=np.float32,
    )

    captured_tensors: list[np.ndarray] = []

    def _run_side_effect(_output_names: Any, feed_dict: dict[str, np.ndarray]) -> Any:
        captured_tensors.append(feed_dict["input"])
        return [confs, boxes]

    mock_session.run.side_effect = _run_side_effect

    detector = dfp.UltraFaceDetector(session=mock_session)

    # Create a patterned 640x480 RGB image so Laplacian sharpness > 0
    arr = np.zeros((480, 640, 3), dtype=np.uint8)
    arr[::2, ::2, :] = 255
    pil_img = Image.fromarray(arr, mode="RGB")

    conf, box, face_sharp, full_sharp = detector.detect(pil_img)
    assert len(captured_tensors) == 1
    tensor = captured_tensors[0]
    assert tensor.shape == (1, 3, 240, 320)
    assert tensor.dtype == np.float32
    assert float(tensor.min()) >= -1.0
    assert float(tensor.max()) <= 1.0

    assert conf == pytest.approx(0.92, abs=1e-5)
    assert box == pytest.approx([0.25, 0.25, 0.75, 0.75])
    assert face_sharp > 0.0
    assert full_sharp > 0.0


def test_fetch_nest_clip_mp4_polls_until_ready() -> None:
    """Verify fetch_nest_clip_mp4 waits 1.0s then polls every 0.4s until HTTP 200 with MP4 bytes."""
    mock_session = MagicMock()
    resp_404 = _make_fake_response(status=404, body_bytes=b"")
    resp_empty = _make_fake_response(status=200, body_bytes=b"")
    resp_ok = _make_fake_response(status=200, body_bytes=b"\x00\x00\x00\x18ftypmp42")

    mock_session.get.side_effect = [
        _FakeAsyncContextManager(resp_404),
        _FakeAsyncContextManager(resp_empty),
        _FakeAsyncContextManager(resp_ok),
    ]

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("tools.doorbell_face_picker.asyncio.sleep", side_effect=_fake_sleep):
        mp4 = asyncio.run(
            dfp.fetch_nest_clip_mp4(
                session=mock_session,
                ha_url=MOCK_HA_URL,
                ha_token=MOCK_HA_TOKEN,
                device_id=MOCK_DEVICE_ID,
                event_id=MOCK_EVENT_ID,
            )
        )

    assert mp4 == b"\x00\x00\x00\x18ftypmp42"
    assert sleep_calls == [pytest.approx(1.0), pytest.approx(0.4), pytest.approx(0.4)]


def test_upload_best_frame_to_ha_pipes_bytes_over_ssh() -> None:
    """Verify upload_best_frame_to_ha invokes ssh with RFC 5737 mock host and stdin bytes."""
    sample_jpeg = b"\xff\xd8\xff\xe0MOCK_JPEG_BYTES\xff\xd9"
    mock_completed = MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch.object(dfp.nas_config, "HOST_1_IP", MOCK_HA_HOST),
        patch.object(dfp.nas_config, "SSH_PORT", "2222"),
        patch.object(dfp.nas_config, "SSH_USER", "testuser"),
        patch("tools.doorbell_face_picker.subprocess.run", return_value=mock_completed) as mock_run,
    ):
        ok = dfp.upload_best_frame_to_ha(sample_jpeg)

    assert ok is True
    mock_run.assert_called_once()
    cmd_arg = mock_run.call_args[0][0]
    assert cmd_arg == [
        "ssh",
        "-p",
        "2222",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        f"testuser@{MOCK_HA_HOST}",
        "docker exec -i home-assistant sh -c 'mkdir -p /config/www && cat > /config/www/doorbell_best.jpg'",
    ]
    assert mock_run.call_args[1]["input"] == sample_jpeg


def test_process_doorbell_event_success_and_fallback(tmp_path: Path) -> None:
    """Verify process_doorbell_event sends /local/doorbell_best.jpg on success and /thumbnail on error."""
    mock_session = MagicMock()
    notify_resp = _make_fake_response(status=200, body_text="[]")
    mock_session.post.return_value = _FakeAsyncContextManager(notify_resp)

    mock_detector = MagicMock()
    mock_detector.detect.return_value = (0.91, [0.25, 0.25, 0.65, 0.65], 400.0, 350.0)

    def _fake_extract(_mp4_bytes: bytes, output_dir: str) -> list[str]:
        f1 = Path(output_dir) / "frame_01.jpg"
        Image.new("RGB", (64, 64), color=(120, 140, 160)).save(f1, format="JPEG")
        return [str(f1)]

    with (
        patch(
            "tools.doorbell_face_picker.fetch_nest_clip_mp4",
            new=AsyncMock(return_value=b"MP4DATA"),
        ),
        patch(
            "tools.doorbell_face_picker.extract_frames_from_mp4",
            side_effect=_fake_extract,
        ),
        patch(
            "tools.doorbell_face_picker.upload_best_frame_to_ha",
            return_value=True,
        ),
    ):
        res_ok = asyncio.run(
            dfp.process_doorbell_event(
                session=mock_session,
                ha_url=MOCK_HA_URL,
                ha_token=MOCK_HA_TOKEN,
                device_id=MOCK_DEVICE_ID,
                event_id=MOCK_EVENT_ID,
                detector=mock_detector,
                epoch_fn=lambda: 1700000000,
            )
        )

    assert res_ok["fallback"] is False
    assert res_ok["image_url"] == "/local/doorbell_best.jpg?t=1700000000"
    assert res_ok["notified"] == list(dfp.DEFAULT_NOTIFY_TARGETS)

    # Verify notification payload content
    sent_json = mock_session.post.call_args[1]["json"]
    assert sent_json["data"]["tag"] == "doorbell-ring"
    assert sent_json["data"]["alert_once"] is True
    assert sent_json["data"]["ttl"] == 0
    assert sent_json["data"]["priority"] == "high"
    assert sent_json["data"]["channel"] == "Doorbell"
    assert sent_json["data"]["clickAction"] == "/lovelace/entryway"
    assert sent_json["data"]["image"] == "/local/doorbell_best.jpg?t=1700000000"

    # Now simulate clip fetch timeout -> fallback to Nest thumbnail URL
    with patch(
        "tools.doorbell_face_picker.fetch_nest_clip_mp4",
        new=AsyncMock(side_effect=TimeoutError("clip not ready")),
    ):
        res_fb = asyncio.run(
            dfp.process_doorbell_event(
                session=mock_session,
                ha_url=MOCK_HA_URL,
                ha_token=MOCK_HA_TOKEN,
                device_id=MOCK_DEVICE_ID,
                event_id=MOCK_EVENT_ID,
                detector=mock_detector,
            )
        )

    assert res_fb["fallback"] is True
    assert (
        res_fb["image_url"]
        == f"/api/nest/event_media/{MOCK_DEVICE_ID}/{MOCK_EVENT_ID}/thumbnail"
    )


def test_extract_doorbell_event_id_filtering() -> None:
    """Verify state_changed filtering ignores unavailable/unknown old_state and duplicate event_ids."""
    seen: set[str] = set()

    # 1. Ignore when old_state is 'unavailable'
    msg_unavail = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": "event.doorbell_chime",
                "old_state": {"state": "unavailable", "attributes": {}},
                "new_state": {
                    "state": "2026-11-25T19:00:00+00:00",
                    "attributes": {"nest_event_id": "evt_1"},
                },
            },
        },
    }
    assert dfp.extract_doorbell_event_id(msg_unavail, seen) is None

    # 2. Ignore other entities
    msg_other = json.loads(json.dumps(msg_unavail))
    msg_other["event"]["data"]["entity_id"] = "event.doorbell_motion"
    msg_other["event"]["data"]["old_state"]["state"] = "2026-11-25T18:00:00+00:00"
    assert dfp.extract_doorbell_event_id(msg_other, seen) is None

    # 3. Accept valid doorbell chime state transition
    msg_valid = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": "event.doorbell_chime",
                "old_state": {
                    "state": "2026-11-25T18:00:00+00:00",
                    "attributes": {"nest_event_id": "evt_0"},
                },
                "new_state": {
                    "state": "2026-11-25T19:00:00+00:00",
                    "attributes": {"nest_event_id": "evt_1"},
                },
            },
        },
    }
    assert dfp.extract_doorbell_event_id(msg_valid, seen) == "evt_1"

    # 4. Reject duplicate nest_event_id
    assert dfp.extract_doorbell_event_id(msg_valid, seen) is None


def test_cli_status_and_test_clip_subcommands() -> None:
    """Verify CLI subcommands status and test-clip --event-id --target."""
    with patch("tools.doorbell_face_picker.get_listener_pid", return_value=4242):
        assert dfp.main(["status"]) == 0
        assert dfp.main(["json"]) == 0

    fake_result = {
        "event_id": MOCK_EVENT_ID,
        "device_id": MOCK_DEVICE_ID,
        "image_url": "/local/doorbell_best.jpg?t=1700000000",
        "fallback": False,
        "notified": ["notify.mobile_app_rqb_pixel_11_xl"],
        "meta": {"mode": "face"},
    }
    with patch(
        "tools.doorbell_face_picker.run_test_clip_async",
        new=AsyncMock(return_value=fake_result),
    ) as mock_test_clip:
        rc = dfp.main(
            [
                "test-clip",
                "--event-id",
                MOCK_EVENT_ID,
                "--target",
                "notify.mobile_app_rqb_pixel_11_xl",
            ]
        )

    assert rc == 0
    mock_test_clip.assert_awaited_once_with(
        event_id=MOCK_EVENT_ID,
        target="notify.mobile_app_rqb_pixel_11_xl",
        device_id=None,
    )

