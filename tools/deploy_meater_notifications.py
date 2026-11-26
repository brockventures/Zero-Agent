"""Deploy Home Assistant MEATER+ mobile push notification automations to Host 1.

Steps executed in full deploy mode:
1. Resolve Home Assistant REST API credentials from environment, /secrets/ha.json,
   or /secrets/env.json, and Host 1 config path via tools/nas_config.py.
2. Dynamically discover active mobile_app_* phone notify services for both Ryan
   and Emily via GET /api/services and GET /api/states (supporting --notify-services
   CLI override), filtering out non-phone/wearable/tablet entries.
3. Fetch live Host 1 automations.yaml over SSH, back it up locally to
   /workspace/scratch/ha_automations.meater.backup.yaml, and idempotently upsert:
   - meater_cook_5min_warning ('MEATER+: 5 Minutes Before Cook Finishes')
   - meater_cook_finished_pull ('MEATER+: Cook Finished — Pull Now')
   - meater_cook_rest_complete ('MEATER+: Resting Complete — Ready to Carve')
4. Atomically write updated automations.yaml to Host 1, call
   POST /api/services/automation/reload, and verify all 3 automation entities
   are active in GET /api/states.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import posixpath
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools import nas_config

BACKUP_AUTOMATIONS_PATH = Path("/workspace/scratch/ha_automations.meater.backup.yaml")

MEATER_AUTOMATION_IDS: tuple[str, ...] = (
    "meater_cook_5min_warning",
    "meater_cook_finished_pull",
    "meater_cook_rest_complete",
)

NON_PHONE_TOKENS: tuple[str, ...] = (
    "watch",
    "wear",
    "tablet",
    "ipad",
    "tv",
    "chromecast",
    "macbook",
    "laptop",
    "desktop",
)

TARGET_PERSONS: tuple[str, ...] = ("ryan", "emily")

REST_MINUTES_JINJA = (
    "{% set preset = states('select.meater_meater_cook_preset') %}"
    "{% if 'Prime Rib' in preset or 'Brisket' in preset or 'Pork Shoulder' in preset %}25"
    "{% elif 'Turkey' in preset %}20"
    "{% else %}10{% endif %}"
)


def get_remote_paths() -> dict[str, str]:
    """Resolve Host 1 Home Assistant automations.yaml path dynamically via nas_config."""
    ha_config_root = nas_config.HA_CONFIG_ROOT
    return {
        "ha_config_root": ha_config_root,
        "automations_path": posixpath.join(ha_config_root, "automations.yaml"),
    }


def resolve_ha_credentials() -> tuple[str, str]:
    """Resolve (ha_url, ha_token) dynamically from env, /secrets/ha.json, or /secrets/env.json."""
    ha_url = (os.environ.get("HA_BASE_URL") or os.environ.get("HA_URL") or "").strip().rstrip("/")
    ha_token = (os.environ.get("HA_ACCESS_TOKEN") or os.environ.get("HA_TOKEN") or "").strip()

    for secret_path in (Path("/secrets/ha.json"), Path("/secrets/env.json")):
        if secret_path.is_file():
            try:
                data = json.loads(secret_path.read_text(encoding="utf-8"))
                if not ha_url:
                    raw_url = data.get("HA_BASE_URL") or data.get("url") or ""
                    ha_url = str(raw_url).strip().rstrip("/")
                if not ha_token:
                    raw_tok = data.get("HA_ACCESS_TOKEN") or data.get("token") or ""
                    ha_token = str(raw_tok).strip()
            except Exception:  # noqa: BLE001, S110
                pass

    if not ha_url:
        ha_url = f"http://{nas_config.HOST_1_IP}:8123"
    return ha_url, ha_token


def _ha_api_request(
    ha_url: str,
    ha_token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    """Perform an authenticated JSON request against the Home Assistant REST API."""
    url = f"{ha_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    if ha_token:
        headers["Authorization"] = f"Bearer {ha_token}"

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HA API {method} {path} failed (HTTP {exc.code}): {err_body}") from exc


def _is_non_phone_name(name: str) -> bool:
    """Return True if a service or tracker slug represents a watch, tablet, TV, or computer."""
    lowered = name.lower()
    tokens = [tok for tok in lowered.replace("-", "_").split("_") if tok]
    for bad in NON_PHONE_TOKENS:
        if bad in tokens or bad in lowered:
            return True
    return False


def _normalize_notify_service(raw: str) -> str:
    """Normalize a notify service name to full 'notify.mobile_app_*' format."""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("notify."):
        return cleaned
    return f"notify.{cleaned}"


def parse_notify_services_override(raw_override: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Parse and normalize a CLI or programmatic notify-services override."""
    if not raw_override:
        return []
    items = raw_override.split(",") if isinstance(raw_override, str) else list(raw_override)

    out: list[str] = []
    for item in items:
        norm = _normalize_notify_service(str(item))
        if norm and norm not in out:
            out.append(norm)
    return out


def _match_tracker_to_mobile_service(tracker_entity_id: str, mobile_services: list[str]) -> str | None:
    """Map a device_tracker.<slug> entity ID to a matching mobile_app_<slug> notify service."""
    slug = tracker_entity_id.strip().lower().removeprefix("device_tracker.")
    if not slug or _is_non_phone_name(slug):
        return None

    exact_target = f"mobile_app_{slug}"
    for svc in mobile_services:
        if svc.lower() == exact_target:
            return svc

    prefix_target = f"mobile_app_{slug}_"
    for svc in mobile_services:
        svc_lower = svc.lower()
        if svc_lower.startswith(prefix_target) or exact_target.startswith(f"{svc_lower}_"):
            return svc

    return None


def discover_phone_notify_services(
    ha_url: str,
    ha_token: str,
    *,
    notify_services_override: str | list[str] | tuple[str, ...] | None = None,
    services_payload: list[dict[str, Any]] | None = None,
    states_payload: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Discover active notify.mobile_app_* phone services for both Ryan and Emily."""
    override_list = parse_notify_services_override(notify_services_override)
    if override_list:
        return override_list

    if services_payload is None:
        services_payload = _ha_api_request(ha_url, ha_token, "/api/services")
    if states_payload is None:
        try:
            states_payload = _ha_api_request(ha_url, ha_token, "/api/states")
        except Exception:  # noqa: BLE001
            states_payload = []

    notify_domain = next(
        (d for d in (services_payload or []) if isinstance(d, dict) and d.get("domain") == "notify"),
        None,
    )
    raw_services_dict = (notify_domain or {}).get("services") or {}
    if isinstance(raw_services_dict, (dict, list)):
        raw_service_names = [str(k) for k in raw_services_dict]
    else:
        raw_service_names = []

    phone_mobile_services = [
        svc
        for svc in raw_service_names
        if svc.startswith("mobile_app_") and not _is_non_phone_name(svc)
    ]
    if not phone_mobile_services:
        raise RuntimeError("No active phone notify.mobile_app_* services found in Home Assistant /api/services")

    state_by_entity: dict[str, dict[str, Any]] = {
        str(st.get("entity_id")): st
        for st in (states_payload or [])
        if isinstance(st, dict) and st.get("entity_id")
    }
    person_states = [
        st
        for eid, st in state_by_entity.items()
        if eid.startswith("person.")
    ]

    selected_services: list[str] = []

    for person_key in TARGET_PERSONS:
        matched_service: str | None = None

        person_entity = next(
            (
                st
                for st in person_states
                if person_key in str(st.get("entity_id", "")).lower()
                or person_key in str((st.get("attributes") or {}).get("friendly_name", "")).lower()
                or person_key in str((st.get("attributes") or {}).get("id", "")).lower()
            ),
            None,
        )

        if person_entity is not None:
            attrs = person_entity.get("attributes") or {}
            trackers: list[str] = [str(t) for t in (attrs.get("device_trackers") or []) if t]
            source_tracker = str(attrs.get("source") or "").strip()
            if source_tracker and source_tracker not in trackers:
                trackers.append(source_tracker)

            candidate_matches: list[tuple[str, str]] = []
            for trk in trackers:
                svc = _match_tracker_to_mobile_service(trk, phone_mobile_services)
                if svc and _normalize_notify_service(svc) not in selected_services:
                    candidate_matches.append((trk, svc))

            if len(candidate_matches) == 1:
                matched_service = candidate_matches[0][1]
            elif len(candidate_matches) > 1:
                # Prefer tracker whose state in /api/states is available/active, or first device_tracker
                active_candidates = [
                    (trk, svc)
                    for (trk, svc) in candidate_matches
                    if str((state_by_entity.get(trk) or {}).get("state") or "")
                    not in ("unavailable", "unknown", "")
                ]
                chosen_pool = active_candidates if active_candidates else candidate_matches
                matched_service = chosen_pool[0][1]

        if not matched_service:
            for svc in phone_mobile_services:
                if person_key in svc.lower() and _normalize_notify_service(svc) not in selected_services:
                    matched_service = svc
                    break

        if matched_service:
            norm = _normalize_notify_service(matched_service)
            if norm not in selected_services:
                selected_services.append(norm)

    for svc in phone_mobile_services:
        if len(selected_services) >= len(TARGET_PERSONS):
            break
        norm = _normalize_notify_service(svc)
        if norm not in selected_services:
            selected_services.append(norm)

    if len(selected_services) < 2:
        raise RuntimeError(
            f"Expected at least 2 phone notify services for Ryan and Emily, discovered: {selected_services}"
        )

    return selected_services


def build_meater_automations(notify_services: list[str]) -> list[dict[str, Any]]:
    """Construct the 3 idempotent MEATER+ Home Assistant mobile notification automation definitions."""
    normalized_services = parse_notify_services_override(notify_services)
    if len(normalized_services) < 2:
        raise ValueError("build_meater_automations requires at least 2 notify services (Ryan and Emily)")

    msg_5min = (
        "{{ states('select.meater_meater_cook_preset') }} has ~5 minutes remaining! "
        "Internal: {{ states('sensor.meater_meater_internal_temperature') | float(0) | round(1) }}°F / "
        "Target: {{ states('number.meater_meater_target_temperature') | float(0) | round(0) | int }}°F "
        "(Ambient: {{ states('sensor.meater_meater_ambient_temperature') | float(0) | round(0) | int }}°F, "
        "ETA: {{ states('sensor.meater_meater_eta') | float(0) | round(1) }}m)."
    )

    msg_pull = (
        "{% set preset = states('select.meater_meater_cook_preset') %}"
        "{% if 'Prime Rib' in preset or 'Brisket' in preset or 'Pork Shoulder' in preset %}{% set rest_min = 25 %}"
        "{% elif 'Turkey' in preset %}{% set rest_min = 20 %}"
        "{% else %}{% set rest_min = 10 %}{% endif %}"
        "{{ preset }} is ready to pull from heat! "
        "Internal: {{ states('sensor.meater_meater_internal_temperature') | float(0) | round(1) }}°F / "
        "Target: {{ states('number.meater_meater_target_temperature') | float(0) | round(0) | int }}°F. "
        "Recommended rest: {{ rest_min }}m "
        "(25m Prime Rib/Brisket/Pork Shoulder, 20m Turkey, 10m Steak/Chicken/Pork/Custom)."
    )

    msg_rest = (
        "{% set preset = states('select.meater_meater_cook_preset') %}"
        "{% if 'Prime Rib' in preset or 'Brisket' in preset or 'Pork Shoulder' in preset %}{% set rest_min = 25 %}"
        "{% elif 'Turkey' in preset %}{% set rest_min = 20 %}"
        "{% else %}{% set rest_min = 10 %}{% endif %}"
        "{{ preset }} {{ rest_min }}m rest is complete — ready to carve! "
        "Current internal: {{ states('sensor.meater_meater_internal_temperature') | float(0) | round(1) }}°F "
        "(Target: {{ states('number.meater_meater_target_temperature') | float(0) | round(0) | int }}°F)."
    )

    def _build_notify_actions(title: str, message: str, tag: str) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for svc in normalized_services:
            actions.append(
                {
                    "action": svc,
                    "continue_on_error": True,
                    "data": {
                        "title": title,
                        "message": message,
                        "data": {
                            "tag": tag,
                            "ttl": 0,
                            "priority": "high",
                            "importance": "high",
                            "channel": "meater_cook_alerts",
                        },
                    },
                }
            )
        return actions

    auto_5min: dict[str, Any] = {
        "id": "meater_cook_5min_warning",
        "alias": "MEATER+: 5 Minutes Before Cook Finishes",
        "description": (
            "Sends a high-priority push notification to both phones when MEATER+ ETA "
            "is below 5.5 minutes for 30 seconds while connected and actively cooking."
        ),
        "triggers": [
            {
                "trigger": "numeric_state",
                "entity_id": "sensor.meater_meater_eta",
                "below": 5.5,
                "for": "00:00:30",
            }
        ],
        "conditions": [
            {
                "condition": "state",
                "entity_id": "binary_sensor.meater_meater_connected",
                "state": "on",
            },
            {
                "condition": "state",
                "entity_id": "sensor.meater_meater_cook_status",
                "state": ["Cooking", "Almost Ready"],
            },
            {
                "condition": "template",
                "value_template": (
                    "{{ this.attributes.last_triggered is none "
                    "or (now() - this.attributes.last_triggered).total_seconds() > 1800 }}"
                ),
            },
        ],
        "actions": _build_notify_actions(
            title="MEATER+: ~5 Minutes Left",
            message=msg_5min,
            tag="meater_cook_5min",
        ),
        "mode": "single",
    }

    auto_finished: dict[str, Any] = {
        "id": "meater_cook_finished_pull",
        "alias": "MEATER+: Cook Finished — Pull Now",
        "description": (
            "Sends a high-priority push notification to both phones when MEATER+ cook status "
            "transitions to Pull Now or Target Reached, with a 20-minute dedup cooldown."
        ),
        "triggers": [
            {
                "trigger": "state",
                "entity_id": "sensor.meater_meater_cook_status",
                "to": ["Pull Now", "Target Reached"],
            }
        ],
        "conditions": [
            {
                "condition": "template",
                "value_template": (
                    "{{ this.attributes.last_triggered is none "
                    "or (now() - this.attributes.last_triggered).total_seconds() > 1200 }}"
                ),
            }
        ],
        "actions": _build_notify_actions(
            title="MEATER+: Time to Pull!",
            message=msg_pull,
            tag="meater_cook_done",
        ),
        "mode": "single",
    }

    auto_rest: dict[str, Any] = {
        "id": "meater_cook_rest_complete",
        "alias": "MEATER+: Resting Complete — Ready to Carve",
        "description": (
            "Waits for the preset-specific recommended rest duration after MEATER+ reaches "
            "Pull Now or Target Reached, then sends a high-priority push notification to both phones."
        ),
        "triggers": [
            {
                "trigger": "state",
                "entity_id": "sensor.meater_meater_cook_status",
                "to": ["Pull Now", "Target Reached"],
            }
        ],
        "conditions": [],
        "actions": [
            {
                "delay": {
                    "minutes": REST_MINUTES_JINJA,
                }
            },
            *_build_notify_actions(
                title="MEATER+: Resting Complete — Ready to Carve!",
                message=msg_rest,
                tag="meater_rest_done",
            ),
        ],
        "mode": "single",
    }

    return [auto_5min, auto_finished, auto_rest]


def upsert_meater_automations(
    existing_automations: list[dict[str, Any]],
    notify_services: list[str],
) -> list[dict[str, Any]]:
    """Idempotently upsert the 3 MEATER+ automations by stable id while preserving all other entries."""
    if not isinstance(existing_automations, list):
        raise TypeError("automations.yaml root structure must be a YAML list")

    merged: list[dict[str, Any]] = [dict(item) for item in existing_automations if isinstance(item, dict)]
    new_automations = build_meater_automations(notify_services)

    index_by_id: dict[str, int] = {}
    for idx, item in enumerate(merged):
        item_id = str(item.get("id") or "").strip()
        if item_id:
            index_by_id[item_id] = idx

    for auto in new_automations:
        auto_id = str(auto["id"])
        if auto_id in index_by_id:
            merged[index_by_id[auto_id]] = auto
        else:
            index_by_id[auto_id] = len(merged)
            merged.append(auto)

    return merged


def patch_automations_yaml(raw_yaml: str, notify_services: list[str]) -> str:
    """Parse automations.yaml, idempotently upsert the 3 MEATER+ automations, validate, and dump YAML."""
    loaded = yaml.safe_load(raw_yaml) if raw_yaml.strip() else []
    if loaded is None:
        loaded = []
    if not isinstance(loaded, list):
        raise TypeError("Expected automations.yaml to contain a top-level YAML list")

    merged = upsert_meater_automations(loaded, notify_services)
    dumped = yaml.safe_dump(
        merged,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    verify_patched_automations_yaml(dumped, notify_services)
    return dumped


def verify_patched_automations_yaml(yaml_text: str, notify_services: list[str]) -> None:
    """Verify that the rendered automations.yaml parses cleanly and contains all 3 MEATER+ automations."""
    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, list):
        raise TypeError("Rendered automations.yaml is not a YAML list")

    by_id: dict[str, dict[str, Any]] = {
        str(item.get("id")): item
        for item in parsed
        if isinstance(item, dict) and item.get("id")
    }
    for req_id in MEATER_AUTOMATION_IDS:
        if req_id not in by_id:
            raise RuntimeError(f"Missing required MEATER+ automation id in YAML: {req_id}")

    normalized_services = parse_notify_services_override(notify_services)
    for req_id in MEATER_AUTOMATION_IDS:
        auto = by_id[req_id]
        actions = auto.get("actions") or []
        action_services = [str(a.get("action")) for a in actions if isinstance(a, dict) and a.get("action")]
        for svc in normalized_services:
            if svc not in action_services:
                raise RuntimeError(f"Automation {req_id} missing notify service action {svc}")


def fetch_live_automations_and_backup(backup_path: Path = BACKUP_AUTOMATIONS_PATH) -> str:
    """Fetch live automations.yaml from Host 1 via SSH and back it up locally."""
    paths = get_remote_paths()
    auto_path = paths["automations_path"]
    print(f"[1/4] Fetching live automations.yaml from Host 1: {auto_path}", flush=True)
    rc, out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"base64 -w 0 {shlex.quote(auto_path)}",
        timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"Failed fetching {auto_path} from Host 1 (rc={rc}): {err}")

    raw_bytes = base64.b64decode(out.strip())
    yaml_text = raw_bytes.decode("utf-8")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    already_patched = all(aid in yaml_text for aid in MEATER_AUTOMATION_IDS)
    if not already_patched or not backup_path.is_file():
        backup_path.write_bytes(raw_bytes)
        print(f"[1/4] Backed up live automations.yaml ({len(raw_bytes):,} bytes) -> {backup_path}", flush=True)
    else:
        print(
            f"[1/4] Fetched live automations.yaml ({len(raw_bytes):,} bytes); preserved backup at {backup_path}",
            flush=True,
        )
    return yaml_text


def deploy_automations_to_host1(patched_yaml: str) -> None:
    """Atomically write updated automations.yaml to Host 1 over SSH."""
    paths = get_remote_paths()
    auto_path = paths["automations_path"]
    tmp_path = f"{auto_path}.meater.tmp"
    b64_payload = base64.b64encode(patched_yaml.encode("utf-8")).decode("ascii")

    remote_cmd = (
        f"base64 -d > {shlex.quote(tmp_path)} && "
        f"chmod 644 {shlex.quote(tmp_path)} && "
        f"mv -f {shlex.quote(tmp_path)} {shlex.quote(auto_path)}"
    )
    rc, _out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        remote_cmd,
        timeout=30,
        stdin_data=b64_payload,
    )
    if rc != 0:
        raise RuntimeError(f"Failed writing {auto_path} on Host 1 (rc={rc}): {err}")
    print(
        f"[3/4] Deployed automations.yaml ({len(patched_yaml.encode('utf-8')):,} bytes) -> {auto_path}",
        flush=True,
    )


def reload_and_verify_ha_automations(
    ha_url: str,
    ha_token: str,
    *,
    retries: int = 5,
    retry_delay_sec: float = 1.0,
) -> list[str]:
    """Reload Home Assistant automations via REST API and verify all 3 MEATER+ automation entities exist."""
    _ha_api_request(ha_url, ha_token, "/api/services/automation/reload", method="POST", payload={})
    print("[4/4] Triggered POST /api/services/automation/reload", flush=True)

    found_entities: dict[str, str] = {}
    for attempt in range(max(1, retries)):
        states = _ha_api_request(ha_url, ha_token, "/api/states")
        if isinstance(states, list):
            for st in states:
                if not isinstance(st, dict):
                    continue
                eid = str(st.get("entity_id") or "")
                if not eid.startswith("automation."):
                    continue
                attrs = st.get("attributes") or {}
                auto_id = str(attrs.get("id") or "")
                if auto_id in MEATER_AUTOMATION_IDS:
                    found_entities[auto_id] = eid

        if all(req_id in found_entities for req_id in MEATER_AUTOMATION_IDS):
            break
        if attempt + 1 < retries and retry_delay_sec > 0:
            time.sleep(retry_delay_sec)

    missing = [req_id for req_id in MEATER_AUTOMATION_IDS if req_id not in found_entities]
    if missing:
        raise RuntimeError(
            f"MEATER+ automations missing from GET /api/states after reload: {missing} (found={found_entities})"
        )

    ordered_entities = [found_entities[req_id] for req_id in MEATER_AUTOMATION_IDS]
    print(f"[4/4] Verified active HA automation entities: {', '.join(ordered_entities)}", flush=True)
    return ordered_entities


def run_deploy(
    *,
    dry_run: bool = False,
    notify_services_override: str | list[str] | tuple[str, ...] | None = None,
) -> int:
    """Execute phone discovery -> fetch & backup -> YAML merge -> atomic deploy -> reload & verify."""
    ha_url, ha_token = resolve_ha_credentials()
    notify_services = discover_phone_notify_services(
        ha_url,
        ha_token,
        notify_services_override=notify_services_override,
    )
    print(f"[0/4] Discovered target phone notify services: {', '.join(notify_services)}", flush=True)

    live_yaml = fetch_live_automations_and_backup(BACKUP_AUTOMATIONS_PATH)
    patched_yaml = patch_automations_yaml(live_yaml, notify_services)
    parsed_count = len(yaml.safe_load(patched_yaml) or [])
    print(
        f"[2/4] Upserted 3 MEATER+ cook/rest automations cleanly "
        f"(total automations={parsed_count}, {len(patched_yaml.encode('utf-8')):,} bytes)",
        flush=True,
    )

    if dry_run:
        print("[dry-run] Skipping Host 1 automations.yaml write and HA automation reload.", flush=True)
        return 0

    deploy_automations_to_host1(patched_yaml)
    verified_entities = reload_and_verify_ha_automations(ha_url, ha_token)
    print(
        f"SUCCESS: Deployed and verified 3 MEATER+ HA notification automations ({', '.join(verified_entities)}).",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy Home Assistant MEATER+ cook/rest mobile push notification automations to Host 1."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover phone services, fetch & back up automations.yaml, and validate merged YAML in memory.",
    )
    parser.add_argument(
        "--notify-services",
        type=str,
        default=None,
        help="Comma-separated override list of notify.mobile_app_* services for both phones.",
    )
    args = parser.parse_args(argv)
    return run_deploy(dry_run=args.dry_run, notify_services_override=args.notify_services)


if __name__ == "__main__":
    raise SystemExit(main())
