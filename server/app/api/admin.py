import os
import zipfile
import httpx
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from server.app.core.config import settings
from server.app.db.session import get_db
from server.app.db.models import Device, Screenshot
from server.app.api.websocket import manager

router = APIRouter(prefix="/admin", tags=["Admin Operations"])

@router.get("/devices")
def list_devices(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db)
):
    query = db.query(Device).options(joinedload(Device.screenshots))
    if status_filter:
        query = query.filter(Device.status == status_filter)
    
    devices = query.order_by(Device.computer_name).all()

    # Presence watchdog: mark devices as offline if inactive for > 20s
    now = datetime.utcnow()
    threshold = now - timedelta(seconds=20)
    device_list = []

    for dev in devices:
        # Both screenshots (3s) and WS heartbeats (10s) update last_seen_at.
        # If last_seen_at is older than 20s, the agent is offline even if a half-open proxy socket hasn't closed yet.
        is_active = bool(dev.last_seen_at and dev.last_seen_at >= threshold)
        if not is_active:
            manager.disconnect_agent(dev.id)
            if dev.status != "offline":
                dev.status = "offline"
                dev.current_mode = "screenshot"
        elif is_active and dev.status != "online":
            dev.status = "online"

        device_list.append(dev.to_dict())

    db.commit()
    return {
        "total": len(device_list),
        "online_count": sum(1 for d in device_list if d["status"] == "online"),
        "devices": device_list
    }

@router.get("/devices/{device_id}")
def get_device(device_id: str, db: Session = Depends(get_db)):
    device = db.query(Device).options(joinedload(Device.screenshots)).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device.to_dict()

@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    manager.disconnect_agent(device_id)
    db.delete(device)
    db.commit()
    await manager.broadcast_to_dashboards({
        "type": "DEVICE_REMOVED",
        "device_id": device_id
    })
    return {"status": "deleted", "device_id": device_id}

@router.get("/devices/{device_id}/screenshot/latest")
def get_latest_screenshot(device_id: str, db: Session = Depends(get_db)):
    screenshot = (
        db.query(Screenshot)
        .filter(Screenshot.device_id == device_id)
        .order_by(Screenshot.captured_at.desc())
        .first()
    )
    if not screenshot or not os.path.exists(screenshot.file_path):
        raise HTTPException(status_code=404, detail="No screenshot available")
    
    return FileResponse(
        screenshot.file_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@router.get("/devices/{device_id}/screenshots/history")
def get_screenshot_history(
    device_id: str,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    screenshots = (
        db.query(Screenshot)
        .filter(Screenshot.device_id == device_id)
        .order_by(Screenshot.captured_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": s.id,
            "file_size_bytes": s.file_size_bytes,
            "resolution": s.resolution,
            "captured_at": s.captured_at.isoformat()
        }
        for s in screenshots
    ]

def _resolve_active_public_url(request: Request) -> Optional[str]:
    project_root = settings.BASE_DIR.parent
    # 1. Environment variable from Render / Railway
    env_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_SERVER_URL")
    if env_url:
        return env_url.strip().rstrip("/")
    # 2. Request host header if accessed via public domain
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if host and not host.startswith("127.0.0.1") and not host.startswith("localhost") and not host.startswith("10.") and not host.startswith("192.168."):
        return f"https://{host}".rstrip("/")
    # 3. Local PUBLIC_URL.txt from Cloudflare tunnel
    pub_file = project_root / "PUBLIC_URL.txt"
    if pub_file.exists():
        try:
            u = pub_file.read_text(encoding="utf-8").strip()
            if u:
                return u.rstrip("/")
        except Exception:
            pass
    return f"{proto}://{host}".rstrip("/") if host else None

@router.get("/public-url")
def get_public_url(request: Request):
    url = _resolve_active_public_url(request)
    if url and url.startswith("https://"):
        try:
            httpx.post("https://ntfy.sh/amvion-emp-monitor-vinith-2026", content=url, timeout=3.0)
        except Exception:
            pass
    return {"public_url": url}

@router.get("/download-agent")
def download_agent_zip(request: Request):
    project_root = settings.BASE_DIR.parent
    installer_dir = project_root / "installer"
    zip_path = project_root / "EmployeeMonitoringAgent-Setup.zip"

    active_url = _resolve_active_public_url(request)
    if installer_dir.exists() and not zip_path.exists():
        try:
            if active_url:
                (installer_dir / "server_url.txt").write_text(active_url + "\n", encoding="utf-8")
                if (installer_dir / "agent").exists():
                    (installer_dir / "agent" / "server_url.txt").write_text(active_url + "\n", encoding="utf-8")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_p in installer_dir.rglob("*"):
                    if file_p.is_file():
                        rel_name = file_p.relative_to(installer_dir).as_posix()
                        zf.write(file_p, arcname=rel_name)
        except Exception as e:
            print(f"[Admin] Dynamic zip build notice: {e}")

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Installer package not built yet")
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename="EmployeeMonitoringAgent-Setup.zip"
    )

@router.get("/download-exe")
def download_agent_exe():
    project_root = settings.BASE_DIR.parent
    exe_path = project_root / "installer" / "EmployeeMonitoringAgent.exe"
    if not exe_path.exists():
        raise HTTPException(status_code=404, detail="Standalone executable not built yet")
    return FileResponse(
        str(exe_path),
        media_type="application/octet-stream",
        filename="EmployeeMonitoringAgent.exe"
    )
