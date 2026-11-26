import os
import base64
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_storage_dir() -> Path:
    explicit = os.getenv("STORAGE_DIR", "").strip()
    if explicit:
        return Path(explicit).resolve()
    # Auto-detect Render persistent disk mount (/var/data) when present and writable
    render_disk = Path("/var/data")
    if os.name != "nt" and render_disk.exists() and os.access(render_disk, os.W_OK):
        return render_disk / "emp_monitoring"
    return BASE_DIR / "storage"


def _normalize_database_url(raw_url: str) -> str:
    url = raw_url.strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


STORAGE_DIR = _resolve_storage_dir()
SCREENSHOTS_DIR = STORAGE_DIR / "screenshots"


class Settings:
    PROJECT_NAME: str = "Employee Monitoring System"
    API_V1_STR: str = "/api/v1"

    # Storage & Database (supports Render PostgreSQL DATABASE_URL + Persistent Disk STORAGE_DIR)
    BASE_DIR: Path = BASE_DIR
    STORAGE_DIR: Path = STORAGE_DIR
    SCREENSHOTS_DIR: Path = SCREENSHOTS_DIR
    MAX_SCREENSHOTS_PER_DEVICE: int = max(5, int(os.getenv("MAX_SCREENSHOTS_PER_DEVICE", "30")))

    DATABASE_URL: str = _normalize_database_url(
        os.getenv("DATABASE_URL", f"sqlite:///{(STORAGE_DIR / 'monitoring.db').as_posix()}")
    )

    # Defaults
    DEFAULT_SCREENSHOT_INTERVAL_SEC: int = int(os.getenv("DEFAULT_SCREENSHOT_INTERVAL_SEC", "3"))
    DEFAULT_COMPRESSION_QUALITY: int = int(os.getenv("DEFAULT_COMPRESSION_QUALITY", "70"))
    DEFAULT_THUMBNAIL_WIDTH: int = int(os.getenv("DEFAULT_THUMBNAIL_WIDTH", "1280"))

    # Enterprise Security & Enrollment
    ORG_ENROLLMENT_KEY: str = os.getenv(
        "ORG_ENROLLMENT_KEY",
        base64.b64decode("YW12aW9uLWVudGVycHJpc2Utc2VjcmV0LWtleS0yMDI2").decode("utf-8"),
    )
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "AdminPass@2026")
    ADMIN_AUTH_TOKEN: str = os.getenv("ADMIN_AUTH_TOKEN", "enterprise-admin-token-xyz789")
    ADMIN_SESSION_TTL_SECONDS: int = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", str(12 * 3600)))

    # CORS Origins (comma-separated list, or * by default for backward compatibility)
    CORS_ORIGINS: list = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    ]

    # Server network
    HOST: str = "0.0.0.0"
    PORT: int = int(os.getenv("PORT", "8000"))

    # Streaming
    STREAM_TIMEOUT_SEC: int = 120


settings = Settings()
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
settings.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
