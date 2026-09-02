#!/usr/bin/env python3
import json
import sys
import os
import urllib.request
import urllib.parse

NAS_SECRETS_PATH = os.environ.get("NAS_OAUTH_SECRETS_PATH", "/docker/discord-agy-agent/secrets/google_oauth.json")
WORKSPACE_SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")
if not os.path.exists(WORKSPACE_SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    WORKSPACE_SECRETS_PATH = "/workspace/config/google_oauth.json"

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT") or str(49000 + 876)
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_SSH_PORT"):
                    ssh_port = str(d["NAS_SSH_PORT"])
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_2 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_2_IP"):
                    host_2 = d["NAS_HOST_2_IP"]
        except Exception:
            pass

    if host_1 and not host_2:
        parts = host_1.split(".")
        if len(parts) == 4 and parts[-1] == "82":
            host_2 = ".".join(parts[:3] + ["84"])

    return host_1 or "127.0.0.1", host_2 or "127.0.0.1", ssh_port

def parse_credentials(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds

def load_credentials(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return parse_credentials(f.read())

def exchange(raw_input: str):
    raw_input = raw_input.strip()
    if "code=" in raw_input:
        parsed = urllib.parse.urlparse(raw_input)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            code = params["code"][0]
        else:
            code = raw_input
    else:
        code = raw_input

    ssh_key = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    _, host_2, ssh_port = _resolve_nas_config()

    # Read client credentials
    if os.path.exists(WORKSPACE_SECRETS_PATH):
        creds = load_credentials(WORKSPACE_SECRETS_PATH)
    else:
        import subprocess
        cmd = f'ssh -i {ssh_key} -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=no {ssh_user}@{host_2} "cat {NAS_SECRETS_PATH}"'
        res = subprocess.check_output(cmd, shell=True).decode()
        creds = parse_credentials(res)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:8080"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode())
            print("RECEIVED TOKENS FROM GOOGLE! Keys:", list(tokens.keys()))
            new_refresh = tokens.get("refresh_token")
            if new_refresh:
                creds["refresh_token"] = new_refresh
            
            # Save to /workspace/config/google_oauth.json and try WORKSPACE_SECRETS_PATH
            local_configs = ["/workspace/config/google_oauth.json"]
            if WORKSPACE_SECRETS_PATH not in local_configs:
                local_configs.append(WORKSPACE_SECRETS_PATH)
            for p in local_configs:
                try:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w") as f:
                        json.dump(creds, f, indent=2)
                    print(f"Saved token to {p}")
                except Exception as se:
                    print(f"Note: local {p} write failed: {se}")
            
            # Save to NAS host directly over SSH
            json_str = json.dumps(creds, indent=2)
            sync_cmd = f'ssh -i {ssh_key} -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=no {ssh_user}@{host_2} "cat << \'INNER\' > {NAS_SECRETS_PATH}\n{json_str}\nINNER"'
            os.system(sync_cmd)
            print(f"SUCCESS: Successfully exchanged and saved new refresh token to host ({host_2})!")
            return True
    except urllib.error.HTTPError as e:
        print("HTTPError:", e.code, e.read().decode())
        return False
    except Exception as e:
        print(f"Exchange error: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        exchange(sys.argv[1])
    else:
        print("Usage: exchange_oauth_token.py <code_or_url>")
