import os
import json
import uuid
import socket
import platform
import sys
import httpx
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "agent_state.json"
SERVER_URL_FILE = BASE_DIR / "server_url.txt"
DISCOVERY_TOPIC = "emp-monitor-vinith-2026"
DEFAULT_PUBLIC_URL = "https://em-productivity-system.onrender.com"

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

def _default_dlp_settings() -> dict:
    return {
        "enabled": True,
        "monitored_directories": [
            "Desktop",
            "Documents",
            "Downloads",
            "Pictures"
        ],
        "monitor_removable_storage": True,
        "evidence_enabled": True,
        "maximum_evidence_size_mb": 25,
        "evidence_events": [
            "CREATED",
            "MODIFIED",
            "MOVED",
            "RENAMED",
            "COPIED",
            "USB_TRANSFER",
            "DELETED"
        ],
        "poll_interval_sec": 2.0,
        "max_queue_size": 500
    }


class AgentConfig:
    def __init__(self):
        self.server_url = os.getenv("SERVER_URL", DEFAULT_PUBLIC_URL)
        self.enrollment_key = os.getenv("ORG_ENROLLMENT_KEY", "enterprise-secret-key-2026")
        self.screenshot_interval_sec = 3
        self.compression_quality = 70
        self.thumbnail_width = 1280
        self.dlp = _default_dlp_settings()
        
        self.device_id = None
        self.device_secret = None
        
        self.load_state()

    def resolve_monitored_directories(self) -> list:
        """Resolve configured directory names (relative to user profile or absolute paths)."""
        home = Path.home()
        resolved = []
        seen = set()
        for entry in self.dlp.get("monitored_directories", []):
            if not entry:
                continue
            p = Path(entry)
            if not p.is_absolute():
                p = home / p
            norm = str(p.resolve()) if p.exists() else str(p)
            key = norm.lower()
            if key not in seen:
                seen.add(key)
                resolved.append(Path(norm))
        return resolved

    def discover_cloud_url(self) -> bool:
        """Automatically discover the active Render Cloud URL if the service subdomain was renamed."""
        try:
            r = httpx.get(f"https://ntfy.sh/{DISCOVERY_TOPIC}/raw?poll=1", timeout=6.0)
            if r.status_code == 200:
                lines = [
                    line.strip().rstrip("/")
                    for line in r.text.splitlines()
                    if line.strip().startswith("https://") and _is_valid_active_url(line.strip())
                ]
                if lines:
                    latest_url = lines[-1]
                    if latest_url != self.server_url:
                        print(f"[Config] Discovered active cloud server URL: {latest_url}")
                        self.server_url = latest_url
                        self.device_id = None
                        self.device_secret = None
                        try:
                            SERVER_URL_FILE.write_text(latest_url + "\n", encoding="utf-8")
                        except Exception:
                            pass
                        self.save_state()
                        return True
        except Exception:
            pass
        return False

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
                    if isinstance(data.get("dlp"), dict):
                        merged = _default_dlp_settings()
                        merged.update(data["dlp"])
                        self.dlp = merged
            except Exception as e:
                print(f"[Config] Error loading state: {e}")

        target_url = saved_server_url if _is_valid_active_url(saved_server_url) else DEFAULT_PUBLIC_URL
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
        if not os.getenv("TESTING"):
            self.discover_cloud_url()

    def save_state(self):
        try:
            data = {
                "server_url": self.server_url,
                "device_id": self.device_id,
                "device_secret": self.device_secret,
                "screenshot_interval_sec": self.screenshot_interval_sec,
                "compression_quality": self.compression_quality,
                "dlp": self.dlp
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

config = AgentConfig()
