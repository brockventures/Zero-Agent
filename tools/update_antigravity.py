#!/usr/bin/env python3
# ruff: noqa
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.append(_p)

try:
    import tools.nas_config as nas_config
except ImportError:
    import nas_config

SHARED_BIN_DIR = "/opt/agy-bin"
SHARED_BIN_PATH = "/opt/agy-bin/agy"
LOCAL_BIN_PATH = "/usr/local/bin/agy"
AGENT_CONTAINERS = (
    "discord-antigravity-agent",
    "discord-ivy-agent",
    "discord-vulcan-agent",
)


def uses_shared_binary_mount() -> bool:
    """Return True if the shared fleet binary mount directory exists and is writable."""
    return os.path.isdir(str(SHARED_BIN_DIR)) and os.access(str(SHARED_BIN_DIR), os.W_OK)


def _update_host_dockerfile(target_version: str) -> tuple[bool, str]:
    """Update Dockerfile on Host 2 with the new release version pin."""
    target_version = target_version.lstrip("v")
    host_agent_dir = os.environ.get("HOST_AGENT_DIR") or os.path.join(
        nas_config.DOCKER_ROOT, "discord-agy-agent"
    )
    cmd = (
        f"sed -i -E 's|antigravity-cli/releases/download/[^/]+/|antigravity-cli/releases/download/"
        f"{target_version}/|g' {host_agent_dir}/Dockerfile"
    )
    try:
        code, out, err = nas_config.run_ssh(nas_config.HOST_2_IP, cmd, timeout=15)
        if code == 0:
            return True, f"Updated host Dockerfile at {host_agent_dir}/Dockerfile"
        return False, f"Remote sed failed (exit {code}): {err.strip() or out.strip()}"
    except Exception as e:
        return False, f"Remote sed execution error: {e}"


def verify_fleet_versions() -> dict[str, str]:
    """Query live agy version in each agent container on Host 2 via docker exec."""
    versions: dict[str, str] = {}
    for container in AGENT_CONTAINERS:
        cmd = f"/usr/local/bin/docker exec {container} agy --version"
        try:
            code, out, _ = nas_config.run_ssh(nas_config.HOST_2_IP, cmd, timeout=15)
            if code == 0 and out.strip():
                ver = out.strip().splitlines()[-1].strip().split()[-1].lstrip("v")
                versions[container] = ver
            else:
                versions[container] = "unknown"
        except Exception:
            versions[container] = "unknown"
    return versions


def get_current_version() -> str:
    try:
        out = subprocess.check_output(["agy", "--version"], stderr=subprocess.STDOUT).decode().strip()
        return out.lstrip("v")
    except Exception:
        return "unknown"


def get_latest_version() -> str:
    url = "https://github.com/google-antigravity/antigravity-cli/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        tag = resp.geturl().split("/")[-1]
        return tag.lstrip("v")


def check_updates(quiet: bool = False):
    curr = get_current_version()
    latest = get_latest_version()

    if curr != "unknown" and latest != "unknown" and curr != latest:
        msg_lines = [
            "🔔 **New Antigravity CLI Release Available!**\n",
            f"• **Installed Version:** `v{curr}`",
            f"• **Latest Release:** `v{latest}`",
            f"• **Release Notes:** https://github.com/google-antigravity/antigravity-cli/releases/tag/{latest}\n",
        ]
        msg = "\n".join(msg_lines)

        decision_staged = False
        try:
            from tools.forge import ZERO_OPS_CHANNEL_ID, stage_decision

            if uses_shared_binary_mount():
                summary = (
                    f"Antigravity CLI `v{curr}` → `v{latest}` is available. Approving downloads the release binary, "
                    "atomically updates the shared mount (/opt/agy-bin/agy, zero restarts required across the fleet), "
                    "and updates the host Dockerfile."
                )
            else:
                summary = (
                    f"Antigravity CLI `v{curr}` → `v{latest}` is available. Approving downloads the release binary, "
                    "updates the host Dockerfile, and triggers container restart."
                )

            stage_decision(
                category="ops_upgrade",
                title=f"Update Antigravity CLI to v{latest}",
                summary=summary,
                handler_type="shell",
                payload={
                    "command": f"python3 /workspace/tools/update_antigravity.py upgrade {latest}",
                    "cwd": "/workspace",
                },
                channel_id=ZERO_OPS_CHANNEL_ID,
                post_to_discord=True,
            )
            decision_staged = True
        except Exception as e:
            msg += f"\n[CHOICES: Update Antigravity to v{latest} | Skip For Now] ({e})"

        if not (quiet and decision_staged):
            print(msg)
    else:
        if not quiet:
            print(f"✅ **Antigravity CLI is up to date:** `v{curr}` (latest release).")


def perform_upgrade(target_version: str | None = None, force: bool = False):
    curr = get_current_version()
    if not target_version:
        target_version = get_latest_version()
    else:
        target_version = target_version.lstrip("v")

    if curr == target_version and not force:
        print(f"✅ Already running latest Antigravity CLI version `v{curr}`.")
        return

    print(f"📦 Downloading Antigravity CLI `v{target_version}`...")
    tmp_dir = "/tmp/agy_upgrade"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    arch = "x64"  # Default for amd64
    tar_url = f"https://github.com/google-antigravity/antigravity-cli/releases/download/{target_version}/agy_cli_linux_{arch}.tar.gz"
    tar_path = os.path.join(tmp_dir, "agy.tar.gz")

    try:
        req = urllib.request.Request(tar_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tar_path, "wb") as f:
            f.write(resp.read())

        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(path=tmp_dir)

        # Locate binary in extracted dir
        new_binary = None
        for root, _, files in os.walk(tmp_dir):
            for file in files:
                fpath = os.path.join(root, file)
                if file == "agy" and os.access(fpath, os.X_OK):
                    new_binary = fpath
                    break
            if new_binary:
                break

        if not new_binary:
            for root, _, files in os.walk(tmp_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    if os.access(fpath, os.X_OK) and not file.endswith(".tar.gz"):
                        new_binary = fpath
                        break
                if new_binary:
                    break

        if not new_binary or not os.path.exists(new_binary):
            print("❌ Error: Could not locate `agy` executable in release archive.")
            return

        # Verify test execution
        test_out = subprocess.check_output([new_binary, "--version"], stderr=subprocess.STDOUT).decode().strip()
        print(f"Verified executable: {test_out}")

        if uses_shared_binary_mount():
            temp_dest = os.path.join(SHARED_BIN_DIR, "agy.new")
            shutil.copy2(new_binary, temp_dest)
            os.chmod(temp_dest, 0o755)
            os.replace(temp_dest, SHARED_BIN_PATH)
            print(f"Installed `v{target_version}` atomically to shared mount {SHARED_BIN_PATH}")

            # Ensure /usr/local/bin/agy symlinks to /opt/agy-bin/agy
            try:
                if os.path.islink(LOCAL_BIN_PATH):
                    if os.readlink(LOCAL_BIN_PATH) != SHARED_BIN_PATH:
                        os.unlink(LOCAL_BIN_PATH)
                        os.symlink(SHARED_BIN_PATH, LOCAL_BIN_PATH)
                elif os.path.exists(LOCAL_BIN_PATH):
                    os.remove(LOCAL_BIN_PATH)
                    os.symlink(SHARED_BIN_PATH, LOCAL_BIN_PATH)
                else:
                    os.symlink(SHARED_BIN_PATH, LOCAL_BIN_PATH)
            except Exception as e:
                print(f"Notice: Symlink update {LOCAL_BIN_PATH} -> {SHARED_BIN_PATH} skipped/failed: {e}")

            ok, msg = _update_host_dockerfile(target_version)
            if ok:
                print(f"Updated host Dockerfile with new version reference: {msg}")
            else:
                print(f"Notice: Host Dockerfile update skipped/failed: {msg}")

            fleet_versions = verify_fleet_versions()
            print(
                f"\n🚀 **Antigravity Upgraded Successfully (Shared Fleet Mount)!**\n"
                f"• Version: `v{curr}` → `v{target_version}`\n"
                "• Zero container restarts required (shared directory bind mount).\n"
                "• Verified fleet versions:"
            )
            for cname in AGENT_CONTAINERS:
                cver = fleet_versions.get(cname, "unknown")
                print(f"  - `{cname}`: `v{cver}`")
        else:
            temp_dest = f"{LOCAL_BIN_PATH}.new"
            shutil.copy2(new_binary, temp_dest)
            os.chmod(temp_dest, 0o755)
            os.replace(temp_dest, LOCAL_BIN_PATH)
            print(f"Installed `v{target_version}` to {LOCAL_BIN_PATH}")

            ok, msg = _update_host_dockerfile(target_version)
            if ok:
                print(f"Updated host Dockerfile with new version reference: {msg}")
            else:
                print(f"Notice: Host Dockerfile update skipped/failed: {msg}")

            # Trigger detached restart in 3 seconds via nas_config.run_ssh
            restart_cmd = "nohup sh -c 'sleep 3 && docker restart discord-antigravity-agent' >/dev/null 2>&1 &"
            try:
                nas_config.run_ssh(nas_config.HOST_2_IP, restart_cmd, timeout=10)
            except Exception as e:
                print(f"Notice: Container restart trigger skipped/failed: {e}")

            print(
                f"\n🚀 **Antigravity Upgraded Successfully!**\n• Version: `v{curr}` → `v{target_version}`\n"
                "• Container is restarting in 3s to load the new binary."
            )

    except Exception as e:
        print(f"❌ Upgrade failed: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "check"
    force = "--force" in sys.argv or "-f" in sys.argv
    if action == "check":
        quiet = "--quiet" in sys.argv
        check_updates(quiet=quiet)
    elif action in ("upgrade", "apply", "update"):
        target = None
        for arg in sys.argv[2:]:
            if not arg.startswith("-"):
                target = arg
                break
        perform_upgrade(target, force=force)
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade [version] [--force]]")
