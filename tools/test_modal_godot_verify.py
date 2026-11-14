from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import modal_godot_verify


def test_build_global_script_class_cache_deterministic_and_sorted(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    (game_dir / "core").mkdir(parents=True)
    (game_dir / "ui").mkdir(parents=True)
    (game_dir / ".godot").mkdir(parents=True)

    # Script with explicit extends
    (game_dir / "core" / "upgrades.gd").write_text(
        "extends Resource\nclass_name ShipUpgrades\nfunc apply(): pass\n",
        encoding="utf-8",
    )
    # Script without extends (defaults to RefCounted)
    (game_dir / "ui" / "card_art.gd").write_text(
        "class_name CardArt\nconst VERSION = 1\n",
        encoding="utf-8",
    )
    # Script without class_name (should be ignored)
    (game_dir / "core" / "helper.gd").write_text(
        "extends Node\nfunc noop(): pass\n",
        encoding="utf-8",
    )
    # Script inside .godot/ (should be ignored)
    (game_dir / ".godot" / "internal.gd").write_text(
        "class_name InternalGhost\nextends Object\n",
        encoding="utf-8",
    )

    content = modal_godot_verify.build_global_script_class_cache(game_dir)
    cfg_file = game_dir / ".godot" / "global_script_class_cache.cfg"
    assert cfg_file.is_file()
    assert cfg_file.read_text(encoding="utf-8") == content

    # CardArt must precede ShipUpgrades alphabetically
    assert content.startswith("list=[")
    assert '"class": &"CardArt"' in content
    assert '"base": &"RefCounted"' in content
    assert '"path": "res://ui/card_art.gd"' in content
    assert '"class": &"ShipUpgrades"' in content
    assert '"base": &"Resource"' in content
    assert '"path": "res://core/upgrades.gd"' in content
    assert "InternalGhost" not in content
    assert content.index("CardArt") < content.index("ShipUpgrades")


def test_build_game_archive_includes_global_script_class_cache_and_excludes_git(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    (game_dir / "core").mkdir(parents=True)
    (game_dir / ".git").mkdir(parents=True)
    (game_dir / ".godot" / "imported").mkdir(parents=True)

    (game_dir / "project.godot").write_text("[application]\n", encoding="utf-8")
    (game_dir / "core" / "contracts.gd").write_text(
        "class_name ContractsEngine\nextends RefCounted\n",
        encoding="utf-8",
    )
    (game_dir / ".git" / "config").write_text("secret", encoding="utf-8")
    (game_dir / ".godot" / "imported" / "heavy.ctex").write_bytes(b"12345")

    archive_bytes = modal_godot_verify._build_game_archive(game_dir)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "project.godot" in names
        assert "core/contracts.gd" in names
        assert ".godot/global_script_class_cache.cfg" in names
        assert not any(n.startswith(".git") for n in names)
        assert ".godot/imported/heavy.ctex" not in names


def test_ensure_modal_credentials_reads_secrets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    secrets_file = tmp_path / "env.json"
    secrets_file.write_text(
        json.dumps({"MODAL_TOKEN_ID": "ak-test-123", "MODAL_TOKEN_SECRET": "as-secret-456"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(modal_godot_verify, "SECRETS_ENV_PATH", secrets_file)

    modal_godot_verify.ensure_modal_credentials()
    assert modal_godot_verify.os.environ.get("MODAL_TOKEN_ID") == "ak-test-123"
    assert modal_godot_verify.os.environ.get("MODAL_TOKEN_SECRET") == "as-secret-456"


def test_main_strips_leading_godot_binary_and_invokes_modal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True)
    (game_dir / "project.godot").write_text("[application]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(modal_godot_verify, "MAIN_GAME_DIR", game_dir)
    monkeypatch.delenv("AGORA_GODOT_LOCAL", raising=False)

    captured: dict[str, object] = {}

    def fake_run_on_modal(gdir: Path, filters: list[str], timeout: int = 300) -> tuple[int, str]:
        captured["game_dir"] = gdir
        captured["filters"] = filters
        return 0, "ALL 42 TESTS PASSED"

    monkeypatch.setattr(modal_godot_verify, "run_on_modal", fake_run_on_modal)
    monkeypatch.setattr(
        modal_godot_verify.sys,
        "argv",
        [
            "modal_godot_verify.py",
            "/workspace/agora-roguelike/.godot-bin/Godot_v4.7.2-stable_linux.x86_64",
            "test_upgrades",
            "test_contracts",
        ],
    )

    rc = modal_godot_verify.main()
    assert rc == 0
    assert captured["game_dir"] == game_dir
    assert captured["filters"] == ["test_upgrades", "test_contracts"]


def test_main_detects_script_error_in_modal_output_and_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True)
    (game_dir / "project.godot").write_text("[application]\n", encoding="utf-8")
    monkeypatch.chdir(game_dir)
    monkeypatch.setattr(modal_godot_verify, "MAIN_GAME_DIR", game_dir)
    monkeypatch.delenv("AGORA_GODOT_LOCAL", raising=False)

    def fake_run_on_modal(gdir: Path, filters: list[str], timeout: int = 300) -> tuple[int, str]:
        return 0, "Running suite...\nSCRIPT ERROR: Parse Error: Identifier not found\nDone"

    monkeypatch.setattr(modal_godot_verify, "run_on_modal", fake_run_on_modal)
    monkeypatch.setattr(modal_godot_verify.sys, "argv", ["modal_godot_verify.py", "test_broken"])

    rc = modal_godot_verify.main()
    assert rc == 1


def test_main_local_fallback_on_modal_exception_and_error_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True)
    (game_dir / "project.godot").write_text("[application]\n", encoding="utf-8")
    monkeypatch.chdir(game_dir)
    monkeypatch.setattr(modal_godot_verify, "MAIN_GAME_DIR", game_dir)
    monkeypatch.delenv("AGORA_GODOT_LOCAL", raising=False)

    def failing_modal(gdir: Path, filters: list[str], timeout: int = 300) -> tuple[int, str]:
        raise RuntimeError("Modal network unreachable")

    monkeypatch.setattr(modal_godot_verify, "run_on_modal", failing_modal)
    monkeypatch.setattr(modal_godot_verify.sys, "argv", ["modal_godot_verify.py", "test_local_fallback"])

    fake_proc = MagicMock(returncode=0, stdout="ERROR: Engine assertion failed\n", stderr="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        rc = modal_godot_verify.main()
        assert rc == 1
        assert mock_run.called
        called_cmd = mock_run.call_args[0][0]
        assert "--headless" in called_cmd
        assert "test_local_fallback" in called_cmd
