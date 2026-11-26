import json
import httpx
import websockets
import asyncio
from datetime import datetime
from agent.config import config, get_system_metadata

class AgentNetworkClient:
    def __init__(self):
        self.ws = None
        self.on_start_stream_cb = None
        self.on_stop_stream_cb = None
        self.running = True

    async def enroll_if_needed(self, force: bool = False) -> bool:
        """Enroll with Central Server if not already enrolled, or re-enroll if forced."""
        if not force and config.device_id and config.device_secret:
            print(f"[Client] Already enrolled with Device ID: {config.device_id}")
            return True

        print(f"[Client] Initiating device enrollment with Central Server ({config.server_url})...")
        url = f"{config.server_url}/api/v1/auth/enroll"
        headers = {
            "X-Enrollment-Key": config.enrollment_key
        }
        payload = get_system_metadata()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    config.device_id = data["device_id"]
                    config.device_secret = data["device_secret"]
                    cfg = data.get("config", {})
                    config.screenshot_interval_sec = cfg.get("screenshot_interval_sec", 3)
                    config.save_state()
                    print(f"[Client] Enrollment SUCCESS. Device ID: {config.device_id}")
                    return True
                else:
                    print(f"[Client] Enrollment returned {res.status_code}: {res.text}")
                    await asyncio.to_thread(config.discover_cloud_url)
                    return False
        except Exception as e:
            print(f"[Client] Enrollment network error: {e}")
            await asyncio.to_thread(config.discover_cloud_url)
            return False

    async def upload_screenshot(self, image_bytes: bytes, resolution: str) -> bool:
        """Send periodic compressed screenshot to Central Server via authenticated HTTP POST."""
        if not config.device_id or not config.device_secret:
            await self.enroll_if_needed(force=True)
            return False

        url = f"{config.server_url}/api/v1/agent/screenshot"
        headers = {
            "X-Device-Id": config.device_id,
            "Authorization": f"Bearer {config.device_secret}"
        }
        files = {
            "file": ("screenshot.jpg", image_bytes, "image/jpeg")
        }
        data = {
            "captured_at": datetime.utcnow().isoformat(),
            "resolution": resolution
        }

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.post(url, headers=headers, files=files, data=data)
                if res.status_code in (401, 404):
                    print(f"[Client] Server returned {res.status_code}. Refreshing cloud URL & enrollment...")
                    await asyncio.to_thread(config.discover_cloud_url)
                    config.device_id = None
                    config.device_secret = None
                    await self.enroll_if_needed(force=True)
                    if self.ws:
                        try:
                            await self.ws.close()
                        except Exception:
                            pass
                    return False
                return res.status_code == 200
        except Exception as e:
            print(f"[Client] Screenshot upload failed: {e}")
            return False

    async def send_ws_message(self, message: dict):
        """Send message through active WebSocket."""
        if self.ws:
            try:
                await self.ws.send(json.dumps(message))
            except Exception as e:
                print(f"[Client] WebSocket send error: {e}")

    async def run_websocket_loop(self):
        """Persistent WebSocket loop for keepalives and server command reception."""
        while self.running:
            ws_base = config.server_url.replace("http://", "ws://").replace("https://", "wss://")
            ws_url = f"{ws_base}/ws/agent/{config.device_id}"
            try:
                print(f"[Client] Connecting WebSocket to {ws_url}...")
                async with websockets.connect(ws_url) as ws:
                    self.ws = ws
                    print("[Client] WebSocket CONNECTED to Central Server.")

                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    try:
                        async for msg in ws:
                            try:
                                data = json.loads(msg)
                                cmd_type = data.get("type")
                                command_device_id = data.get("device_id")
                                if command_device_id and command_device_id != config.device_id:
                                    print(f"[Client] Ignoring {cmd_type} command for another device.")
                                    continue
                                if cmd_type == "START_STREAM":
                                    fps = data.get("target_fps", 15)
                                    stream_id = data.get("stream_id")
                                    if self.on_start_stream_cb:
                                        await self.on_start_stream_cb(stream_id, fps)
                                elif cmd_type == "STOP_STREAM":
                                    if self.on_stop_stream_cb:
                                        await self.on_stop_stream_cb(data.get("stream_id"))
                            except Exception as err:
                                print(f"[Client] WS command handling error: {err}")
                    finally:
                        heartbeat_task.cancel()
                        self.ws = None

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Client] WebSocket disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _heartbeat_loop(self):
        while True:
            try:
                await asyncio.sleep(10)
                await self.send_ws_message({
                    "type": "HEARTBEAT",
                    "mode": "screenshot"
                })
            except asyncio.CancelledError:
                break
            except Exception:
                pass
