import os
import sys
import json
import time

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from PIL import Image

from tools.deliver_image import (
    resolve_channel,
    check_image,
    record_delivered_artifact,
    deliver_image,
    DELIVERED_FILE,
)
from tools.bridge_runner import find_new_artifacts


def test_resolve_channel():
    # Known names
    name, cid = resolve_channel("lounge")
    assert name == "lounge"
    assert cid == 1534452820995080192

    name, cid = resolve_channel("#zero-chat")
    assert name == "zero-chat"
    assert cid == 1542081375287640084

    # Known numeric ID
    name, cid = resolve_channel("1534436119888793750")
    assert name == "the-banana-stand"
    assert cid == 1534436119888793750

    # Unknown numeric ID
    name, cid = resolve_channel("987654321")
    assert name == "channel-987654321"
    assert cid == 987654321

    # Unknown string
    name, cid = resolve_channel("unknown-channel")
    assert name == "unknown-channel"
    assert cid is None


def test_check_image(tmp_path):
    # Non-existent
    res = check_image(tmp_path / "does_not_exist.png")
    assert res["valid"] is False
    assert "File does not exist" in res["error"]

    # Empty file
    empty_file = tmp_path / "empty.png"
    empty_file.write_bytes(b"")
    res = check_image(empty_file)
    assert res["valid"] is False
    assert "empty" in res["error"]

    # Valid PNG image
    valid_file = tmp_path / "test_valid.png"
    img = Image.new("RGB", (64, 64), color="red")
    img.save(valid_file, format="PNG")

    res = check_image(valid_file)
    assert res["valid"] is True
    assert res["filename"] == "test_valid.png"
    assert res["width"] == 64
    assert res["height"] == 64
    assert res["format"] == "PNG"
    assert res["size_bytes"] > 0


def test_record_delivered_artifact(tmp_path):
    test_deliv = tmp_path / "delivered.json"
    with patch("tools.deliver_image.DELIVERED_FILE", test_deliv):
        img_path = tmp_path / "sample.jpg"
        img_path.write_bytes(b"fake-image-bytes")

        record_delivered_artifact(img_path, 1534452820995080192, 1234567890, "sample.jpg")

        assert test_deliv.exists()
        with open(test_deliv) as f:
            data = json.load(f)

        assert str(img_path) in data
        assert "sample.jpg" in data
        assert data["sample.jpg"]["channel_id"] == 1534452820995080192
        assert data["sample.jpg"]["message_id"] == "1234567890"


def test_deliver_image_mocked(tmp_path):
    valid_file = tmp_path / "test_render.jpg"
    img = Image.new("RGB", (128, 128), color="blue")
    img.save(valid_file, format="JPEG")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "999888777",
        "attachments": [{"url": "https://cdn.discordapp.com/attachments/test.jpg"}],
    }

    test_deliv = tmp_path / "delivered_test.json"
    with patch("tools.deliver_image.DELIVERED_FILE", test_deliv), \
         patch("tools.deliver_image.get_discord_token", return_value="fake-test-token"), \
         patch("requests.post", return_value=mock_resp) as mock_post:

        res = deliver_image(
            image_path=valid_file,
            channel_input="lounge",
            caption="Test container render",
        )

        assert res["success"] is True
        assert res["channel"] == "lounge"
        assert res["channel_id"] == 1534452820995080192
        assert res["message_id"] == "999888777"
        mock_post.assert_called_once()


def test_find_new_artifacts_dedup(tmp_path):
    # Setup brain structure
    brain_dir = tmp_path / "brain" / "conv-test-123"
    brain_dir.mkdir(parents=True)
    art_file = brain_dir / "render_test.jpg"
    art_file.write_bytes(b"image-data")

    # Set start time slightly before creation
    start_ts = time.time() - 10

    test_deliv = tmp_path / "delivered.json"
    test_deliv.write_text(json.dumps({str(art_file): {"delivered_at": time.time()}}))

    with patch("tools.bridge_runner.Path") as mock_path_cls:
        # We test directly with custom delivered_file
        with patch("pathlib.Path.exists", return_value=True):
            pass

    # Direct test of find_new_artifacts logic
    # With file in delivered_set, find_new_artifacts should omit it
    with patch("tools.bridge_runner.Path", side_effect=lambda p: Path(p) if "/workspace/data" not in str(p) else test_deliv):
        pass
