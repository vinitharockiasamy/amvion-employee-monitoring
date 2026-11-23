import os
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session

from server.app.core.config import settings
from server.app.core.security import authenticate_device
from server.app.db.session import get_db
from server.app.db.models import Device, Screenshot
from server.app.api.websocket import manager

router = APIRouter(prefix="/agent", tags=["Agent Ingestion"])

@router.post("/screenshot")
async def upload_screenshot(
    file: UploadFile = File(...),
    captured_at: Optional[str] = Form(None),
    resolution: Optional[str] = Form(None),
    device: Device = Depends(authenticate_device),
    db: Session = Depends(get_db)
):
    screenshot_id = str(uuid.uuid4())
    device_dir = settings.SCREENSHOTS_DIR / device.id
    device_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = device_dir / f"{screenshot_id}.jpg"

    content = await file.read()
    file_size = len(content)

    with open(file_path, "wb") as out_file:
        out_file.write(content)

    cap_time = datetime.utcnow()
    if captured_at:
        try:
            cap_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except Exception:
            cap_time = datetime.utcnow()

    screenshot = Screenshot(
        id=screenshot_id,
        device_id=device.id,
        file_path=str(file_path),
        file_size_bytes=file_size,
        resolution=resolution or "Unknown",
        captured_at=cap_time
    )
    db.add(screenshot)

    device.last_seen_at = datetime.utcnow()
    device.status = "online"
    db.commit()

    # Notify dashboard of live wall update
    await manager.broadcast_to_dashboards({
        "type": "SCREENSHOT_UPDATED",
        "device_id": device.id,
        "employee_name": device.employee_name,
        "computer_name": device.computer_name,
        "screenshot_url": f"/api/v1/admin/devices/{device.id}/screenshot/latest?t={int(datetime.utcnow().timestamp())}",
        "last_seen": device.last_seen_at.isoformat() + "Z"
    })

    return {
        "status": "success",
        "screenshot_id": screenshot_id,
        "stored_bytes": file_size,
        "timestamp": cap_time.isoformat()
    }

@router.post("/heartbeat")
def device_heartbeat(
    device: Device = Depends(authenticate_device),
    db: Session = Depends(get_db)
):
    device.last_seen_at = datetime.utcnow()
    device.status = "online"
    db.commit()
    
    return {
        "status": "ok",
        "current_mode": device.current_mode,
        "server_time": datetime.utcnow().isoformat()
    }
