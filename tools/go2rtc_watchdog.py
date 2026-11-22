#!/usr/bin/env python3
"""
Host 1 go2rtc Resource & Zombie Stream Watchdog

Monitors Host 1 go2rtc container CPU, RAM, and active Nest streams.
Detects and safely auto-reaps leaked keyframe zombie consumers.
Alerts to #zero-ops if go2rtc exceeds resource thresholds, leaks memory,
holds stuck background Nest streams with no live viewer, or fails health checks.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _p in ["/workspace", "/workspace/tools", _TOOLS_DIR, _REPO_ROOT]:
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
try:
    from nas_config import (
        HOST_1_IP,
        SSH_KEY,
        SSH_PORT,
        SSH_USER,
        evict_ssh_mux_socket,
        run_ssh,
    )
except Exception:  # noqa: BLE001
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
    SSH_PORT = int(os.environ.get("NAS_SSH_PORT", "22"))
    SSH_KEY = "/root/.ssh/id_ed25519"

    def evict_ssh_mux_socket(*args, **kwargs) -> bool:
        return False


DEFAULT_CPU_THRESHOLD = 25.0  # 25.0% sustained CPU
DEFAULT_MEM_THRESHOLD_MIB = 350.0  # 350.0 MiB RAM
DEFAULT_MEM_RATIO_THRESHOLD = 0.50  # 50% of container limit
DEFAULT_MAX_NEST_STREAMS = 3  # >= 3 active Nest streams triggers warning
GO2RTC_DEFAULT_PORT = 1984
CONTAINER_MEM_LIMIT_DEFAULT = 768.0  # 768 MiB memory cap on Host 1

LIVE_VIEWER_FORMATS = {"webrtc", "mse", "rtsp", "mp4", "mjpeg", "hls"}


def parse_mem_mib(val_str: str) -> float:
    """Parses memory string (e.g. '12.59MiB', '1.2 GiB', '500kB') to MiB float."""
    if not val_str:
        return 0.0
    s = val_str.strip()
    if "/" in s:
        s = s.split("/")[0].strip()
    match = re.match(r"^([0-9.]+)\s*([a-zA-Z]+)?$", s)
    if not match:
        return 0.0
    try:
        num = float(match.group(1))
    except ValueError:
        return 0.0
    unit = (match.group(2) or "").lower()
    if unit in ("b", "bytes"):
        return round(num / (1024.0 * 1024.0), 2)
    elif unit in ("k", "kb", "kib"):
        return round(num / 1024.0, 2)
    elif unit in ("m", "mb", "mib", ""):
        return round(num, 2)
    elif unit in ("g", "gb", "gib"):
        return round(num * 1024.0, 2)
    elif unit in ("t", "tb", "tib"):
        return round(num * 1024.0 * 1024.0, 2)
    return round(num, 2)


def parse_mem_stat(stat_str: str) -> tuple[float, float]:
    """Parses docker stats memory usage string (e.g. '12.59MiB / 768MiB') into (used_mib, limit_mib)."""
    if not stat_str or "/" not in stat_str:
        return parse_mem_mib(stat_str), 0.0
    parts = stat_str.split("/", 1)
    return parse_mem_mib(parts[0]), parse_mem_mib(parts[1])


def parse_cpu_perc(cpu_str: str) -> float:
    """Parses CPU percentage string (e.g. '2.59%') to float."""
    if not cpu_str:
        return 0.0
    clean = cpu_str.strip().rstrip("%").strip()
    try:
        return float(clean)
    except ValueError:
        return 0.0


def sanitize_stream_url(url: str) -> str:
    """Strips credentials, tokens, and secrets from stream URLs for safe logging/reporting."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.query:
            q_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            safe_pairs = []
            for k, v in q_pairs:
                if any(sec in k.lower() for sec in ("token", "secret", "client_id", "password", "key", "auth")):
                    safe_pairs.append((k, "[REDACTED]"))
                else:
                    safe_pairs.append((k, v))
            new_query = urllib.parse.urlencode(safe_pairs, safe="[]:")
            return urllib.parse.urlunparse(parsed._replace(query=new_query))
        return url
    except Exception:  # noqa: BLE001
        return re.sub(
            r"([?&](?:token|secret|client_id|refresh_token|password)=)[^&]+",
            r"\1[REDACTED]",
            url,
            flags=re.IGNORECASE,
        )


def is_live_viewer_consumer(consumer: dict[str, Any]) -> bool:
    """Returns True if consumer represents an active live viewer."""
    fmt = str(consumer.get("format_name", "")).lower()
    bytes_send = consumer.get("bytes_send", 0) or 0
    if fmt in LIVE_VIEWER_FORMATS:
        return True
    return bool(fmt and fmt != "keyframe" and bytes_send > 0)


def is_zombie_consumer(consumer: dict[str, Any]) -> bool:
    """Returns True if consumer is a keyframe consumer or stalled 0-byte client."""
    fmt = str(consumer.get("format_name", "")).lower()
    bytes_send = consumer.get("bytes_send", 0) or 0
    user_agent = str(consumer.get("user_agent", "")).lower()

    if fmt == "keyframe":
        return True
    if bytes_send == 0 and ("python" in user_agent or "urllib" in user_agent or not fmt):
        return True
    return bool(bytes_send == 0 and fmt != "webrtc")


def evaluate_stream_zombie_state(stream_name: str, stream_data: dict[str, Any]) -> dict[str, Any] | None:
    """
    Evaluates whether a stream has leaked zombie consumers and no live viewers.
    Returns a dict with details if zombie, or None.
    """
    consumers = stream_data.get("consumers")
    if not consumers or not isinstance(consumers, list) or len(consumers) == 0:
        return None

    # If ANY consumer is a live viewer, do NOT reap
    for c in consumers:
        if is_live_viewer_consumer(c):
            return None

    # Check if ALL consumers on this stream are zombie consumers
    all_zombies = all(is_zombie_consumer(c) for c in consumers)
    if not all_zombies:
        return None

    producers = stream_data.get("producers") or []
    producer_url = ""
    if isinstance(producers, list) and len(producers) > 0 and isinstance(producers[0], dict):
        producer_url = producers[0].get("url") or ""

    can_reap = bool(producer_url and producer_url.strip())
    return {
        "name": stream_name,
        "producer_url": producer_url,
        "can_reap": can_reap,
        "consumer_count": len(consumers),
        "reason": f"All {len(consumers)} consumer(s) are keyframe/stalled zombies with no live viewer",
    }


def is_nest_stream(stream_name: str, stream_data: dict[str, Any]) -> bool:
    """Checks if a stream represents a Nest camera."""
    if "nest" in stream_name.lower():
        return True
    producers = stream_data.get("producers") or []
    for p in producers:
        if isinstance(p, dict):
            url = str(p.get("url", "")).lower()
            if url.startswith("nest") or "nest:" in url or "googleapis.com" in url:
                return True
    return False


def is_stream_active(stream_data: dict[str, Any]) -> bool:
    """Checks if a stream is actively producing/streaming (has receivers, bytes, or consumers)."""
    consumers = stream_data.get("consumers")
    if consumers and isinstance(consumers, list) and len(consumers) > 0:
        return True
    producers = stream_data.get("producers") or []
    for p in producers:
        if isinstance(p, dict):
            if p.get("receivers") and len(p.get("receivers")) > 0:
                return True
            if p.get("bytes_recv", 0) > 0:
                return True
            if p.get("medias") and len(p.get("medias")) > 0:
                return True
            if p.get("format_name"):
                return True
    return False


def parse_go2rtc_streams(streams_raw: Any) -> dict[str, Any]:
    """
    Parses go2rtc /api/streams payload (raw JSON string or dict).
    Returns summary dictionary with counts, active streams, and zombie candidates.
    """
    if isinstance(streams_raw, str):
        try:
            data = json.loads(streams_raw) if streams_raw.strip() else {}
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return {
                "error": f"Failed to parse streams JSON: {e}",
                "total_streams": 0,
                "active_streams": 0,
                "active_nest_streams": 0,
                "zombie_streams": [],
                "streams": {},
            }
    elif isinstance(streams_raw, dict):
        data = streams_raw
    else:
        return {
            "error": "Invalid streams data type",
            "total_streams": 0,
            "active_streams": 0,
            "active_nest_streams": 0,
            "zombie_streams": [],
            "streams": {},
        }

    total_streams = len(data)
    active_streams = 0
    active_nest_streams = 0
    zombie_streams = []

    for name, sdata in data.items():
        if not isinstance(sdata, dict):
            continue
        active = is_stream_active(sdata)
        if active:
            active_streams += 1
            if is_nest_stream(name, sdata):
                active_nest_streams += 1

        zombie_info = evaluate_stream_zombie_state(name, sdata)
        if zombie_info:
            zombie_streams.append(zombie_info)

    return {
        "error": None,
        "total_streams": total_streams,
        "active_streams": active_streams,
        "active_nest_streams": active_nest_streams,
        "zombie_streams": zombie_streams,
        "streams": data,
    }


def reap_zombie_stream(host_ip: str, stream_name: str, producer_url: str) -> tuple[bool, str]:
    """
    Reaps a zombie stream via Host 1 SSH curl DELETE + PUT re-registration.
    Returns (success, error_message).
    """
    if not producer_url or not producer_url.strip():
        return False, "Missing producer URL"
    stream_enc = urllib.parse.quote(stream_name, safe="")
    url_enc = urllib.parse.quote(producer_url, safe="")
    cmd = (
        f"curl -s -m 5 -X DELETE 'http://127.0.0.1:{GO2RTC_DEFAULT_PORT}/api/streams?src={stream_enc}' && "
        f"curl -s -m 5 -X PUT 'http://127.0.0.1:{GO2RTC_DEFAULT_PORT}/api/streams?name={stream_enc}&src={url_enc}'"
    )
    code, _, err = run_ssh(
        host_ip,
        cmd,
        timeout=15,
        connect_timeout=10,
        user=SSH_USER,
        port=SSH_PORT,
        key=SSH_KEY,
    )
    if code != 0:
        return False, f"SSH curl reap failed (code {code}): {err.strip()}"
    return True, ""


def collect_go2rtc_telemetry(host_ip: str = HOST_1_IP) -> dict[str, Any]:
    """
    Collects Host 1 go2rtc telemetry in a SINGLE bundled SSH command:
    - Container status & restart count
    - 2 docker stats samples (separated by 2s) for CPU% and memory usage/limit
    - GET /api/streams JSON
    """
    cmd = (
        "docker inspect -f '{{.State.Status}} {{.RestartCount}}' go2rtc 2>/dev/null || echo 'NOT_FOUND 0'; "
        "docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' go2rtc 2>/dev/null || echo '0.0% 0B / 0B'; "
        "sleep 2; "
        "docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' go2rtc 2>/dev/null || echo '0.0% 0B / 0B'; "
        "echo '---JSON---'; "
        f"curl -s -m 5 http://127.0.0.1:{GO2RTC_DEFAULT_PORT}/api/streams 2>/dev/null || echo '{{}}'"
    )
    code, out, err = -1, "", ""
    for attempt in range(1, 4):
        try:
            code, out, err = run_ssh(
                host_ip,
                cmd,
                timeout=40,
                connect_timeout=20,
                user=SSH_USER,
                port=SSH_PORT,
                key=SSH_KEY,
                disable_mux=(attempt == 2),
                retry_without_mux_on_timeout=(attempt == 1),
            )
        except TypeError:
            code, out, err = run_ssh(host_ip, cmd, timeout=40, user=SSH_USER, port=SSH_PORT, key=SSH_KEY)

        if code == 0 and "---JSON---" in out:
            break
        if attempt < 3:
            evict_ssh_mux_socket(host_ip, user=SSH_USER, port=SSH_PORT)
            time.sleep(2.0 * attempt)

    if code != 0:
        return {
            "status": "error",
            "host_ip": host_ip,
            "error": f"SSH connection failed (code {code}): {err.strip()}",
            "container_status": "unknown",
            "restart_count": 0,
            "avg_cpu": 0.0,
            "mem_used_mib": 0.0,
            "mem_limit_mib": CONTAINER_MEM_LIMIT_DEFAULT,
            "mem_ratio": 0.0,
            "streams_summary": parse_go2rtc_streams({}),
        }

    parts = out.split("---JSON---", 1)
    stats_text = parts[0].strip()
    json_text = parts[1].strip() if len(parts) > 1 else "{}"

    stats_lines = [line.strip() for line in stats_text.split("\n") if line.strip()]

    container_status = "unknown"
    restart_count = 0
    cpu_samples = []
    mem_used_samples = []
    mem_limit_samples = []

    if len(stats_lines) >= 1:
        inspect_parts = stats_lines[0].split()
        container_status = inspect_parts[0] if inspect_parts else "unknown"
        if len(inspect_parts) > 1:
            try:
                restart_count = int(inspect_parts[1])
            except ValueError:
                restart_count = 0

    for line in stats_lines[1:]:
        match = re.search(r"([0-9.]+%)\s+([0-9.]+\s*[a-zA-Z]+\s*/\s*[0-9.]+\s*[a-zA-Z]+)", line)
        if match:
            cpu_val = parse_cpu_perc(match.group(1))
            used_mib, lim_mib = parse_mem_stat(match.group(2))
            cpu_samples.append(cpu_val)
            mem_used_samples.append(used_mib)
            mem_limit_samples.append(lim_mib)
        else:
            tokens = line.split(None, 1)
            if tokens:
                cpu_samples.append(parse_cpu_perc(tokens[0]))
            if len(tokens) > 1:
                u, l_ = parse_mem_stat(tokens[1])
                mem_used_samples.append(u)
                mem_limit_samples.append(l_)

    avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
    mem_used_mib = max(mem_used_samples) if mem_used_samples else 0.0
    mem_limit_mib = (
        max(mem_limit_samples)
        if (mem_limit_samples and max(mem_limit_samples) > 0)
        else CONTAINER_MEM_LIMIT_DEFAULT
    )
    mem_ratio = (mem_used_mib / mem_limit_mib) if mem_limit_mib > 0 else 0.0

    streams_summary = parse_go2rtc_streams(json_text)

    return {
        "status": "nominal" if container_status == "running" else "container_issue",
        "host_ip": host_ip,
        "error": None,
        "container_status": container_status,
        "restart_count": restart_count,
        "avg_cpu": round(avg_cpu, 2),
        "mem_used_mib": round(mem_used_mib, 2),
        "mem_limit_mib": round(mem_limit_mib, 2),
        "mem_ratio": round(mem_ratio, 4),
        "streams_summary": streams_summary,
    }


def evaluate_go2rtc_health(
    telemetry: dict[str, Any],
    auto_reap: bool = True,
    cpu_threshold: float = DEFAULT_CPU_THRESHOLD,
    mem_threshold_mib: float = DEFAULT_MEM_THRESHOLD_MIB,
    max_nest_streams: int = DEFAULT_MAX_NEST_STREAMS,
    host_ip: str | None = None,
    force: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Evaluates go2rtc telemetry against health invariants and optionally auto-reaps zombie streams.
    Returns (has_issues, summary_report, evaluation_data).
    """
    target_ip = host_ip or telemetry.get("host_ip") or HOST_1_IP
    has_issues = False
    issue_reasons: list[str] = []

    # 1. Transport / SSH Failure
    if telemetry.get("status") == "error" or telemetry.get("error"):
        has_issues = True
        err = telemetry.get("error") or "Unknown telemetry collection failure"
        summary = (
            f"### ⚠️ Host 1 go2rtc Transport Alert\n"
            f"- Host: Host 1 (Synology NAS) ({target_ip})\n"
            f"- Error: {err}"
        )
        return True, summary, {"telemetry": telemetry, "has_issues": True, "reasons": [err]}

    # 2. Container Status
    c_status = telemetry.get("container_status", "unknown")
    restarts = telemetry.get("restart_count", 0)
    if c_status != "running":
        has_issues = True
        issue_reasons.append(f"Container status is '{c_status}' (expected 'running')")

    # 3. CPU & Memory Thresholds
    avg_cpu = telemetry.get("avg_cpu", 0.0)
    mem_used_mib = telemetry.get("mem_used_mib", 0.0)
    mem_limit_mib = telemetry.get("mem_limit_mib", CONTAINER_MEM_LIMIT_DEFAULT)
    mem_ratio = telemetry.get("mem_ratio", 0.0)

    if avg_cpu >= cpu_threshold:
        has_issues = True
        issue_reasons.append(f"Sustained CPU usage {avg_cpu:.1f}% exceeds threshold {cpu_threshold:.1f}%")

    if mem_used_mib >= mem_threshold_mib:
        has_issues = True
        issue_reasons.append(f"Memory usage {mem_used_mib:.1f} MiB exceeds ceiling {mem_threshold_mib:.1f} MiB")
    elif mem_ratio >= DEFAULT_MEM_RATIO_THRESHOLD:
        has_issues = True
        issue_reasons.append(
            f"Memory usage {mem_used_mib:.1f} MiB is {mem_ratio:.1%} of {mem_limit_mib:.1f} MiB container limit"
        )

    # 4. Streams & Active Nest Producers
    streams_summary = telemetry.get("streams_summary") or {}
    api_err = streams_summary.get("error")
    if api_err:
        has_issues = True
        issue_reasons.append(f"go2rtc API unreachable/hung: {api_err}")

    active_nest = streams_summary.get("active_nest_streams", 0)
    if active_nest >= max_nest_streams:
        has_issues = True
        issue_reasons.append(f"Active Nest camera streams ({active_nest}) reach or exceed limit ({max_nest_streams})")

    # 5. Zombie Stream Evaluation and Auto-Reaping
    zombie_streams = streams_summary.get("zombie_streams", [])
    reaped_streams: list[str] = []
    unreapable_streams: list[str] = []
    failed_reap_streams: list[str] = []
    unreaped_detected: list[str] = []

    for z in zombie_streams:
        name = z.get("name", "unknown")
        can_reap = z.get("can_reap", False)
        prod_url = z.get("producer_url", "")

        if auto_reap:
            if can_reap:
                ok, reap_err = reap_zombie_stream(target_ip, name, prod_url)
                if ok:
                    reaped_streams.append(name)
                    has_issues = True
                else:
                    failed_reap_streams.append(f"{name} ({reap_err})")
                    has_issues = True
                    issue_reasons.append(f"Failed to reap zombie stream {name}: {reap_err}")
            else:
                unreapable_streams.append(name)
                has_issues = True
                issue_reasons.append(f"Zombie stream {name} cannot be reaped (missing producer URL)")
        else:
            unreaped_detected.append(name)
            has_issues = True
            issue_reasons.append(f"Zombie stream {name} detected (auto-reap disabled)")

    if reaped_streams:
        issue_reasons.append(f"Auto-reaped {len(reaped_streams)} zombie stream(s): {', '.join(reaped_streams)}")

    eval_data = {
        "telemetry": telemetry,
        "has_issues": has_issues,
        "reasons": issue_reasons,
        "reaped_streams": reaped_streams,
        "unreapable_streams": unreapable_streams,
        "failed_reap_streams": failed_reap_streams,
        "unreaped_detected": unreaped_detected,
    }

    # If nominal and not force, return compact one-line status
    if not has_issues and not force:
        summary = (
            f"(nominal - go2rtc: CPU {avg_cpu:.1f}%, RAM {mem_used_mib:.1f} MiB / {mem_limit_mib:.1f} MiB [{mem_ratio:.1%}], "
            f"{active_nest} active Nest stream(s), 0 zombies)"
        )
        return False, summary, eval_data

    # Format full report
    lines = []
    header_emoji = "🚨" if (c_status != "running" or failed_reap_streams or unreapable_streams) else "⚠️"
    if not has_issues and force:
        header_emoji = "🟢"
        lines.append("### 🟢 Host 1 go2rtc Health Status (Forced Run)")
    else:
        lines.append(f"### {header_emoji} Host 1 go2rtc Health Alert")

    lines.append(f"- Host: Host 1 (Synology NAS) ({target_ip})")
    lines.append(f"- Container Status: {c_status} ({restarts} restarts)")
    lines.append(f"- CPU Usage: {avg_cpu:.1f}% (threshold: {cpu_threshold:.1f}%)")
    lines.append(
        f"- Memory Usage: {mem_used_mib:.1f} MiB / {mem_limit_mib:.1f} MiB "
        f"({mem_ratio:.1%}, threshold: {mem_threshold_mib:.1f} MiB)"
    )
    lines.append(f"- Active Nest Streams: {active_nest} (threshold: {max_nest_streams})")

    if reaped_streams:
        lines.append(f"- Reaped Zombie Streams: {', '.join(reaped_streams)}")
    if unreapable_streams:
        lines.append(f"- Unreapable Zombie Streams: {', '.join(unreapable_streams)} (missing producer URL)")
    if failed_reap_streams:
        lines.append(f"- Failed Stream Reaps: {', '.join(failed_reap_streams)}")
    if unreaped_detected:
        lines.append(f"- Detected Zombie Streams: {', '.join(unreaped_detected)}")

    if issue_reasons and has_issues:
        lines.append("- Triggers:")
        for r in issue_reasons:
            lines.append(f"  - {r}")

    report = "\n".join(lines)
    return has_issues, report, eval_data


def audit_go2rtc_health(
    force: bool = False,
    auto_reap: bool = True,
    cpu_threshold: float = DEFAULT_CPU_THRESHOLD,
    mem_threshold_mib: float = DEFAULT_MEM_THRESHOLD_MIB,
    host_ip: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Audits Host 1 go2rtc resource usage and zombie streams.
    Returns (has_issues, summary_report, telemetry_data).
    """
    target_ip = host_ip or HOST_1_IP
    telemetry = collect_go2rtc_telemetry(target_ip)
    return evaluate_go2rtc_health(
        telemetry,
        auto_reap=auto_reap,
        cpu_threshold=cpu_threshold,
        mem_threshold_mib=mem_threshold_mib,
        host_ip=target_ip,
        force=force,
    )


def main():
    parser = argparse.ArgumentParser(description="Host 1 go2rtc Resource & Zombie Stream Watchdog")
    parser.add_argument("--test", action="store_true", help="Force output and test run")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if nominal")
    parser.add_argument("--no-reap", action="store_true", help="Disable auto-reaping of zombie streams")
    parser.add_argument("--cpu-threshold", type=float, default=DEFAULT_CPU_THRESHOLD, help="CPU % warning threshold")
    parser.add_argument(
        "--mem-threshold",
        type=float,
        default=DEFAULT_MEM_THRESHOLD_MIB,
        help="Memory MiB warning threshold",
    )
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to #zero-ops if issues detected")
    args = parser.parse_args()

    has_issues, summary, _ = audit_go2rtc_health(
        force=args.test,
        auto_reap=not args.no_reap,
        cpu_threshold=args.cpu_threshold,
        mem_threshold_mib=args.mem_threshold,
    )

    if args.dispatch and has_issues:
        try:
            from tools.outbox import send_message

            send_message(channel_id="1542081463124881478", content=summary)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to dispatch to #zero-ops: {e}", file=sys.stderr)

    if not args.quiet or has_issues or args.test:
        print(summary)

    sys.exit(1 if has_issues and not args.test else 0)


if __name__ == "__main__":
    main()
