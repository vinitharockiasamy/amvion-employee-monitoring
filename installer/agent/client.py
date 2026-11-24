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
                                if cmd_type == "START_STREAM":
                                    fps = data.get("target_fps", 15)
                                    if self.on_start_stream_cb:
                                        self.on_start_stream_cb(fps)
                                elif cmd_type == "STOP_STREAM":
                                    if self.on_stop_stream_cb:
                                        self.on_stop_stream_cb()
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

    async def upload_dlp_evidence(
        self,
        file_path: str,
        expected_sha256: str,
        original_filename: str,
        timeout: float = 20.0
    ) -> dict:
        """
        Upload an eligible DLP audit file (<= configured max MB) to POST /api/dlp/files.
        Returns {"status": "ok", "file_id": ..., "sha256": ...} or {"status": "retry"|"permanent_error", "code": ...}.
        """
        if not config.device_id or not config.device_secret:
            enrolled = await self.enroll_if_needed(force=True)
            if not enrolled:
                return {"status": "retry", "detail": "Not enrolled"}

        url = f"{config.server_url}/api/dlp/files"
        headers = {
            "X-Device-Id": config.device_id,
            "Authorization": f"Bearer {config.device_secret}"
        }

        try:
            with open(file_path, "rb") as f_obj:
                files = {"file": (original_filename, f_obj, "application/octet-stream")}
                data = {"expected_sha256": expected_sha256}
                async with httpx.AsyncClient(timeout=timeout) as http_client:
                    res = await http_client.post(url, headers=headers, data=data, files=files)

            if res.status_code in (200, 201):
                body = res.json()
                return {
                    "status": "ok",
                    "file_id": body.get("file_id"),
                    "sha256": body.get("sha256"),
                    "deduplicated": body.get("deduplicated", False)
                }
            if res.status_code == 401:
                config.device_id = None
                config.device_secret = None
                await self.enroll_if_needed(force=True)
                return {"status": "auth_error", "code": 401, "detail": "Re-enrolled after 401"}
            if res.status_code in (400, 413, 415, 422):
                return {"status": "permanent_error", "code": res.status_code, "detail": res.text}
            return {"status": "retry", "code": res.status_code, "detail": res.text}
        except (OSError, PermissionError) as fs_err:
            return {"status": "permanent_error", "code": 0, "detail": f"File unavailable: {fs_err}"}
        except Exception as net_err:
            return {"status": "retry", "code": 0, "detail": str(net_err)}

    async def post_dlp_event(self, event_payload: dict, timeout: float = 12.0) -> dict:
        """
        Deliver an authenticated DLP audit event to POST /api/dlp/events.
        Returns {"status": "ok", "event": ...} or {"status": "retry"|"auth_error"|"permanent_error"}.
        """
        if not config.device_id or not config.device_secret:
            enrolled = await self.enroll_if_needed(force=True)
            if not enrolled:
                return {"status": "retry", "detail": "Not enrolled"}

        url = f"{config.server_url}/api/dlp/events"
        headers = {
            "X-Device-Id": config.device_id,
            "Authorization": f"Bearer {config.device_secret}"
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                res = await http_client.post(url, headers=headers, json=event_payload)

            if res.status_code in (200, 201):
                return {"status": "ok", "event": res.json()}
            if res.status_code == 401:
                config.device_id = None
                config.device_secret = None
                await self.enroll_if_needed(force=True)
                return {"status": "auth_error", "code": 401, "detail": "Re-enrolled after 401"}
            if res.status_code in (400, 403, 404, 422):
                return {"status": "permanent_error", "code": res.status_code, "detail": res.text}
            return {"status": "retry", "code": res.status_code, "detail": res.text}
        except Exception as net_err:
            return {"status": "retry", "code": 0, "detail": str(net_err)}
