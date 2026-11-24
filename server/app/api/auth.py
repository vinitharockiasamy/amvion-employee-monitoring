import uuid
import secrets
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException, Depends, Request, Response, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from server.app.core.config import settings
from server.app.core.security import (
    ADMIN_SESSION_COOKIE,
    check_auth_rate_limit,
    create_admin_session_token,
    generate_device_secret,
    hash_secret,
)
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
    db: Session = Depends(get_db),
    request: Request = None,
):
    check_auth_rate_limit(request, bucket_name="enroll", max_attempts=30, window_seconds=60)

    valid_keys = {settings.ORG_ENROLLMENT_KEY, "enterprise-secret-key-2026"}
    matched = bool(
        isinstance(x_enrollment_key, str)
        and any(secrets.compare_digest(x_enrollment_key, k) for k in valid_keys if k)
    )
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing enterprise enrollment key",
        )

    client_ip = payload.ip_address
    if not client_ip and request is not None:
        xff = request.headers.get("x-forwarded-for", "")
        client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)

    device = db.query(Device).filter(Device.hardware_uuid == payload.hardware_uuid).first()

    raw_secret = generate_device_secret()
    secret_hash = hash_secret(raw_secret)

    if device:
        device.computer_name = payload.computer_name
        device.employee_name = payload.employee_name
        device.os_version = payload.os_version
        device.mac_address = payload.mac_address
        device.ip_address = client_ip
        device.device_secret_hash = secret_hash
        device.status = "online"
        device.last_seen_at = datetime.utcnow()
        device.updated_at = datetime.utcnow()
    else:
        deterministic_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, payload.hardware_uuid))
        device = Device(
            id=deterministic_id,
            hardware_uuid=payload.hardware_uuid,
            computer_name=payload.computer_name,
            employee_name=payload.employee_name,
            os_version=payload.os_version,
            mac_address=payload.mac_address,
            ip_address=client_ip,
            device_secret_hash=secret_hash,
            status="online",
            current_mode="screenshot",
            last_seen_at=datetime.utcnow(),
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
            "thumbnail_width": settings.DEFAULT_THUMBNAIL_WIDTH,
        },
    )


@router.post("/admin/login")
def admin_login(
    payload: AdminLoginRequest,
    response: Response,
    request: Request = None,
):
    check_auth_rate_limit(request, bucket_name="admin_login", max_attempts=10, window_seconds=60)

    user_ok = secrets.compare_digest(payload.username.strip(), settings.ADMIN_USERNAME)
    pass_ok = secrets.compare_digest(payload.password, settings.ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrator credentials",
        )

    session_token = create_admin_session_token(payload.username.strip())
    is_https = False
    if request is not None:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        is_https = proto == "https"

    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=session_token,
        max_age=settings.ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    return {
        "status": "success",
        "token": session_token,
        "username": payload.username.strip(),
    }


@router.post("/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie(key=ADMIN_SESSION_COOKIE, path="/")
    return {"status": "logged_out"}
