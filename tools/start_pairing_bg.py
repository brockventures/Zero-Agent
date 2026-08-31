#!/usr/bin/env python3
import subprocess, re, sys, time, os, qrcode, shutil, json, urllib.parse

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if not host_1 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
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

def main():
    ssh_key = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    _, host_2, ssh_port = _resolve_nas_config()

    print(f"[Pair] Starting Google Messages pairing daemon on host ({host_2})...")
    # Clean up old log and process
    subprocess.run([
        "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes",
        f"{ssh_user}@{host_2}", "rm -f /tmp/pair.log"
    ], check=False)

    # Run openmessage pair inside the container in background
    cmd = [
        "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes",
        f"{ssh_user}@{host_2}", "nohup docker exec -i openmessage openmessage pair > /tmp/pair.log 2>&1 &"
    ]
    subprocess.run(cmd, check=True)
    
    # Poll /tmp/pair.log for QR code
    qr_url = None
    ascii_lines = []
    capture = False
    
    for _ in range(30):
        time.sleep(0.5)
        res = subprocess.run([
            "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes",
            f"{ssh_user}@{host_2}", "cat /tmp/pair.log 2>/dev/null || true"
        ], capture_output=True, text=True)
        
        output = res.stdout
        if "URL: https://" in output:
            for line in output.splitlines():
                if "Scan this QR code" in line:
                    capture = True
                    continue
                if "URL: https://" in line:
                    capture = False
                    m = re.search(r"URL:\s*(https://\S+)", line)
                    if m:
                        qr_url = m.group(1)
                if capture:
                    ascii_lines.append(line)
            break
            
    if qr_url:
        # Generate PNG
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_path = "/workspace/data/gmessages_qr.png"
        img.save(img_path)
        
        art_path = "/root/.gemini/antigravity-cli/brain/0b8eebe0-5f7b-4c41-8297-db2c09e250de/gmessages_qr.png"
        shutil.copy(img_path, art_path)
        
        print(f"QR_URL={qr_url}")
        print("\n--- ASCII QR CODE ---")
        print("\n".join(ascii_lines))
        print("--- END ASCII QR ---")
        return 0
    else:
        print("Failed to capture QR code from /tmp/pair.log")
        return 1

if __name__ == "__main__":
    sys.exit(main())
