#!/usr/bin/env python3
"""
nas_log_triage.py - Autonomous NAS Log Review & Triage Harvester
Scans containers across Host 1 (.82) and Host 2 (.84) via fast remote batch execution.
Features:
- Exact RFC3339 Docker timestamp extraction (-t) to differentiate active vs stale historical transients.
- Advanced homelab noise gate (filtering expected standby/polling drops: Chromecast, Matter standby, Octoprint off, etc.).
- Sub-3-second execution profile across 39+ containers via single-pass remote Python runners.
- Structured incident dossiers for autonomous agent remediation turns.
- Clean 1-line nominal output when all systems are healthy.
"""

import os
import sys
import time
import json
import re
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Resolve SSH and host configuration from nas_docker_mcp or environment
def _resolve_nas_config():
    try:
        try:
            from tools.nas_docker_mcp import _resolve_nas_config as _mcp_resolve
        except ImportError:
            from nas_docker_mcp import _resolve_nas_config as _mcp_resolve
        return _mcp_resolve()
    except Exception:
        ssh_port = os.environ.get("NAS_SSH_PORT") or str(49000 + 876)
        return os.environ.get("NAS_HOST_1_IP", "127.0.0.1"), os.environ.get("NAS_HOST_2_IP", "127.0.0.1"), ssh_port

HOST_1_IP, HOST_2_IP, SSH_PORT = _resolve_nas_config()
SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

SERVERBROCK_STOPPED_ALLOWLIST = {
    "esphome", "ai-cli", "overseerr", "baseball_db",
    "baseball_shiny_app", "baseball_shiny_pro", "baseball_shiny_dev", "baseball-scraper-1"
}

# The remote Python code executed locally on each NAS host via ssh 'python3 -'
REMOTE_BATCH_SCANNER = r'''
import subprocess, json, time, re, calendar
from concurrent.futures import ThreadPoolExecutor

now_utc = time.time()
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Noise filter for benign, expected, or standby operational chatter
noise_re = re.compile(
    r'('
    r'libusb_init failed|'
    r'TaskCanceledException|'
    r'TVDb convert warning|'
    r'OpenSubtitles|'
    r'forecast_solar|'
    r'Matter Node 2|'
    r'connection reset by peer|'
    r'socket\.timeout|'
    r'\[EnvUpdateCheck\]|'
    r'DNSSD packet parsing|'
    r'\"error\":\s*0|'
    r'\"error\":0|'
    r'images.*\/error\/|'
    r'Failed to load resource.*status of 503|'
    r'Transient Google auth\/eligibility error|'
    r'Warmed channel history|'
    r'Generated new chapter thumbnails|'
    r'closing transport|'
    r'TimeoutNegativeWarning|'
    r'\[Nest\] API observe: error|'
    r'unsupported method: GET|'
    r'UptimeRobot|'
    r'Dozzle-Agent|'
    r'upstream timed out|'
    r'<httpProxy>|'
    r'credentialedProxyHandler|'
    r'connect EHOSTUNREACH|'
    r'Error calling http:\/\/|'
    r'octoprint.*|'
    r'android_ip_webcam.*|'
    r'failed to sufficiently increase receive buffer size|'
    r'Frame rx failed, error:Duplicated|'
    r'Frame data is truncated.*Status\.NOT_SUPPORTED|'
    r'Received an exception while passing frame to API.*CommandType\.AREQ|'
    r'Data is too short to contain \d+ bytes|'
    r'zigpy_znp.*from_frame|'
    r'CASESession timed out while waiting for a response from peer <0000000000000002|'
    r'CHIP_ERROR.*Msg Retransmission to [0-9]:000000000000000[02] failure|'
    r'CHIP_ERROR.*Node: <000000000000000[02]|'
    r'Stopped reading data from server error=.*EOF|'
    r'Fontconfig error: No writable cache directories|'
    r'CHIP_ERROR.*Subscription Liveness timeout|'
    r'matter_server.*Node.*is offline|'
    r'matter_server.*Unable to subscribe to Node|'
    r'matter_server.*Subscription failed with CHIP Error|'
    r'WARN.*\[SubprocessManager\] Go worker error for env 1: list containers|'
    r'check_update.*Error trying to get releases from GitHub|'
    r'pychromecast.*(Heartbeat timeout|Failed to connect|Connection reestablished)|'
    r'homeassistant\.components\.octoprint.*Config entry.*not ready yet|'
    r'homeassistant\.components\.android_ip_webcam.*Config entry.*not ready yet|'
    r'homeassistant\.components\.steam_online.*Failed to connect to Steam|'
    r'homeassistant\.components\.tplink\.coordinator.*Unable to communicate with the device|'
    r'roombapy\.roomba.*Unexpectedly disconnected from Roomba|'
    r'otbr-agent.*(Failed to handle ICMPv6 message|Sent ICMPv6 Error|Failed to write CLI output)|'
    r'ERR failed to run the datagram handler error=\"timeout: no recent network activity\"|'
    r'\[BridgeTimer:#.*\] \[ERROR_-?\d+\]|'
    r'Warning releasing Banana.*timed out|'
    r'browserless\.io:error No route or file found for resource GET: /|'
    r'Failed to reply to query.*OS Error 0x02000001: Operation not permitted|'
    r'192\.168\.1\.164:3333.*connection refused|'
    r'twcc_sender_interceptor.*read\/write on closed pipe|'
    r'write tcp.*write: broken pipe|'
    r'\[Summarizer\] LLM synthesis fallback'
    r')',
    re.IGNORECASE
)

err_re = re.compile(r'\b(?:panic|fatal|segfault|oom\b|killed|error|exception|failed|failure|traceback|critical)', re.I)
iso_re = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)$')

allowlist = {'calibre-web', 'openbooks', 'esphome', 'ai-cli', 'homebridge'}
ps = subprocess.run(['docker', 'ps', '--format', '{{.Names}}\t{{.Status}}'], capture_output=True, text=True)

targets = []
for line in ps.stdout.strip().splitlines():
    parts = line.split('\t')
    if len(parts) >= 2 and parts[1].startswith('Up'):
        cname, status = parts[0], parts[1]
        if cname not in allowlist:
            targets.append((cname, status))

def inspect_container(target):
    cname, status = target
    cmd = ['docker', 'logs', '--since', '__SINCE__', '-t', '--tail', '250', cname]
    try:
        logs_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3)
        raw_lines = logs_res.stdout.splitlines()
    except subprocess.TimeoutExpired:
        raw_lines = []

    valid_errs = []
    for rl in raw_lines:
        clean = ansi_escape.sub('', rl).strip()
        if not clean or not err_re.search(clean) or noise_re.search(clean):
            continue
        m = iso_re.match(clean)
        if m:
            ts_str, msg = m.group(1), m.group(2)
            try:
                dt_str = ts_str[:19]
                epoch = calendar.timegm(time.strptime(dt_str, '%Y-%m-%dT%H:%M:%S'))
                age = now_utc - epoch
            except Exception:
                age = 0
            valid_errs.append({'age': age, 'ts': ts_str, 'msg': msg})
        else:
            valid_errs.append({'age': 0, 'ts': '', 'msg': clean})

    if valid_errs:
        active_cnt = sum(1 for e in valid_errs if e['age'] < 3600)
        recent_cnt = sum(1 for e in valid_errs if 3600 <= e['age'] < 21600)
        hist_cnt = sum(1 for e in valid_errs if e['age'] >= 21600)
        is_transient = (active_cnt == 0 and recent_cnt == 0 and hist_cnt > 0)
        return {
            'container': cname,
            'status': status,
            'total_errors': len(valid_errs),
            'active_errors': active_cnt,
            'recent_errors': recent_cnt,
            'historical_errors': hist_cnt,
            'is_transient': is_transient,
            'sample_errors': [e['msg'][:200] for e in valid_errs[-5:]]
        }
    return None

flagged = []
with ThreadPoolExecutor(max_workers=10) as executor:
    results = executor.map(inspect_container, targets)
    for r in results:
        if r:
            flagged.append(r)

print(json.dumps({'scanned': len(targets), 'flagged': flagged}))
'''

def _ssh_python_batch(host: str, since: str = "24h", timeout: int = 20, port: str = None, key: str = None, user: str = None) -> dict:
    """Run the batch scanner remotely on a host via SSH in a single pass."""
    script = REMOTE_BATCH_SCANNER.replace("__SINCE__", since)
    _, _, resolved_port = _resolve_nas_config()
    p = port or resolved_port or SSH_PORT
    k = key or SSH_KEY
    u = user or SSH_USER
    try:
        proc = subprocess.run([
            "ssh", "-i", k, "-p", p,
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=6",
            f"{u}@{host}", "python3 -"
        ], input=script, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
        else:
            err_snip = proc.stderr.strip()[:120] if proc.stderr else f"exit code {proc.returncode}"
            print(f"[NASTriage] Warning on {host}: {err_snip}", file=sys.stderr)
            return {"scanned": 0, "flagged": [], "error": err_snip}
    except Exception as e:
        print(f"[NASTriage] Error connecting to {host}: {e}", file=sys.stderr)
        return {"scanned": 0, "flagged": [], "error": str(e)}


def scan_all_nas_containers(since: str = "24h") -> dict:
    """
    Perform high-speed batch log scanning across Host 1 and Host 2 in parallel.
    Returns:
        {
            "total_scanned": int,
            "actionable_issues": list[dict],
            "stale_transients": list[dict],
            "host_errors": dict[str, str],
            "duration_sec": float
        }
    """
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.time()
    actionable = []
    stale = []
    total_scanned = 0
    host_errors = {}

    h1, h2, _ = _resolve_nas_config()
    hosts = [h1 or HOST_1_IP, h2 or HOST_2_IP]
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        futures = {executor.submit(_ssh_python_batch, host, since=since): host for host in hosts}
        for future in futures:
            host = futures[future]
            try:
                res = future.result()
                scanned = res.get("scanned", 0)
                total_scanned += scanned
                if res.get("error"):
                    host_errors[host] = res["error"]
                for item in res.get("flagged", []):
                    item["host"] = host
                    if item.get("is_transient"):
                        stale.append(item)
                    else:
                        actionable.append(item)
            except Exception as e:
                host_errors[host] = str(e)

    duration = time.time() - t0
    return {
        "total_scanned": total_scanned,
        "actionable_issues": actionable,
        "stale_transients": stale,
        "host_errors": host_errors,
        "duration_sec": round(duration, 2)
    }


def run_nas_log_review(since: str = "24h") -> tuple[bool, str, dict]:
    """
    Bridge-compatible sidecar entry point.
    Returns (ok, report_or_summary, extra_data).
    - If 0 actionable issues: emits crisp 1-liner nominal report.
    - If actionable issues exist: emits structured candidate dossier for Zero.
    """
    now_pt = datetime.now(PT).strftime("%A, %b %d, %Y, %I:%M %p PT")
    res = scan_all_nas_containers(since=since)
    total_scanned = res["total_scanned"]
    actionable = res["actionable_issues"]
    stale = res["stale_transients"]

    extra = {
        "total_issues": len(actionable),
        "tier1_count": sum(1 for a in actionable if any("fatal" in s.lower() or "panic" in s.lower() or re.search(r"\boom\b", s, re.I) for s in a.get("sample_errors", []))),
        "tier2_count": len(actionable),
        "dossier": actionable,
        "stale_transients": stale,
        "duration_sec": res["duration_sec"],
        "scanned": total_scanned
    }

    host_errors = res.get("host_errors", {})

    # Failure guard: 0 containers scanned means connection/timeout failure, not nominal health
    if total_scanned == 0:
        err_details = "; ".join(f"{h}: {e}" for h, e in host_errors.items()) if host_errors else "SSH timeout / unreachable"
        rep = f"⚠️ **NAS Log Review** [{now_pt}]: Failed to scan containers across NAS clusters ({err_details})."
        return False, rep, extra

    # Nominal: All scanned containers healthy or only harmless historical transients
    if len(actionable) == 0:
        if host_errors:
            err_hosts = ", ".join(host_errors.keys())
            rep = f"⚠️ **NAS Log Review** [{now_pt}]: {total_scanned} containers healthy, but scan failed on {err_hosts}."
            return False, rep, extra
        rep = f"🗄️ **NAS Log Review** [{now_pt}]: All {total_scanned} containers healthy across .82 and .84. ✅"
        return True, rep, extra

    # Candidate issues detected
    c_word = "container" if len(actionable) == 1 else "containers"
    lines = [
        f"🗄️ **NAS Log Review — Flagged Issues** [{now_pt}]",
        f"Scanned {total_scanned} running containers across NAS clusters. Candidate issues flagged in {len(actionable)} {c_word}:\n"
    ]

    for item in actionable:
        act = item.get("active_errors", 0)
        rec = item.get("recent_errors", 0)
        lines.append(f"* **{item['container']}** ({item.get('host')}) — Status: `{item.get('status')}`")
        lines.append(f"  * **Active (<1h):** {act} | **Recent (1-6h):** {rec} | **Total:** {item.get('total_errors')}")
        if item.get("sample_errors"):
            lines.append("  * **Sample Log Errors:**")
            for err in item["sample_errors"][:3]:
                lines.append(f"    - `{err}`")

    if stale:
        lines.append(f"\n🟢 **Historical Transients Cleared (>6h ago, 0 recent):** {', '.join(s['container'] for s in stale)}")

    return True, "\n".join(lines).strip(), extra


def format_autonomous_triage_prompt(extra: dict) -> str:
    """Format an actionable prompt for Zero to investigate and auto-remediate flagged containers."""
    now_pt = datetime.now(PT).strftime("%A, %b %d, %Y, %I:%M %p PT")
    actionable = extra.get("dossier", [])
    stale = extra.get("stale_transients", [])
    total = len(actionable)

    dossier_json = json.dumps({
        "actionable_containers": actionable,
        "cleared_stale_transients": [s["container"] for s in stale]
    }, indent=2)

    return (
        f"Nightly Autonomous NAS Log Triage [{now_pt}]:\n"
        f"The log harvester scanned {extra.get('scanned', 39)} running containers across Host1 (.82) and Host2 (.84) "
        f"and flagged candidate issues in {total} container(s).\n\n"
        f"### Incident Dossier:\n```json\n{dossier_json}\n```\n\n"
        f"### Autonomous Directives:\n"
        f"1. **Forensic Liveness & Root-Cause Analysis:** Silently investigate each flagged container. Determine if errors are actively recurring or transient hiccups.\n"
        f"2. **Autonomous Safe Remediation:** For actionable code, config, schema, or mapping issues within homelab policy "
        f"(e.g., Kometa configs/syntax/exclusions, volume mount mismatches, stale lock files, directory permissions), "
        f"apply the fix immediately and verify it with validation commands.\n"
        f"3. **Human Approval Gating:** NEVER restart services, Docker daemons, or containers autonomously. "
        f"If a container genuinely requires a restart or manual physical intervention, package clear options with interactive choice buttons (`[CHOICES: ...]`).\n"
        f"4. **Deliverable:** Deliver a single cohesive executive report with zero fluff: "
        f"(a) 🛠️ **Auto-Remediated & Verified Clean**, "
        f"(b) 🟢 **Investigated & Cleared** (stale transients / harmless noise), and "
        f"(c) ⚠️ **Decisions & Approvals Requested** (with one-click buttons)."
    )


if __name__ == "__main__":
    if "--json" in sys.argv:
        res = scan_all_nas_containers()
        print(json.dumps(res, indent=2))
    elif "--dossier-prompt" in sys.argv:
        ok, rep, extra = run_nas_log_review()
        if extra.get("total_issues", 0) > 0:
            print(format_autonomous_triage_prompt(extra))
        else:
            print(rep)
    else:
        ok, rep, extra = run_nas_log_review()
        print(rep)
