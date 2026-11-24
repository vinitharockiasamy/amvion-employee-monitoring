import json
import asyncio
from datetime import datetime
from typing import Dict, Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from server.app.db.session import SessionLocal
from server.app.db.models import Device
from server.app.core.security import verify_admin_ws_request, verify_admin_ws_token

router = APIRouter(tags=["WebSockets"])

class ConnectionManager:
    def __init__(self):
        # device_id -> WebSocket
        self.active_agents: Dict[str, WebSocket] = {}
        # Set of dashboard WebSockets
        self.active_dashboards: Set[WebSocket] = set()
        # Set of authenticated DLP admin WebSockets
        self.active_dlp_admins: Set[WebSocket] = set()
        # device_id -> set of dashboard WebSockets currently watching this device
        self.stream_viewers: Dict[str, Set[WebSocket]] = {}

    async def connect_agent(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_agents[device_id] = websocket
        await self.set_device_status(device_id, "online", "screenshot")
        await self.broadcast_to_dashboards({
            "type": "DEVICE_STATUS",
            "device_id": device_id,
            "status": "online"
        })

    async def disconnect_agent(self, device_id: str):
        if device_id in self.active_agents:
            del self.active_agents[device_id]
        await self.set_device_status(device_id, "offline", "screenshot")
        await self.broadcast_to_dashboards({
            "type": "DEVICE_STATUS",
            "device_id": device_id,
            "status": "offline"
        })
        # If any dashboard was streaming this device, notify them
        if device_id in self.stream_viewers:
            viewers = list(self.stream_viewers[device_id])
            for ws in viewers:
                try:
                    await ws.send_json({
                        "type": "STREAM_STOPPED",
                        "device_id": device_id,
                        "reason": "Agent disconnected"
                    })
                except Exception:
                    pass
            self.stream_viewers.pop(device_id, None)

    async def connect_dashboard(self, websocket: WebSocket):
        await websocket.accept()
        self.active_dashboards.add(websocket)

    def disconnect_dashboard(self, websocket: WebSocket):
        self.active_dashboards.discard(websocket)
        # Clean up any active stream views by this dashboard
        for device_id, viewers in list(self.stream_viewers.items()):
            if websocket in viewers:
                viewers.discard(websocket)
                if len(viewers) == 0:
                    # No more viewers for this device, tell agent to stop stream
                    asyncio.create_task(self.stop_device_stream(device_id))
                    self.stream_viewers.pop(device_id, None)

    async def broadcast_to_dashboards(self, message: dict):
        dead_sockets = set()
        for ws in self.active_dashboards:
            try:
                await ws.send_json(message)
            except Exception:
                dead_sockets.add(ws)
        self.active_dashboards.difference_update(dead_sockets)

    async def connect_dlp_admin(self, websocket: WebSocket):
        await websocket.accept()
        self.active_dlp_admins.add(websocket)

    def disconnect_dlp_admin(self, websocket: WebSocket):
        self.active_dlp_admins.discard(websocket)

    async def broadcast_dlp_event(self, event_dict: dict):
        payload = {
            "type": "DLP_EVENT_CREATED",
            "event": event_dict
        }
        dead_admins = set()
        for ws in self.active_dlp_admins:
            try:
                await ws.send_json(payload)
            except Exception:
                dead_admins.add(ws)
        self.active_dlp_admins.difference_update(dead_admins)
        await self.broadcast_to_dashboards(payload)

    async def send_to_agent(self, device_id: str, message: dict):
        ws = self.active_agents.get(device_id)
        if ws:
            try:
                await ws.send_json(message)
                return True
            except Exception:
                return False
        # Fallback: if agent re-enrolled after container restart and its WS holds its prior device_id, send to active sockets
        sent_any = False
        for aid, active_ws in list(self.active_agents.items()):
            try:
                await active_ws.send_json(message)
                sent_any = True
            except Exception:
                pass
        return sent_any

    async def start_device_stream(self, device_id: str, dashboard_ws: WebSocket):
        if device_id not in self.stream_viewers:
            self.stream_viewers[device_id] = set()
        self.stream_viewers[device_id].add(dashboard_ws)

        await self.set_device_status(device_id, "online", "streaming")
        await self.send_to_agent(device_id, {
            "type": "START_STREAM",
            "device_id": device_id,
            "target_fps": 15
        })

    async def stop_device_stream(self, device_id: str):
        if device_id in self.stream_viewers:
            self.stream_viewers.pop(device_id, None)
        await self.set_device_status(device_id, "online", "screenshot")
        await self.send_to_agent(device_id, {
            "type": "STOP_STREAM",
            "device_id": device_id
        })

    async def relay_to_viewers(self, device_id: str, message: dict):
        viewers = self.stream_viewers.get(device_id, set())
        dead_viewers = set()
        for ws in viewers:
            try:
                await ws.send_json(message)
            except Exception:
                dead_viewers.add(ws)
        viewers.difference_update(dead_viewers)

    async def set_device_status(self, device_id: str, status_val: str, mode_val: str):
        try:
            with SessionLocal() as db:
                dev = db.query(Device).filter(Device.id == device_id).first()
                if dev:
                    dev.status = status_val
                    dev.current_mode = mode_val
                    dev.last_seen_at = datetime.utcnow()
                    db.commit()
        except Exception as e:
            print(f"[WS] Error setting device status for {device_id}: {e}")

manager = ConnectionManager()

@router.websocket("/ws/agent/{device_id}")
async def agent_websocket_endpoint(websocket: WebSocket, device_id: str):
    await manager.connect_agent(device_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except Exception:
                continue

            msg_type = message.get("type")
            if msg_type == "HEARTBEAT":
                await manager.set_device_status(device_id, "online", message.get("mode", "screenshot"))
            elif msg_type in ("SIGNAL_OFFER", "SIGNAL_ANSWER", "SIGNAL_ICE"):
                # WebRTC signaling relay to dashboard viewers
                await manager.relay_to_viewers(device_id, message)
            elif msg_type == "LIVE_FRAME":
                # Fast video frame relay (base64/binary JPEG) for live stream player
                target_ids = [device_id] if device_id in manager.stream_viewers else list(manager.stream_viewers.keys())
                for tid in target_ids:
                    await manager.relay_to_viewers(tid, {
                        "type": "LIVE_FRAME",
                        "device_id": tid,
                        "frame": message.get("frame"),
                        "timestamp": message.get("timestamp")
                    })
    except WebSocketDisconnect:
        await manager.disconnect_agent(device_id)
    except Exception as e:
        print(f"[WS Agent] Exception for {device_id}: {e}")
        await manager.disconnect_agent(device_id)

@router.websocket("/ws/dashboard")
async def dashboard_websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None)
):
    if not verify_admin_ws_request(websocket, token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect_dashboard(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except Exception:
                continue

            msg_type = message.get("type")
            device_id = message.get("device_id")

            if msg_type == "REQUEST_STREAM" and device_id:
                await manager.start_device_stream(device_id, websocket)
            elif msg_type == "STOP_STREAM" and device_id:
                # Remove this dashboard from viewers
                viewers = manager.stream_viewers.get(device_id, set())
                viewers.discard(websocket)
                if len(viewers) == 0:
                    await manager.stop_device_stream(device_id)
            elif msg_type in ("SIGNAL_OFFER", "SIGNAL_ANSWER", "SIGNAL_ICE") and device_id:
                # WebRTC signaling relay to agent
                await manager.send_to_agent(device_id, message)
    except WebSocketDisconnect:
        manager.disconnect_dashboard(websocket)
    except Exception as e:
        print(f"[WS Dashboard] Exception: {e}")
        manager.disconnect_dashboard(websocket)

@router.websocket("/ws/dlp")
async def dlp_admin_websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None)
):
    """Authenticated WebSocket feed for administrator DLP event notifications."""
    if not verify_admin_ws_request(websocket, token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect_dlp_admin(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_dlp_admin(websocket)
    except Exception:
        manager.disconnect_dlp_admin(websocket)
