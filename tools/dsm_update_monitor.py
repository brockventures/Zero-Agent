#!/usr/bin/env python3
# ruff: noqa
"""
dsm_update_monitor.py — Synology DSM Update Monitor, Schedule Drift Remediator & Host 2 Pixel ADB Watchdog.

Features:
1. Checks /etc.defaults/VERSION and runs /usr/syno/sbin/synoupgrade --check (via privileged alpine
   container chroot /host /usr/syno/sbin/synoupgrade --check) on Host 1 and Host 2 using nas_config.
2. Checks /usr/syno/etc/synoschedule.d/root/1.task on both hosts and automatically remediates drift
   if action=#schedule:dsm_autoupdate_hotfix# is found by switching it to action=#schedule:dsm_autoupdate_notify#.
3. When an update is available (available: true), stages an atomic SQLite Decision Card (Approve | Dismiss)
   in #zero-chat (1542081375287640084) via tools/forge.py decision card API (handler "shell_cmd",
   command "python3 /workspace/tools/dsm_update_monitor.py --apply --host <1|2>").
4. Supports --check (default), --apply --host <1|2>, and --ensure-adb.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

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
except ImportError:
    import nas_config  # type: ignore

ZERO_CHAT_CHANNEL_ID = 1542081375287640084
PIXEL_SERIAL = "36261FDJG0003L"
HOTFIX_ACTION = "action=#schedule:dsm_autoupdate_hotfix#"
NOTIFY_ACTION = "action=#schedule:dsm_autoupdate_notify#"
SCHEDULE_TASK_PATH = "/usr/syno/etc/synoschedule.d/root/1.task"
PIXEL_ADB_RC_PATH = "/usr/local/etc/rc.d/pixel_adb.sh"


def parse_version_file(content: str) -> dict[str, Any]:
    """Parse /etc.defaults/VERSION key="value" content into a dictionary and formatted version string."""
    parsed: dict[str, str] = {}
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        parsed[k.strip()] = v.strip().strip('"').strip("'")

    os_name = parsed.get("os_name", "DSM") or "DSM"
    product_ver = parsed.get("productversion", "")
    build_num = parsed.get("buildnumber", "")
    smallfix = parsed.get("smallfixnumber") or parsed.get("nano") or "0"

    if product_ver and build_num:
        base_str = f"{os_name} {product_ver}-{build_num}"
        if smallfix.isdigit() and int(smallfix) > 0:
            formatted = f"{base_str} Update {int(smallfix)}"
        else:
            formatted = base_str
    else:
        formatted = "unknown"

    return {
        "raw": parsed,
        "os_name": os_name,
        "productversion": product_ver,
        "buildnumber": build_num,
        "smallfixnumber": smallfix,
        "current_version": formatted,
    }


def parse_synoupgrade_check(output: str) -> dict[str, Any]:
    """Parse output from /usr/syno/sbin/synoupgrade --check."""
    result: dict[str, Any] = {
        "available": False,
        "version": "",
        "reboot": "",
        "restart": "",
        "type": "",
        "version_details": {},
    }
    if not output:
        return result

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Result:"):
            json_part = line[len("Result:") :].strip()
            try:
                data = json.loads(json_part)
                if isinstance(data, dict):
                    result.update(data)
                    result["available"] = bool(data.get("available", False))
                    return result
            except Exception:
                pass
        elif line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and "available" in data:
                    result.update(data)
                    result["available"] = bool(data.get("available", False))
                    return result
            except Exception:
                pass

    match = re.search(r"Result:\s*(\{.*?\})", output, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                result.update(data)
                result["available"] = bool(data.get("available", False))
        except Exception:
            pass

    return result


def check_and_remediate_schedule_task(
    host_ip: str,
    task_content: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Check /usr/syno/etc/synoschedule.d/root/1.task on host_ip and remediate drift if
    action=#schedule:dsm_autoupdate_hotfix# is found by switching it to action=#schedule:dsm_autoupdate_notify#.
    """
    if task_content is None:
        _, out, _ = nas_config.run_ssh(
            host_ip,
            f"cat {SCHEDULE_TASK_PATH} 2>/dev/null || "
            f"PATH=/usr/local/bin:/usr/bin:/bin:$PATH docker run --rm --privileged -v /:/host alpine "
            f"chroot /host cat {SCHEDULE_TASK_PATH} 2>/dev/null",
        )
        task_content = out or ""

    if HOTFIX_ACTION in task_content:
        remediate_cmd = (
            "PATH=/usr/local/bin:/usr/bin:/bin:$PATH "
            "docker run --rm --privileged -v /:/host alpine "
            f"chroot /host sed -i 's/{HOTFIX_ACTION}/{NOTIFY_ACTION}/g' {SCHEDULE_TASK_PATH}"
        )
        nas_config.run_ssh(host_ip, remediate_cmd)
        return True, NOTIFY_ACTION

    for line in task_content.splitlines():
        if line.strip().startswith("action="):
            return False, line.strip()
    return False, ""


def stage_dsm_update_decision(
    host_num: int,
    host_ip: str,
    current_version: str,
    target_version: str,
    channel_id: int = ZERO_CHAT_CHANNEL_ID,
    post_to_discord: bool = True,
) -> dict[str, Any]:
    """Stage an atomic SQLite Decision Card in #zero-chat via tools/forge.py decision card API."""
    cmd = f"python3 /workspace/tools/dsm_update_monitor.py --apply --host {host_num}"
    title = f"Apply Synology DSM Update on Host {host_num} ({target_version})"
    summary = (
        f"Synology **Host {host_num}** (`{current_version}`) has DSM update **{target_version}** ready to install.\n\n"
        f"**Approve Action:** Clicking Approve downloads and applies the DSM update ({target_version}) on Host {host_num} and reboots that NAS."
    )
    payload = {"command": cmd}

    from tools import forge

    return forge.stage_decision(
        category="dsm_update",
        title=title,
        summary=summary,
        handler_type="shell_cmd",
        payload=payload,
        channel_id=int(channel_id),
        post_to_discord=post_to_discord,
    )


def check_host_dsm(
    host_num: int,
    host_ip: str,
    stage_card: bool = True,
    post_to_discord: bool = True,
    channel_id: int = ZERO_CHAT_CHANNEL_ID,
) -> dict[str, Any]:
    """Check DSM version, 1.task schedule drift, and synoupgrade --check on a single NAS host."""
    probe_cmd = (
        "cat /etc.defaults/VERSION 2>/dev/null; "
        "echo '===TASK_SPLIT==='; "
        f"cat {SCHEDULE_TASK_PATH} 2>/dev/null; "
        "echo '===UPGRADE_SPLIT==='; "
        "PATH=/usr/local/bin:/usr/bin:/bin:$PATH "
        "docker run --rm --privileged -v /:/host alpine "
        "chroot /host /usr/syno/sbin/synoupgrade --check 2>&1"
    )
    rc, stdout, stderr = nas_config.run_ssh(host_ip, probe_cmd)
    out = stdout or ""

    if "===TASK_SPLIT===" in out and "===UPGRADE_SPLIT===" in out:
        ver_part, rest = out.split("===TASK_SPLIT===", 1)
        task_part, upg_part = rest.split("===UPGRADE_SPLIT===", 1)
    else:
        ver_part = out
        task_part = out
        upg_part = out

    ver_info = parse_version_file(ver_part)
    remediated, task_action = check_and_remediate_schedule_task(host_ip, task_content=task_part)
    upgrade_info = parse_synoupgrade_check(upg_part)

    available = bool(upgrade_info.get("available", False))
    target_version = str(upgrade_info.get("version") or "").strip()
    current_version = ver_info["current_version"]

    decision_res: Optional[dict[str, Any]] = None
    if available and stage_card:
        decision_res = stage_dsm_update_decision(
            host_num=host_num,
            host_ip=host_ip,
            current_version=current_version,
            target_version=target_version or "New DSM Update",
            channel_id=channel_id,
            post_to_discord=post_to_discord,
        )

    return {
        "host_num": host_num,
        "host_ip": host_ip,
        "rc": rc,
        "current_version": current_version,
        "version_info": ver_info,
        "schedule_drift_remediated": remediated,
        "schedule_action": task_action,
        "available": available,
        "target_version": target_version,
        "upgrade_info": upgrade_info,
        "decision": decision_res,
        "stderr": stderr if rc != 0 else "",
    }


def check_all_hosts(
    stage_card: bool = True,
    post_to_discord: bool = True,
    channel_id: int = ZERO_CHAT_CHANNEL_ID,
) -> dict[str, Any]:
    """Run DSM update check and schedule drift verification across Host 1 and Host 2."""
    host1_ip = getattr(nas_config, "HOST_1_IP", "127.0.0.1")
    host2_ip = getattr(nas_config, "HOST_2_IP", "127.0.0.1")

    h1 = check_host_dsm(
        host_num=1,
        host_ip=host1_ip,
        stage_card=stage_card,
        post_to_discord=post_to_discord,
        channel_id=channel_id,
    )
    h2 = check_host_dsm(
        host_num=2,
        host_ip=host2_ip,
        stage_card=stage_card,
        post_to_discord=post_to_discord,
        channel_id=channel_id,
    )
    return {"host1": h1, "host2": h2}


def apply_dsm_update(host_num: int) -> dict[str, Any]:
    """
    Download and start the DSM update in the background on the target host (1 or 2) via chroot.
    Runs synoupgrade --download then synoupgrade --start in background via chroot, with automatic
    fallback to synowebapi SYNO.Core.Upgrade.Server.Download + SYNO.Core.Upgrade start on DSM 7.3+.
    """
    if int(host_num) == 1:
        host_ip = getattr(nas_config, "HOST_1_IP", "127.0.0.1")
    elif int(host_num) == 2:
        host_ip = getattr(nas_config, "HOST_2_IP", "127.0.0.1")
    else:
        raise ValueError(f"Invalid host number: {host_num} (expected 1 or 2)")

    inner_upgrade_script = (
        "(/usr/syno/sbin/synoupgrade --download && /usr/syno/sbin/synoupgrade --start) || ("
        "/usr/syno/bin/synowebapi --exec api=SYNO.Core.Upgrade.Server method=check version=1 && "
        "/usr/syno/bin/synowebapi --exec api=SYNO.Core.Upgrade.Server.Download method=start version=1 && "
        "for i in $(seq 1 90); do "
        "sleep 2; "
        "p=$(/usr/syno/bin/synowebapi --exec api=SYNO.Core.Upgrade.Server.Download method=progress version=1 2>/dev/null); "
        "case \"$p\" in *'\"downloading\" : false'*|*'\"downloading\":false'*|*'\"progress\" : 100'*|*'\"progress\":100'*) break ;; esac; "
        "done && "
        "(/usr/syno/bin/synowebapi --exec api=SYNO.Core.Upgrade method=start version=1 || /usr/syno/sbin/synoupgrade --autoupdate=1)"
        ")"
    )

    remote_cmd = (
        "PATH=/usr/local/bin:/usr/bin:/bin:$PATH "
        "nohup docker run --rm --privileged -v /:/host alpine "
        f"chroot /host sh -c '{inner_upgrade_script}' "
        ">/tmp/dsm_upgrade_apply.log 2>&1 </dev/null &"
    )
    rc, stdout, stderr = nas_config.run_ssh(host_ip, remote_cmd)
    return {
        "ok": rc == 0,
        "host_num": int(host_num),
        "host_ip": host_ip,
        "rc": rc,
        "stdout": (stdout or "").strip(),
        "stderr": (stderr or "").strip(),
        "command": remote_cmd,
    }


def build_pixel_adb_rc_script(docker_root: Optional[str] = None) -> str:
    """Build the canonical /usr/local/etc/rc.d/pixel_adb.sh script content for Host 2."""
    root_dir = docker_root or getattr(nas_config, "DOCKER_ROOT", "/docker")
    adb_home = f"{root_dir}/discord-agy-agent/workspace/data/adb"
    adb_key = f"{adb_home}/adbkey"
    adb_bin = f"{root_dir}/discord-agy-agent/workspace/tools/bin/adb"
    return (
        "#!/bin/sh\n"
        'case "$1" in\n'
        '    start|"")\n'
        "        chmod -R 666 /dev/bus/usb/* >/dev/null 2>&1 || true\n"
        f"        export HOME={adb_home}\n"
        f"        export ADB_VENDOR_KEYS={adb_key}\n"
        f"        {adb_bin} kill-server >/dev/null 2>&1 || true\n"
        f"        nohup {adb_bin} -a -P 5037 fork-server server >/tmp/adb_host.log 2>&1 &\n"
        "        sleep 2\n"
        f"        {adb_bin} -H 127.0.0.1 -P 5037 -s {PIXEL_SERIAL} forward tcp:9223 localabstract:chrome_devtools_remote >/dev/null 2>&1 || true\n"
        "        ;;\n"
        "    stop)\n"
        f"        {adb_bin} kill-server >/dev/null 2>&1 || true\n"
        "        ;;\n"
        "esac\n"
    )


def ensure_host2_adb(host_ip: Optional[str] = None) -> dict[str, Any]:
    """
    Ensure Host 2 USB permissions (chmod -R 666 /dev/bus/usb/*) via alpine container,
    update /usr/local/etc/rc.d/pixel_adb.sh to include chmod -R 666 /dev/bus/usb/*,
    start persistent Host 2 ADB server on :5037, and set tcp:9223 forward for 36261FDJG0003L.
    """
    target_ip = host_ip or getattr(nas_config, "HOST_2_IP", "127.0.0.1")
    docker_root = getattr(nas_config, "DOCKER_ROOT", "/docker")
    adb_home = f"{docker_root}/discord-agy-agent/workspace/data/adb"
    adb_key = f"{adb_home}/adbkey"
    adb_bin = f"{docker_root}/discord-agy-agent/workspace/tools/bin/adb"

    rc_script = build_pixel_adb_rc_script(docker_root=docker_root)
    import base64

    b64_rc = base64.b64encode(rc_script.encode("utf-8")).decode("ascii")

    remote_cmd = (
        "PATH=/usr/local/bin:/usr/bin:/bin:$PATH "
        "docker run --rm --privileged -v /:/host alpine chroot /host sh -c \""
        "chmod -R 666 /dev/bus/usb/* && "
        f"echo '{b64_rc}' | base64 -d > {PIXEL_ADB_RC_PATH} && "
        f"chmod 755 {PIXEL_ADB_RC_PATH}"
        "\" && "
        f"export HOME={adb_home} ADB_VENDOR_KEYS={adb_key} && "
        f"devs=$({adb_bin} -H 127.0.0.1 -P 5037 devices 2>/dev/null || true); "
        f"case \"$devs\" in *{PIXEL_SERIAL}*device*) ;; *) "
        f"{adb_bin} kill-server >/dev/null 2>&1 || true; "
        f"nohup {adb_bin} -a -P 5037 fork-server server >/tmp/adb_host.log 2>&1 </dev/null & "
        "sleep 2 ;; esac && "
        f"{adb_bin} -H 127.0.0.1 -P 5037 -s {PIXEL_SERIAL} forward tcp:9223 localabstract:chrome_devtools_remote 2>&1 || true; "
        f"{adb_bin} -H 127.0.0.1 -P 5037 devices -l 2>&1; "
        f"{adb_bin} -H 127.0.0.1 -P 5037 forward --list 2>&1"
    )
    rc, stdout, stderr = nas_config.run_ssh(target_ip, remote_cmd)
    out = (stdout or "").strip()
    return {
        "ok": rc == 0,
        "host_ip": target_ip,
        "rc": rc,
        "device_online": PIXEL_SERIAL in out and "device" in out,
        "forward_active": "tcp:9223" in out,
        "stdout": out,
        "stderr": (stderr or "").strip(),
    }


def format_check_summary(results: dict[str, Any], adb_res: Optional[dict[str, Any]] = None) -> str:
    """Format a concise human-readable summary of DSM update status across Host 1 and Host 2."""
    lines: list[str] = []
    for key, label in (("host1", "Host 1 (.82)"), ("host2", "Host 2 (.84)")):
        info = results.get(key, {})
        cur = info.get("current_version", "unknown")
        avail = info.get("available", False)
        target = info.get("target_version", "")
        remediated = info.get("schedule_drift_remediated", False)
        dec = info.get("decision") or {}
        dec_id = dec.get("decision_id", "")

        status_str = f"Update available -> **{target}**" if avail else "Up to date"
        if dec_id:
            status_str += f" (Decision Card `{dec_id}` staged)"
        if remediated:
            status_str += " [Remediated 1.task hotfix -> notify]"
        lines.append(f"• **{label}:** `{cur}` — {status_str}")

    if adb_res is not None:
        adb_ok = adb_res.get("ok", False)
        dev_str = "online" if adb_res.get("device_online") else "checked"
        fwd_str = "tcp:9223 active" if adb_res.get("forward_active") else "tcp:9223 configured"
        lines.append(f"• **Host 2 Pixel ADB (`:5037`):** {'OK' if adb_ok else 'ERROR'} ({dev_str}, {fwd_str})")

    return "\n".join(lines)


def run_dsm_update_sidecar(
    ensure_adb: bool = True,
    stage_card: bool = True,
    post_to_discord: bool = True,
    quiet: bool = False,
) -> str:
    """Entry point invoked by tools/sidecars.py dsm_update."""
    adb_res = ensure_host2_adb() if ensure_adb else None
    results = check_all_hosts(stage_card=stage_card, post_to_discord=post_to_discord)
    any_avail = results["host1"].get("available") or results["host2"].get("available")
    any_remediated = (
        results["host1"].get("schedule_drift_remediated")
        or results["host2"].get("schedule_drift_remediated")
    )
    if quiet and not any_avail and not any_remediated:
        return ""
    return format_check_summary(results, adb_res=adb_res)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synology DSM Update Monitor, Schedule Drift Remediator & Host 2 ADB Watchdog"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check DSM version, 1.task schedule drift, and available updates on Host 1 & Host 2 (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Download and start DSM update installation on target --host <1|2>",
    )
    parser.add_argument(
        "--host",
        type=int,
        choices=[1, 2],
        help="Target NAS host number (1 or 2) for --apply",
    )
    parser.add_argument(
        "--ensure-adb",
        action="store_true",
        help="Ensure chmod -R 666 /dev/bus/usb/*, update pixel_adb.sh, and start Host 2 ADB server + tcp:9223 forward",
    )
    parser.add_argument(
        "--no-stage",
        action="store_true",
        help="Do not stage Decision Cards when updates are available",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress output when no updates or drift are detected",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.apply:
        if args.host not in (1, 2):
            parser.error("--apply requires --host <1|2>")
        res = apply_dsm_update(args.host)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    adb_res: Optional[dict[str, Any]] = None
    if args.ensure_adb:
        adb_res = ensure_host2_adb()
        if not args.check:
            print(json.dumps(adb_res, indent=2))
            return 0 if adb_res.get("ok") else 1

    # Default action or explicit --check
    results = check_all_hosts(stage_card=not args.no_stage, post_to_discord=True)
    summary = format_check_summary(results, adb_res=adb_res)
    if not args.quiet or results["host1"].get("available") or results["host2"].get("available"):
        if summary:
            print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
