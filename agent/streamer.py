import asyncio
import base64
import time
from typing import Awaitable, Callable

from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor


class LiveStreamer:
    def __init__(
        self,
        capture: ScreenCapture,
        compressor: ImageCompressor,
        send_ws_fn: Callable[[dict], Awaitable[None]],
        device_id_fn: Callable[[], str] | None = None,
    ):
        self.capture = capture
        self.compressor = compressor
        self.send_ws = send_ws_fn
        self.device_id_fn = device_id_fn or (lambda: None)
        self.is_streaming = False
        self._stream_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.active_stream_id: str | None = None
        self.target_fps = 15

    async def start(self, stream_id: str, target_fps: int = 15):
        if not stream_id:
            print("[Streamer] Ignoring START_STREAM without a stream_id.")
            return
        async with self._lifecycle_lock:
            if self.is_streaming and self.active_stream_id == stream_id and self._stream_task and not self._stream_task.done():
                return
            if self._stream_task is not None:
                await self._stop_locked()

            self.target_fps = max(1, int(target_fps))
            self.active_stream_id = stream_id
            self.is_streaming = True
            self._stream_task = asyncio.create_task(self._stream_loop(stream_id))
            print(f"[Streamer] Live Stream Mode ACTIVATED at {self.target_fps} FPS.")

    async def stop(self, stream_id: str | None = None):
        async with self._lifecycle_lock:
            # A delayed STOP from an older session must not stop a newer session.
            if stream_id and stream_id != self.active_stream_id:
                return
            await self._stop_locked()

    async def _stop_locked(self):
        task = self._stream_task
        self.is_streaming = False
        self.active_stream_id = None
        self._stream_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        print("[Streamer] Live Stream Mode DEACTIVATED. Resuming Screenshot Mode.")

    async def _stream_loop(self, stream_id: str):
        frame_delay = 1.0 / self.target_fps
        try:
            while self.is_streaming and self.active_stream_id == stream_id:
                start_time = time.perf_counter()
                try:
                    img = self.capture.capture_primary()
                    frame_bytes = self.compressor.compress_for_stream(img, quality=55)
                    b64_frame = base64.b64encode(frame_bytes).decode("ascii")

                    # Recheck after capture/compression and immediately before sending.
                    if not self.is_streaming or self.active_stream_id != stream_id:
                        break
                    await self.send_ws({
                        "type": "LIVE_FRAME",
                        "device_id": self.device_id_fn(),
                        "stream_id": stream_id,
                        "frame": b64_frame,
                        "timestamp": int(time.time() * 1000)
                    })
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[Streamer] Frame error: {e}")

                elapsed = time.perf_counter() - start_time
                sleep_time = max(0.005, frame_delay - elapsed)
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            raise
