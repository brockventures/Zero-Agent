#!/usr/bin/env python3
"""Modal Serverless Headless Godot 4.7.2 Test & Verification Runner.

1. Deterministically generates `game/.godot/global_script_class_cache.cfg` from all
   `class_name <Name>` / `extends <Base>` declarations in `game/**/*.gd` in <0.02s,
   eliminating the 5-minute silent `godot --headless --import` hang in fresh git worktrees.
2. Offloads headless Godot test execution (including 120-round bot simulation suites)
   to Modal serverless compute (`8 vCPUs, 16 GB RAM`) backed by the persistent
   `zero-godot-cache` Modal Volume (caching the Godot 4.7.2 binary and project assets).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

GODOT_BIN_LOCAL = Path("/workspace/agora-roguelike/.godot-bin/Godot_v4.7.2-stable_linux.x86_64")
MAIN_GAME_DIR = Path("/workspace/agora-roguelike/game")
SECRETS_ENV_PATH = Path("/secrets/env.json")

_CLASS_NAME_RE = re.compile(r"^\s*class_name\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_EXTENDS_RE = re.compile(r"^\s*extends\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def ensure_modal_credentials() -> None:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return
    if SECRETS_ENV_PATH.exists():
        try:
            data = json.loads(SECRETS_ENV_PATH.read_text(encoding="utf-8"))
            if data.get("MODAL_TOKEN_ID") and data.get("MODAL_TOKEN_SECRET"):
                os.environ["MODAL_TOKEN_ID"] = str(data["MODAL_TOKEN_ID"])
                os.environ["MODAL_TOKEN_SECRET"] = str(data["MODAL_TOKEN_SECRET"])
        except (OSError, ValueError):
            return


def build_global_script_class_cache(game_dir: Path) -> str:
    """Scan `game_dir/**/*.gd` and deterministically build `.godot/global_script_class_cache.cfg`."""
    entries: list[dict[str, str]] = []
    for gd_path in sorted(game_dir.rglob("*.gd")):
        rel = gd_path.relative_to(game_dir).as_posix()
        if rel.startswith(".godot/"):
            continue
        try:
            text = gd_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m_cls = _CLASS_NAME_RE.search(text)
        if not m_cls:
            continue
        cls_name = m_cls.group(1)
        m_ext = _EXTENDS_RE.search(text)
        base_name = m_ext.group(1) if m_ext else "RefCounted"
        entries.append(
            {
                "base": base_name,
                "class": cls_name,
                "path": f"res://{rel}",
            }
        )

    entries.sort(key=lambda e: e["class"])
    blocks: list[str] = []
    for e in entries:
        blocks.append(
            "{\n"
            f'"base": &"{e["base"]}",\n'
            f'"class": &"{e["class"]}",\n'
            '"icon": "",\n'
            '"is_abstract": false,\n'
            '"is_tool": false,\n'
            '"language": &"GDScript",\n'
            f'"path": "{e["path"]}"\n'
            "}"
        )
    content = "list=[" + ", ".join(blocks) + "]\n"
    dot_godot = game_dir / ".godot"
    dot_godot.mkdir(parents=True, exist_ok=True)
    (dot_godot / "global_script_class_cache.cfg").write_text(content, encoding="utf-8")
    return content


def _build_game_archive(game_dir: Path) -> bytes:
    """Create a fast gzipped tar archive of `game_dir` (including `.godot/global_script_class_cache.cfg`)."""
    build_global_script_class_cache(game_dir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(game_dir):
            rel_root = os.path.relpath(root, game_dir)
            if rel_root == ".godot":
                dirs[:] = []
                cfg = Path(root) / "global_script_class_cache.cfg"
                if cfg.exists():
                    tar.add(str(cfg), arcname=".godot/global_script_class_cache.cfg", recursive=False)
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".git")]
            for fname in files:
                full_p = Path(root) / fname
                if not full_p.is_file() or full_p.is_symlink():
                    continue
                arcname = fname if rel_root == "." else os.path.join(rel_root, fname)
                tar.add(str(full_p), arcname=arcname, recursive=False)
    return buf.getvalue()


def run_on_modal(game_dir: Path, filters: list[str], timeout: int = 300) -> tuple[int, str]:
    ensure_modal_credentials()
    import modal  # type: ignore

    app = modal.App("zero-godot-verify")
    vol = modal.Volume.from_name("zero-godot-cache", create_if_missing=True)
    image = modal.Image.debian_slim(python_version="3.11").apt_install(
        "ca-certificates",
        "curl",
        "unzip",
        "libx11-6",
        "libxcursor1",
        "libxinerama1",
        "libgl1",
        "libxi6",
        "libxrandr2",
        "libxrender1",
        "libasound2",
        "libpulse0",
        "libdbus-1-3",
        "libudev1",
        "libfontconfig1",
    )

    payload = _build_game_archive(game_dir)

    # Ensure Godot binary is uploaded to the persistent Modal Volume once
    marker = Path("/tmp/.zero_godot_modal_bin_synced")
    if not marker.exists() and GODOT_BIN_LOCAL.exists():
        try:
            with vol.batch_upload(force=True) as batch:
                batch.put_file(str(GODOT_BIN_LOCAL), "/Godot_v4.7.2-stable_linux.x86_64")
            marker.write_text("ok", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[modal_godot_verify] Volume upload skipped: {exc}", file=sys.stderr)

    @app.function(
        image=image,
        volumes={"/cache": vol},
        cpu=8.0,
        memory=16384,
        timeout=max(120, int(timeout)),
        serialized=True,
    )
    def _remote_godot_test(archive_bytes: bytes, test_filters: list[str]) -> tuple[int, str]:
        import io as _io
        import os as _os
        import stat as _stat
        import subprocess as _sp
        import tarfile as _tf
        import tempfile as _tmp

        godot_bin = "/cache/Godot_v4.7.2-stable_linux.x86_64"
        if not _os.path.exists(godot_bin):
            return 127, "ERROR: Godot binary missing from /cache Volume"
        st = _os.stat(godot_bin)
        if not (st.st_mode & _stat.S_IXUSR):
            _os.chmod(godot_bin, st.st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)

        work_dir = _tmp.mkdtemp(prefix="agora-godot-")
        with _tf.open(fileobj=_io.BytesIO(archive_bytes), mode="r:gz") as t:
            t.extractall(path=work_dir)

        cmd = [godot_bin, "--headless", "--path", work_dir, "-s", "res://tests/run_tests.gd"]
        if test_filters:
            cmd.append("--")
            cmd.extend(test_filters)

        proc = _sp.run(cmd, capture_output=True, text=True, timeout=280, check=False)
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return int(proc.returncode), combined

    with app.run():
        return _remote_godot_test.remote(payload, filters)


def main() -> int:
    args = sys.argv[1:]
    # Compatibility with `run.sh [godot-binary] [filters...]`
    if args and (args[0].endswith("godot") or "Godot_" in args[0] or "/" in args[0]):
        args = args[1:]

    cwd = Path.cwd()
    if (cwd / "project.godot").exists():
        game_dir = cwd
    elif (cwd / "game" / "project.godot").exists():
        game_dir = cwd / "game"
    else:
        game_dir = MAIN_GAME_DIR

    # Always keep local global_script_class_cache.cfg fresh
    build_global_script_class_cache(game_dir)
    if MAIN_GAME_DIR.exists() and MAIN_GAME_DIR != game_dir:
        build_global_script_class_cache(MAIN_GAME_DIR)

    use_local = os.environ.get("AGORA_GODOT_LOCAL", "").strip().lower() in ("1", "true", "yes")
    if not use_local:
        try:
            rc, out = run_on_modal(game_dir, args)
            print(out)
            if re.search(r"^(SCRIPT ERROR|ERROR):", out, flags=re.MULTILINE):
                print("run.sh: script or engine error in test output; failing the run", file=sys.stderr)
                return 1
            return rc
        except Exception as exc:  # noqa: BLE001
            print(f"[modal_godot_verify] Modal offload fallback to local ({exc})", file=sys.stderr)

    # Local fallback using pre-built global_script_class_cache.cfg (NO hanging --import pass)
    godot_bin = str(GODOT_BIN_LOCAL) if GODOT_BIN_LOCAL.exists() else "godot"
    cmd = [godot_bin, "--headless", "--path", str(game_dir), "-s", "res://tests/run_tests.gd"]
    if args:
        cmd.append("--")
        cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    print(out)
    if re.search(r"^(SCRIPT ERROR|ERROR):", out, flags=re.MULTILINE):
        print("run.sh: script or engine error in test output; failing the run", file=sys.stderr)
        return 1
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
