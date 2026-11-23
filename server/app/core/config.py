import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = BASE_DIR / "storage"
SCREENSHOTS_DIR = STORAGE_DIR / "screenshots"

class Settings:
    PROJECT_NAME: str = "Employee Monitoring System"
    API_V1_STR: str = "/api/v1"
    
    # Storage & Database
    BASE_DIR: Path = BASE_DIR
    STORAGE_DIR: Path = STORAGE_DIR
    SCREENSHOTS_DIR: Path = SCREENSHOTS_DIR
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'monitoring.db'}"
    
    # Defaults
    DEFAULT_SCREENSHOT_INTERVAL_SEC: int = 3
    DEFAULT_COMPRESSION_QUALITY: int = 70
    DEFAULT_THUMBNAIL_WIDTH: int = 1280
    
    # Enterprise Security & Enrollment
    ORG_ENROLLMENT_KEY: str = os.getenv("ORG_ENROLLMENT_KEY", "amvion-enterprise-secret-key-2026")
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "AdminPass@2026")
    ADMIN_AUTH_TOKEN: str = os.getenv("ADMIN_AUTH_TOKEN", "enterprise-admin-token-xyz789")
    
    # Server network
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # Streaming
    STREAM_TIMEOUT_SEC: int = 120  # Auto-revert live stream to screenshot mode if idle

settings = Settings()
settings.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
