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

import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _p in (_REPO_ROOT, _TOOLS_DIR, "/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.append(_p)


# Resolve SSH and host configuration from nas_docker_mcp or environment
def _resolve_nas_config():
    try:
        try:
            from tools.nas_docker_mcp import _resolve_nas_config as _mcp_resolve
        except ImportError:
            from nas_docker_mcp import _resolve_nas_config as _mcp_resolve
        return _mcp_resolve()
    except Exception:  # noqa: BLE001
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
import subprocess, json, time, re, calendar, os
from concurrent.futures import ThreadPoolExecutor

now_utc = time.time()
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Noise filter for benign, expected, or standby operational chatter
noise_re = re.compile(
    r'('
    r'\[debug\]|'
    r'libusb_init failed|'
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
    r'CHIP:DL: Failed to (?:get WiFi interface|reset WiFi statistic counts)|'
    r'CHIP_ERROR.*Shutting down the stack|'
    r'CHIP_ERROR.*Failed to advertise records.*(?:Network is unreachable|No endpoint was available)|'
    r'CHIP_ERROR.*No unsolicited message handler at.*BdxTransferServer|'
    r'CHIP_ERROR.*Time out! failed to receive report data from Exchange|'
    r'CHIP_ERROR.*SendMessage\(\) to UDP:\[fd[0-9a-f:]+\]:5540 failed:.*Network is unreachable|'
    r'matter_server\.server\.client_handler.*Error while handling: read_attribute.*Network is unreachable|'
    r'Stopped reading data from server error=.*EOF|'
    r'Fontconfig error: No writable cache directories|'
    r'CHIP_ERROR.*Subscription Liveness timeout|'
    r'matter_server.*Node.*is offline|'
    r'matter_server.*Unable to subscribe to Node|'
    r'matter_server.*Subscription failed with (?:CHIP|IM) Error|'
    r'browserless\.io:server:error WebSocket socket error:\s*(?:write EPIPE|read ECONNRESET)|'
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
    r'(?:\d{1,3}\.){3}\d{1,3}:3333.*connection refused|'
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
        tail_bytes = len(logs_res.stdout.encode('utf-8', errors='replace'))
    except subprocess.TimeoutExpired:
        raw_lines = []
        tail_bytes = 0

    valid_errs = []
    suppressed_errors = 0
    suppressed_active = 0
    suppressed_samples = []
    first_ts = None
    last_ts = None
    last_sample = ''

    for rl in raw_lines:
        clean = ansi_escape.sub('', rl).strip()
        if not clean:
            continue
        m = iso_re.match(clean)
        age = 0.0
        ts_str = ''
        msg = clean
        if m:
            ts_str, msg = m.group(1), m.group(2)
            try:
                dt_str = ts_str[:19]
                epoch = float(calendar.timegm(time.strptime(dt_str, '%Y-%m-%dT%H:%M:%S')))
                if len(ts_str) > 20 and ts_str[19] == '.':
                    frac = ts_str[20:].rstrip('Z')[:6]
                    if frac.isdigit():
                        epoch += float('0.' + frac)
                age = max(0.0, now_utc - epoch)
                if first_ts is None or epoch < first_ts:
                    first_ts = epoch
                if last_ts is None or epoch > last_ts:
                    last_ts = epoch
            except Exception:
                age = 0.0
        last_sample = msg[:200]
        if not err_re.search(clean):
            continue
        if noise_re.search(clean):
            suppressed_errors += 1
            if age < 3600:
                suppressed_active += 1
            suppressed_samples.append(msg[:200])
            continue
        valid_errs.append({'age': age, 'ts': ts_str, 'msg': msg})

    tail_span_sec = round(max(0.0, last_ts - first_ts), 3) if (first_ts is not None and last_ts is not None and last_ts > first_ts) else 0.0

    flagged_item = None
    if valid_errs:
        active_cnt = sum(1 for e in valid_errs if e['age'] < 3600)
        recent_cnt = sum(1 for e in valid_errs if 3600 <= e['age'] < 21600)
        hist_cnt = sum(1 for e in valid_errs if e['age'] >= 21600)
        is_transient = (active_cnt == 0 and recent_cnt == 0 and hist_cnt > 0)
        flagged_item = {
            'container': cname,
            'status': status,
            'total_errors': len(valid_errs),
            'active_errors': active_cnt,
            'recent_errors': recent_cnt,
            'historical_errors': hist_cnt,
            'is_transient': is_transient,
            'sample_errors': [e['msg'][:200] for e in valid_errs[-5:]]
        }

    muted_storm = None
    if suppressed_errors >= 50:
        muted_storm = {
            'container': cname,
            'status': status,
            'suppressed_errors': suppressed_errors,
            'suppressed_active': suppressed_active,
            'sample_suppressed': suppressed_samples[-2:]
        }

    tail_stat = {
        'container': cname,
        'tail_lines': len(raw_lines),
        'tail_bytes': tail_bytes,
        'tail_span_sec': tail_span_sec,
        'sample_line': last_sample
    }

    return {
        'flagged': flagged_item,
        'muted_storm': muted_storm,
        'tail_stat': tail_stat
    }

flagged = []
muted_storms = []
tail_stats = {}
with ThreadPoolExecutor(max_workers=10) as executor:
    results = executor.map(inspect_container, targets)
    for r in results:
        if not r:
            continue
        if r.get('flagged'):
            flagged.append(r['flagged'])
        if r.get('muted_storm'):
            muted_storms.append(r['muted_storm'])
        tstat = r.get('tail_stat')
        if tstat and tstat.get('container'):
            tail_stats[tstat['container']] = tstat

versions = {}
try:
    with open('/etc.defaults/VERSION') as vf:
        kv = dict(re.findall(r'^(\w+)="([^"]*)"', vf.read(), re.M))
        if kv.get('productversion'):
            versions['synology-dsm'] = kv['productversion']
except Exception:
    pass

db_log_containers = []
log_metrics = {}
for nm, tstat in tail_stats.items():
    t_lines = int(tstat.get('tail_lines', 0) or 0)
    t_bytes = int(tstat.get('tail_bytes', 0) or 0)
    t_span = float(tstat.get('tail_span_sec', 0.0) or 0.0)
    s_line = str(tstat.get('sample_line', '') or '')
    est_mb_day = round((t_bytes / t_span) * 86400.0 / (1024.0 * 1024.0), 2) if (t_lines >= 200 and t_span > 0 and t_bytes > 0) else 0.0
    log_metrics[nm] = {
        'container': nm,
        'log_driver': '',
        'log_path': '',
        'log_size_bytes': 0,
        'log_size_mb': 0.0,
        'tail_lines': t_lines,
        'tail_bytes': t_bytes,
        'tail_span_sec': t_span,
        'est_mb_per_day': est_mb_day,
        'sample_line': s_line,
    }

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
                host_cfg = item.get('HostConfig') or {}
                log_cfg = host_cfg.get('LogConfig') or {}
                log_type = str(log_cfg.get('Type') or '').strip()
                if log_type == 'db':
                    db_log_containers.append({'container': nm, 'log_driver': 'db'})
                log_path = str(item.get('LogPath') or '').strip()
                log_size_bytes = 0
                if log_path:
                    try:
                        log_size_bytes = int(os.path.getsize(log_path))
                    except Exception:
                        log_size_bytes = 0
                entry = log_metrics.setdefault(nm, {
                    'container': nm,
                    'tail_lines': 0,
                    'tail_bytes': 0,
                    'tail_span_sec': 0.0,
                    'est_mb_per_day': 0.0,
                    'sample_line': '',
                })
                entry['log_driver'] = log_type
                entry['log_path'] = log_path
                entry['log_size_bytes'] = log_size_bytes
                entry['log_size_mb'] = round(log_size_bytes / (1024.0 * 1024.0), 2)
except Exception:
    pass

velocity_alerts = [
    {
        'container': m['container'],
        'est_mb_per_day': m['est_mb_per_day'],
        'log_size_mb': m['log_size_mb'],
        'sample_line': m['sample_line'],
    }
    for m in log_metrics.values()
    if m.get('est_mb_per_day', 0.0) > 5.0
]

print(json.dumps({
    'scanned': len(targets),
    'flagged': flagged,
    'versions': versions,
    'muted_storms': muted_storms,
    'db_log_containers': db_log_containers,
    'log_metrics': log_metrics,
    'velocity_alerts': velocity_alerts,
}))
'''

DEFAULT_LOG_VELOCITY_MB_PER_DAY = 5.0
VELOCITY_STATE_FILE = DATA_DIR / "nas_log_velocity_state.json"

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
    try:
        if KEV_CACHE_FILE.exists() and (time.time() - KEV_CACHE_FILE.stat().st_mtime) < max_age_sec:
            data = json.loads(KEV_CACHE_FILE.read_text())
            return data.get("vulnerabilities", []) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        req = urllib.request.Request(CISA_KEV_URL, headers={"User-Agent": "Zero-Homelab-Monitor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict) and "vulnerabilities" in data:
                try:
                    tmp_kev = KEV_CACHE_FILE.with_suffix(".tmp")
                    tmp_kev.write_text(raw, encoding="utf-8")
                    tmp_kev.replace(KEV_CACHE_FILE)
                except Exception:  # noqa: BLE001, S110
                    pass
                return data["vulnerabilities"]
    except Exception:  # noqa: BLE001
        try:
            if KEV_CACHE_FILE.exists():
                data = json.loads(KEV_CACHE_FILE.read_text())
                return data.get("vulnerabilities", []) if isinstance(data, dict) else []
        except Exception:  # noqa: BLE001, S110
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

    ver_extract_re = re.compile(r"(?:prior to|before|earlier than|<|<=)\s*v?(\d+\.\d+(?:\.\d+)*)", re.IGNORECASE)

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

def _ssh_python_batch(host: str, since: str = "24h", timeout: int = 20, port: str | None = None, key: str | None = None, user: str | None = None) -> dict:
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
        ], input=script, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
        else:
            err_snip = proc.stderr.strip()[:120] if proc.stderr else f"exit code {proc.returncode}"
            print(f"[NASTriage] Warning on {host}: {err_snip}", file=sys.stderr)
            return {"scanned": 0, "flagged": [], "error": err_snip}
    except Exception as e:  # noqa: BLE001
        print(f"[NASTriage] Error connecting to {host}: {e}", file=sys.stderr)
        return {"scanned": 0, "flagged": [], "error": str(e)}


def evaluate_log_velocity(
    metrics_by_host: dict | list,
    threshold_mb_per_day: float = DEFAULT_LOG_VELOCITY_MB_PER_DAY,
    state_file: Path | None = None,
    now_ts: float | None = None,
) -> list[dict]:
    """
    Evaluate per-container log write velocity using tail timestamp span projection
    (when >=200 lines fill the 250-line tail) and/or persisted LogPath byte deltas across runs.
    Flags any container exceeding threshold_mb_per_day (default 5.0 MB/day).
    """
    if not metrics_by_host:
        return []

    curr_ts = float(now_ts if now_ts is not None else time.time())
    target_state_file = state_file or VELOCITY_STATE_FILE
    prev_state: dict[str, dict] = {}
    try:
        if target_state_file and target_state_file.exists():
            loaded = json.loads(target_state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_state = loaded
    except Exception:  # noqa: BLE001
        prev_state = {}

    normalized_items: list[tuple[str, str, dict]] = []
    if isinstance(metrics_by_host, list):
        for item in metrics_by_host:
            if isinstance(item, dict):
                cname = str(item.get("container") or item.get("name") or "").strip()
                host = str(item.get("host") or "unknown").strip()
                if cname:
                    normalized_items.append((host, cname, item))
    elif isinstance(metrics_by_host, dict):
        # Support either {host: {cname: metrics}} or {host: [metrics]} or single metric dict
        if "container" in metrics_by_host and ("tail_bytes" in metrics_by_host or "tail_lines" in metrics_by_host):
            cname = str(metrics_by_host.get("container") or "").strip()
            host = str(metrics_by_host.get("host") or "unknown").strip()
            if cname:
                normalized_items.append((host, cname, metrics_by_host))
        else:
            for host_key, host_val in metrics_by_host.items():
                if isinstance(host_val, dict):
                    if "tail_bytes" in host_val or "tail_lines" in host_val or "log_size_bytes" in host_val:
                        cname = str(host_val.get("container") or host_key).strip()
                        host = str(host_val.get("host") or "unknown").strip()
                        normalized_items.append((host, cname, host_val))
                    else:
                        for cname, m_dict in host_val.items():
                            if isinstance(m_dict, dict):
                                normalized_items.append((str(host_key), str(m_dict.get("container") or cname), m_dict))
                elif isinstance(host_val, list):
                    for m_dict in host_val:
                        if isinstance(m_dict, dict):
                            cname = str(m_dict.get("container") or m_dict.get("name") or "").strip()
                            if cname:
                                normalized_items.append((str(host_key), cname, m_dict))

    new_state = dict(prev_state)
    alerts: list[dict] = []

    for host, cname, item in normalized_items:
        tail_lines = int(item.get("tail_lines", 250 if "tail_bytes" in item and "tail_lines" not in item else 0) or 0)
        tail_bytes = int(item.get("tail_bytes", 0) or 0)
        tail_span_sec = float(item.get("tail_span_sec", 0.0) or 0.0)
        log_size_bytes = int(item.get("log_size_bytes", 0) or 0)
        raw_size_mb = item.get("log_size_mb")
        if isinstance(raw_size_mb, (int, float)) and raw_size_mb > 0:
            log_size_mb = round(float(raw_size_mb), 2)
        else:
            log_size_mb = round(log_size_bytes / (1024.0 * 1024.0), 2) if log_size_bytes > 0 else 0.0
        sample_line = str(item.get("sample_line") or "")[:200]

        tail_rate_mb_day = 0.0
        if tail_lines >= 200 and tail_span_sec > 0 and tail_bytes > 0:
            tail_rate_mb_day = (tail_bytes / tail_span_sec) * 86400.0 / (1024.0 * 1024.0)
        elif item.get("est_mb_per_day") is not None and tail_lines == 0 and tail_bytes == 0:
            tail_rate_mb_day = float(item.get("est_mb_per_day") or 0.0)

        delta_rate_mb_day = 0.0
        state_key = f"{host}:{cname}"
        if log_size_bytes > 0:
            prev_entry = prev_state.get(state_key)
            if isinstance(prev_entry, dict):
                prev_bytes = int(prev_entry.get("log_size_bytes", 0) or 0)
                prev_ts = float(prev_entry.get("ts", 0.0) or 0.0)
                dt = curr_ts - prev_ts
                dbytes = log_size_bytes - prev_bytes
                if dt >= 60.0 and dbytes > 0:
                    delta_rate_mb_day = (dbytes / dt) * 86400.0 / (1024.0 * 1024.0)
            new_state[state_key] = {
                "log_size_bytes": log_size_bytes,
                "ts": curr_ts,
            }

        est_mb_per_day = round(max(tail_rate_mb_day, delta_rate_mb_day), 2)
        if est_mb_per_day > threshold_mb_per_day:
            alerts.append({
                "container": cname,
                "host": host,
                "est_mb_per_day": est_mb_per_day,
                "log_size_mb": log_size_mb,
                "sample_line": sample_line,
            })

    is_test_runner = (
        "PYTEST_CURRENT_TEST" in os.environ
        or "pytest" in sys.modules
        or ("unittest" in sys.modules and any(k.startswith("test_") or ".test_" in k for k in list(sys.modules)))
    )
    if target_state_file and (state_file is not None or not is_test_runner):
        try:
            target_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_state = target_state_file.with_suffix(".tmp")
            tmp_state.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
            tmp_state.replace(target_state_file)
        except Exception:  # noqa: BLE001, S110
            pass

    return alerts


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
            "velocity_alerts": list[dict],
            "muted_storms": list[dict],
            "db_log_containers": list[dict],
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
    muted_storms = []
    db_log_containers = []
    log_metrics_by_host = {}
    pre_velocity_alerts = []

    h1, h2, _ = _resolve_nas_config()
    hosts = [h1 or HOST_1_IP, h2 or HOST_2_IP]
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        futures = {executor.submit(_ssh_python_batch, host, since=since): host for host in hosts}
        for future, host in futures.items():
            try:
                res = future.result()
                scanned = res.get("scanned", 0)
                total_scanned += scanned
                if res.get("error"):
                    host_errors[host] = res["error"]
                if res.get("versions"):
                    versions_by_host[host] = res["versions"]
                if res.get("log_metrics"):
                    log_metrics_by_host[host] = res["log_metrics"]
                for item in res.get("flagged", []):
                    item["host"] = host
                    if item.get("is_transient"):
                        stale.append(item)
                    else:
                        actionable.append(item)
                for storm in res.get("muted_storms", []):
                    if isinstance(storm, dict):
                        s_item = dict(storm)
                        s_item["host"] = host
                        muted_storms.append(s_item)
                for db_item in res.get("db_log_containers", []):
                    if isinstance(db_item, dict):
                        d_entry = dict(db_item)
                        d_entry.setdefault("host", host)
                        d_entry.setdefault("log_driver", "db")
                        db_log_containers.append(d_entry)
                    elif isinstance(db_item, str):
                        db_log_containers.append({"container": db_item, "host": host, "log_driver": "db"})
                for valert in res.get("velocity_alerts", []):
                    if isinstance(valert, dict):
                        v_item = dict(valert)
                        v_item.setdefault("host", host)
                        pre_velocity_alerts.append(v_item)
            except Exception as e:  # noqa: BLE001
                host_errors[host] = str(e)

    velocity_alerts = evaluate_log_velocity(log_metrics_by_host) if log_metrics_by_host else []
    seen_vel = {(v.get("host"), v.get("container")) for v in velocity_alerts}
    for v_item in pre_velocity_alerts:
        v_key = (v_item.get("host"), v_item.get("container"))
        if v_key not in seen_vel and float(v_item.get("est_mb_per_day", 0.0) or 0.0) > DEFAULT_LOG_VELOCITY_MB_PER_DAY:
            seen_vel.add(v_key)
            velocity_alerts.append(v_item)

    duration = time.time() - t0
    return {
        "total_scanned": total_scanned,
        "actionable_issues": actionable,
        "stale_transients": stale,
        "host_errors": host_errors,
        "versions_by_host": versions_by_host,
        "velocity_alerts": velocity_alerts,
        "muted_storms": muted_storms,
        "db_log_containers": db_log_containers,
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


def _format_velocity_report_block(velocity_alerts: list[dict]) -> str:
    if not velocity_alerts:
        return ""
    lines = [f"📈 **Log Velocity / Flood Alert ({len(velocity_alerts)}):**"]
    for v in velocity_alerts:
        cname = v.get("container", "unknown")
        host = v.get("host")
        host_part = f" ({host})" if host else ""
        rate = float(v.get("est_mb_per_day", 0.0) or 0.0)
        size_mb = v.get("log_size_mb")
        size_part = f", current log `{float(size_mb):.2f} MB`" if isinstance(size_mb, (int, float)) and size_mb > 0 else ""
        sample = str(v.get("sample_line") or "").strip()
        sample_part = f" — sample: `{sample[:140]}`" if sample else ""
        lines.append(
            f"- **{cname}**{host_part}: `{rate:.2f} MB/day` (> `{DEFAULT_LOG_VELOCITY_MB_PER_DAY:.1f} MB/day` threshold{size_part}){sample_part}"
        )
    return "\n".join(lines)


def _format_muted_storms_report_block(muted_storms: list[dict]) -> str:
    if not muted_storms:
        return ""
    lines = [f"🔇 **Muted Error Storm Circuit Breaker ({len(muted_storms)}):**"]
    for s in muted_storms:
        cname = s.get("container", "unknown")
        host = s.get("host")
        host_part = f" ({host})" if host else ""
        supp = int(s.get("suppressed_errors", 0) or 0)
        act = int(s.get("suppressed_active", 0) or 0)
        samples = s.get("sample_suppressed") or []
        sample_part = f" — sample: `{str(samples[0])[:140]}`" if samples else ""
        lines.append(
            f"- **{cname}**{host_part}: `{supp}` muted error lines in 250-line tail (`{act}` active <1h){sample_part}"
        )
    return "\n".join(lines)


def _format_db_driver_drift_block(db_log_containers: list) -> str:
    if not db_log_containers:
        return ""
    lines = [f"🗃️ **SQLite `db` Log-Driver Drift Flagged ({len(db_log_containers)}):**"]
    for item in db_log_containers:
        if isinstance(item, dict):
            cname = item.get("container") or item.get("name") or "unknown"
            host = item.get("host")
            driver = item.get("log_driver") or "db"
            host_part = f" ({host})" if host else ""
            lines.append(
                f"- **{cname}**{host_part}: using Synology `{driver}` log driver (migrate to `json-file` with `max-size=10m`, `max-file=3`)"
            )
        else:
            lines.append(
                f"- **{item}**: using Synology `db` log driver (migrate to `json-file` with `max-size=10m`, `max-file=3`)"
            )
    return "\n".join(lines)


UNIFI_ENV_FILE = Path("/secrets/env.json")
_BENIGN_LOCAL_DNS_BLOCK_IPS = frozenset({"127.0.0.1", "::1", "0.0.0.0"})


def _get_unifi_credentials() -> tuple[str, str, str]:
    """Load (base_url, username, password) from os.environ or /secrets/env.json."""
    base = os.environ.get("UNIFI_BASE_URL", "")
    user = os.environ.get("UNIFI_USER", "")
    pw = os.environ.get("UNIFI_PASSWORD", "")
    if (not base or not user or not pw) and UNIFI_ENV_FILE.exists():
        try:
            env_data = json.loads(UNIFI_ENV_FILE.read_text(encoding="utf-8"))
            if isinstance(env_data, dict):
                base = base or str(env_data.get("UNIFI_BASE_URL", ""))
                user = user or str(env_data.get("UNIFI_USER", ""))
                pw = pw or str(env_data.get("UNIFI_PASSWORD", ""))
        except (OSError, ValueError, TypeError):
            pass
    return base.strip().rstrip("/"), user.strip(), pw


def _extract_unifi_flows(raw_data: object) -> list[dict]:
    """Extract flow records from UniFi traffic-flows response payload."""
    if isinstance(raw_data, list):
        return [f for f in raw_data if isinstance(f, dict)]
    if isinstance(raw_data, dict):
        if isinstance(raw_data.get("flows"), list):
            return [f for f in raw_data["flows"] if isinstance(f, dict)]
        data_field = raw_data.get("data")
        if isinstance(data_field, dict) and isinstance(data_field.get("flows"), list):
            return [f for f in data_field["flows"] if isinstance(f, dict)]
        if isinstance(data_field, list):
            return [f for f in data_field if isinstance(f, dict)]
    return []


def _normalize_unifi_flow(flow: dict) -> dict | None:
    """Normalize a UniFi IDS/traffic-flow item and drop benign local DNS blocks."""
    dest = flow.get("destination") if isinstance(flow.get("destination"), dict) else {}
    src = flow.get("source") if isinstance(flow.get("source"), dict) else {}

    dst_ip = str(
        dest.get("ip")
        or flow.get("destination_ip")
        or flow.get("dest_ip")
        or flow.get("dst_ip")
        or ""
    ).strip()
    if dst_ip in _BENIGN_LOCAL_DNS_BLOCK_IPS:
        return None

    risk_val = flow.get("risk")
    if isinstance(risk_val, dict):
        risk_level = str(risk_val.get("level") or "").strip().upper()
        risk_cat = str(risk_val.get("category") or "").strip()
    else:
        risk_level = str(risk_val or flow.get("risk_level") or "").strip().upper()
        risk_cat = str(flow.get("category") or "").strip()

    if risk_level in {"LOW", "NONE"}:
        return None

    ips_obj = flow.get("ips") if isinstance(flow.get("ips"), dict) else {}
    signature = str(
        ips_obj.get("signature")
        or flow.get("signature")
        or flow.get("threat_name")
        or risk_cat
        or ""
    ).strip()

    src_ip = str(
        src.get("ip")
        or flow.get("source_ip")
        or flow.get("src_ip")
        or ""
    ).strip()
    src_name = str(
        src.get("name")
        or src.get("hostname")
        or flow.get("source_name")
        or flow.get("client_name")
        or ""
    ).strip()
    dst_port = dest.get("port") if dest.get("port") is not None else flow.get("destination_port") or flow.get("dst_port")
    dst_domain = str(dest.get("domain") or flow.get("destination_domain") or flow.get("domain") or "").strip()
    dst_country = str(dest.get("country") or flow.get("destination_country") or "").strip()
    src_port = src.get("port") if src.get("port") is not None else flow.get("source_port") or flow.get("src_port")

    return {
        "id": flow.get("id", ""),
        "risk": risk_level or "UNKNOWN",
        "category": risk_cat,
        "signature": signature,
        "action": str(flow.get("action") or "").strip(),
        "protocol": str(flow.get("protocol") or "").strip(),
        "source_ip": src_ip,
        "source_name": src_name,
        "source_port": src_port,
        "destination_ip": dst_ip,
        "destination_port": dst_port,
        "destination_domain": dst_domain,
        "destination_country": dst_country,
        "timestamp": flow.get("flow_start_time") or flow.get("timestamp") or flow.get("time") or "",
    }


def fetch_unifi_ids_alerts(hours: int = 24) -> dict:
    """Query UCG-Max UniFi OS traffic-flows for HIGH/MEDIUM IDS threat alerts."""
    base_url, user, password = _get_unifi_credentials()
    if not base_url or not user or not password:
        return {"ok": False, "error": "UniFi credentials not configured", "alerts": [], "count": 0}

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(cj),
    )

    try:
        login_payload = json.dumps({"username": user, "password": password}).encode("utf-8")
        login_req = urllib.request.Request(
            f"{base_url}/api/auth/login",
            data=login_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login_req, timeout=8) as resp:
            csrf_token = (
                resp.headers.get("X-Csrf-Token")
                or resp.headers.get("X-CSRF-Token")
                or resp.headers.get("x-csrf-token")
                or ""
            )

        now_ms = int(time.time() * 1000)
        from_ms = now_ms - (max(1, int(hours)) * 3600 * 1000)
        flow_headers = {"Content-Type": "application/json"}
        if csrf_token:
            flow_headers["X-Csrf-Token"] = csrf_token
        flow_payload = json.dumps({
            "timestampFrom": from_ms,
            "timestampTo": now_ms,
            "risks": ["HIGH", "MEDIUM"],
        }).encode("utf-8")
        flow_req = urllib.request.Request(
            f"{base_url}/proxy/network/v2/api/site/default/traffic-flows",
            data=flow_payload,
            headers=flow_headers,
            method="POST",
        )
        with opener.open(flow_req, timeout=10) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
        raw_data = json.loads(raw_body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "alerts": [], "count": 0}

    alerts: list[dict] = []
    for flow in _extract_unifi_flows(raw_data):
        normalized = _normalize_unifi_flow(flow)
        if normalized is not None:
            alerts.append(normalized)

    return {"ok": True, "alerts": alerts, "count": len(alerts)}


def _format_unifi_ids_report_block(alerts: list[dict]) -> str:
    if not alerts:
        return ""
    lines = [f"🚨 **UniFi IDS Alerts ({len(alerts)}):**"]
    for alert in alerts[:10]:
        risk = alert.get("risk") or "UNKNOWN"
        src_name = alert.get("source_name") or ""
        src_ip = alert.get("source_ip") or ""
        if src_name and src_ip and src_name != src_ip:
            src = f"{src_name} ({src_ip})"
        else:
            src = src_name or src_ip or "unknown"

        dst_ip = alert.get("destination_ip") or "unknown"
        dst_port = alert.get("destination_port")
        dst = f"{dst_ip}:{dst_port}" if dst_port else dst_ip
        if alert.get("destination_domain"):
            dst = f"{alert['destination_domain']} ({dst})"

        sig = alert.get("signature") or alert.get("category") or "IDS threat flow"
        act = alert.get("action")
        act_str = f" [{act}]" if act else ""
        lines.append(f"- **[{risk}]**{act_str} `{src}` -> `{dst}` — {sig}")
    if len(alerts) > 10:
        lines.append(f"- *...and {len(alerts) - 10} more alert(s).*")
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
    velocity_alerts = res.get("velocity_alerts", []) or []
    muted_storms = res.get("muted_storms", []) or []
    db_log_containers = res.get("db_log_containers", []) or []

    # Silent high-threshold CISA KEV + critical version floor check
    kev_risks = []
    try:
        kev_risks = evaluate_kev_risks(versions_by_host)
    except Exception:  # noqa: BLE001
        kev_risks = []
    kev_report = _format_kev_report_block(kev_risks)
    velocity_report = _format_velocity_report_block(velocity_alerts)
    muted_storms_report = _format_muted_storms_report_block(muted_storms)
    db_drift_report = _format_db_driver_drift_block(db_log_containers)

    # Run Home Assistant + VictoriaMetrics statistical telemetry & self-healing check
    ha_telemetry = {}
    ha_report = ""
    try:
        from tools.ha_anomaly_detector import analyze_anomalies, format_report
        ha_telemetry = analyze_anomalies(auto_heal=True)
        if ha_telemetry.get("has_anomalies"):
            ha_report = format_report(ha_telemetry, quiet=True)
    except Exception as e:  # noqa: BLE001
        ha_telemetry = {"ok": False, "error": str(e)}

    # Poll UCG-Max UniFi IDS threat flows (silent on 0 alerts or API failure)
    unifi_ids = {"ok": False, "alerts": [], "count": 0}
    try:
        unifi_ids = fetch_unifi_ids_alerts(24)
    except Exception as e:  # noqa: BLE001
        unifi_ids = {"ok": False, "error": str(e), "alerts": [], "count": 0}
    ids_alerts = unifi_ids.get("alerts", []) if isinstance(unifi_ids, dict) else []
    ids_count = int(unifi_ids.get("count", len(ids_alerts))) if isinstance(unifi_ids, dict) else 0
    ids_report = _format_unifi_ids_report_block(ids_alerts)

    extra = {
        "total_issues": (
            len(actionable)
            + len(kev_risks)
            + max(0, ids_count)
            + len(velocity_alerts)
            + len(muted_storms)
            + len(db_log_containers)
        ),
        "tier1_count": sum(1 for a in actionable if any("fatal" in s.lower() or "panic" in s.lower() or re.search(r"\boom\b", s, re.IGNORECASE) for s in a.get("sample_errors", []))) + len(kev_risks),
        "tier2_count": len(actionable) + len(velocity_alerts) + len(muted_storms) + len(db_log_containers),
        "dossier": actionable,
        "kev_risks": kev_risks,
        "unifi_ids_alerts": unifi_ids,
        "velocity_alerts": velocity_alerts,
        "muted_storms": muted_storms,
        "db_log_containers": db_log_containers,
        "stale_transients": stale,
        "duration_sec": res["duration_sec"],
        "scanned": total_scanned,
        "ha_telemetry": ha_telemetry,
    }

    host_errors = res.get("host_errors", {})
    extra_blocks = [b for b in (ha_report, kev_report, ids_report, velocity_report, muted_storms_report, db_drift_report) if b]

    # Failure guard: 0 containers scanned means connection/timeout failure, not nominal health
    if total_scanned == 0:
        err_details = "; ".join(f"{h}: {e}" for h, e in host_errors.items()) if host_errors else "SSH timeout / unreachable"
        rep = f"⚠️ **NAS Log Review** [{now_pt}]: Failed to scan containers across NAS clusters ({err_details})."
        for blk in extra_blocks:
            rep = f"{rep}\n\n{blk}"
        return False, rep, extra

    # Nominal container logs: All scanned containers healthy or only harmless historical transients
    if len(actionable) == 0:
        if host_errors:
            err_hosts = ", ".join(host_errors.keys())
            rep = f"⚠️ **NAS Log Review** [{now_pt}]: {total_scanned} containers healthy, but scan failed on {err_hosts}."
            for blk in extra_blocks:
                rep = f"{rep}\n\n{blk}"
            return False, rep, extra
        monitored_ha = ha_telemetry.get("monitored_count", 0)
        unifi_cnt = ha_telemetry.get("unifi_clients_count", 0)
        unifi_part = f", {unifi_cnt} UniFi clients" if unifi_cnt else ""
        ha_suffix = f" ({monitored_ha} HA sensors{unifi_part} nominal)" if monitored_ha and not ha_report else ""
        rep = f"🗄️ **NAS Log Review** [{now_pt}]: All {total_scanned} containers healthy across .82 and .84{ha_suffix}. ✅"
        for blk in extra_blocks:
            rep = f"{rep}\n\n{blk}"
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

    for blk in extra_blocks:
        lines.append(f"\n{blk}")

    return True, "\n".join(lines).strip(), extra


def format_autonomous_triage_prompt(extra: dict) -> str:
    """Format an actionable prompt for Zero to investigate and auto-remediate flagged containers."""
    now_pt = datetime.now(PT).strftime("%A, %b %d, %Y, %I:%M %p PT")
    actionable = extra.get("dossier", [])
    kev_risks = extra.get("kev_risks", [])
    stale = extra.get("stale_transients", [])
    ha_telemetry = extra.get("ha_telemetry", {})
    velocity_alerts = extra.get("velocity_alerts", []) or []
    muted_storms = extra.get("muted_storms", []) or []
    db_log_containers = extra.get("db_log_containers", []) or []
    raw_ids = extra.get("unifi_ids_alerts", {})
    if isinstance(raw_ids, dict):
        ids_alerts = raw_ids.get("alerts", [])
    elif isinstance(raw_ids, list):
        ids_alerts = raw_ids
    else:
        ids_alerts = []
    total = len(actionable)

    dossier_payload = {
        "actionable_containers": actionable,
        "kev_vulnerability_risks": kev_risks,
        "cleared_stale_transients": [s["container"] for s in stale],
        "ha_telemetry_anomalies": {
            "healed_events": ha_telemetry.get("healed_events", []),
            "zombies": ha_telemetry.get("zombies", []),
            "amplitude_anomalies": ha_telemetry.get("amplitude_anomalies", []),
            "network_anomalies": ha_telemetry.get("network_anomalies", []),
        } if ha_telemetry.get("has_anomalies") else "nominal"
    }
    if ids_alerts:
        dossier_payload["unifi_ids_alerts"] = ids_alerts
    if velocity_alerts:
        dossier_payload["velocity_alerts"] = velocity_alerts
    if muted_storms:
        dossier_payload["muted_storms"] = muted_storms
    if db_log_containers:
        dossier_payload["db_log_containers"] = db_log_containers

    dossier_json = json.dumps(dossier_payload, indent=2)
    extra_sections = ""
    if ids_alerts:
        extra_sections += f"\n\n### UniFi IDS Alerts:\n{_format_unifi_ids_report_block(ids_alerts)}"
    if velocity_alerts:
        extra_sections += f"\n\n### Log Velocity Alerts:\n{_format_velocity_report_block(velocity_alerts)}"
    if muted_storms:
        extra_sections += f"\n\n### Muted Error Storms:\n{_format_muted_storms_report_block(muted_storms)}"
    if db_log_containers:
        extra_sections += f"\n\n### SQLite Log-Driver Drift:\n{_format_db_driver_drift_block(db_log_containers)}"

    return (
        f"Nightly Autonomous NAS Log & Telemetry Triage [{now_pt}]:\n"
        f"The log harvester scanned {extra.get('scanned', 39)} running containers across Host1 (.82) and Host2 (.84) "
        f"and flagged candidate issues in {total} container(s).\n\n"
        f"### Incident Dossier:\n```json\n{dossier_json}\n```{extra_sections}\n\n"
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
