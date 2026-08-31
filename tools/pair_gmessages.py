#!/usr/bin/env python3
"""
Google Messages Pairing Orchestrator for OpenMessage on Host2 (.84).
Launches pairing, extracts QR code URL, generates PNG image, and captures successful pairing.
"""
import subprocess, re, sys, time, os, qrcode, json, urllib.parse

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

def start_pairing():
    ssh_key = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    _, host_2, ssh_port = _resolve_nas_config()

    print(f"[Pair] Starting Google Messages pairing session on host ({host_2})...")
    cmd = [
        "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "BatchMode=yes",
        f"{ssh_user}@{host_2}", "docker exec openmessage openmessage pair"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    qr_url = None
    ascii_qr = []
    capture_ascii = False

    for line in iter(proc.stdout.readline, ''):
        sys.stdout.write(line)
        sys.stdout.flush()

        if "Scan this QR code with Google Messages:" in line:
            capture_ascii = True
            continue

        if "URL: https://" in line:
            capture_ascii = False
            m = re.search(r"URL:\s*(https://\S+)", line)
            if m:
                qr_url = m.group(1)
                # Generate PNG
                qr = qrcode.QRCode(box_size=10, border=4)
                qr.add_data(qr_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                img.save("/workspace/data/gmessages_qr.png")
                print(f"\n[Pair] Generated QR code image at /workspace/data/gmessages_qr.png")

        if "Pairing successful!" in line:
            print("\n[Pair] 🎉 PAIRING SUCCESSFUL! Device linked.")
            break

    proc.wait()
    return proc.returncode

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Usage: pair_gmessages.py")
        sys.exit(0)
    sys.exit(start_pairing())
