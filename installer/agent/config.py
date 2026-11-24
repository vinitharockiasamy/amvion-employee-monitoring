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

AGENT_VERSION = "1.2.1"
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
    env_enabled = os.getenv("DLP_EVIDENCE_ENABLED", "").strip().lower()
    evidence_enabled = False if env_enabled in ("0", "false", "no", "off") else True
    try:
        max_mb = float(os.getenv("DLP_MAX_EVIDENCE_SIZE_MB", "25"))
    except ValueError:
        max_mb = 25.0
    max_mb_val = int(max_mb) if max_mb.is_integer() else max_mb

    return {
        "enabled": True,
        "monitored_directories": [
            "Desktop",
            "Documents",
            "Downloads",
            "Pictures",
            "Music",
            "Videos"
        ],
        "monitor_removable_storage": True,
        "evidence_enabled": evidence_enabled,
        "maximum_evidence_size_mb": max_mb_val,
        "baseline_hash_max_mb": 100,
        "recycle_bin_recovery_enabled": True,
        "secondary_backup_directories": [],
        "debug_diagnostics": False,
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
        """Resolve configured directory names (relative to user profile, OneDrive folders, or absolute paths)."""
        home = Path.home()
        resolved = []
        seen = set()

        def _add_candidate(cand_path: Path, require_existing: bool = False):
            if require_existing and not cand_path.is_dir():
                return
            norm = str(cand_path.resolve()) if cand_path.exists() else str(cand_path)
            key = norm.lower()
            if key not in seen:
                seen.add(key)
                resolved.append(Path(norm))

        for entry in self.dlp.get("monitored_directories", []):
            if not entry:
                continue
            p = Path(entry)
            if p.is_absolute():
                _add_candidate(p, require_existing=False)
            else:
                _add_candidate(home / p, require_existing=False)
                # Also monitor OneDrive-redirected counterparts (e.g. OneDrive\Desktop, OneDrive\Documents) if present
                onedrive_cand = home / "OneDrive" / p
                _add_candidate(onedrive_cand, require_existing=True)
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
                        if merged.get("monitored_directories") == ["Desktop", "Documents", "Downloads", "Pictures"]:
                            merged["monitored_directories"] = [
                                "Desktop",
                                "Documents",
                                "Downloads",
                                "Pictures",
                                "Music",
                                "Videos",
                            ]
                        self.dlp = merged
            except Exception as e:
                print(f"[Config] Error loading state: {e}")

        target_url = saved_server_url if _is_valid_active_url(saved_server_url) else DEFAULT_PUBLIC_URL
        url_candidates = [SERVER_URL_FILE]
        if not os.getenv("TESTING") and BASE_DIR.name.lower() == "agent":
            url_candidates.append(BASE_DIR.parent / "server_url.txt")
        for url_file in url_candidates:
            if url_file.exists():
                try:
                    with open(url_file, "r", encoding="utf-8-sig") as f:
                        u = f.read().strip().rstrip("/")
                        if _is_valid_active_url(u):
                            target_url = u
                            break
                except Exception:
                    pass

        if saved_server_url and saved_server_url != target_url:
            self.device_id = None
            self.device_secret = None

        self.server_url = target_url
        if not os.getenv("TESTING") and not target_url.startswith(("http://127.0.0.1", "http://localhost")):
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
