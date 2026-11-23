import os
import sys
import time
import asyncio
import signal

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.config import config, BASE_DIR, get_system_metadata
from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor
from agent.streamer import LiveStreamer
from agent.client import AgentNetworkClient

# If running without a console window (noconsole/pythonw), redirect output to agent.log
if sys.stdout is None or sys.stderr is None:
    try:
        log_path = BASE_DIR / "agent.log"
        log_f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_f
        sys.stderr = log_f
    except Exception:
        pass

def clear_zone_identifier():
    """Remove Windows Mark-of-the-Web (Zone.Identifier ADS) so Windows never prompts on reboot."""
    targets = [sys.executable]
    try:
        for p in BASE_DIR.iterdir():
            targets.append(str(p))
    except Exception:
        pass
    for t in targets:
        try:
            os.remove(f"{t}:Zone.Identifier")
        except Exception:
            pass

clear_zone_identifier()

def print_banner():
    meta = get_system_metadata()
    print("=" * 65)
    print("  EMPLOYEE MONITORING SYSTEM — WINDOWS AGENT (MANAGED MVP)")
    print("=" * 65)
    print(f"  Computer Name : {meta['computer_name']}")
    print(f"  Employee Name : {meta['employee_name']}")
    print(f"  OS Version    : {meta['os_version']}")
    print(f"  Hardware UUID : {meta['hardware_uuid']}")
    print(f"  Central Server: {config.server_url}")
    print(f"  Capture Rate  : Every {config.screenshot_interval_sec}s (Screenshot Mode)")
    print("=" * 65)
    print("  Status: Enterprise transparent monitoring service active.")
    print("=" * 65)

class AgentDaemon:
    def __init__(self):
        self.capture = ScreenCapture()
        self.compressor = ImageCompressor(
            target_width=config.thumbnail_width,
            quality=config.compression_quality
        )
        self.client = AgentNetworkClient()
        self.streamer = LiveStreamer(
            capture=self.capture,
            compressor=self.compressor,
            send_ws_fn=self.client.send_ws_message
        )

        # Wire streaming callbacks
        self.client.on_start_stream_cb = self.streamer.start
        self.client.on_stop_stream_cb = self.streamer.stop

        self.running = True

    async def run(self):
        print_banner()

        # 1. Device Enrollment (with automatic retry loop so it never dies on network lag)
        while self.running:
            enrolled = await self.client.enroll_if_needed()
            if enrolled:
                break
            print("[Agent] Server not reachable yet. Retrying enrollment in 5 seconds...")
            await asyncio.sleep(5)

        # 2. Start WebSocket background listener
        ws_task = asyncio.create_task(self.client.run_websocket_loop())

        # 3. Screenshot loop
        print(f"[Agent] Starting Periodic Screenshot Mode (Interval: {config.screenshot_interval_sec}s)...")
        resolution = self.capture.get_resolution_str()

        try:
            while self.running:
                if not self.streamer.is_streaming:
                    try:
                        start_time = time.time()
                        # Capture desktop
                        pil_img = self.capture.capture_primary()
                        # Compress with Pillow
                        compressed_bytes = self.compressor.compress_for_upload(pil_img)
                        # Transmit to server
                        success = await self.client.upload_screenshot(compressed_bytes, resolution)
                        
                        elapsed = time.time() - start_time
                        if success:
                            print(f"[Agent] Screenshot uploaded successfully ({len(compressed_bytes) // 1024} KB in {elapsed:.2f}s)")
                        else:
                            print("[Agent] Screenshot upload returned non-200 status")
                    except Exception as err:
                        print(f"[Agent] Error in capture/upload cycle: {err}")

                    # Sleep remaining time until next screenshot
                    await asyncio.sleep(config.screenshot_interval_sec)
                else:
                    # While live streaming, live frames are handled by streamer at 15 FPS
                    await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        finally:
            print("[Agent] Stopping agent tasks...")
            self.running = False
            self.client.running = False
            self.streamer.stop()
            ws_task.cancel()
            self.capture.close()
            print("[Agent] Shutdown complete.")

def main():
    daemon = AgentDaemon()

    def signal_handler(sig, frame):
        print("\n[Agent] Interrupted by user. Exiting cleanly...")
        daemon.running = False

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
