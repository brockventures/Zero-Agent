#!/usr/bin/env python3
import os, sys, subprocess, urllib.request, tarfile, shutil

def get_current_version() -> str:
    try:
        out = subprocess.check_output(["agy", "--version"], stderr=subprocess.STDOUT).decode().strip()
        return out.lstrip("v")
    except Exception as e:
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
        print(f"🔔 **New Antigravity CLI Release Available!**\n")
        print(f"• **Installed Version:** `v{curr}`")
        print(f"• **Latest Release:** `v{latest}`")
        print(f"• **Release Notes:** https://github.com/google-antigravity/antigravity-cli/releases/tag/{latest}\n")
        print(f"[CHOICES: Update Antigravity to v{latest} | Skip For Now]")
    else:
        if not quiet:
            print(f"✅ **Antigravity CLI is up to date:** `v{curr}` (latest release).")

def perform_upgrade(target_version: str = None):
    curr = get_current_version()
    if not target_version:
        target_version = get_latest_version()
    else:
        target_version = target_version.lstrip("v")

    if curr == target_version:
        print(f"✅ Already running latest Antigravity CLI version `v{curr}`.")
        return

    print(f"📦 Downloading Antigravity CLI `v{target_version}`...")
    tmp_dir = "/tmp/agy_upgrade"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    arch = "x64" # Default for amd64
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
            print(f"❌ Error: Could not locate `agy` executable in release archive.")
            return

        # Verify test execution
        test_out = subprocess.check_output([new_binary, "--version"], stderr=subprocess.STDOUT).decode().strip()
        print(f"Verified executable: {test_out}")

        # Replace /usr/local/bin/agy
        dest_binary = "/usr/local/bin/agy"
        temp_dest = "/usr/local/bin/agy.new"
        shutil.copy2(new_binary, temp_dest)
        os.chmod(temp_dest, 0o755)
        os.replace(temp_dest, dest_binary)
        print(f"Installed `v{target_version}` to {dest_binary}")

        # Update Dockerfile on host for immutability
        try:
            ssh_key = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
            ssh_port = os.environ.get("NAS_SSH_PORT", "22")
            ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
            host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")
            host_agent_dir = os.environ.get("HOST_AGENT_DIR", "/docker/discord-agy-agent")

            update_dockerfile_cmd = [
                "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes", f"{ssh_user}@{host_2}",
                f"sed -i -E 's|antigravity-cli/releases/download/[^/]+/|antigravity-cli/releases/download/{target_version}/|g' {host_agent_dir}/Dockerfile"
            ]
            subprocess.run(update_dockerfile_cmd, timeout=5, check=True)
            print("Updated host Dockerfile with new version reference.")
        except Exception as e:
            print(f"Notice: Host Dockerfile update skipped/failed: {e}")

        # Trigger detached restart in 3 seconds
        restart_cmd = [
            "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes", f"{ssh_user}@{host_2}",
            "nohup sh -c 'sleep 3 && docker restart discord-antigravity-agent' >/dev/null 2>&1 &"
        ]
        subprocess.run(restart_cmd, timeout=5, check=True)

        print(f"\n🚀 **Antigravity Upgraded Successfully!**\n• Version: `v{curr}` → `v{target_version}`\n• Container is restarting in 3s to load the new binary.")

    except Exception as e:
        print(f"❌ Upgrade failed: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        quiet = "--quiet" in sys.argv
        check_updates(quiet=quiet)
    elif action in ("upgrade", "apply", "update"):
        target = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None
        perform_upgrade(target)
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade [version]]")
