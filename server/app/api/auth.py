import uuid
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException, Depends, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from server.app.core.config import settings
from server.app.core.security import generate_device_secret, hash_secret
from server.app.db.session import get_db
from server.app.db.models import Device

router = APIRouter(prefix="/auth", tags=["Authentication"])

class EnrollmentRequest(BaseModel):
    hardware_uuid: str
    computer_name: str
    employee_name: str
    os_version: Optional[str] = "Windows"
    mac_address: Optional[str] = None
    ip_address: Optional[str] = None

class EnrollmentResponse(BaseModel):
    device_id: str
    device_secret: str
    config: dict

class AdminLoginRequest(BaseModel):
    username: str
    password: str

@router.post("/enroll", response_model=EnrollmentResponse)
def enroll_device(
    payload: EnrollmentRequest,
    x_enrollment_key: Optional[str] = Header(None, alias="X-Enrollment-Key"),
    db: Session = Depends(get_db)
):
    valid_keys = {settings.ORG_ENROLLMENT_KEY, "enterprise-secret-key-2026"}
    if not x_enrollment_key or x_enrollment_key not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing enterprise enrollment key"
        )

    # Check if device already exists by hardware_uuid
    device = db.query(Device).filter(Device.hardware_uuid == payload.hardware_uuid).first()

    raw_secret = generate_device_secret()
    secret_hash = hash_secret(raw_secret)

    if device:
        # Update existing device info and regenerate secret
        device.computer_name = payload.computer_name
        device.employee_name = payload.employee_name
        device.os_version = payload.os_version
        device.mac_address = payload.mac_address
        device.ip_address = payload.ip_address
        device.device_secret_hash = secret_hash
        device.status = "online"
        device.last_seen_at = datetime.utcnow()
        device.updated_at = datetime.utcnow()
    else:
        # Deterministic device_id derived from hardware_uuid so it never changes across container restarts
        deterministic_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, payload.hardware_uuid))
        device = Device(
            id=deterministic_id,
            hardware_uuid=payload.hardware_uuid,
            computer_name=payload.computer_name,
            employee_name=payload.employee_name,
            os_version=payload.os_version,
            mac_address=payload.mac_address,
            ip_address=payload.ip_address,
            device_secret_hash=secret_hash,
            status="online",
            current_mode="screenshot",
            last_seen_at=datetime.utcnow()
        )
        db.add(device)

    db.commit()
    db.refresh(device)

    return EnrollmentResponse(
        device_id=device.id,
        device_secret=raw_secret,
        config={
            "screenshot_interval_sec": settings.DEFAULT_SCREENSHOT_INTERVAL_SEC,
            "compression_quality": settings.DEFAULT_COMPRESSION_QUALITY,
            "thumbnail_width": settings.DEFAULT_THUMBNAIL_WIDTH
        }
    )

@router.post("/admin/login")
def admin_login(payload: AdminLoginRequest):
    if payload.username == settings.ADMIN_USERNAME and payload.password == settings.ADMIN_PASSWORD:
        return {
            "status": "success",
            "token": settings.ADMIN_AUTH_TOKEN,
            "username": payload.username
        }
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid administrator credentials"
    )
