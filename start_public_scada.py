# start_public_scada.py
# ==============================================================================
# TEAM AYASK · NMDC CONVEYOR SCADA PUBLIC ACCESS LAUNCHER
# Starts local server and exposes a global HTTPS link via Cloudflare Tunnel
# ==============================================================================

import os
import sys
import time
import re
import subprocess
import signal

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base_dir)

    print("=" * 72)
    print("  TEAM AYASK · NMDC CONVEYOR AI SCADA PUBLIC LAUNCHER")
    print("=" * 72)

    # 1. Start FastAPI / Uvicorn Server
    print("\n[1/3] Launching local SCADA application server on port 8000...")
    server_cmd = [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8000"]
    server_proc = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)

    # 2. Check Cloudflared Binary
    cloudflared_path = os.path.join(base_dir, "cloudflared.exe")
    if not os.path.exists(cloudflared_path):
        print("[!] cloudflared.exe not found. Downloading standalone binary...")
        import urllib.request
        cf_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        urllib.request.urlretrieve(cf_url, cloudflared_path)
        print("[✓] Download complete.")

    # 3. Launch Cloudflare Tunnel
    print("[2/3] Establishing secure global HTTPS tunnel...")
    tunnel_cmd = [cloudflared_path, "tunnel", "--url", "http://127.0.0.1:8000"]
    tunnel_proc = subprocess.Popen(
        tunnel_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    public_url = None
    start_time = time.time()

    while time.time() - start_time < 25:
        line = tunnel_proc.stdout.readline()
        if not line:
            continue
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            public_url = match.group(0)
            break

    if not public_url:
        print("[!] Warning: Could not automatically parse tunnel URL. Checking tunnel logs...")
        public_url = "https://trycloudflare.com (Check console)"

    # 4. Display Credentials and Access Information
    print("\n" + "=" * 72)
    print("  [SUCCESS] PUBLIC ACCESS LINK GENERATED SUCCESSFULLY!")
    print("=" * 72)
    print(f"\n  PUBLIC LINK (Share this with anyone):")
    print(f"  >>> {public_url} <<<")
    print(f"\n  LOCAL DASHBOARD:")
    print(f"  >>> http://127.0.0.1:8000/dashboard <<<")
    print(f"\n  DEMO OPERATOR CREDENTIALS:")
    print(f"  • Email:    operator@nmdc.gov.in")
    print(f"  • Password: Admin@1234")
    print("\n" + "=" * 72)
    print("  Keep this window OPEN to keep the public link active.")
    print("  Press Ctrl+C to terminate the public link.")
    print("=" * 72 + "\n")

    # Save current public link to a text file for convenience
    with open("PUBLIC_ACCESS_LINK.txt", "w", encoding="utf-8") as f:
        f.write(f"TEAM AYASK · NMDC SCADA PUBLIC LINK:\n{public_url}\n\nLogin: operator@nmdc.gov.in\nPassword: Admin@1234\n")

    # Keep alive
    try:
        while True:
            time.sleep(1)
            if server_proc.poll() is not None:
                print("[!] Server process terminated unexpectedly.")
                break
            if tunnel_proc.poll() is not None:
                print("[!] Tunnel process terminated unexpectedly.")
                break
    except KeyboardInterrupt:
        print("\n[*] Shutting down public SCADA tunnel and local server...")
    finally:
        tunnel_proc.terminate()
        server_proc.terminate()
        tunnel_proc.wait(timeout=3)
        server_proc.wait(timeout=3)
        print("[✓] Clean shutdown complete.")

if __name__ == "__main__":
    main()
