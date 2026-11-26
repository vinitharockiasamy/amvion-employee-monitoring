import os
import hmac
import time
import base64
import hashlib
import secrets
from collections import defaultdict, deque
from typing import Optional
from fastapi import Cookie, Header, HTTPException, Query, Request, WebSocket, status, Depends
from sqlalchemy.orm import Session
from server.app.core.config import settings
from server.app.db.session import get_db
from server.app.db.models import Device

ADMIN_SESSION_COOKIE = "emp_admin_session"

# Sliding-window rate limit buckets: key -> deque[timestamps]
_rate_buckets: dict = defaultdict(deque)


def check_auth_rate_limit(
    request: Optional[Request],
    bucket_name: str,
    max_attempts: int = 10,
    window_seconds: int = 60,
):
    """Enforce per-IP sliding-window rate limiting on authentication endpoints."""
    if os.getenv("TESTING") == "1" and bucket_name != "test_rate_limit":
        return
    client_ip = "local"
    if request is not None:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            client_ip = xff.split(",")[0].strip()
        elif request.client and request.client.host:
            client_ip = request.client.host

    now = time.monotonic()
    key = f"{bucket_name}:{client_ip}"
    dq = _rate_buckets[key]
    cutoff = now - window_seconds
    while dq and dq[0] < cutoff:
        dq.popleft()

    if len(dq) >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts. Please retry shortly.",
        )
    dq.append(now)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(plain_secret: str, hashed_secret: str) -> bool:
    return secrets.compare_digest(hash_secret(plain_secret), hashed_secret)


def generate_device_secret() -> str:
    return f"sec_live_{secrets.token_urlsafe(32)}"


def create_admin_session_token(username: str, ttl_seconds: Optional[int] = None) -> str:
    """Create a stateless HMAC-SHA256 signed administrator session token."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.ADMIN_SESSION_TTL_SECONDS
    exp = int(time.time()) + max(60, int(ttl))
    user_b64 = base64.urlsafe_b64encode(username.encode("utf-8")).decode("ascii").rstrip("=")
    payload = f"v1.{exp}.{user_b64}"
    sig = hmac.new(
        settings.ADMIN_AUTH_TOKEN.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_admin_token_value(token_str: Optional[str]) -> bool:
    """Verify either a signed admin session token (v1.*) or the configured ADMIN_AUTH_TOKEN."""
    if not isinstance(token_str, str) or not token_str.strip():
        return False
    candidate = token_str.strip()

    if candidate.startswith("v1."):
        parts = candidate.split(".")
        if len(parts) != 4:
            return False
        _, exp_str, user_b64, provided_sig = parts
        try:
            exp = int(exp_str)
        except ValueError:
            return False
        if exp < int(time.time()):
            return False
        payload = f"v1.{exp_str}.{user_b64}"
        expected_sig = hmac.new(
            settings.ADMIN_AUTH_TOKEN.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return secrets.compare_digest(provided_sig, expected_sig)

    return secrets.compare_digest(candidate, settings.ADMIN_AUTH_TOKEN)


def extract_request_admin_token(request: Request) -> Optional[str]:
    """Extract admin token from Authorization header, X-Admin-Token, Cookie, or query."""
    auth_h = request.headers.get("authorization", "")
    if auth_h.startswith("Bearer "):
        return auth_h.split("Bearer ", 1)[1].strip()
    x_tok = request.headers.get("x-admin-token", "")
    if x_tok:
        return x_tok.strip()
    c_tok = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if c_tok:
        return c_tok.strip()
    q_tok = request.query_params.get("token", "")
    if q_tok:
        return q_tok.strip()
    return None


def authenticate_device(
    x_device_id: str = Header(..., alias="X-Device-Id"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = authorization.split("Bearer ", 1)[1].strip()
    device = db.query(Device).filter(Device.id == x_device_id).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device not found or not enrolled",
        )

    if not verify_secret(token, device.device_secret_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device credentials",
        )

    return device


def authenticate_admin(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    token: Optional[str] = Query(None, description="Optional admin token for browser media/download links"),
    session_cookie: Optional[str] = Cookie(None, alias=ADMIN_SESSION_COOKIE),
) -> bool:
    candidates = []
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        candidates.append(authorization.split("Bearer ", 1)[1].strip())
    if isinstance(x_admin_token, str) and x_admin_token:
        candidates.append(x_admin_token.strip())
    if isinstance(token, str) and token:
        candidates.append(token.strip())
    if isinstance(session_cookie, str) and session_cookie:
        candidates.append(session_cookie.strip())

    for cand in candidates:
        if verify_admin_token_value(cand):
            return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required",
    )


def verify_admin_ws_token(token: Optional[str]) -> bool:
    return verify_admin_token_value(token)


def verify_admin_ws_request(websocket: WebSocket, query_token: Optional[str] = None) -> bool:
    """Authenticate an admin WebSocket handshake via query token, headers, or HttpOnly session cookie."""
    candidates = []
    if isinstance(query_token, str) and query_token:
        candidates.append(query_token.strip())
    x_tok = websocket.headers.get("x-admin-token")
    if x_tok:
        candidates.append(x_tok.strip())
    auth_h = websocket.headers.get("authorization", "")
    if auth_h.startswith("Bearer "):
        candidates.append(auth_h.split("Bearer ", 1)[1].strip())
    c_tok = websocket.cookies.get(ADMIN_SESSION_COOKIE)
    if c_tok:
        candidates.append(c_tok.strip())

    return any(verify_admin_token_value(c) for c in candidates)
