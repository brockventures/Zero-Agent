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
    r'\[debug\]|'
    r'libusb_init failed|'
    r'init_VideoIn failed|'
    r'TaskCanceledException|'
    r'--- End of inner exception stack trace ---|'
    r'at System\.Net\.Http\.HttpClient\.HandleFailure|'
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
    r'Failed to load resource.*status of (?:404|503)|'
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
    r'zigpy.*(?:DeliveryError|InvalidCommandResponse|from_frame)|'
    r'Status\.(?:APS_NO_ACK|NWK_NO_ROUTE)|'
    r'raise DeliveryError|'
    r'During handling of the above exception|'
    r'The above exception was the direct cause of the following exception|'
    r'CASESession timed out while waiting for a response from peer <0000000000000002|'
    r'CHIP_ERROR.*Msg Retransmission to [0-9]:000000000000000[02] failure|'
    r'CHIP_ERROR.*Node: <000000000000000[02]|'
    r'CHIP_ERROR.*OperationalSessionSetup\[1:0000000000000002\]|'
    r'CHIP_ERROR.*Failed to establish CASE for re-subscription.*CHIP Error 0x00000032: Timeout|'
    r'Stopped reading data from server error=.*EOF|'
    r'Fontconfig error: No writable cache directories|'
    r'CHIP_ERROR.*Subscription Liveness timeout|'
    r'matter_server.*Node.*is offline|'
    r'matter_server.*Unable to subscribe to Node|'
    r'matter_server.*Subscription failed with CHIP Error|'
    r'WARN.*\[SubprocessManager\] Go worker error for env 1: list containers|'
    r'failed to (?:inspect container, falling back to list entry|list containers while adding container)|'
    r'check_update.*Error trying to get releases from GitHub|'
    r'pychromecast.*(Heartbeat timeout|Failed to connect|Connection reestablished)|'
    r'homeassistant\.components\.octoprint.*Config entry.*not ready yet|'
    r'homeassistant\.components\.android_ip_webcam.*Config entry.*not ready yet|'
    r'homeassistant\.components\.steam_online.*Failed to connect to Steam|'
    r'homeassistant\.components\.tplink\.coordinator.*Unable to communicate with the device|'
    r'homeassistant\.components\.broadlink\.updater.*(Network timeout|data recovered)|'
    r'roombapy\.roomba.*Unexpectedly disconnected from Roomba|'
    r'otbr-agent.*(?:\[I\]|Failed to handle ICMPv6 message|Sent ICMPv6 Error|Failed to write CLI output|Frame tx attempt.*failed|Failed to send IPv6 UDP msg|P-MdnsSocket--:\s*sendto\(IPv4\) failed)|'
    r'ERR failed to run the datagram handler error=\"timeout: no recent network activity\"|'
    r'\[BridgeTimer:#.*\] \[ERROR_-?\d+\]|'
    r'Warning releasing Banana.*timed out|'
    r'browserless\.io:error No route or file found for resource GET: /|'
    r'browserless\.io.*Current period usage|'
    r'Failed to reply to query.*OS Error 0x02000001: Operation not permitted|'
    r'192\.168\.1\.164:3333.*connection refused|'
    r'twcc_sender_interceptor.*read\/write on closed pipe|'
    r'\[Summarizer\] LLM synthesis fallback|'
    r'canceled by remote with error code 0|'
    r'Persistent worker for #.* terminated unexpectedly \(exit code -?\d+\)|'
    r'\[BridgeTimer:#.*\] \[FAILED: Persistent worker for|'
    r'hyundai_kia_connect_api.*unknown error response|'
    r'custom_components\.kia_uvo\.coordinator.*falling back to cached|'
    r'custom_components\.emporia_vue.*(Read timed out|Error communicating with Emporia API)|'
    r'requests\.exceptions\.RequestException|'
    r'Traceback \(most recent call last\):|'
    r'\[Classifier\] Error: Command|'
    r'pychromecast\.socket_client.*(Failed to connect|Error reading from socket|retrying in)|'
    r'homeassistant\.components\.enphase_envoy\.coordinator.*(Error communicating with Envoy API|Unexpected error updating listener)|'
    r'ValueError: Sensor sensor\.encharge_.*has device class .temperature..*non-numeric value: .unknown.|'
    r'Unable to reach the origin service.*(dial tcp.*:(?:8000|8001): connect: connection refused|robots\.txt)|'
    r'If you restarted Plex just before running ImageMaid|'
    r'\[error\]\[Plex Scan\]: Failed to process Plex media.*No Guid metadata|'
    r'homeassistant\.components\.plex\.server.*Could not connect to Plex server.*Read timed out|'
    r'aioesphomeapi\.reconnect_logic.*Can\'t connect to ESPHome API|'
    r'homeassistant\.components\.xbox\.coordinator.*Failed to connect to Xbox Network|'
    r'homeassistant\.components\.tesla_wall_connector\.coordinator.*Could not fetch data from Tesla WallConnector|'
    r'myuser@baseball_data.*(?:column.*does not exist|syntax error|relation.*does not exist|function.*does not exist|missing FROM-clause entry)|'
    r'myuser@baseball_data.*FATAL:\s*connection to client lost|'
    r'myuser@baseball_db.*FATAL:\s*database.*does not exist|'
    r'postgres@baseball_data.*FATAL:\s*password authentication failed|'
    r'FATAL:\s*the database system is (?:in recovery mode|not yet accepting connections|starting up)|'
    r'Failed to interview node 2|'
    r'Attempting a reconnect in \d|'
    r'\[IvyDaemon\] Turn result received \(status: ERROR\)|'
    r'WRN (?:Serve tunnel error|Connection terminated) error=\"(?:datagram manager|accept stream listener) error:.*(?:Application error 0x0 \(remote\)|timeout: no recent network activity)|'
    r'WRN Failed to dial a quic connection error=\"failed to dial to edge with quic: timeout: no recent network activity\"|'
    r'--gotify-notify-error|'
    r'\[BridgePipeline\] target_dest delivery failed.*50035|'
    r'In message_reference: Cannot reply to a system message|'
    r'homeassistant\.components\.tplink\.entity.*Unable to read data for.*get_auto_off_config|'
    r'snitun\.multiplexer\.core.*Timeout error while pinging peer|'
    r'matter_server\.server\.client_handler.*Disconnected: Received error message: No PONG received after'
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

versions = {}
try:
    with open('/etc.defaults/VERSION') as vf:
        kv = dict(re.findall(r'^(\w+)="([^"]*)"', vf.read(), re.M))
        if kv.get('productversion'):
            versions['synology-dsm'] = kv['productversion']
except Exception:
    pass

try:
    cnames = [t[0] for t in targets]
    if cnames:
        insp = subprocess.run(['docker', 'inspect'] + cnames, capture_output=True, text=True, timeout=4)
        if insp.returncode == 0:
            for item in json.loads(insp.stdout):
                nm = item.get('Name', '').lstrip('/')
                labels = (item.get('Config') or {}).get('Labels') or {}
                ver = (
                    labels.get('org.opencontainers.image.version')
                    or labels.get('build_version')
                    or labels.get('version')
                    or ''
                )
                if ver:
                    versions[nm] = ver
except Exception:
    pass

print(json.dumps({'scanned': len(targets), 'flagged': flagged, 'versions': versions}))
'''

CISA_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_CACHE_FILE = DATA_DIR / "cisa_kev_cache.json"

# Deterministic minimum safe versions for exposed / core homelab services
CRITICAL_MIN_VERSIONS = {
    "plex": ("1.43.2", "Plex Media Server RCE / Plex2Shell (require >= 1.43.2)"),
    "seerr": ("1.33.2", "Seerr / Overseerr security baseline"),
    "overseerr": ("1.33.2", "Overseerr security baseline"),
    "homeassistant": ("2024.1.0", "Home Assistant Core security baseline"),
    "home-assistant": ("2024.1.0", "Home Assistant Core security baseline"),
    "tautulli": ("2.13.0", "Tautulli unauthenticated RCE / path traversal fix"),
    "synology-dsm": ("7.2.2", "Synology DSM Pwn2Own RCE baseline (require >= 7.2.2)"),
}

KEV_PRODUCT_MAP = {
    "plex": ("plex", "plex media server"),
    "homeassistant": ("home assistant", "home-assistant"),
    "home-assistant": ("home assistant", "home-assistant"),
    "synology-dsm": ("synology", "diskstation manager"),
    "nginx": ("f5", "nginx"),
    "cloudflared": ("cloudflare", "cloudflared"),
    "zero-cloudflared": ("cloudflare", "cloudflared"),
}


def parse_semver_tuple(ver_str: str) -> tuple[int, ...]:
    """Extract numeric version tuple (e.g. 'v1.43.4.10903-e5521' -> (1, 43, 4, 10903))."""
    if not ver_str:
        return ()
    m = re.search(r"(\d+(?:\.\d+)+)", str(ver_str))
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def _is_version_below(running_ver: str, min_ver: str) -> bool:
    """Return True only when both versions parse cleanly and running_ver < min_ver."""
    r_tup = parse_semver_tuple(running_ver)
    m_tup = parse_semver_tuple(min_ver)
    if not r_tup or not m_tup:
        return False
    width = min(len(r_tup), len(m_tup))
    if r_tup[:width] != m_tup[:width]:
        return r_tup[:width] < m_tup[:width]
    return len(r_tup) < len(m_tup)


def fetch_cisa_kev_catalog(max_age_sec: int = 86400) -> list[dict]:
    """Fetch CISA KEV catalog with 24h on-disk cache and silent fallback on network failure."""
    import urllib.request

    try:
        if KEV_CACHE_FILE.exists() and (time.time() - KEV_CACHE_FILE.stat().st_mtime) < max_age_sec:
            data = json.loads(KEV_CACHE_FILE.read_text())
            return data.get("vulnerabilities", []) if isinstance(data, dict) else []
    except Exception:
        pass

    try:
        req = urllib.request.Request(CISA_KEV_URL, headers={"User-Agent": "Zero-Homelab-Monitor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict) and "vulnerabilities" in data:
                try:
                    KEV_CACHE_FILE.write_text(raw)
                except Exception:
                    pass
                return data["vulnerabilities"]
    except Exception:
        try:
            if KEV_CACHE_FILE.exists():
                data = json.loads(KEV_CACHE_FILE.read_text())
                return data.get("vulnerabilities", []) if isinstance(data, dict) else []
        except Exception:
            pass
    return []


def evaluate_kev_risks(versions_by_host: dict, kev_catalog: list[dict] | None = None) -> list[dict]:
    """
    Silently evaluate running container/DSM versions against deterministic critical floors
    and active CISA KEV advisories. Only returns items when a real version mismatch risk exists.
    """
    if not versions_by_host:
        return []

    risks = []
    seen_keys = set()

    # 1. Deterministic critical minimum version floor checks
    for host, ver_map in versions_by_host.items():
        if not isinstance(ver_map, dict):
            continue
        for service, running_ver in ver_map.items():
            svc_key = service.lower()
            if svc_key in CRITICAL_MIN_VERSIONS:
                min_ver, desc = CRITICAL_MIN_VERSIONS[svc_key]
                if _is_version_below(running_ver, min_ver):
                    rkey = (host, svc_key, min_ver)
                    if rkey not in seen_keys:
                        seen_keys.add(rkey)
                        risks.append({
                            "host": host,
                            "service": service,
                            "running_version": running_ver,
                            "fixed_version": min_ver,
                            "cve_id": "CRITICAL-FLOOR",
                            "summary": desc,
                        })

    # 2. CISA KEV catalog check against running versions
    catalog = kev_catalog if kev_catalog is not None else fetch_cisa_kev_catalog()
    if not catalog:
        return risks

    ver_extract_re = re.compile(r"(?:prior to|before|earlier than|<|<=)\s*v?(\d+\.\d+(?:\.\d+)*)", re.I)

    for host, ver_map in versions_by_host.items():
        if not isinstance(ver_map, dict):
            continue
        for service, running_ver in ver_map.items():
            svc_key = service.lower()
            if svc_key not in KEV_PRODUCT_MAP or not parse_semver_tuple(running_ver):
                continue
            vendor_kw, prod_kw = KEV_PRODUCT_MAP[svc_key]
            for vuln in catalog:
                v_vendor = str(vuln.get("vendorProject", "")).lower()
                v_prod = str(vuln.get("product", "")).lower()
                if vendor_kw not in v_vendor or prod_kw not in v_prod:
                    continue
                desc_text = f"{vuln.get('shortDescription', '')} {vuln.get('requiredAction', '')}"
                m = ver_extract_re.search(desc_text)
                if m:
                    fixed_ver = m.group(1)
                    if _is_version_below(running_ver, fixed_ver):
                        cve_id = vuln.get("cveID", "CISA-KEV")
                        rkey = (host, svc_key, cve_id)
                        if rkey not in seen_keys:
                            seen_keys.add(rkey)
                            risks.append({
                                "host": host,
                                "service": service,
                                "running_version": running_ver,
                                "fixed_version": fixed_ver,
                                "cve_id": cve_id,
                                "summary": vuln.get("vulnerabilityName") or vuln.get("shortDescription", "")[:140],
                            })

    return risks

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
            "versions_by_host": dict[str, dict],
            "duration_sec": float
        }
    """
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.time()
    actionable = []
    stale = []
    total_scanned = 0
    host_errors = {}
    versions_by_host = {}

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
                if res.get("versions"):
                    versions_by_host[host] = res["versions"]
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
        "versions_by_host": versions_by_host,
        "duration_sec": round(duration, 2)
    }


def _format_kev_report_block(kev_risks: list[dict]) -> str:
    if not kev_risks:
        return ""
    lines = ["🛡️ **Active Vulnerability / Version Floor Risk Flagged:**"]
    for r in kev_risks:
        lines.append(
            f"- **{r['service']}** ({r['host']}): running `{r['running_version']}` < `{r['fixed_version']}` "
            f"(`{r['cve_id']}` — {r['summary']})"
        )
    return "\n".join(lines)


def run_nas_log_review(since: str = "24h") -> tuple[bool, str, dict]:
    """
    Bridge-compatible sidecar entry point.
    Returns (ok, report_or_summary, extra_data).
    - If 0 actionable issues: emits crisp 1-liner nominal report (or appends self-healed/telemetry report).
    - If actionable issues exist: emits structured candidate dossier for Zero.
    """
    now_pt = datetime.now(PT).strftime("%A, %b %d, %Y, %I:%M %p PT")
    res = scan_all_nas_containers(since=since)
    total_scanned = res["total_scanned"]
    actionable = res["actionable_issues"]
    stale = res["stale_transients"]
    versions_by_host = res.get("versions_by_host", {})

    # Silent high-threshold CISA KEV + critical version floor check
    kev_risks = []
    try:
        kev_risks = evaluate_kev_risks(versions_by_host)
    except Exception:
        kev_risks = []
    kev_report = _format_kev_report_block(kev_risks)

    # Run Home Assistant + VictoriaMetrics statistical telemetry & self-healing check
    ha_telemetry = {}
    ha_report = ""
    try:
        from tools.ha_anomaly_detector import analyze_anomalies, format_report
        ha_telemetry = analyze_anomalies(auto_heal=True)
        if ha_telemetry.get("has_anomalies"):
            ha_report = format_report(ha_telemetry, quiet=True)
    except Exception as e:
        ha_telemetry = {"ok": False, "error": str(e)}

    extra = {
        "total_issues": len(actionable) + len(kev_risks),
        "tier1_count": sum(1 for a in actionable if any("fatal" in s.lower() or "panic" in s.lower() or re.search(r"\boom\b", s, re.I) for s in a.get("sample_errors", []))) + len(kev_risks),
        "tier2_count": len(actionable),
        "dossier": actionable,
        "kev_risks": kev_risks,
        "stale_transients": stale,
        "duration_sec": res["duration_sec"],
        "scanned": total_scanned,
        "ha_telemetry": ha_telemetry,
    }

    host_errors = res.get("host_errors", {})

    # Failure guard: 0 containers scanned means connection/timeout failure, not nominal health
    if total_scanned == 0:
        err_details = "; ".join(f"{h}: {e}" for h, e in host_errors.items()) if host_errors else "SSH timeout / unreachable"
        rep = f"⚠️ **NAS Log Review** [{now_pt}]: Failed to scan containers across NAS clusters ({err_details})."
        if ha_report:
            rep = f"{rep}\n\n{ha_report}"
        return False, rep, extra

    # Nominal container logs: All scanned containers healthy or only harmless historical transients
    if len(actionable) == 0:
        if host_errors:
            err_hosts = ", ".join(host_errors.keys())
            rep = f"⚠️ **NAS Log Review** [{now_pt}]: {total_scanned} containers healthy, but scan failed on {err_hosts}."
            if ha_report:
                rep = f"{rep}\n\n{ha_report}"
            if kev_report:
                rep = f"{rep}\n\n{kev_report}"
            return False, rep, extra
        monitored_ha = ha_telemetry.get("monitored_count", 0)
        unifi_cnt = ha_telemetry.get("unifi_clients_count", 0)
        unifi_part = f", {unifi_cnt} UniFi clients" if unifi_cnt else ""
        ha_suffix = f" ({monitored_ha} HA sensors{unifi_part} nominal)" if monitored_ha and not ha_report else ""
        rep = f"🗄️ **NAS Log Review** [{now_pt}]: All {total_scanned} containers healthy across .82 and .84{ha_suffix}. ✅"
        if ha_report:
            rep = f"{rep}\n\n{ha_report}"
        if kev_report:
            rep = f"{rep}\n\n{kev_report}"
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

    if ha_report:
        lines.append(f"\n{ha_report}")

    if kev_report:
        lines.append(f"\n{kev_report}")

    return True, "\n".join(lines).strip(), extra


def format_autonomous_triage_prompt(extra: dict) -> str:
    """Format an actionable prompt for Zero to investigate and auto-remediate flagged containers."""
    now_pt = datetime.now(PT).strftime("%A, %b %d, %Y, %I:%M %p PT")
    actionable = extra.get("dossier", [])
    kev_risks = extra.get("kev_risks", [])
    stale = extra.get("stale_transients", [])
    ha_telemetry = extra.get("ha_telemetry", {})
    total = len(actionable)

    dossier_json = json.dumps({
        "actionable_containers": actionable,
        "kev_vulnerability_risks": kev_risks,
        "cleared_stale_transients": [s["container"] for s in stale],
        "ha_telemetry_anomalies": {
            "healed_events": ha_telemetry.get("healed_events", []),
            "zombies": ha_telemetry.get("zombies", []),
            "amplitude_anomalies": ha_telemetry.get("amplitude_anomalies", []),
            "network_anomalies": ha_telemetry.get("network_anomalies", []),
        } if ha_telemetry.get("has_anomalies") else "nominal"
    }, indent=2)

    return (
        f"Nightly Autonomous NAS Log & Telemetry Triage [{now_pt}]:\n"
        f"The log harvester scanned {extra.get('scanned', 39)} running containers across Host1 (.82) and Host2 (.84) "
        f"and flagged candidate issues in {total} container(s).\n\n"
        f"### Incident Dossier:\n```json\n{dossier_json}\n```\n\n"
        f"### Autonomous Directives:\n"
        f"1. **Forensic Liveness & Root-Cause Analysis:** Silently investigate each flagged container and any HA telemetry anomalies. Determine if errors are actively recurring or transient hiccups.\n"
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
