#!/usr/bin/env python3
"""
postgres_update_check.py — Monthly PostgreSQL minor-patch checker & backup-first upgrader.

Monitors pinned major PostgreSQL containers on Host 1 (postgres-arr, 16-alpine) and
Host 2 (baseball_db, 15) against Docker Hub linux/amd64 digests, enforces 180-day EOL
warnings, stages one atomic FORGE Decision Card per outdated host in #zero-ops, and
executes pg_dumpall + gzip verification (local & USB) before scoped docker compose
pull && up -d.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import posixpath
import re
import shlex
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

for _p in [
    str(Path(__file__).resolve().parent.parent),
    str(Path(__file__).resolve().parent),
    "/workspace",
    "/workspace/tools",
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
    from tools.nas_config import (
        DOCKER_ROOT,
        HOST_1_IP,
        HOST_2_DOCKER_ROOT,
        HOST_2_IP,
        run_ssh,
    )
except ImportError:
    import nas_config  # type: ignore
    from nas_config import (  # type: ignore
        DOCKER_ROOT,
        HOST_1_IP,
        HOST_2_DOCKER_ROOT,
        HOST_2_IP,
        run_ssh,
    )

_ORIG_HOST_1_IP = HOST_1_IP
_ORIG_HOST_2_IP = HOST_2_IP
_ORIG_RUN_SSH = run_ssh

SYNOLOGY_PATH = "/usr/local/bin:/usr/bin:/bin:/volume1/@appstore/ContainerManager/usr/bin:$PATH"
DOCKER_HUB_TAG_URL = "https://hub.docker.com/v2/repositories/library/postgres/tags/{tag}"
PG_ISREADY_TIMEOUT_SECONDS = 45
PG_ISREADY_POLL_INTERVAL = 3.0

POSTGRES_TARGETS: dict[str, dict[str, str]] = {
    "host1": {
        "label": "Host 1 Arr DB",
        "host_attr": "HOST_1_IP",
        "container": "postgres-arr",
        "compose_dir": os.path.join(DOCKER_ROOT, "appdata"),
        "service": "postgres-arr",
        "tag": "16-alpine",
        "pg_user": "postgres",
        "eol_date": "2028-11-09",
        "local_backup_dir": os.environ.get(
            "PG_HOST1_BACKUP_DIR",
            os.path.join(os.sep, "volume1", "data", "backups", "postgres"),
        ),
        "usb_backup_dir": "/volumeUSB1/usbshare/backups/postgres",
        "backup_env_var": "PG_HOST1_BACKUP_DIR",
    },
    "host2": {
        "label": "Host 2 Baseball DB",
        "host_attr": "HOST_2_IP",
        "container": "baseball_db",
        "compose_dir": os.path.join(HOST_2_DOCKER_ROOT, "baseball"),
        "service": "db",
        "tag": "15",
        "pg_user": "myuser",
        "eol_date": "2027-11-11",
        "local_backup_dir": os.environ.get(
            "PG_HOST2_BACKUP_DIR",
            os.path.join(HOST_2_DOCKER_ROOT, "baseball", "backups", "postgres"),
        ),
        "usb_backup_dir": "/volumeUSB1/usbshare/backups/postgres",
        "backup_env_var": "PG_HOST2_BACKUP_DIR",
    },
}


def _get_host_ip(target: dict[str, str]) -> str:
    """Resolve target NAS host IP dynamically from nas_config."""
    attr = target["host_attr"]
    if attr == "HOST_1_IP" and HOST_1_IP != _ORIG_HOST_1_IP:
        return HOST_1_IP
    if attr == "HOST_2_IP" and HOST_2_IP != _ORIG_HOST_2_IP:
        return HOST_2_IP
    return str(getattr(nas_config, attr, "127.0.0.1"))


def _ssh(host_ip: str, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    """Execute a remote command via nas_config.run_ssh (or patched module run_ssh)."""
    if run_ssh is not _ORIG_RUN_SSH:
        return run_ssh(host_ip, cmd, timeout=timeout)
    return nas_config.run_ssh(host_ip, cmd, timeout=timeout)


def get_hub_tag_digests(tag: str) -> tuple[set[str], str]:
    """Fetch Docker Hub official postgres tag digests (top-level + linux/amd64) and last_updated date."""
    url = DOCKER_HUB_TAG_URL.format(tag=tag)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError, ValueError):
        return set(), ""

    if not isinstance(data, dict):
        return set(), ""

    digests: set[str] = set()
    top_digest = str(data.get("digest") or "").strip()
    if top_digest:
        digests.add(top_digest)

    for img in data.get("images") or []:
        if not isinstance(img, dict):
            continue
        if img.get("architecture") == "amd64" and img.get("os") == "linux":
            img_digest = str(img.get("digest") or "").strip()
            if img_digest:
                digests.add(img_digest)

    last_updated = str(data.get("last_updated") or "")[:10]
    return digests, last_updated


def get_host_image_digests(host_ip: str, container: str) -> set[str]:
    """Extract sha256 digests from the running container's image RepoDigests over SSH."""
    q_container = shlex.quote(container)
    cmd = (
        f"export PATH={SYNOLOGY_PATH}; "
        f"img_id=$(docker inspect -f '{{{{.Image}}}}' {q_container} 2>/dev/null || true); "
        f'if [ -n "$img_id" ]; then docker inspect -f \'{{{{json .RepoDigests}}}}\' "$img_id" 2>/dev/null || true; fi'
    )
    rc, stdout, _ = _ssh(host_ip, cmd, timeout=30)
    if rc != 0 or not stdout:
        return set()
    return set(re.findall(r"sha256:[A-Za-z0-9_-]+", stdout))


def get_host_pg_version(host_ip: str, container: str, pg_user: str) -> str:
    """Query PostgreSQL version via SELECT version() inside the running container (fallback postgres --version)."""
    q_container = shlex.quote(container)
    q_user = shlex.quote(pg_user)
    cmd = (
        f"export PATH={SYNOLOGY_PATH}; "
        f"out=$(docker exec {q_container} psql -U {q_user} -d postgres -tAc 'SELECT version();' 2>/dev/null || true); "
        f'if [ -z "$out" ]; then out=$(docker exec {q_container} psql -U {q_user} -tAc \'SELECT version();\' 2>/dev/null || true); fi; '
        f'if [ -z "$out" ]; then out=$(docker exec {q_container} postgres --version 2>/dev/null || true); fi; '
        f'printf "%s\\n" "$out"'
    )
    rc, stdout, _ = _ssh(host_ip, cmd, timeout=30)
    if rc != 0 or not stdout:
        return "unknown"
    for line in stdout.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return "unknown"


def check_eol_warning(
    eol_date_str: str,
    today: datetime.date | datetime.datetime | str | None = None,
    warn_days: int = 180,
) -> tuple[bool, int]:
    """Return (within_window, days_remaining) relative to the PostgreSQL major version EOL date."""
    eol_date = datetime.date.fromisoformat(str(eol_date_str).strip())
    if today is None:
        today_date = datetime.date.today()
    elif isinstance(today, datetime.datetime):
        today_date = today.date()
    elif isinstance(today, str):
        today_date = datetime.date.fromisoformat(today.strip())
    else:
        today_date = today

    days_remaining = (eol_date - today_date).days
    return (days_remaining <= int(warn_days), days_remaining)


def check_updates(quiet: bool = False) -> dict[str, Any]:
    """Check all PostgreSQL targets for minor-patch updates and stage one Decision Card per outdated host."""
    try:
        from tools.forge import ZERO_OPS_CHANNEL_ID, stage_decision
    except ImportError:
        from forge import ZERO_OPS_CHANNEL_ID, stage_decision  # type: ignore

    results: dict[str, Any] = {}
    outdated_lines: list[str] = []
    status_lines: list[str] = []
    all_staged_cleanly = True
    any_outdated = False

    for host_key, target in POSTGRES_TARGETS.items():
        host_ip = _get_host_ip(target)
        tag = target["tag"]
        container = target["container"]
        service = target["service"]
        label = target["label"]
        eol_date = target["eol_date"]

        hub_digests, updated = get_hub_tag_digests(tag)
        host_digests = get_host_image_digests(host_ip, container)
        pg_version = get_host_pg_version(host_ip, container, target["pg_user"])
        eol_warn, days_remaining = check_eol_warning(eol_date)

        is_outdated = bool(hub_digests and host_digests and host_digests.isdisjoint(hub_digests))
        staged_info = None
        stage_error = None

        if is_outdated:
            any_outdated = True
            hub_preview = min(hub_digests)[:19]
            running_preview = min(host_digests)[:19]
            eol_notice = ""
            if eol_warn:
                eol_notice = (
                    f"\n⚠️ **EOL Warning:** `postgres:{tag}` reaches End-of-Life on `{eol_date}` "
                    f"({days_remaining} days remaining)."
                )

            title = f"Backup & Patch {label} (postgres:{tag})"
            summary = (
                f"**{label}** (`{container}`, pinned `postgres:{tag}`) has a newer minor patch on Docker Hub "
                f"(updated `{updated or 'unknown'}`, hub `{hub_preview}...`, running `{running_preview}...`).\n"
                f"• **Running Version:** `{pg_version}`"
                f"{eol_notice}\n\n"
                f"**Approve Action:** Clicking Approve creates a logical `pg_dumpall` backup (local + USB) with "
                f"`gzip -t` verification (aborting safely before any container changes if backup fails), runs scoped "
                f"`docker compose pull {service} && docker compose up -d {service}` on pinned `postgres:{tag}` only, "
                f"and verifies readiness via `pg_isready`."
            )

            try:
                staged_info = stage_decision(
                    category="ops_upgrade",
                    title=title,
                    summary=summary,
                    handler_type="shell",
                    payload={
                        "command": f"python3 /workspace/tools/postgres_update_check.py upgrade --host {host_key}",
                        "cwd": "/workspace",
                    },
                    channel_id=ZERO_OPS_CHANNEL_ID,
                    post_to_discord=True,
                )
            except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error, urllib.error.URLError) as exc:
                all_staged_cleanly = False
                stage_error = str(exc)

            line = (
                f"• **{label}** (`{container}` / `postgres:{tag}`): patch available "
                f"(Docker Hub `{updated or 'unknown'}`, running `{pg_version}`)"
            )
            if eol_warn:
                line += f" ⚠️ EOL {eol_date} ({days_remaining}d)"
            if stage_error:
                line += f" — ❌ failed to stage Decision Card: {stage_error}"
            outdated_lines.append(line)
        else:
            state = "up to date" if (hub_digests and host_digests) else "check skipped (digest unavailable)"
            line = f"• **{label}** (`{container}` / `postgres:{tag}`): {state} (`{pg_version}`)"
            if eol_warn:
                line += f" ⚠️ EOL {eol_date} ({days_remaining}d)"
            status_lines.append(line)

        results[host_key] = {
            "label": label,
            "container": container,
            "tag": tag,
            "pg_version": pg_version,
            "hub_digests": sorted(hub_digests),
            "host_digests": sorted(host_digests),
            "hub_updated": updated,
            "outdated": is_outdated,
            "eol_warning": eol_warn,
            "days_until_eol": days_remaining,
            "decision": staged_info,
            "stage_error": stage_error,
        }

    if any_outdated:
        if not (quiet and all_staged_cleanly):
            print("🐘 **PostgreSQL Minor-Patch Updates Available:**\n" + "\n".join(outdated_lines))
    elif not quiet:
        print("✅ **PostgreSQL containers are up to date:**\n" + "\n".join(status_lines))

    return results


def perform_upgrade(host_key: str) -> int:
    """Run backup-first pg_dumpall + gzip verification, scoped compose pull/up, and pg_isready check."""
    if host_key not in POSTGRES_TARGETS:
        print(f"❌ Unknown PostgreSQL host target '{host_key}'. Valid options: {', '.join(sorted(POSTGRES_TARGETS))}")
        return 1

    target = POSTGRES_TARGETS[host_key]
    host_ip = _get_host_ip(target)
    container = target["container"]
    compose_dir = target["compose_dir"]
    service = target["service"]
    tag = target["tag"]
    pg_user = target["pg_user"]
    label = target["label"]
    local_backup_dir = os.environ.get(target["backup_env_var"], target["local_backup_dir"])
    usb_backup_dir = target["usb_backup_dir"]

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{container}_{ts}.sql.gz"
    local_backup_file = posixpath.join(local_backup_dir, backup_filename)
    usb_backup_file = posixpath.join(usb_backup_dir, backup_filename)

    print(f"🐘 **Patching {label}** (`{container}` / `postgres:{tag}`)...")

    # (a) Backup-first with non-empty + gzip -t verification BEFORE touching Docker
    q_local_dir = shlex.quote(local_backup_dir)
    q_usb_dir = shlex.quote(usb_backup_dir)
    q_local_file = shlex.quote(local_backup_file)
    q_usb_file = shlex.quote(usb_backup_file)
    q_container = shlex.quote(container)
    q_user = shlex.quote(pg_user)

    backup_cmd = (
        f"export PATH={SYNOLOGY_PATH}; "
        f"mkdir -p {q_local_dir} {q_usb_dir} && "
        f"rc_file=$(mktemp) && "
        f'(docker exec {q_container} pg_dumpall -U {q_user}; echo $? > "$rc_file") | gzip > {q_local_file} && '
        f'dump_rc=$(cat "$rc_file") && rm -f "$rc_file" && '
        f'[ "$dump_rc" = "0" ] && '
        f"test -s {q_local_file} && "
        f"gzip -t {q_local_file} && "
        f'[ "$(gzip -dc {q_local_file} | head -c 1 | wc -c)" -gt 0 ] && '
        f"cp {q_local_file} {q_usb_file} && "
        f"gzip -t {q_usb_file} && "
        f"ls -lh {q_local_file} | awk '{{print $5}}'"
    )

    print("1. Creating and verifying logical pg_dumpall backup (local + USB)...")
    rc_bak, out_bak, err_bak = _ssh(host_ip, backup_cmd, timeout=300)
    backup_size = (out_bak or "").strip().splitlines()[-1].strip() if (out_bak or "").strip() else ""
    if rc_bak != 0 or not backup_size or backup_size in ("0", "0B"):
        detail = (err_bak or out_bak or f"exit {rc_bak}").strip()
        print(f"❌ Backup or gzip verification failed; aborting before Docker changes: {detail}")
        return 1

    print(f"   ✅ Backup verified ({backup_size}): {local_backup_file} & {usb_backup_file}")

    # (b) Scoped docker compose pull & up -d
    q_compose_dir = shlex.quote(compose_dir)
    q_service = shlex.quote(service)
    compose_cmd = (
        f"export PATH={SYNOLOGY_PATH}; "
        f"cd {q_compose_dir} && docker compose pull {q_service} && docker compose up -d {q_service}"
    )

    print(f"2. Pulling and recreating pinned service `{service}` in `{compose_dir}`...")
    rc_comp, out_comp, err_comp = _ssh(host_ip, compose_cmd, timeout=300)
    if rc_comp != 0:
        detail = (err_comp or out_comp or f"exit {rc_comp}").strip()
        print(f"❌ docker compose pull/up failed: {detail}")
        return 1

    # (c) Poll pg_isready up to 45s
    print("3. Waiting for PostgreSQL readiness (pg_isready, up to 45s)...")
    ready_cmd = f"export PATH={SYNOLOGY_PATH}; docker exec {q_container} pg_isready -U {q_user}"
    deadline = time.monotonic() + PG_ISREADY_TIMEOUT_SECONDS
    is_ready = False
    while True:
        rc_ready, _, _ = _ssh(host_ip, ready_cmd, timeout=15)
        if rc_ready == 0:
            is_ready = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(PG_ISREADY_POLL_INTERVAL, remaining))

    if not is_ready:
        print(f"❌ PostgreSQL container `{container}` did not pass pg_isready within {PG_ISREADY_TIMEOUT_SECONDS}s.")
        return 1

    new_version = get_host_pg_version(host_ip, container, pg_user)
    print(
        f"✅ **{label} patched and ready!**\n"
        f"• **PostgreSQL Version:** `{new_version}`\n"
        f"• **Verified Backup:** `{local_backup_file}` ({backup_size})\n"
        f"• **USB Mirror:** `{usb_backup_file}`"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint supporting `check [--quiet]` and `upgrade --host <host1|host2>`."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        args_list = ["check"]

    parser = argparse.ArgumentParser(description="PostgreSQL minor-patch checker and backup-first upgrader.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Check Host 1 and Host 2 PostgreSQL image digests.")
    check_parser.add_argument("--quiet", action="store_true", help="Suppress output when up to date or staged cleanly.")

    upgrade_parser = subparsers.add_parser("upgrade", help="Backup-first minor-patch upgrade for one target host.")
    upgrade_parser.add_argument(
        "--host",
        required=True,
        choices=sorted(POSTGRES_TARGETS.keys()),
        help="Target host key (host1 or host2).",
    )

    parsed = parser.parse_args(args_list)
    if parsed.command == "check":
        check_updates(quiet=bool(parsed.quiet))
        return 0
    if parsed.command == "upgrade":
        return perform_upgrade(parsed.host)
    return 1


if __name__ == "__main__":
    sys.exit(main())
