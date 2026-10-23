#!/usr/bin/env python3
import sys
import json
import urllib.request
import os

SECRETS_PATH = os.environ.get("HA_SECRETS_PATH", "/secrets/ha.json")
DEFAULT_GH_STRUCTURE_ID = "736744c9-ccae-4c7e-b436-c1438a84c3e0"


def get_ha_config() -> tuple[str, str]:
    base_url = os.environ.get("HA_BASE_URL", "").rstrip("/")
    token = os.environ.get("HA_ACCESS_TOKEN", "")

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if not base_url and d.get("HA_BASE_URL"):
                    base_url = d["HA_BASE_URL"].rstrip("/")
                if not token and d.get("HA_ACCESS_TOKEN"):
                    token = d["HA_ACCESS_TOKEN"]
        except Exception:
            pass

    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH) as f:
                d = json.load(f)
                if not base_url and d.get("url"):
                    base_url = d["url"].rstrip("/")
                if not token and d.get("token"):
                    token = d["token"]
        except Exception:
            pass

    return base_url or "http://127.0.0.1:8123", token


def check_google_home_batteries(threshold: float = 15.0) -> list[tuple[float, str, str]]:
    """Query Google Home MCP states for Nest cameras, doorbell, and Nest x Yale lock batteries."""
    low_gh: list[tuple[float, str, str]] = []
    try:
        if "/workspace" not in sys.path:
            sys.path.insert(0, "/workspace")
        from tools import google_home_mcp

        raw = google_home_mcp.list_home_states(DEFAULT_GH_STRUCTURE_ID)
        outer = json.loads(raw)
        if not outer.get("ok"):
            return low_gh
        content = outer.get("result", {}).get("content", [])
        if not content:
            return low_gh
        states = json.loads(content[0].get("text", "{}")).get("states", [])
        for st in states:
            name = st.get("displayName") or st.get("id", "Unknown Google Home Device")
            dev_id = st.get("id", "")
            traits = st.get("traits", {})
            ps = traits.get("PowerSource", {})
            eps = traits.get("ExtendedPowerSource", {})
            ps_state = ps.get("state", {})
            eps_state = eps.get("state", {})

            # Skip non-battery devices
            if not ps_state and not eps_state:
                continue

            dev_type = ps.get("type") or eps.get("type") or st.get("type", "")
            # Skip duplicate HA-bridged sensors already tracked in HA (OtaRequestorDevice / GoogleGenericSensorDevice)
            if dev_type in ("OtaRequestorDevice", "GoogleGenericSensorDevice"):
                continue

            pct = ps_state.get("batPercentRemaining")
            charge_level = ps_state.get("batChargeLevel", "")
            replace_needed = ps_state.get("batReplacementNeeded", False)
            desc_cap = eps_state.get("descriptiveCapacityRemaining", "")
            replace_ind = eps_state.get("batteryReplacementIndicator", "")

            is_low = False
            val_num = 100.0
            if pct is not None:
                try:
                    val_num = float(pct)
                    if dev_type == "DoorLockDevice":
                        # Matter PowerSource BatPercentRemaining (0x000C) uses 0.5% steps (0-200) on DoorLockDevice
                        val_num = val_num / 2.0
                    if val_num <= threshold:
                        is_low = True
                except (ValueError, TypeError):
                    pass

            if (
                replace_needed
                or charge_level in ("BAT_CHARGE_LEVEL_ENUM_WARNING", "BAT_CHARGE_LEVEL_ENUM_CRITICAL")
                or desc_cap in ("DESCRIPTIVE_CAPACITY_REMAINING_LOW", "DESCRIPTIVE_CAPACITY_REMAINING_CRITICAL")
                or replace_ind in ("BATTERY_REPLACEMENT_INDICATOR_SOON", "BATTERY_REPLACEMENT_INDICATOR_IMMEDIATELY")
            ):
                is_low = True
                if pct is None:
                    val_num = 10.0

            if is_low:
                label_eid = f"google_home:{name.lower().replace(' ', '_')}" if dev_id else "google_home"
                low_gh.append((val_num, f"{name} (Google Home)", label_eid))
    except Exception:
        pass
    return low_gh


def check_batteries(threshold: float = 15.0, quiet: bool = False, include_google_home: bool = True) -> int:
    base_url, token = get_ha_config()
    if not token:
        if not quiet:
            print("⚠️ Home Assistant token not found.")
        return 1

    req = urllib.request.Request(f"{base_url}/api/states", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            states = json.loads(resp.read().decode())
    except Exception as e:
        if not quiet:
            print(f"⚠️ Failed to query Home Assistant states: {e}")
        return 1

    ignore_patterns = ["pixel", "fold", "watch", "ev9", "envoy", "encharge", "reserve_battery", "balance"]
    low_batteries = []

    for s in states:
        attrs = s.get("attributes", {})
        eid = s.get("entity_id", "")

        if attrs.get("device_class") != "battery":
            continue
        if any(p in eid.lower() for p in ignore_patterns):
            continue
        name = attrs.get("friendly_name", eid)
        if any(p in name.lower() for p in ignore_patterns):
            continue

        # 1. Numeric battery percentage sensors
        if attrs.get("unit_of_measurement") == "%":
            try:
                val = float(s.get("state", 100))
                if val <= threshold:
                    low_batteries.append((val, name, eid))
            except (ValueError, TypeError):
                pass
        # 2. Binary battery health/low-battery sensors (on == low battery)
        elif eid.startswith("binary_sensor.") and s.get("state") == "on":
            low_batteries.append((0.0, name, eid))

    if include_google_home:
        for item in check_google_home_batteries(threshold=threshold):
            low_batteries.append(item)

    low_batteries.sort()

    if low_batteries:
        print("🔋 **Home Assistant Low Battery Alert**\n")
        for val, name, eid in low_batteries:
            print(f"• **{name}:** `{val:.0f}%` (Entity: `{eid}`)")
        print("\n*Please replace or recharge batteries before sensors go offline.*")
    else:
        if not quiet:
            print(f"✅ All {len(states)} Home Assistant IoT sensors have healthy battery levels (> {threshold:.0f}%).")
    return 0


if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    thresh = 15.0
    for arg in sys.argv[1:]:
        if arg.startswith("--threshold="):
            try:
                thresh = float(arg.split("=")[1])
            except ValueError:
                pass
    sys.exit(check_batteries(threshold=thresh, quiet=quiet))
