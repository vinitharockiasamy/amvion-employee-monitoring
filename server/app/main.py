import os
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from server.app.core.config import settings
from server.app.db.session import init_db, SessionLocal
from server.app.db.models import Device
from server.app.api import auth, agent, admin, websocket, dlp

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Enterprise Employee Screen Monitoring MVP Central Server",
    version="1.0.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and templates
app.mount("/static", StaticFiles(directory=settings.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=settings.BASE_DIR / "templates")

# Register routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(agent.router, prefix=settings.API_V1_STR)
app.include_router(admin.router, prefix=settings.API_V1_STR)
app.include_router(dlp.router)
app.include_router(websocket.router)

async def presence_watchdog():
    """Periodically check for timed-out agents and update their status to offline."""
    while True:
        try:
            await asyncio.sleep(10)
            threshold = datetime.utcnow() - timedelta(seconds=20)
            with SessionLocal() as db:
                devices = db.query(Device).filter(Device.status == "online").all()
                for dev in devices:
                    if not dev.last_seen_at or dev.last_seen_at < threshold:
                        websocket.manager.disconnect_agent(dev.id)
                        dev.status = "offline"
                        dev.current_mode = "screenshot"
                        await websocket.manager.broadcast_to_dashboards({
                            "type": "DEVICE_STATUS",
                            "device_id": dev.id,
                            "status": "offline"
                        })
                db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Watchdog] Error: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()
    if not os.getenv("TESTING"):
        asyncio.create_task(presence_watchdog())
    print("[Server] Database initialized.")

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": settings.PROJECT_NAME,
            "admin_token": settings.ADMIN_AUTH_TOKEN
        }
    )

@app.get("/health")
async def health_check():
    return {"status": "healthy", "time": datetime.utcnow().isoformat()}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
