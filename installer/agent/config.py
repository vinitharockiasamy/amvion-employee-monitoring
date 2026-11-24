import os
import json
import uuid
import socket
import platform
import base64
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "agent_state.json"
SERVER_URL_FILE = BASE_DIR / "server_url.txt"
DEFAULT_PUBLIC_URL = base64.b64decode("aHR0cHM6Ly9hbXZpb24tZW1wbG95ZWUtbW9uaXRvcmluZy5vbnJlbmRlci5jb20=").decode("utf-8")

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
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()))

def get_system_metadata() -> dict:
    return {
        "computer_name": socket.gethostname(),
        "employee_name": os.getenv("USERNAME", os.getenv("USER", "Employee")),
        "os_version": f"{platform.system()} {platform.release()} ({platform.version()})",
        "hardware_uuid": get_hardware_uuid()
    }

def _is_valid_active_url(u: str) -> bool:
    if not u or not u.startswith("http"):
        return False
    if "trycloudflare.com" in u or "10.79." in u or "192.168." in u:
        return False
    return True

class AgentConfig:
    def __init__(self):
        self.server_url = os.getenv("SERVER_URL", DEFAULT_PUBLIC_URL)
        self.enrollment_key = os.getenv(
            "ORG_ENROLLMENT_KEY",
            base64.b64decode("YW12aW9uLWVudGVycHJpc2Utc2VjcmV0LWtleS0yMDI2").decode("utf-8")
        )
        self.screenshot_interval_sec = 3
        self.compression_quality = 70
        self.thumbnail_width = 1280
        
        self.device_id = None
        self.device_secret = None
        
        self.load_state()

    def load_state(self):
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

        target_url = DEFAULT_PUBLIC_URL
        if SERVER_URL_FILE.exists():
            try:
                with open(SERVER_URL_FILE, "r") as f:
                    u = f.read().strip().rstrip("/")
                    if _is_valid_active_url(u):
                        target_url = u
            except Exception:
                pass

        if saved_server_url and saved_server_url != target_url:
            self.device_id = None
            self.device_secret = None

        self.server_url = target_url

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
