#!/usr/bin/env python3
import sys, json, urllib.request, os

SECRETS_PATH = os.environ.get("HA_SECRETS_PATH", "/secrets/ha.json")
BASE_URL = os.environ.get("HA_BASE_URL", "http://127.0.0.1:8123")

def get_ha_token():
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH) as f:
                return json.load(f).get("token", "")
        except Exception:
            pass
    return os.environ.get("HA_ACCESS_TOKEN", "")

def check_batteries(threshold: float = 15.0, quiet: bool = False):
    token = get_ha_token()
    if not token:
        if not quiet:
            print("⚠️ Home Assistant token not found.")
        return

    req = urllib.request.Request(f"{BASE_URL}/api/states", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            states = json.loads(resp.read().decode())
    except Exception as e:
        if not quiet:
            print(f"⚠️ Failed to query Home Assistant states: {e}")
        return

    ignore_patterns = ["pixel", "fold", "watch", "ev9", "envoy", "encharge", "reserve_battery", "balance"]
    low_batteries = []

    for s in states:
        attrs = s.get("attributes", {})
        eid = s.get("entity_id", "")
        
        # Check only device batteries measuring in %
        if attrs.get("device_class") == "battery" and attrs.get("unit_of_measurement") == "%":
            if any(p in eid.lower() for p in ignore_patterns):
                continue
            name = attrs.get("friendly_name", eid)
            if any(p in name.lower() for p in ignore_patterns):
                continue
            
            try:
                val = float(s.get("state", 100))
                if val <= threshold:
                    low_batteries.append((val, name, eid))
            except (ValueError, TypeError):
                pass

    low_batteries.sort()

    if low_batteries:
        print("🔋 **Home Assistant Low Battery Alert**\n")
        for val, name, eid in low_batteries:
            print(f"• **{name}:** `{val:.0f}%` (Entity: `{eid}`)")
        print("\n*Please replace or recharge batteries before sensors go offline.*")
    else:
        if not quiet:
            print(f"✅ All {len(states)} Home Assistant IoT sensors have healthy battery levels (> {threshold:.0f}%).")

if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    thresh = 15.0
    for arg in sys.argv[1:]:
        if arg.startswith("--threshold="):
            try: thresh = float(arg.split("=")[1])
            except ValueError: pass
    check_batteries(threshold=thresh, quiet=quiet)
