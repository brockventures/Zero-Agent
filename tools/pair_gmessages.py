#!/usr/bin/env python3
"""
Google Messages Pairing Orchestrator for OpenMessage on Host2 (.84).
Launches pairing, extracts QR code URL, generates PNG image, and captures successful pairing.
"""
import subprocess, re, sys, time, os, qrcode

def start_pairing():
    ssh_key = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

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
    sys.exit(start_pairing())
