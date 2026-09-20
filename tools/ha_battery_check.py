#!/usr/bin/env python3
import sys, json, urllib.request, os

SECRETS_PATH = os.environ.get("HA_SECRETS_PATH", "/secrets/ha.json")

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

def check_batteries(threshold: float = 15.0, quiet: bool = False) -> int:
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
    return 0

if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    thresh = 15.0
    for arg in sys.argv[1:]:
        if arg.startswith("--threshold="):
            try: thresh = float(arg.split("=")[1])
            except ValueError: pass
    sys.exit(check_batteries(threshold=thresh, quiet=quiet))
