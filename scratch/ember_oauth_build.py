#!/usr/bin/env python3
"""Build, test, push, CI-wait, and stage Ember v1.1.0 with native Android OAuth handoff.

Automates:
1. Verification of Ember v1.1.0 changes in /workspace/ember (ruff + pytest)
2. Git commit and push to brockventures/ember main
3. Waiting for GitHub Actions CI build of the ember-debug-apk artifact
4. Staging the resulting APK to $NAS_DOCKER_ROOT/hearth/web/static/ember-ota.apk on Host 2
5. Syncing Ember repository sources to $NAS_DOCKER_ROOT/hearth/client/ember on Host 2
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Runtime topology dynamically resolved via nas_config (zero hardcoded topology invariant)
sys.path.insert(0, "/workspace")
try:
    from tools import nas_config
except ImportError:
    nas_config = None  # type: ignore


def run_cmd(
    cmd: list[str],
    cwd: str | Path | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Execute command with logging and error reporting."""
    print(f"[EXEC] {' '.join(str(c) for c in cmd)} (cwd={cwd or '.'})")
    res = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.stdout:
        print(f"[STDOUT] {res.stdout.strip()}")
    if res.stderr:
        print(f"[STDERR] {res.stderr.strip()}", file=sys.stderr)
    if check and res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(str(c) for c in cmd)}\n{res.stderr.strip()}"
        )
    return res


def build_and_stage_ember_oauth(
    ember_dir: str | Path = "/workspace/ember",
    repo: str = "brockventures/ember",
    host_2_ip: str | None = None,
    timeout_ci_seconds: int = 360,
) -> bool:
    """Orchestrate building, pushing, CI artifact retrieval, and staging for Ember OAuth."""
    ember_path = Path(ember_dir).resolve()
    if not ember_path.exists():
        raise FileNotFoundError(f"Ember directory not found: {ember_path}")

    # Determine Host 2 target IP dynamically
    target_host = host_2_ip or (nas_config.HOST_2_IP if nas_config else None)
    if not target_host:
        raise ValueError("Host 2 IP must be specified or resolved from tools.nas_config")

    ssh_port = str(getattr(nas_config, "SSH_PORT", "22"))
    ssh_user = getattr(nas_config, "SSH_USER", "Brock")
    docker_root = (
        getattr(nas_config, "DOCKER_ROOT", None)
        or os.environ.get("NAS_DOCKER_ROOT")
        or "/docker"
    )

    print("=== Step 1: Pre-flight Verification (ruff + pytest) ===")
    run_cmd(["ruff", "check", "tools/", "tests/"], cwd=ember_path, check=True)
    run_cmd(["pytest", "tests/test_ember.py"], cwd=ember_path, check=True)

    print("=== Step 2: Stage & Commit in brockventures/ember ===")
    run_cmd(
        [
            "git",
            "add",
            "app/build.gradle.kts",
            "app/src/main/java/ventures/brock/ember/MainActivity.kt",
            "tests/test_ember.py",
        ],
        cwd=ember_path,
        check=True,
    )

    diff_cached = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=ember_path).stdout.strip()
    head_sha = ""
    if diff_cached:
        commit_msg = (
            "feat(oauth): wire native Android Google OAuth Chrome Custom Tab handoff "
            "and APK download handler in Ember v1.1.0"
        )
        run_cmd(["git", "commit", "-m", commit_msg], cwd=ember_path, check=True)
        print("Committed Ember changes cleanly.")
    else:
        print("No uncommitted changes staged.")

    head_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=ember_path).stdout.strip()
    print(f"Active HEAD SHA: {head_sha}")

    print("=== Step 3: Push to brockventures/ember main ===")
    run_cmd(["git", "push", "origin", "main"], cwd=ember_path, check=True)

    print(f"=== Step 4: Await GitHub Actions CI Artifact (HEAD={head_sha}) ===")
    start_time = time.time()
    artifact_downloaded = False
    download_dir = Path(tempfile.mkdtemp(prefix="ember_ci_apk_"))

    try:
        while time.time() - start_time < timeout_ci_seconds:
            runs_out = run_cmd(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    repo,
                    "-L",
                    "6",
                    "--json",
                    "databaseId,headSha,status,conclusion",
                ],
                check=False,
            ).stdout.strip()

            runs = json.loads(runs_out) if runs_out else []
            matching = [r for r in runs if r.get("headSha") == head_sha]

            if matching:
                run_info = matching[0]
                status = run_info.get("status")
                conclusion = run_info.get("conclusion")
                run_id = str(run_info.get("databaseId"))
                print(f"CI Run ID {run_id}: status={status}, conclusion={conclusion}")

                if status == "completed":
                    if conclusion != "success":
                        raise RuntimeError(
                            f"GitHub Actions CI run {run_id} failed with conclusion: {conclusion}"
                        )

                    print(f"Downloading CI artifact from run {run_id}...")
                    run_cmd(
                        [
                            "gh",
                            "run",
                            "download",
                            run_id,
                            "--repo",
                            repo,
                            "-n",
                            "ember-debug-apk",
                            "-D",
                            str(download_dir),
                        ],
                        check=True,
                    )

                    apk_candidates = list(download_dir.glob("**/*.apk"))
                    if not apk_candidates:
                        raise FileNotFoundError(
                            f"No .apk files found in downloaded artifact at {download_dir}"
                        )

                    downloaded_apk = apk_candidates[0]
                    local_staged_apk = ember_path / "ember-debug.apk"
                    shutil.copy2(downloaded_apk, local_staged_apk)
                    print(
                        f"Stored compiled APK locally at {local_staged_apk} "
                        f"({local_staged_apk.stat().st_size} bytes)"
                    )
                    artifact_downloaded = True
                    break
            else:
                print("Waiting for GitHub Actions run to register for commit...")

            time.sleep(8)

        if not artifact_downloaded:
            raise TimeoutError(f"Timed out awaiting CI APK artifact after {timeout_ci_seconds}s")

    finally:
        shutil.rmtree(download_dir, ignore_errors=True)

    print("=== Step 5: Stage ember-ota.apk to Host 2 Hearth Web Static ===")
    local_apk = ember_path / "ember-debug.apk"
    apk_bytes = local_apk.read_bytes()
    remote_ota_dest = f"{docker_root}/hearth/web/static/ember-ota.apk"

    ssh_apk_cmd = [
        "ssh",
        "-p",
        ssh_port,
        "-o",
        "StrictHostKeyChecking=no",
        f"{ssh_user}@{target_host}",
        f"cat > {remote_ota_dest} && ls -la {remote_ota_dest}",
    ]
    p_apk = subprocess.run(
        ssh_apk_cmd,
        input=apk_bytes,
        capture_output=True,
        timeout=60,
        check=True,
    )
    print(f"Staged OTA APK to Host 2: {p_apk.stdout.decode().strip()}")

    print("=== Step 6: Sync Ember Sources to Host 2 Client Directory ===")
    remote_ember_dest = f"{docker_root}/hearth/client/ember"
    tar_cmd = [
        "tar",
        "-C",
        str(ember_path),
        "--exclude=.git",
        "--exclude=.pytest_cache",
        "--exclude=.ruff_cache",
        "--exclude=*.apk",
        "-czf",
        "-",
        ".",
    ]
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)

    ssh_tar_cmd = [
        "ssh",
        "-p",
        ssh_port,
        "-o",
        "StrictHostKeyChecking=no",
        f"{ssh_user}@{target_host}",
        f"mkdir -p {remote_ember_dest} && tar -xzf - -C {remote_ember_dest}",
    ]
    ssh_proc = subprocess.run(
        ssh_tar_cmd,
        stdin=tar_proc.stdout,
        capture_output=True,
        timeout=60,
        check=True,
    )
    tar_proc.wait()
    if tar_proc.returncode != 0:
        raise RuntimeError(f"Local tar compression failed ({tar_proc.returncode})")

    # Verify remote build.gradle.kts and MainActivity.kt
    verify_cmd = [
        "ssh",
        "-p",
        ssh_port,
        "-o",
        "StrictHostKeyChecking=no",
        f"{ssh_user}@{target_host}",
        f"grep 'versionName = \"1.1.0\"' {remote_ember_dest}/app/build.gradle.kts && "
        f"grep 'fun openSystemAuth' {remote_ember_dest}/app/src/main/java/ventures/brock/ember/MainActivity.kt",
    ]
    verify_res = subprocess.run(verify_cmd, capture_output=True, text=True, check=True)
    print(f"Host 2 Ember sync verified successfully:\n{verify_res.stdout.strip()}")

    print("=== Ember v1.1.0 OAuth Handoff Build & Staging Complete ===")
    return True


if __name__ == "__main__":
    success = build_and_stage_ember_oauth()
    sys.exit(0 if success else 1)
