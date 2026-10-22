#!/usr/bin/env python3
"""
vault_sync.py — Zero-Vault Private Repository & Encrypted Secrets Synchronization

Synchronizes high-value gitignored state from /workspace into the private GitHub repository
`brockventures/Zero-Vault`:
1. Durable Memory & Config State (Plaintext in Private Repo):
   - /workspace/memory/ (private, vault, public, crab_cavern)
   - /workspace/config/schedule.json
   - /workspace/tools/promote_memory.py & /workspace/tools/pack_shard.py
2. Cryptographic Boundary for /secrets/ (AES-256-GCM Authenticated Encryption):
   - Never commits raw plaintext credential files (/secrets/env.json, *.pem, google_oauth.json).
   - Packs /secrets/ (excluding the master key itself) into a tar.gz archive in memory and
     encrypts it with 256-bit AES-GCM using /secrets/.vault_master.key into `secrets/secrets.tar.gz.enc`.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PT_TZ = ZoneInfo("America/Los_Angeles")
WORKSPACE_DIR = Path("/workspace")
SECRETS_DIR = Path("/secrets")
MASTER_KEY_FILE = Path("/root/.gemini/.vault_master.key")
VAULT_REPO_DIR = Path("/workspace/data/zero_vault_repo")
VAULT_REMOTE_URL = "https://github.com/brockventures/Zero-Vault.git"


def get_or_create_master_key(key_path: Path = MASTER_KEY_FILE) -> bytes:
    """Load or generate the 256-bit (32-byte) AES-GCM master key."""
    for candidate in (key_path, SECRETS_DIR / ".vault_master.key", MASTER_KEY_FILE):
        if candidate.exists():
            raw = candidate.read_text(encoding="utf-8").strip()
            key = base64.urlsafe_b64decode(raw.encode("ascii"))
            if len(key) == 32:
                return key
    key = AESGCM.generate_key(bit_length=256)
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(encoded + "\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
    except OSError:
        MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MASTER_KEY_FILE.write_text(encoded + "\n", encoding="utf-8")
        os.chmod(MASTER_KEY_FILE, 0o600)
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


def decrypt_secrets_archive(
    encrypted_file: Path = VAULT_REPO_DIR / "secrets" / "secrets.tar.gz.enc",
    key_path: Path = MASTER_KEY_FILE,
) -> list[str]:
    """Verify/decrypt `secrets.tar.gz.enc` and return list of contained member filenames."""
    key = get_or_create_master_key(key_path)
    raw = encrypted_file.read_bytes()
    if not raw.startswith(b"ZVLT1") or len(raw) < 18:
        raise ValueError("Invalid ZVLT1 encrypted archive header.")
    nonce = raw[5:17]
    ciphertext = raw[17:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, b"zero-vault-secrets-v1")
    buf = io.BytesIO(plaintext)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        return tar.getnames()


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


def sync_vault(
    repo_dir: Path = VAULT_REPO_DIR,
    push: bool = True,
) -> dict:
    """Stage durable gitignored state + AES-256-GCM encrypted secrets into Zero-Vault and push."""
    ensure_vault_repo_initialized(repo_dir)

    # 1. Mirror /workspace/memory/
    src_mem = WORKSPACE_DIR / "memory"
    dst_mem = repo_dir / "memory"
    if src_mem.exists():
        if dst_mem.exists():
            shutil.rmtree(dst_mem)
        shutil.copytree(src_mem, dst_mem)

    # 2. Mirror /workspace/config/schedule.json
    dst_cfg = repo_dir / "config"
    dst_cfg.mkdir(parents=True, exist_ok=True)
    sched_src = WORKSPACE_DIR / "config" / "schedule.json"
    if sched_src.exists():
        shutil.copy2(sched_src, dst_cfg / "schedule.json")

    # 3. Mirror private memory tools
    dst_tools = repo_dir / "tools"
    dst_tools.mkdir(parents=True, exist_ok=True)
    for tname in ("promote_memory.py", "pack_shard.py"):
        tsrc = WORKSPACE_DIR / "tools" / tname
        if tsrc.exists():
            shutil.copy2(tsrc, dst_tools / tname)

    # 4. Encrypt /secrets/ into secrets/secrets.tar.gz.enc
    enc_info = encrypt_secrets_archive(output_file=repo_dir / "secrets" / "secrets.tar.gz.enc")

    # 5. Stage, commit, and push
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True)
    changed_count = len([l for l in st.stdout.splitlines() if l.strip()])
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
    }


def main():
    parser = argparse.ArgumentParser(description="Zero-Vault Synchronization & Encrypted Secrets CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Sync memory, schedule, and encrypted secrets to brockventures/Zero-Vault")
    p_sync.add_argument("--no-push", action="store_true")
    sub.add_parser("verify", help="Verify round-trip decryption of secrets/secrets.tar.gz.enc")

    args = parser.parse_args()
    if args.cmd == "sync":
        res = sync_vault(push=not args.no_push)
        print(json.dumps(res, indent=2))
    elif args.cmd == "verify":
        members = decrypt_secrets_archive()
        print(json.dumps({"ok": True, "verified_secret_files_count": len(members), "files": members}, indent=2))


if __name__ == "__main__":
    main()
