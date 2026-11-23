#!/usr/bin/env python3
"""Matter-over-Thread Auto-Recovery Watchdog Sidecar.

Detects sleepy Matter-over-Thread battery sensors (e.g., IKEA MYGGSPRAY motion
sensors) parked as ``available=False`` by Matter Server after its 30-minute mDNS
retry timeout, persists last-known IPv6 addresses across restarts, and injects
a temporary 2-second ``_matter._tcp.local.`` mDNS ``ServiceInfo`` poke plus
``ping_node`` inside ``matterserver`` to restore subscriptions silently.

Alerts only when consecutive recovery attempts fail (indicating a genuinely
dead battery or hardware dropout) or when a previously alerted sensor recovers.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("matter_thread_watchdog")

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
for _path in (str(REPO_ROOT), str(TOOLS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from tools import nas_config
except ImportError:
    import nas_config  # type: ignore[no-redef]

DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DEFAULT_STATE_PATH = DATA_DIR / "matter_thread_watchdog_state.json"

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_ALERT_COOLDOWN_SECONDS = 43200  # 12 hours
DEFAULT_POKE_BACKOFF_SECONDS = 21600  # 6 hours after hitting failure threshold


def should_attempt_poke(
    node_state: dict[str, Any] | None,
    now_ts: float,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    backoff_seconds: int = DEFAULT_POKE_BACKOFF_SECONDS,
) -> bool:
    """Return True if an offline node is eligible for an mDNS recovery poke.

    Once a node hits ``failure_threshold`` consecutive failed recoveries, backs
    off to poking at most once every ``backoff_seconds`` (default 6 hours) so a
    marginal or dying battery sensor is never hammered.
    """
    if not isinstance(node_state, dict):
        return True
    failures = int(node_state.get("consecutive_failures") or 0)
    if failures < failure_threshold:
        return True
    last_poke_ts = node_state.get("last_poke_ts")
    if last_poke_ts is None:
        return True
    return (float(now_ts) - float(last_poke_ts)) >= backoff_seconds

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from log or terminal text."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def format_matter_service_name(compressed_fabric_id: int, node_id: int) -> str:
    """Format canonical Matter operational mDNS service name.

    Returns ``<16-HEX-FABRIC>-<16-HEX-NODE>._matter._tcp.local.``.
    """
    return f"{int(compressed_fabric_id):016X}-{int(node_id):016X}._matter._tcp.local."


def is_tracked_thread_battery_node(node: dict[str, Any]) -> bool:
    """Return True if node is a battery-powered Thread or IKEA Matter sensor.

    Matches nodes exposing PowerSource battery attributes (``0/47/11`` or
    ``0/47/12``) AND either Thread Network Diagnostics (``0/53/*``) or an
    IKEA manufacturer string in Basic Information (``0/40/1``).
    """
    if not isinstance(node, dict):
        return False
    attrs = node.get("attributes")
    if not isinstance(attrs, dict):
        return False

    has_battery = (
        attrs.get("0/47/11") is not None or attrs.get("0/47/12") is not None
    )
    if not has_battery:
        return False

    has_thread_diag = any(str(k).startswith("0/53/") for k in attrs)
    vendor = str(attrs.get("0/40/1") or "")
    is_ikea = "IKEA" in vendor.upper()
    return has_thread_diag or is_ikea


def extract_sdk_ipv6_from_logs(logs: str, node_id: int) -> str | None:
    """Extract the most recent Matter SDK IPv6 address for a node from logs."""
    if not logs:
        return None
    clean = strip_ansi(logs)
    pattern = re.compile(
        rf"<Node:{int(node_id)}>\s+The SDK is communicating with the device using\s+([0-9a-fA-F:]+)"
    )
    matches = pattern.findall(clean)
    if not matches:
        return None
    return matches[-1]


def extract_battery_pct(node: dict[str, Any]) -> int | None:
    """Extract battery percentage (0-100) from Matter PowerSource ``0/47/12``."""
    attrs = node.get("attributes") if isinstance(node, dict) else None
    if not isinstance(attrs, dict):
        return None
    raw = attrs.get("0/47/12")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= 200:
        return round(float(raw) / 2.0)
    return None


def extract_node_label(node: dict[str, Any]) -> str:
    """Derive a plain-English label for a Matter node without entity IDs."""
    node_id = node.get("node_id", "unknown")
    attrs = node.get("attributes") if isinstance(node, dict) else None
    if isinstance(attrs, dict):
        for key in ("0/40/5", "0/40/3"):
            val = attrs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"Matter Node {node_id}"


def load_state(state_path: Path | None = None) -> dict[str, Any]:
    """Load persisted per-node watchdog state from JSON."""
    path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                nodes = data.get("nodes")
                if isinstance(nodes, dict):
                    return {"nodes": nodes}
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("Failed to load state from %s: %s", path, exc)
    return {"nodes": {}}


def save_state(state: dict[str, Any], state_path: Path | None = None) -> None:
    """Atomically persist watchdog state to JSON."""
    path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _format_battery_clause(battery_pct: Any) -> str:
    if isinstance(battery_pct, (int, float)) and not isinstance(battery_pct, bool):
        return f"last known battery {round(float(battery_pct))}%"
    return "last known battery unknown"


def evaluate_watchdog_state(
    results: list[dict[str, Any]],
    state: dict[str, Any],
    now_ts: float,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    alert_cooldown_seconds: int = DEFAULT_ALERT_COOLDOWN_SECONDS,
) -> tuple[dict[str, Any], str | None]:
    """Update per-node recovery counters and produce an optional alert message.

    Returns ``(new_state, alert_message)`` where ``alert_message`` is ``None``
    during normal healthy operation or silent auto-recovery.
    """
    new_state: dict[str, Any] = copy.deepcopy(state) if isinstance(state, dict) else {}
    nodes_state = new_state.setdefault("nodes", {})
    if not isinstance(nodes_state, dict):
        nodes_state = {}
        new_state["nodes"] = nodes_state

    alert_lines: list[str] = []
    recovery_lines: list[str] = []

    for item in results:
        if not isinstance(item, dict) or item.get("node_id") is None:
            continue
        nid = str(item["node_id"])
        existing = nodes_state.get(nid)
        entry: dict[str, Any] = (
            copy.deepcopy(existing)
            if isinstance(existing, dict)
            else {
                "ipv6": None,
                "label": f"Matter Node {nid}",
                "battery_pct": None,
                "consecutive_failures": 0,
                "last_poke_ts": None,
                "last_recovered_ts": None,
                "last_alert_ts": None,
                "alerted": False,
            }
        )

        if item.get("ipv6"):
            entry["ipv6"] = str(item["ipv6"]).strip()
        if item.get("label"):
            entry["label"] = str(item["label"]).strip()
        if item.get("battery_pct") is not None:
            entry["battery_pct"] = item["battery_pct"]

        label = str(entry.get("label") or f"Matter Node {nid}")
        bat_clause = _format_battery_clause(entry.get("battery_pct"))
        is_available = bool(item.get("available"))

        if is_available:
            was_alerted = bool(entry.get("alerted"))
            if item.get("recovered") or item.get("was_offline"):
                entry["last_recovered_ts"] = float(now_ts)
            entry["consecutive_failures"] = 0
            entry["last_poke_ts"] = None
            entry["alerted"] = False
            entry["last_alert_ts"] = None
            if was_alerted:
                recovery_lines.append(
                    f"- {label}: back online ({bat_clause})."
                )
        else:
            if item.get("poked"):
                entry["last_poke_ts"] = float(now_ts)
            if not item.get("backoff_skipped"):
                failures = int(entry.get("consecutive_failures") or 0) + 1
                entry["consecutive_failures"] = failures
            else:
                failures = int(entry.get("consecutive_failures") or 0)
            if failures >= failure_threshold:
                last_alert_ts = entry.get("last_alert_ts")
                was_alerted = bool(entry.get("alerted"))
                cooldown_elapsed = (
                    last_alert_ts is None
                    or (float(now_ts) - float(last_alert_ts)) >= alert_cooldown_seconds
                )
                if not was_alerted or cooldown_elapsed:
                    entry["alerted"] = True
                    entry["last_alert_ts"] = float(now_ts)
                    alert_lines.append(
                        f"- {label}: offline after {failures} consecutive recovery attempts "
                        f"({bat_clause}). Check battery or sensor."
                    )

        nodes_state[nid] = entry

    sections: list[str] = []
    if alert_lines:
        sections.append(
            "Matter Thread Sensor Alert:\n" + "\n".join(alert_lines)
        )
    if recovery_lines:
        sections.append(
            "Matter Thread Sensor Recovered:\n" + "\n".join(recovery_lines)
        )

    if not sections:
        return new_state, None
    return new_state, "\n".join(sections)


def _build_remote_watchdog_script(
    cached_ipv6_by_node: dict[str, str],
    dry_run: bool = False,
    skip_poke_node_ids: list[int] | None = None,
) -> str:
    """Build the Host 1 Python script executed inside a single SSH roundtrip."""
    phase1_inner = r"""
import asyncio
import json
import sys
import aiohttp

def is_tracked(node):
    if not isinstance(node, dict):
        return False
    attrs = node.get("attributes")
    if not isinstance(attrs, dict):
        return False
    if attrs.get("0/47/11") is None and attrs.get("0/47/12") is None:
        return False
    if any(str(k).startswith("0/53/") for k in attrs):
        return True
    return "IKEA" in str(attrs.get("0/40/1") or "").upper()

def get_label(node):
    attrs = node.get("attributes") or {}
    for k in ("0/40/5", "0/40/3"):
        v = attrs.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    n = node.get("name")
    if isinstance(n, str) and n.strip():
        return n.strip()
    return f"Matter Node {node.get('node_id')}"

def get_bat(node):
    attrs = node.get("attributes") or {}
    raw = attrs.get("0/47/12")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= 200:
        return round(float(raw) / 2.0)
    return None

def pick_ipv6(addrs):
    if not isinstance(addrs, list):
        return None
    v6 = [str(a).split("%", 1)[0].strip() for a in addrs if isinstance(a, str) and ":" in a]
    if not v6:
        return None
    for a in v6:
        if not a.lower().startswith("fe80:"):
            return a
    return v6[0]

async def ws_cmd(ws, msg_id, command, args=None, timeout=25.0):
    payload = {"message_id": str(msg_id), "command": command}
    if args is not None:
        payload["args"] = args
    await ws.send_json(payload)
    while True:
        msg = await ws.receive_json(timeout=timeout)
        if isinstance(msg, dict) and str(msg.get("message_id")) == str(msg_id):
            return msg

async def main():
    inp = json.loads(sys.stdin.read() or "{}")
    dry_run = bool(inp.get("dry_run", False))
    skip_ids = {int(x) for x in (inp.get("skip_poke_node_ids") or [])}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("http://127.0.0.1:5580/ws", timeout=15) as ws:
            info = await ws.receive_json(timeout=15)
            cfid = info.get("compressed_fabric_id")
            res = await ws_cmd(ws, "1", "get_nodes", timeout=25.0)
            nodes = res.get("result") or []
            tracked = []
            seq = 2
            for node in nodes:
                if not is_tracked(node):
                    continue
                nid = node.get("node_id")
                if nid is None:
                    continue
                nid = int(nid)
                avail = bool(node.get("available"))
                ip_res = await ws_cmd(
                    ws, str(seq), "get_node_ip_addresses", {"node_id": nid, "prefer_cache": True}, timeout=20.0
                )
                seq += 1
                ipv6 = pick_ipv6(ip_res.get("result"))
                if not avail and not ipv6 and not dry_run and nid not in skip_ids:
                    try:
                        await ws_cmd(ws, str(seq), "ping_node", {"node_id": nid, "attempts": 1}, timeout=25.0)
                    except Exception:
                        pass
                    seq += 1
                tracked.append({
                    "node_id": nid,
                    "label": get_label(node),
                    "battery_pct": get_bat(node),
                    "available": avail,
                    "was_offline": not avail,
                    "ipv6": ipv6,
                })
    print(json.dumps({"compressed_fabric_id": cfid, "nodes": tracked}))

asyncio.run(main())
"""

    phase2_inner = r"""
import asyncio
import json
import sys
import time
import aiohttp
from zeroconf import IPVersion, ServiceInfo, Zeroconf

def get_bat(node):
    attrs = (node or {}).get("attributes") or {}
    raw = attrs.get("0/47/12")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= 200:
        return round(float(raw) / 2.0)
    return None

def register_temp_mdns(targets):
    if not targets:
        return
    try:
        zc = Zeroconf(ip_version=IPVersion.All)
    except Exception:
        zc = Zeroconf()
    infos = []
    try:
        for t in targets:
            info = ServiceInfo(
                "_matter._tcp.local.",
                t["service_name"],
                port=5540,
                properties={b"SII": b"5000", b"SAI": b"300", b"T": b"1"},
                parsed_addresses=[t["ipv6"]],
                server=f"node{t['node_id']}-poke.local.",
            )
            zc.register_service(info, allow_name_change=True)
            infos.append(info)
        time.sleep(2.0)
    finally:
        for info in infos:
            try:
                zc.unregister_service(info)
            except Exception:
                pass
        try:
            zc.close()
        except Exception:
            pass

async def ws_cmd(ws, msg_id, command, args=None, timeout=25.0):
    payload = {"message_id": str(msg_id), "command": command}
    if args is not None:
        payload["args"] = args
    await ws.send_json(payload)
    while True:
        msg = await ws.receive_json(timeout=timeout)
        if isinstance(msg, dict) and str(msg.get("message_id")) == str(msg_id):
            return msg

async def run_recovery(payload):
    targets = payload.get("targets") or []
    offline_ids = [int(n) for n in (payload.get("offline_ids") or [])]
    if targets:
        await asyncio.to_thread(register_temp_mdns, targets)

    updated = {}
    if not offline_ids:
        return updated

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("http://127.0.0.1:5580/ws", timeout=15) as ws:
            await ws.receive_json(timeout=15)
            seq = 1
            for nid in offline_ids:
                try:
                    await ws_cmd(ws, str(seq), "ping_node", {"node_id": nid}, timeout=25.0)
                except Exception:
                    pass
                seq += 1
            await asyncio.sleep(6.0)
            for nid in offline_ids:
                res = await ws_cmd(ws, str(seq), "get_node", {"node_id": nid}, timeout=20.0)
                seq += 1
                node_obj = res.get("result") if isinstance(res, dict) else None
                if isinstance(node_obj, dict):
                    updated[str(nid)] = {
                        "available": bool(node_obj.get("available")),
                        "battery_pct": get_bat(node_obj),
                    }
    return updated

payload = json.loads(sys.stdin.read() or "{}")
out = asyncio.run(run_recovery(payload))
print(json.dumps(out))
"""

    host_script = f"""
import base64
import json
import os
import re
import subprocess
import sys

CACHED_IPV6 = {json.dumps(cached_ipv6_by_node)}
DRY_RUN = {bool(dry_run)!r}
SKIP_POKE_IDS = {json.dumps(sorted({int(x) for x in (skip_poke_node_ids or [])}))}
PHASE1_B64 = {base64.b64encode(phase1_inner.encode("utf-8")).decode("ascii")!r}
PHASE2_B64 = {base64.b64encode(phase2_inner.encode("utf-8")).decode("ascii")!r}
ANSI_RE = re.compile(r"\\x1b\\[[0-9;?]*[ -/]*[@-~]|\\x1b[@-Z\\\\-_]")

docker_bin = "/usr/local/bin/docker" if os.path.exists("/usr/local/bin/docker") else "docker"
docker_prefix = ["sudo", docker_bin] if os.geteuid() != 0 else [docker_bin]

def exec_in_matterserver(code_b64, stdin_obj):
    cmd = docker_prefix + [
        "exec", "-i", "matterserver", "python3", "-c",
        f"import base64; exec(base64.b64decode('{{code_b64}}').decode('utf-8'))",
    ]
    res = subprocess.run(
        cmd,
        input=json.dumps(stdin_obj),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or f"docker exec exit {{res.returncode}}").strip())
    return json.loads((res.stdout or "").strip())

def extract_log_ipv6(logs_text, node_id):
    clean = ANSI_RE.sub("", logs_text or "")
    matches = re.findall(
        rf"<Node:{{int(node_id)}}>\\s+The SDK is communicating with the device using\\s+([0-9a-fA-F:]+)",
        clean,
    )
    return matches[-1] if matches else None

phase1 = exec_in_matterserver(PHASE1_B64, {{"dry_run": DRY_RUN, "skip_poke_node_ids": SKIP_POKE_IDS}})
cfid = phase1.get("compressed_fabric_id")
nodes = phase1.get("nodes") or []

need_logs = [n for n in nodes if not n.get("available") and not n.get("ipv6") and int(n["node_id"]) not in SKIP_POKE_IDS]
logs_text = ""
if need_logs:
    log_res = subprocess.run(
        docker_prefix + ["logs", "--tail", "60", "matterserver"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    logs_text = (log_res.stdout or "") + "\\n" + (log_res.stderr or "")

for n in nodes:
    nid_str = str(n["node_id"])
    if not n.get("ipv6"):
        log_ip = extract_log_ipv6(logs_text, n["node_id"]) if logs_text else None
        n["ipv6"] = log_ip or CACHED_IPV6.get(nid_str)

offline_nodes = [n for n in nodes if not n.get("available") and int(n["node_id"]) not in SKIP_POKE_IDS]
targets = []
if cfid is not None and not DRY_RUN:
    for n in offline_nodes:
        if n.get("ipv6"):
            svc = f"{{int(cfid):016X}}-{{int(n['node_id']):016X}}._matter._tcp.local."
            targets.append({{"node_id": int(n["node_id"]), "service_name": svc, "ipv6": n["ipv6"]}})

updated = {{}}
if offline_nodes and not DRY_RUN:
    updated = exec_in_matterserver(
        PHASE2_B64,
        {{"targets": targets, "offline_ids": [int(n["node_id"]) for n in offline_nodes]}},
    )

results = []
poked_ids = {{t["node_id"] for t in targets}}
for n in nodes:
    nid = int(n["node_id"])
    nid_str = str(nid)
    upd = updated.get(nid_str) or {{}}
    final_avail = bool(upd.get("available")) if nid_str in updated else bool(n.get("available"))
    bat = upd.get("battery_pct")
    if bat is None:
        bat = n.get("battery_pct")
    was_offline = bool(n.get("was_offline"))
    results.append({{
        "node_id": nid,
        "label": n.get("label"),
        "ipv6": n.get("ipv6"),
        "battery_pct": bat,
        "was_offline": was_offline,
        "poked": nid in poked_ids,
        "backoff_skipped": was_offline and nid in SKIP_POKE_IDS,
        "available": final_avail,
        "recovered": was_offline and final_avail,
    }})

print(json.dumps({{
    "compressed_fabric_id": cfid,
    "dry_run": DRY_RUN,
    "results": results,
}}))
"""
    return host_script.strip()


def _run_watchdog_with_details(
    dry_run: bool = False,
    state_path: Path | None = None,
    ssh_runner: Callable[..., tuple[int, str, str]] | None = None,
    now_ts: float | None = None,
) -> tuple[str, dict[str, Any]]:
    resolved_state_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
    state = load_state(resolved_state_path)
    nodes_state = state.get("nodes") if isinstance(state, dict) else {}
    cached_ipv6 = {
        str(nid): str(entry["ipv6"])
        for nid, entry in (nodes_state or {}).items()
        if isinstance(entry, dict) and entry.get("ipv6")
    }

    ts = float(now_ts) if now_ts is not None else time.time()
    skip_poke_node_ids = [
        int(nid)
        for nid, entry in (nodes_state or {}).items()
        if str(nid).isdigit() and not should_attempt_poke(entry, ts)
    ]

    remote_script = _build_remote_watchdog_script(
        cached_ipv6, dry_run=dry_run, skip_poke_node_ids=skip_poke_node_ids
    )
    b64_payload = base64.b64encode(remote_script.encode("utf-8")).decode("ascii")
    remote_cmd = (
        "python3 -c 'import base64; "
        f"exec(base64.b64decode(\"{b64_payload}\").decode(\"utf-8\"))'"
    )

    runner = ssh_runner or nas_config.run_ssh
    rc, stdout, stderr = runner(nas_config.HOST_1_IP, remote_cmd)
    if rc != 0:
        err_detail = (stderr or stdout or f"SSH exited with code {rc}").strip()
        raise RuntimeError(f"Matter Thread watchdog SSH execution failed: {err_detail}")

    raw_out = (stdout or "").strip()
    try:
        payload = json.loads(raw_out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Matter Thread watchdog received invalid JSON from Host 1: {exc}"
        ) from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise TypeError("Matter Thread watchdog payload missing 'results' list")

    new_state, alert_msg = evaluate_watchdog_state(results, state, now_ts=ts)
    if not dry_run:
        save_state(new_state, resolved_state_path)

    return (alert_msg or ""), {
        "compressed_fabric_id": payload.get("compressed_fabric_id"),
        "dry_run": dry_run,
        "results": results,
        "state": new_state,
    }


def run_matter_thread_watchdog(
    dry_run: bool = False,
    state_path: Path | None = None,
    ssh_runner: Callable[..., tuple[int, str, str]] | None = None,
) -> str:
    """Execute one Matter-over-Thread watchdog cycle via Host 1 SSH.

    Returns an empty string when all tracked sensors are healthy or silently
    auto-recovered; returns a Discord-ready alert string only when a sensor
    hits consecutive recovery failures or recovers after a prior alert.
    """
    alert_msg, _ = _run_watchdog_with_details(
        dry_run=dry_run,
        state_path=state_path,
        ssh_runner=ssh_runner,
    )
    return alert_msg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Matter-over-Thread Auto-Recovery Watchdog Sidecar"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect nodes without injecting mDNS pokes or mutating state",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON diagnostics",
    )
    args = parser.parse_args(argv)

    alert_msg, details = _run_watchdog_with_details(dry_run=args.dry_run)
    if args.json:
        print(json.dumps({"alert": alert_msg, **details}, indent=2))
    elif alert_msg:
        print(alert_msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
