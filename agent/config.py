import os
import json
import uuid
import socket
import platform
import sys
import httpx
from pathlib import Path

if getattr(sys, "frozen", False):
    # When packaged as standalone PyInstaller executable
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "agent_state.json"
SERVER_URL_FILE = BASE_DIR / "server_url.txt"
DISCOVERY_TOPIC = "amvion-emp-monitor-vinith-2026"
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8000"

def get_hardware_uuid() -> str:
    """Extract Windows MachineGuid from Registry or generate deterministic fallback."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if guid:
                return str(guid).strip()
    except Exception:
        pass
    
    # Fallback to node-based uuid
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()))

def get_system_metadata() -> dict:
    return {
        "computer_name": socket.gethostname(),
        "employee_name": os.getenv("USERNAME", os.getenv("USER", "Employee")),
        "os_version": f"{platform.system()} {platform.release()} ({platform.version()})",
        "hardware_uuid": get_hardware_uuid()
    }

class AgentConfig:
    def __init__(self):
        self.server_url = os.getenv("SERVER_URL", DEFAULT_PUBLIC_URL)
        self.enrollment_key = os.getenv("ORG_ENROLLMENT_KEY", "amvion-enterprise-secret-key-2026")
        self.screenshot_interval_sec = 3
        self.compression_quality = 70
        self.thumbnail_width = 1280
        
        self.device_id = None
        self.device_secret = None
        
        self.load_state()

    def discover_public_url(self) -> bool:
        """Query the permanent discovery channel for the latest active public tunnel URL."""
        try:
            r = httpx.get(f"https://ntfy.sh/{DISCOVERY_TOPIC}/raw?poll=1", timeout=6.0)
            if r.status_code == 200:
                lines = [
                    line.strip()
                    for line in r.text.splitlines()
                    if line.strip().startswith("https://") and ("trycloudflare.com" in line or "onrender.com" in line or "railway.app" in line or "koyeb.app" in line or "hf.space" in line)
                ]
                if lines:
                    latest_url = lines[-1].rstrip("/")
                    if latest_url != self.server_url:
                        print(f"[Config] Discovered active public server URL: {latest_url}")
                        self.server_url = latest_url
                        try:
                            with open(SERVER_URL_FILE, "w") as f:
                                f.write(latest_url + "\n")
                        except Exception:
                            pass
                        self.save_state()
                        return True
        except Exception as e:
            print(f"[Config] Discovery check skipped: {e}")
        return False

    def load_state(self):
        # 1. Check for persisted agent state
        saved_server_url = None
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    saved_server_url = data.get("server_url")
                    self.device_id = data.get("device_id")
                    self.device_secret = data.get("device_secret")
                    self.screenshot_interval_sec = 3
            except Exception as e:
                print(f"[Config] Error loading state: {e}")

        # 2. Check for explicit server_url.txt
        target_url = saved_server_url or self.server_url
        if SERVER_URL_FILE.exists():
            try:
                with open(SERVER_URL_FILE, "r") as f:
                    u = f.read().strip()
                    if u.startswith("http"):
                        target_url = u.rstrip("/")
            except Exception:
                pass

        self.server_url = target_url

        # 3. Automatically check permanent discovery channel for the latest live public URL
        self.discover_public_url()

    def save_state(self):
        try:
            data = {
                "server_url": self.server_url,
                "device_id": self.device_id,
                "device_secret": self.device_secret,
                "screenshot_interval_sec": self.screenshot_interval_sec,
                "compression_quality": self.compression_quality
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

config = AgentConfig()
