#!/usr/bin/env python3
"""
remote_transfer.py - Decoupled Remote Transfer Runner & Governor

Manages background rsync transfers across LAN / SSH targets (e.g. Steam Deck, remote host).
Enforces:
1. Bandwidth throttling (--bwlimit) to prevent flash controller I/O queue exhaustion ('D' state).
2. Idle sleep inhibition (systemd-inhibit --what=idle) to prevent deep sleep lockups.
3. SSH socket timeouts and keepalives to prevent TCP black-hole hangs.
4. Non-blocking background daemonization so Discord bridge turns never stall.
"""

import os
import sys
import json
import time
import signal
import subprocess
import shlex
import argparse
from pathlib import Path
from datetime import datetime, timezone

TRANSFERS_DIR = Path("/workspace/data/transfers")
TRANSFERS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BWLIMIT_KBPS = 30000  # 30 MB/s baseline to protect UHS-I MicroSD bus
DEFAULT_SSH_TIMEOUT = 10
DEFAULT_SSH_KEEPALIVE_INTERVAL = 15
DEFAULT_SSH_KEEPALIVE_COUNT = 3


def get_transfer_meta(transfer_id: str) -> dict | None:
    meta_file = TRANSFERS_DIR / f"{transfer_id}.json"
    if not meta_file.exists():
        return None
    try:
        with open(meta_file, "r") as f:
            return json.load(f)
    except Exception:
        return None


def write_transfer_meta(transfer_id: str, data: dict):
    meta_file = TRANSFERS_DIR / f"{transfer_id}.json"
    temp_file = TRANSFERS_DIR / f"{transfer_id}.json.tmp"
    with open(temp_file, "w") as f:
        json.dump(data, f, indent=2)
    temp_file.replace(meta_file)


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def start_transfer(
    src: str,
    dest: str,
    target_host: str = "",
    source_host: str = "",
    bwlimit: int = DEFAULT_BWLIMIT_KBPS,
    inhibit_idle: bool = True,
    description: str = "",
    extra_args: list[str] | None = None
) -> dict:
    transfer_id = f"xfer-{int(time.time())}-{os.getpid()}"
    log_file = TRANSFERS_DIR / f"{transfer_id}.log"
    
    ssh_opts = (
        f"-o ConnectTimeout={DEFAULT_SSH_TIMEOUT} "
        f"-o ServerAliveInterval={DEFAULT_SSH_KEEPALIVE_INTERVAL} "
        f"-o ServerAliveCountMax={DEFAULT_SSH_KEEPALIVE_COUNT} "
        f"-o BatchMode=yes"
    )

    rsync_path_opt = "systemd-inhibit --what=idle rsync" if inhibit_idle else "rsync"
    
    cmd_parts = [
        "rsync",
        "-av",
        "--partial",
        "-s",
        f"--bwlimit={bwlimit}",
        f"--rsync-path='{rsync_path_opt}'",
        f"-e 'ssh {ssh_opts}'"
    ]
    extra_str = (" " + " ".join(extra_args)) if extra_args else ""
    if source_host:
        remote_rsync_cmd = f"rsync -av --partial -s --bwlimit={bwlimit} --rsync-path='{rsync_path_opt}' -e 'ssh {ssh_opts}'{extra_str} '{src}' '{dest}'"
        exec_cmd = f"ssh {ssh_opts} {source_host} \"{remote_rsync_cmd}\""
    else:
        cmd_parts.append(f"'{src}'")
        cmd_parts.append(f"'{dest}'")
        exec_cmd = " ".join(cmd_parts)

    meta = {
        "id": transfer_id,
        "src": src,
        "dest": dest,
        "target_host": target_host,
        "source_host": source_host,
        "bwlimit_kbps": bwlimit,
        "inhibit_idle": inhibit_idle,
        "description": description or f"Transfer {Path(src).name} to {dest}",
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "log_path": str(log_file),
        "command": exec_cmd
    }
    write_transfer_meta(transfer_id, meta)

    with open(log_file, "w") as out:
        out.write(f"=== Transfer Started: {meta['started_at']} ===\n")
        out.write(f"Command: {exec_cmd}\n\n")
        out.flush()
        
        proc = subprocess.Popen(
            exec_cmd,
            shell=True,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    meta["pid"] = proc.pid
    write_transfer_meta(transfer_id, meta)
    return meta


def update_and_get_status(transfer_id: str) -> dict | None:
    meta = get_transfer_meta(transfer_id)
    if not meta:
        return None

    if meta["status"] == "running":
        pid = meta.get("pid", 0)
        if not is_pid_running(pid):
            log_file = Path(meta["log_path"])
            last_lines = ""
            if log_file.exists():
                try:
                    with open(log_file, "r") as f:
                        lines = f.readlines()
                        last_lines = "".join(lines[-10:])
                except Exception:
                    pass

            if "sent" in last_lines and "bytes/sec" in last_lines:
                meta["status"] = "completed"
            elif "rsync error" in last_lines.lower() or "failed" in last_lines.lower():
                meta["status"] = "failed"
            else:
                meta["status"] = "completed"

            meta["completed_at"] = datetime.now(timezone.utc).isoformat()
            write_transfer_meta(transfer_id, meta)

    log_file = Path(meta["log_path"])
    tail_content = ""
    if log_file.exists():
        try:
            with open(log_file, "r") as f:
                tail_content = "".join(f.readlines()[-5:])
        except Exception:
            pass
    meta["log_tail"] = tail_content
    return meta


def list_transfers() -> list[dict]:
    results = []
    for f in sorted(TRANSFERS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True):
        t_id = f.stem
        st = update_and_get_status(t_id)
        if st:
            results.append(st)
    return results


def cancel_transfer(transfer_id: str) -> bool:
    meta = update_and_get_status(transfer_id)
    if not meta:
        return False
    if meta["status"] != "running":
        return False
    pid = meta.get("pid", 0)
    if pid > 0 and is_pid_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    meta["status"] = "cancelled"
    meta["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_transfer_meta(transfer_id, meta)
    return True


def main():
    parser = argparse.ArgumentParser(description="Decoupled Remote Transfer Runner")
    subparsers = parser.add_subparsers(dest="action", required=True)

    start_p = subparsers.add_parser("start", help="Start background throttled transfer")
    start_p.add_argument("--src", required=True, help="Source path or pattern")
    start_p.add_argument("--dest", required=True, help="Destination path")
    start_p.add_argument("--target-host", default="", help="Target host user@ip if applicable")
    start_p.add_argument("--source-host", default="", help="Source host user@ip if running remotely (e.g. Host1)")
    start_p.add_argument("--bwlimit", type=int, default=DEFAULT_BWLIMIT_KBPS, help="Bandwidth limit in KB/s (default 30000 = 30MB/s)")
    start_p.add_argument("--no-inhibit", action="store_true", help="Disable systemd-inhibit on target")
    start_p.add_argument("--desc", default="", help="Human-readable description")
    start_p.add_argument("--extra-args", default="", help="Extra raw arguments passed to rsync (e.g. include/exclude filters)")

    status_p = subparsers.add_parser("status", help="Get transfer status")
    status_p.add_argument("--id", required=True, help="Transfer ID")

    subparsers.add_parser("list", help="List all transfers")

    cancel_p = subparsers.add_parser("cancel", help="Cancel running transfer")
    cancel_p.add_argument("--id", required=True, help="Transfer ID")

    args = parser.parse_args()

    if args.action == "start":
        extra = shlex.split(args.extra_args) if args.extra_args else None
        meta = start_transfer(
            src=args.src,
            dest=args.dest,
            target_host=args.target_host,
            source_host=args.source_host,
            bwlimit=args.bwlimit,
            inhibit_idle=not args.no_inhibit,
            description=args.desc,
            extra_args=extra
        )
        print(json.dumps(meta, indent=2))

    elif args.action == "status":
        meta = update_and_get_status(args.id)
        if not meta:
            print(f"Transfer {args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(meta, indent=2))

    elif args.action == "list":
        transfers = list_transfers()
        print(json.dumps(transfers, indent=2))

    elif args.action == "cancel":
        success = cancel_transfer(args.id)
        print(json.dumps({"id": args.id, "cancelled": success}))


if __name__ == "__main__":
    main()
