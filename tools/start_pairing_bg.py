#!/usr/bin/env python3
import subprocess, re, sys, time, os, qrcode, shutil

def main():
    ssh_key = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

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
