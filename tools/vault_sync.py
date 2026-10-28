#!/usr/bin/env python3
"""
vault_sync.py — Zero-Vault Private Repository & Encrypted Secrets Synchronization

Synchronizes high-value gitignored state between the workspace and the private GitHub repository
`brockventures/Zero-Vault`:
1. Durable Memory & Config State (Plaintext in Private Repo):
   - <workspace>/memory/ (private, vault, public, crab_cavern)
   - <workspace>/config/schedule.json
   - <workspace>/tools/promote_memory.py & <workspace>/tools/pack_shard.py
2. Cryptographic Boundary for /secrets/ (AES-256-GCM Authenticated Encryption):
   - Never commits raw plaintext credential files (/secrets/env.json, *.pem, google_oauth.json).
   - Packs /secrets/ (excluding the master key itself) into a tar.gz archive in memory and
     encrypts it with 256-bit AES-GCM using .vault_master.key into `secrets/secrets.tar.gz.enc`.
3. Cross-Platform Hydration (`pull`):
   - Pulls `brockventures/Zero-Vault`, restores `memory/`, `config/schedule.json`, private tools,
     and decrypts `secrets/secrets.tar.gz.enc` into `/secrets` (and `<workspace>/secrets` on Windows).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PT_TZ = ZoneInfo("America/Los_Angeles")
WORKSPACE_DIR = Path(os.environ.get("ZERO_WORKSPACE_DIR", str(Path(__file__).resolve().parent.parent)))


def _default_secrets_dir() -> Path:
    env_override = os.environ.get("ZERO_SECRETS_DIR")
    if env_override:
        return Path(env_override)
    if os.name != "nt" and Path("/secrets").exists():
        return Path("/secrets")
    if os.name == "nt":
        return Path("C:/secrets")
    return WORKSPACE_DIR / "secrets"


def _default_master_key_file() -> Path:
    env_override = os.environ.get("ZERO_VAULT_KEY_FILE")
    if env_override:
        return Path(env_override)
    home_key = Path.home() / ".gemini" / ".vault_master.key"
    if home_key.exists() or os.name == "nt":
        return home_key
    return Path("/root/.gemini/.vault_master.key")


SECRETS_DIR = _default_secrets_dir()
MASTER_KEY_FILE = _default_master_key_file()
VAULT_REPO_DIR = WORKSPACE_DIR / "data" / "zero_vault_repo"
VAULT_REMOTE_URL = "https://github.com/brockventures/Zero-Vault.git"


def get_or_create_master_key(key_path: Path = MASTER_KEY_FILE) -> bytes:
    """Load or generate the 256-bit (32-byte) AES-GCM master key."""
    candidates = [
        key_path,
        Path.home() / ".gemini" / ".vault_master.key",
        SECRETS_DIR / ".vault_master.key",
        WORKSPACE_DIR / "secrets" / ".vault_master.key",
        Path("/root/.gemini/.vault_master.key"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                raw = candidate.read_text(encoding="utf-8").strip()
                key = base64.urlsafe_b64decode(raw.encode("ascii"))
                if len(key) == 32:
                    return key
        except OSError:
            continue
    key = AESGCM.generate_key(bit_length=256)
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(encoded + "\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
    except OSError:
        fallback = Path.home() / ".gemini" / ".vault_master.key"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(encoded + "\n", encoding="utf-8")
    return key


def encrypt_secrets_archive(
    secrets_dir: Path = SECRETS_DIR,
    output_file: Path = VAULT_REPO_DIR / "secrets" / "secrets.tar.gz.enc",
    key_path: Path = MASTER_KEY_FILE,
) -> dict:
    """Pack /secrets/ into an in-memory tar.gz and encrypt with 256-bit AES-GCM."""
    key = get_or_create_master_key(key_path)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)

    buf = io.BytesIO()
    included_files = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if secrets_dir.exists():
            for item in sorted(secrets_dir.iterdir()):
                if item.name == key_path.name or item.name.startswith(".vault_"):
                    continue
                if item.is_file():
                    tar.add(str(item), arcname=item.name)
                    included_files.append(item.name)

    plaintext_bytes = buf.getvalue()
    ciphertext = aesgcm.encrypt(nonce, plaintext_bytes, b"zero-vault-secrets-v1")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    # File format: magic header b"ZVLT1" (5 bytes) + 12-byte nonce + ciphertext
    payload = b"ZVLT1" + nonce + ciphertext
    output_file.write_bytes(payload)
    return {
        "encrypted_file": str(output_file),
        "included_count": len(included_files),
        "ciphertext_bytes": len(payload),
    }


def _decrypt_archive_bytes(
    encrypted_file: Path = VAULT_REPO_DIR / "secrets" / "secrets.tar.gz.enc",
    key_path: Path = MASTER_KEY_FILE,
) -> bytes:
    """Decrypt `secrets.tar.gz.enc` and return raw tar.gz bytes."""
    key = get_or_create_master_key(key_path)
    raw = encrypted_file.read_bytes()
    if not raw.startswith(b"ZVLT1") or len(raw) < 18:
        raise ValueError("Invalid ZVLT1 encrypted archive header.")
    nonce = raw[5:17]
    ciphertext = raw[17:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, b"zero-vault-secrets-v1")


def decrypt_secrets_archive(
    encrypted_file: Path = VAULT_REPO_DIR / "secrets" / "secrets.tar.gz.enc",
    key_path: Path = MASTER_KEY_FILE,
) -> list[str]:
    """Verify/decrypt `secrets.tar.gz.enc` and return list of contained member filenames."""
    plaintext = _decrypt_archive_bytes(encrypted_file=encrypted_file, key_path=key_path)
    buf = io.BytesIO(plaintext)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        return tar.getnames()


def extract_secrets_archive(
    encrypted_file: Path = VAULT_REPO_DIR / "secrets" / "secrets.tar.gz.enc",
    target_dirs: list[Path] | None = None,
    key_path: Path = MASTER_KEY_FILE,
) -> list[str]:
    """Decrypt and extract `secrets.tar.gz.enc` into target secret directories."""
    plaintext = _decrypt_archive_bytes(encrypted_file=encrypted_file, key_path=key_path)
    if target_dirs is None:
        target_dirs = [SECRETS_DIR]
        if os.name == "nt" and WORKSPACE_DIR / "secrets" not in target_dirs:
            target_dirs.append(WORKSPACE_DIR / "secrets")

    extracted_names: list[str] = []
    for dest in target_dirs:
        dest.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO(plaintext)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or ".." in member.name or member.name.startswith("/"):
                    continue
                fobj = tar.extractfile(member)
                if fobj is not None:
                    out_path = dest / Path(member.name).name
                    out_path.write_bytes(fobj.read())
                    if out_path.name not in extracted_names:
                        extracted_names.append(out_path.name)
    return extracted_names


def ensure_vault_repo_initialized(repo_dir: Path = VAULT_REPO_DIR) -> None:
    """Initialize local clone of brockventures/Zero-Vault with mandatory README.md and AGENTS.md."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", VAULT_REMOTE_URL], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Zero Agent"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(repo_dir), capture_output=True)

    readme = repo_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Zero-Vault (**V**ersioned **A**rchive for **U**ntracked **L**ocal **T**reasury)\n\n"
            "Private companion repository for `brockventures/Zero-Agent`, storing durable memory tiers, "
            "scheduler configurations, and AES-256-GCM encrypted secret archives.\n\n"
            "## Contents\n"
            "- `memory/`: Durable markdown memory tiers (`private/`, `vault/`, `public/`, `crab_cavern/`).\n"
            "- `config/`: Local scheduler (`schedule.json`) and non-credential runtime configurations.\n"
            "- `tools/`: Private memory management utilities (`promote_memory.py`, `pack_shard.py`).\n"
            "- `secrets/secrets.tar.gz.enc`: 256-bit AES-GCM encrypted `/secrets/` archive (requires `/secrets/.vault_master.key`).\n",
            encoding="utf-8",
        )

    agents_md = repo_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            "# Zero-Vault — Agent Operational Instructions\n\n"
            "## Core Invariants\n"
            "- **Strict Confidentiality:** Never copy or expose files from this repository into public channels or `Zero-Agent`.\n"
            "- **Zero Plaintext Credentials:** Never commit unencrypted `.env`, `env.json`, `*.pem`, or OAuth tokens. Always use `python3 /workspace/tools/vault_sync.py sync`.\n"
            "- **Verification Command:** `python3 /workspace/tools/vault_sync.py verify`\n",
            encoding="utf-8",
        )


def is_zero_prime() -> bool:
    """Return True if this process is running on Zero Prime (Linux host with /app/bridge.py)."""
    env_val = os.environ.get("ZERO_IS_PRIME")
    if env_val is not None:
        return env_val.strip() in ("1", "true", "True")
    return os.name != "nt" and Path("/app/bridge.py").exists()


def _get_git_file_commit_timestamps(repo_dir: Path) -> dict[str, float]:
    """Run git log to map repo-relative POSIX paths to their latest commit epoch timestamp."""
    if not (repo_dir / ".git").exists():
        return {}
    try:
        proc = subprocess.run(
            ["git", "log", "--format=__COMMIT__%ct", "--name-only"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return {}
    except (subprocess.SubprocessError, OSError):
        return {}

    timestamps: dict[str, float] = {}
    current_ts: float | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("__COMMIT__"):
            raw_ts = line[len("__COMMIT__") :].strip()
            try:
                current_ts = float(raw_ts)
            except ValueError:
                current_ts = None
        elif current_ts is not None:
            posix_path = line.replace("\\", "/")
            if posix_path not in timestamps:
                timestamps[posix_path] = current_ts
    return timestamps


def _load_last_pull_ts(workspace_dir: Path = WORKSPACE_DIR) -> float:
    """Load epoch timestamp of last vault pull from data/last_vault_pull.json."""
    pull_info_file = workspace_dir / "data" / "last_vault_pull.json"
    if pull_info_file.exists():
        try:
            data = json.loads(pull_info_file.read_text(encoding="utf-8"))
            return float(data.get("timestamp", 0.0))
        except (ValueError, OSError):
            return 0.0
    return 0.0


def _save_last_pull_ts(sha: str, file_count: int, workspace_dir: Path = WORKSPACE_DIR) -> None:
    """Record epoch timestamp and commit SHA of completed vault pull."""
    pull_info_file = workspace_dir / "data" / "last_vault_pull.json"
    pull_info_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "sha": sha,
        "file_count": file_count,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    pull_info_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def sync_vault(
    repo_dir: Path = VAULT_REPO_DIR,
    workspace_dir: Path = WORKSPACE_DIR,
    push: bool = True,
    fetch_remote: bool = True,
    prune: bool = False,
    is_prime: bool | None = None,
) -> dict:
    """Stage durable gitignored state + AES-256-GCM encrypted secrets into Zero-Vault and push."""
    if is_prime is None:
        is_prime = is_zero_prime()

    ensure_vault_repo_initialized(repo_dir)

    # 0. Fetch remote before staging if enabled and git repo exists
    if fetch_remote and (repo_dir / ".git").exists():
        try:
            fetch_proc = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=str(repo_dir),
                capture_output=True,
                timeout=30,
            )
            if fetch_proc.returncode == 0:
                subprocess.run(
                    ["git", "checkout", "-B", "main", "origin/main"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
        except Exception:
            pass

    commit_timestamps = _get_git_file_commit_timestamps(repo_dir)

    # 1. Safe file-by-file merge of memory/
    src_mem = workspace_dir / "memory"
    dst_mem = repo_dir / "memory"
    skipped_stale_files: list[str] = []
    preserved_remote_only_files: list[str] = []

    if src_mem.exists():
        for src_file in sorted(src_mem.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_mem).as_posix()
            dst_file = dst_mem / rel

            if dst_file.exists():
                if src_file.read_bytes() == dst_file.read_bytes():
                    continue
                remote_ts = commit_timestamps.get(f"memory/{rel}", 0.0)
                if not is_prime and remote_ts > 0.0 and (src_file.stat().st_mtime + 2.0) < remote_ts:
                    skipped_stale_files.append(rel)
                    shutil.copy2(dst_file, src_file)
                else:
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
            else:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)

    if dst_mem.exists():
        for dst_file in sorted(dst_mem.rglob("*")):
            if not dst_file.is_file():
                continue
            rel = dst_file.relative_to(dst_mem).as_posix()
            src_file = src_mem / rel
            if not src_file.exists():
                if prune and is_prime:
                    dst_file.unlink()
                else:
                    preserved_remote_only_files.append(rel)
                    src_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst_file, src_file)

    # 2. Mirror schedule.json
    dst_cfg = repo_dir / "config"
    dst_cfg.mkdir(parents=True, exist_ok=True)
    sched_src = workspace_dir / "data" / "schedule.json"
    if not sched_src.exists():
        sched_src = workspace_dir / "config" / "schedule.json"
    if sched_src.exists():
        if is_prime or not (dst_cfg / "schedule.json").exists():
            shutil.copy2(sched_src, dst_cfg / "schedule.json")

    # 3. Mirror private memory tools
    dst_tools = repo_dir / "tools"
    dst_tools.mkdir(parents=True, exist_ok=True)
    for tname in ("promote_memory.py", "pack_shard.py"):
        tsrc = workspace_dir / "tools" / tname
        if tsrc.exists():
            shutil.copy2(tsrc, dst_tools / tname)

    # 4. Encrypt /secrets/ into secrets/secrets.tar.gz.enc
    enc_file = repo_dir / "secrets" / "secrets.tar.gz.enc"
    enc_info = None
    if is_prime or not enc_file.exists():
        try:
            enc_info = encrypt_secrets_archive(
                secrets_dir=SECRETS_DIR,
                output_file=enc_file,
                key_path=MASTER_KEY_FILE,
            )
        except Exception:
            pass

    # 5. Stage, commit, and push
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True)
    changed_count = len([line for line in st.stdout.splitlines() if line.strip()])
    sha = "clean"

    if changed_count > 0:
        now_pt = datetime.now(timezone.utc).astimezone(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT")
        subprocess.run(
            ["git", "commit", "-m", f"chore(vault): sync memory, schedule, and encrypted secrets ({now_pt})"],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
        )
        if push:
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(repo_dir), check=True, capture_output=True, timeout=30)

    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True)
    if rev.returncode == 0:
        sha = rev.stdout.strip()

    return {
        "ok": True,
        "repo_dir": str(repo_dir),
        "commit_sha": sha,
        "changed_files": changed_count,
        "encrypted_secrets": enc_info,
        "skipped_stale_files_count": len(skipped_stale_files),
        "skipped_stale_files": skipped_stale_files,
        "preserved_remote_only_files_count": len(preserved_remote_only_files),
        "preserved_remote_only_files": preserved_remote_only_files,
        "preserved_local_edits_count": 0,
    }


def pull_vault(
    repo_dir: Path = VAULT_REPO_DIR,
    workspace_dir: Path = WORKSPACE_DIR,
    pull_remote: bool = True,
    preserve_newer_local: bool = True,
) -> dict:
    """Pull Zero-Vault and hydrate memory/, config/schedule.json, private tools, and decrypted secrets."""
    if pull_remote and (repo_dir / ".git").exists():
        try:
            fetch_proc = subprocess.run(["git", "fetch", "origin", "main"], cwd=str(repo_dir), capture_output=True, timeout=30)
            if fetch_proc.returncode == 0:
                subprocess.run(["git", "checkout", "-B", "main", "origin/main"], cwd=str(repo_dir), check=True, capture_output=True, timeout=30)
        except Exception:
            pass

    last_pull_ts = _load_last_pull_ts(workspace_dir)
    commit_timestamps = _get_git_file_commit_timestamps(repo_dir)

    # 1. Hydrate memory/
    src_mem = repo_dir / "memory"
    dst_mem = workspace_dir / "memory"
    memory_files = 0
    preserved_local_edits: list[str] = []

    if src_mem.exists():
        for src_file in sorted(src_mem.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_mem).as_posix()
            dst_file = dst_mem / rel
            should_copy = True

            if dst_file.exists():
                if src_file.read_bytes() == dst_file.read_bytes():
                    should_copy = False
                elif preserve_newer_local:
                    remote_ts = commit_timestamps.get(f"memory/{rel}", 0.0)
                    cutoff_ts = max(last_pull_ts, remote_ts)
                    if cutoff_ts > 0.0 and dst_file.stat().st_mtime > (cutoff_ts + 2.0):
                        preserved_local_edits.append(rel)
                        should_copy = False

            if should_copy:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)

    if dst_mem.exists():
        memory_files = sum(1 for p in dst_mem.rglob("*") if p.is_file())

    # 2. Hydrate config/schedule.json (and data/schedule.json on non-Prime replicas)
    src_sched = repo_dir / "config" / "schedule.json"
    if src_sched.exists():
        dst_sched = workspace_dir / "config" / "schedule.json"
        dst_sched.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_sched, dst_sched)
        if not is_zero_prime():
            dst_data_sched = workspace_dir / "data" / "schedule.json"
            dst_data_sched.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_sched, dst_data_sched)

    # 3. Hydrate private memory tools
    src_tools = repo_dir / "tools"
    dst_tools = workspace_dir / "tools"
    dst_tools.mkdir(parents=True, exist_ok=True)
    for tname in ("promote_memory.py", "pack_shard.py"):
        tsrc = src_tools / tname
        if tsrc.exists():
            shutil.copy2(tsrc, dst_tools / tname)

    # 4. Decrypt and extract secrets/secrets.tar.gz.enc
    enc_file = repo_dir / "secrets" / "secrets.tar.gz.enc"
    extracted: list[str] = []
    if enc_file.exists():
        try:
            extracted = extract_secrets_archive(encrypted_file=enc_file)
        except Exception:
            pass

    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True)
    sha = rev.stdout.strip() if rev.returncode == 0 else "unknown"

    _save_last_pull_ts(sha=sha, file_count=memory_files, workspace_dir=workspace_dir)

    return {
        "ok": True,
        "repo_dir": str(repo_dir),
        "commit_sha": sha,
        "hydrated_memory_files": memory_files,
        "decrypted_secret_files": len(extracted),
        "secrets_dir": str(SECRETS_DIR),
        "preserved_local_edits_count": len(preserved_local_edits),
        "preserved_local_edits": preserved_local_edits,
        "skipped_stale_files_count": 0,
        "preserved_remote_only_files_count": 0,
    }


def auto_sync_workstation_if_stale(
    max_age_hours: float = 6.0,
    workspace_dir: Path = WORKSPACE_DIR,
    repo_dir: Path = VAULT_REPO_DIR,
) -> dict:
    """Debounced wake-up sync for workstation replicas. Skips on Prime or if recently pulled."""
    if is_zero_prime():
        return {"ok": True, "skipped": True, "reason": "zero_prime"}

    last_pull_ts = _load_last_pull_ts(workspace_dir)
    now = time.time()
    age_seconds = now - last_pull_ts
    if last_pull_ts > 0.0 and age_seconds < (max_age_hours * 3600):
        return {
            "ok": True,
            "skipped": True,
            "reason": "fresh",
            "age_hours": round(age_seconds / 3600.0, 2),
            "last_pull_ts": last_pull_ts,
        }

    workspace_pull_info = {}
    if (workspace_dir / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=str(workspace_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
            workspace_pull_info = {
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except Exception as e:
            workspace_pull_info = {"error": str(e)}

    vault_pull_res = pull_vault(repo_dir=repo_dir, workspace_dir=workspace_dir)

    return {
        "ok": True,
        "skipped": False,
        "workspace_pull": workspace_pull_info,
        "vault_pull": vault_pull_res,
    }


def main():
    parser = argparse.ArgumentParser(description="Zero-Vault Synchronization & Encrypted Secrets CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Sync memory, schedule, and encrypted secrets to brockventures/Zero-Vault")
    p_sync.add_argument("--no-push", action="store_true")
    p_sync.add_argument("--no-fetch", action="store_true", help="Sync without git fetch from origin")
    p_sync.add_argument("--prune", action="store_true", help="Prune deleted files from vault (Zero Prime only)")
    p_pull = sub.add_parser("pull", help="Pull brockventures/Zero-Vault and hydrate memory, schedule, and decrypted secrets")
    p_pull.add_argument("--no-fetch", action="store_true", help="Hydrate from local data/zero_vault_repo without git fetch")
    p_wake = sub.add_parser("auto-wake", help="Auto-sync workstation replica if vault pull is stale")
    p_wake.add_argument("--max-age-hours", type=float, default=6.0, help="Max age in hours before sync (default 6.0)")
    sub.add_parser("verify", help="Verify round-trip decryption of secrets/secrets.tar.gz.enc")

    args = parser.parse_args()
    if args.cmd == "sync":
        res = sync_vault(
            push=not args.no_push,
            fetch_remote=not getattr(args, "no_fetch", False),
            prune=getattr(args, "prune", False),
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "pull":
        res = pull_vault(pull_remote=not args.no_fetch)
        print(json.dumps(res, indent=2))
    elif args.cmd == "auto-wake":
        res = auto_sync_workstation_if_stale(max_age_hours=args.max_age_hours)
        print(json.dumps(res, indent=2))
    elif args.cmd == "verify":
        members = decrypt_secrets_archive()
        print(json.dumps({"ok": True, "verified_secret_files_count": len(members), "files": members}, indent=2))


if __name__ == "__main__":
    main()
