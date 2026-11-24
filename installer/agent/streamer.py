import time
import base64
import asyncio
from typing import Callable, Awaitable
from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor

class LiveStreamer:
    def __init__(self, capture: ScreenCapture, compressor: ImageCompressor, send_ws_fn: Callable[[dict], Awaitable[None]]):
        self.capture = capture
        self.compressor = compressor
        self.send_ws = send_ws_fn
        self.is_streaming = False
        self._stream_task: asyncio.Task = None
        self.target_fps = 15

    def start(self, target_fps: int = 15):
        if self.is_streaming:
            return
        self.is_streaming = True
        self.target_fps = target_fps
        self._stream_task = asyncio.create_task(self._stream_loop())
        print(f"[Streamer] Live Stream Mode ACTIVATED at {target_fps} FPS.")

    def stop(self):
        if not self.is_streaming:
            return
        self.is_streaming = False
        if self._stream_task:
            self._stream_task.cancel()
            self._stream_task = None
        print("[Streamer] Live Stream Mode DEACTIVATED. Resuming Screenshot Mode.")

    async def _stream_loop(self):
        frame_delay = 1.0 / self.target_fps
        while self.is_streaming:
            start_time = time.perf_counter()
            try:
                # Capture and compress frame
                img = self.capture.capture_primary()
                frame_bytes = self.compressor.compress_for_stream(img, quality=55)
                b64_frame = base64.b64encode(frame_bytes).decode("ascii")

                # Send live frame over WebSocket
                await self.send_ws({
                    "type": "LIVE_FRAME",
                    "frame": b64_frame,
                    "timestamp": int(time.time() * 1000)
                })
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Streamer] Frame error: {e}")

            elapsed = time.perf_counter() - start_time
            sleep_time = max(0.005, frame_delay - elapsed)
            await asyncio.sleep(sleep_time)
