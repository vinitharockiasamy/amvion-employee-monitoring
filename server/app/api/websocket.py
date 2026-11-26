import json
import asyncio
import uuid
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
        # device_id -> set of dashboard WebSockets currently watching this device
        self.stream_viewers: Dict[str, Set[WebSocket]] = {}
        # device_id -> currently valid stream session ID
        self.active_streams: Dict[str, str] = {}

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
        self.active_streams.pop(device_id, None)

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

    async def send_to_agent(self, device_id: str, message: dict):
        ws = self.active_agents.get(device_id)
        if not ws:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception:
            return False

    async def start_device_stream(self, device_id: str, dashboard_ws: WebSocket):
        viewers = self.stream_viewers.setdefault(device_id, set())
        if dashboard_ws in viewers:
            return True
        is_new_session = not viewers or device_id not in self.active_streams
        viewers.add(dashboard_ws)

        if is_new_session:
            stream_id = str(uuid.uuid4())
            self.active_streams[device_id] = stream_id
            sent = await self.send_to_agent(device_id, {
                "type": "START_STREAM",
                "device_id": device_id,
                "stream_id": stream_id,
                "target_fps": 15
            })
            if not sent:
                viewers.discard(dashboard_ws)
                if not viewers:
                    self.stream_viewers.pop(device_id, None)
                self.active_streams.pop(device_id, None)
                await self.set_device_status(device_id, "offline", "screenshot")
                return False

        await self.set_device_status(device_id, "online", "streaming")
        return True

    async def stop_device_stream(self, device_id: str):
        self.stream_viewers.pop(device_id, None)
        stream_id = self.active_streams.pop(device_id, None)
        status_val = "online" if device_id in self.active_agents else "offline"
        await self.set_device_status(device_id, status_val, "screenshot")
        if not stream_id:
            return True
        return await self.send_to_agent(device_id, {
            "type": "STOP_STREAM",
            "device_id": device_id,
            "stream_id": stream_id
        })

    async def relay_live_frame(self, device_id: str, message: dict):
        """Relay only frames for the active session on the sending agent connection."""
        stream_id = self.active_streams.get(device_id)
        if not stream_id or message.get("stream_id") != stream_id:
            return False
        if device_id not in self.stream_viewers:
            return False
        await self.relay_to_viewers(device_id, {
            "type": "LIVE_FRAME",
            "device_id": device_id,
            "stream_id": stream_id,
            "frame": message.get("frame"),
            "timestamp": message.get("timestamp")
        })
        return True

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
                # The path-bound agent device_id is authoritative; ignore payload device_id.
                await manager.relay_live_frame(device_id, message)
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
                started = await manager.start_device_stream(device_id, websocket)
                if not started:
                    await websocket.send_json({
                        "type": "STREAM_ERROR",
                        "device_id": device_id,
                        "detail": "Requested device is unavailable"
                    })
            elif msg_type == "STOP_STREAM" and device_id:
                # Remove this dashboard from viewers
                viewers = manager.stream_viewers.get(device_id, set())
                viewers.discard(websocket)
                if len(viewers) == 0:
                    stopped = await manager.stop_device_stream(device_id)
                    if not stopped:
                        await websocket.send_json({
                            "type": "STREAM_ERROR",
                            "device_id": device_id,
                            "detail": "Requested device is unavailable"
                        })
            elif msg_type in ("SIGNAL_OFFER", "SIGNAL_ANSWER", "SIGNAL_ICE") and device_id:
                # WebRTC signaling relay to agent
                await manager.send_to_agent(device_id, message)
    except WebSocketDisconnect:
        manager.disconnect_dashboard(websocket)
    except Exception as e:
        print(f"[WS Dashboard] Exception: {e}")
        manager.disconnect_dashboard(websocket)
