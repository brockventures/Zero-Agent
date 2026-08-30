#!/usr/bin/env python3
import json
import sys
import os
import urllib.request
import urllib.parse

NAS_SECRETS_PATH = os.environ.get("NAS_OAUTH_SECRETS_PATH", "/docker/discord-agy-agent/secrets/google_oauth.json")
WORKSPACE_SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")

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

    # Read client credentials
    if os.path.exists(WORKSPACE_SECRETS_PATH):
        with open(WORKSPACE_SECRETS_PATH) as f:
            creds = json.load(f)
    else:
        import subprocess
        ssh_key = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
        ssh_port = os.environ.get("NAS_SSH_PORT", "22")
        ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
        host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

        cmd = f'ssh -i {ssh_key} -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=no {ssh_user}@{host_2} "cat {NAS_SECRETS_PATH}"'
        res = subprocess.check_output(cmd, shell=True).decode()
        try:
            creds = json.loads(res)
        except Exception:
            import re
            client_id = re.search(r'client_id[:\s]+([^\s,]+)', res).group(1).strip('"\'')
            client_secret = re.search(r'client_secret[:\s]+([^\s,]+)', res).group(1).strip('"\'')
            creds = {"client_id": client_id, "client_secret": client_secret}

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
            
            # Save to /secrets/ or configured path
            os.makedirs(os.path.dirname(WORKSPACE_SECRETS_PATH), exist_ok=True)
            with open(WORKSPACE_SECRETS_PATH, "w") as f:
                json.dump(creds, f, indent=2)
            
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
