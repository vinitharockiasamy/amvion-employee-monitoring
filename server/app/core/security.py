import hashlib
import secrets
from typing import Optional
from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.orm import Session
from server.app.core.config import settings
from server.app.db.session import get_db
from server.app.db.models import Device

def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()

def verify_secret(plain_secret: str, hashed_secret: str) -> bool:
    return secrets.compare_digest(hash_secret(plain_secret), hashed_secret)

def generate_device_secret() -> str:
    return f"sec_live_{secrets.token_urlsafe(32)}"

def authenticate_device(
    x_device_id: str = Header(..., alias="X-Device-Id"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db)
) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header"
        )
    
    token = authorization.split("Bearer ")[1].strip()
    device = db.query(Device).filter(Device.id == x_device_id).first()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device not found or not enrolled"
        )
    
    if not verify_secret(token, device.device_secret_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device credentials"
        )
    
    return device

def authenticate_admin(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")
) -> bool:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1].strip()
    elif x_admin_token:
        token = x_admin_token
        
    if not token or token != settings.ADMIN_AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required"
        )
    return True
