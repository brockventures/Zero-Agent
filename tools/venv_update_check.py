#!/usr/bin/env python3
"""Monthly persistent virtualenv update checker and gated upgrader for Zero & Ivy."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import sys
from typing import Any

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from tools import nas_config
except ImportError:
    import nas_config

VENV_TARGETS: list[dict[str, Any]] = [
    {
        "id": "zero-extras",
        "name": "Zero Extras",
        "venv_path": "/workspace/.venv/extras",
        "container": None,
        "smoke_imports": ("playwright", "matplotlib", "fastembed", "sqlite_vec"),
    },
    {
        "id": "zero-modal",
        "name": "Zero Modal/CAD",
        "venv_path": "/workspace/.venv/modal",
        "container": None,
        "smoke_imports": ("modal", "build123d", "trimesh", "manifold3d"),
    },
    {
        "id": "ivy-baseball",
        "name": "Ivy Baseball",
        "venv_path": "/workspace/.venv/baseball",
        "container": "discord-ivy-agent",
        "smoke_imports": (
            "numpy",
            "pandas",
            "scipy",
            "sklearn",
            "matplotlib",
            "fastembed",
            "sqlite_vec",
        ),
    },
]


def parse_major_version(ver: str) -> int | None:
    """Extract the leading integer major version component from a version string."""
    if not ver or not isinstance(ver, str):
        return None
    cleaned = ver.strip().lstrip("vV")
    match = re.match(r"^(\d+)", cleaned)
    if not match:
        return None
    return int(match.group(1))


def classify_outdated(packages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split outdated pip JSON entries into safe (same major) vs major (major jump) lists."""
    safe: list[dict[str, Any]] = []
    major: list[dict[str, Any]] = []
    for pkg in packages or []:
        if not isinstance(pkg, dict) or not pkg.get("name"):
            continue
        cur_major = parse_major_version(str(pkg.get("version", "")))
        lat_major = parse_major_version(str(pkg.get("latest_version", "")))
        if cur_major is not None and lat_major is not None and cur_major == lat_major:
            safe.append(pkg)
        else:
            major.append(pkg)
    return safe, major


def _run_target_cmd(target: dict[str, Any], args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a command inside the target's local environment or via Host 2 docker exec."""
    container = target.get("container")
    if container:
        quoted_args = " ".join(shlex.quote(arg) for arg in args)
        remote_cmd = f"/usr/local/bin/docker exec {shlex.quote(str(container))} {quoted_args}"
        try:
            return nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=timeout)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return 1, "", str(exc)

    try:
        res = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return res.returncode, res.stdout or "", res.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def list_outdated_packages(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Return outdated packages from `<venv>/bin/pip list --outdated --format=json`."""
    pip_bin = f"{str(target['venv_path']).rstrip('/')}/bin/pip"
    code, out, _ = _run_target_cmd(target, [pip_bin, "list", "--outdated", "--format=json"], timeout=90)
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def run_smoke_imports(target: dict[str, Any]) -> tuple[bool, str]:
    """Verify target smoke_imports via `<venv>/bin/python3`."""
    mods = [str(m) for m in target.get("smoke_imports", ()) if m]
    if not mods:
        return True, "No smoke imports configured"

    py_bin = f"{str(target['venv_path']).rstrip('/')}/bin/python3"
    snippet = f"import importlib; [importlib.import_module(m) for m in {json.dumps(mods)}]; print('OK')"
    code, out, err = _run_target_cmd(target, [py_bin, "-c", snippet], timeout=60)
    if code == 0:
        return True, f"Verified imports: {', '.join(mods)}"
    detail = (err or out or f"exit {code}").strip()
    return False, detail


def _format_examples(pkgs: list[dict[str, Any]], limit: int = 4) -> str:
    """Format top package upgrade examples concisely for reports and Decision Card summaries."""
    if not pkgs:
        return ""
    sample = [
        f"{p['name']} ({p.get('version', '?')}→{p.get('latest_version', '?')})"
        for p in pkgs[:limit]
    ]
    suffix = ", ..." if len(pkgs) > limit else ""
    return ", ".join(sample) + suffix


def check_updates(quiet: bool = False) -> None:
    """Check all persistent virtualenvs for outdated packages and stage a Decision Card if any exist."""
    findings: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for target in VENV_TARGETS:
        outdated = list_outdated_packages(target)
        safe, major = classify_outdated(outdated)
        if safe or major:
            findings.append((target, safe, major))

    if not findings:
        if not quiet:
            print("✅ **All persistent agent virtualenvs are up to date.**")
        return

    venv_lines: list[str] = []
    for target, safe, major in findings:
        examples = _format_examples(safe + major)
        example_str = f" — e.g. {examples}" if examples else ""
        venv_lines.append(
            f"• **{target['name']}** (`{target['venv_path']}`): {len(safe)} safe, {len(major)} major{example_str}"
        )

    msg = "\n".join(["📦 **Persistent Agent Virtualenv Updates Available!**\n", *venv_lines])
    summary = "\n".join([
        f"Outdated packages detected across {len(findings)} persistent virtualenv(s):",
        *venv_lines,
        "**Approve Action:** Upgrades safe minor/patch packages across the persistent virtualenvs and runs import smoke tests (skipping major-version jumps).",
    ])

    decision_staged = False
    try:
        from tools.forge import ZERO_OPS_CHANNEL_ID, stage_decision

        stage_decision(
            category="ops_upgrade",
            title="Upgrade Persistent Agent Virtualenv Packages",
            summary=summary,
            handler_type="shell",
            payload={
                "command": "python3 /workspace/tools/venv_update_check.py upgrade",
                "cwd": "/workspace",
            },
            channel_id=ZERO_OPS_CHANNEL_ID,
            post_to_discord=True,
        )
        decision_staged = True
    except (ImportError, OSError, RuntimeError, ValueError, LookupError, sqlite3.Error) as exc:
        msg += f"\n\n[CHOICES: Upgrade Safe Venv Packages | Skip For Now] ({exc})"

    if not (quiet and decision_staged):
        print(msg)


def perform_upgrade(include_major: bool = False) -> dict[str, Any]:
    """Upgrade eligible packages in each persistent virtualenv and run import smoke checks."""
    results: dict[str, Any] = {"upgraded": [], "skipped": [], "failed": []}

    for target in VENV_TARGETS:
        name = str(target["name"])
        outdated = list_outdated_packages(target)
        safe, major = classify_outdated(outdated)
        to_upgrade = safe + major if include_major else safe

        if not to_upgrade:
            skip_note = f" ({len(major)} major jump(s) skipped)" if major and not include_major else ""
            print(f"• **{name}:** No eligible packages to upgrade{skip_note}.")
            results["skipped"].append(name)
            continue

        pip_bin = f"{str(target['venv_path']).rstrip('/')}/bin/pip"
        pkg_names = [str(pkg["name"]) for pkg in to_upgrade if pkg.get("name")]
        print(f"📦 Upgrading {len(pkg_names)} package(s) in **{name}** (`{target['venv_path']}`)...")
        code, out, err = _run_target_cmd(
            target,
            [pip_bin, "install", "--upgrade", "--no-cache-dir", *pkg_names],
            timeout=600,
        )
        if code != 0:
            detail = (err or out or f"exit {code}").strip()
            print(f"❌ **{name}** pip upgrade failed: {detail}")
            results["failed"].append({"target": name, "stage": "pip", "error": detail})
            continue

        smoke_ok, smoke_msg = run_smoke_imports(target)
        if smoke_ok:
            skip_note = f" (skipped {len(major)} major)" if major and not include_major else ""
            print(f"✅ **{name}** upgraded ({len(pkg_names)} pkg){skip_note}: {smoke_msg}")
            results["upgraded"].append({"target": name, "packages": pkg_names, "smoke": smoke_msg})
        else:
            print(f"❌ **{name}** smoke import check failed after upgrade: {smoke_msg}")
            results["failed"].append({"target": name, "stage": "smoke", "error": smoke_msg})

    return results


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "check"
    if action == "check":
        check_updates(quiet="--quiet" in sys.argv)
    elif action in ("upgrade", "apply"):
        perform_upgrade(include_major="--include-major" in sys.argv)
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade [--include-major]]")
