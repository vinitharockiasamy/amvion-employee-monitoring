import os
import sys
import re
import time
import threading
import subprocess
import httpx
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Always redirect stdout and stderr to server.log so pythonw.exe / Uvicorn never crash without a console
LOG_FILE = PROJECT_ROOT / "server.log"
try:
    log_handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_handle
    sys.stderr = log_handle
except Exception:
    pass

import uvicorn

DISCOVERY_TOPIC = "emp-monitor-vinith-2026"
PUBLIC_URL_FILE = PROJECT_ROOT / "PUBLIC_URL.txt"
INSTALLER_URL_FILE = PROJECT_ROOT / "installer" / "server_url.txt"
CLOUDFLARED_BIN = PROJECT_ROOT / "cloudflared.exe"
CREATE_NO_WINDOW = 0x08000000

def publish_public_url(url: str):
    """Save the active public tunnel URL locally and publish to discovery channel for all agents."""
    url = url.strip().rstrip("/")
    print(f"\n==============================================================")
    print(f"  LIVE PUBLIC ADMIN WEBSITE : {url}")
    print(f"==============================================================\n")
    try:
        PUBLIC_URL_FILE.write_text(url + "\n", encoding="utf-8")
    except Exception:
        pass
    try:
        INSTALLER_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        INSTALLER_URL_FILE.write_text(url + "\n", encoding="utf-8")
    except Exception:
        pass
    try:
        httpx.post(f"https://ntfy.sh/{DISCOVERY_TOPIC}", content=url, timeout=8.0)
        print(f"[Tunnel] Published live URL to global agent discovery channel ({DISCOVERY_TOPIC}).")
    except Exception as e:
        print(f"[Tunnel] Warning: Could not publish to discovery channel: {e}")

def tunnel_guardian_loop():
    """Keep Cloudflare HTTPS Tunnel running 24/7 with CREATE_NO_WINDOW so it never dies on console close."""
    if not CLOUDFLARED_BIN.exists():
        print("[Tunnel] cloudflared.exe not found; skipping public tunnel.")
        return

    try:
        subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass

    url_pattern = re.compile(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)")

    while True:
        try:
            proc = subprocess.Popen(
                [str(CLOUDFLARED_BIN), "tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW
            )
            for line in proc.stdout:
                match = url_pattern.search(line)
                if match:
                    publish_public_url(match.group(1))
            proc.wait()
            print(f"[Tunnel] cloudflared exited with code {proc.returncode}, restarting in 3s...")
        except Exception as e:
            print(f"[Tunnel] Guardian error: {e}")
        time.sleep(3)

if __name__ == "__main__":
    cloud_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_SERVER_URL")
    if cloud_url:
        # Running on 24/7 Cloud Hosting (Render / Railway / Koyeb)
        publish_public_url(cloud_url)
    elif os.name == "nt":
        # Running locally on Windows: start Cloudflare Tunnel guardian
        t = threading.Thread(target=tunnel_guardian_loop, daemon=True)
        t.start()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "server.app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )
