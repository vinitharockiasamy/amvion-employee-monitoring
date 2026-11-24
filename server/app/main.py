import os
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from server.app.core.config import settings
from server.app.core.security import (
    create_admin_session_token,
    extract_request_admin_token,
    verify_admin_token_value,
)
from server.app.db.session import IS_SQLITE, init_db, SessionLocal
from server.app.db.models import Device, DlpEvent, DlpFileBlob
from server.app.api import auth, agent, admin, websocket, dlp

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Enterprise Employee Productivity & DLP Monitoring Console",
    version="1.2.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_production_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path.startswith("/api/") or request.url.path == "/":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.mount("/static", StaticFiles(directory=settings.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=settings.BASE_DIR / "templates")

app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(agent.router, prefix=settings.API_V1_STR)
app.include_router(admin.router, prefix=settings.API_V1_STR)
app.include_router(dlp.router)
app.include_router(websocket.router)


def _prune_expired_dlp_records():
    """Prune DLP audit events older than DLP_RETENTION_DAYS and any unreferenced vault blobs."""
    cutoff = datetime.utcnow() - timedelta(days=settings.DLP_RETENTION_DAYS)
    with SessionLocal() as db:
        deleted_events = db.query(DlpEvent).filter(DlpEvent.timestamp < cutoff).delete(synchronize_session=False)
        if deleted_events:
            orphan_blobs = (
                db.query(DlpFileBlob)
                .outerjoin(DlpEvent, DlpFileBlob.id == DlpEvent.file_blob_id)
                .filter(DlpEvent.id.is_(None), DlpFileBlob.created_at < cutoff)
                .all()
            )
            for blob in orphan_blobs:
                if blob.storage_path and os.path.exists(blob.storage_path):
                    try:
                        os.remove(blob.storage_path)
                    except OSError:
                        pass
                db.delete(blob)
            db.commit()


async def presence_watchdog():
    """Periodically check for timed-out agents and run scheduled storage maintenance."""
    ticks = 0
    while True:
        try:
            await asyncio.sleep(10)
            ticks += 1
            threshold = datetime.utcnow() - timedelta(seconds=20)
            stale_ids = []
            with SessionLocal() as db:
                devices = db.query(Device).filter(Device.status == "online").all()
                for dev in devices:
                    if not dev.last_seen_at or dev.last_seen_at < threshold:
                        dev.status = "offline"
                        dev.current_mode = "screenshot"
                        stale_ids.append(dev.id)
                if stale_ids:
                    db.commit()

            for sid in stale_ids:
                await websocket.manager.disconnect_agent(sid)

            # Run DLP retention check once per hour (360 * 10s)
            if ticks % 360 == 0:
                await asyncio.to_thread(_prune_expired_dlp_records)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Watchdog] Error: {e}")


@app.on_event("startup")
async def startup_event():
    init_db()
    if not os.getenv("TESTING"):
        asyncio.create_task(presence_watchdog())
    db_mode = "sqlite-wal" if IS_SQLITE else "postgresql"
    print(f"[Server] Production server initialized (db={db_mode}, storage={settings.STORAGE_DIR}).")


@app.get("/")
async def root(request: Request):
    tok = extract_request_admin_token(request)
    if not verify_admin_token_value(tok):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"app_name": settings.PROJECT_NAME},
        )

    session_tok = tok if (tok and tok.startswith("v1.")) else create_admin_session_token(settings.ADMIN_USERNAME)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": settings.PROJECT_NAME,
            "admin_token": session_tok,
        },
    )


@app.get("/health")
async def health_check():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        storage_ok = settings.STORAGE_DIR.exists()
        return {
            "status": "healthy" if storage_ok else "degraded",
            "version": app.version,
            "database": "sqlite-wal" if IS_SQLITE else "postgresql",
            "storage_ready": storage_ok,
            "time": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "detail": str(exc)},
        )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
