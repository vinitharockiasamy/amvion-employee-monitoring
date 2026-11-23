import os
import sys
import io
import asyncio
from datetime import datetime

# Set test environment
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from starlette.datastructures import UploadFile
from server.app.core.config import settings
from server.app.db.session import init_db, SessionLocal
from server.app.db.models import Device, Screenshot
from server.app.api.auth import enroll_device, EnrollmentRequest
from server.app.api.agent import upload_screenshot, device_heartbeat
from server.app.api.admin import list_devices, get_device, get_latest_screenshot
from server.app.api.websocket import manager
from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor
from agent.config import get_system_metadata

async def run_direct_suite():
    print("==================================================", flush=True)
    print("  EMPLOYEE MONITORING SYSTEM — VERIFICATION SUITE", flush=True)
    print("==================================================", flush=True)

    # 1. Screen Capture with MSS
    print("\n[TEST 1] Testing Windows MSS Screen Capture...", flush=True)
    cap = ScreenCapture()
    img = cap.capture_primary()
    res_str = cap.get_resolution_str()
    print(f"  -> Successfully captured desktop screen! Size: {img.size}, Mode: {img.mode}, Res: {res_str}", flush=True)
    assert img.size[0] > 0 and img.size[1] > 0, "Screen capture failed: invalid dimensions!"

    # 2. Compression with Pillow
    print("\n[TEST 2] Testing Pillow Compression (Dual Modes)...", flush=True)
    compressor = ImageCompressor(target_width=1280, quality=70)
    upload_bytes = compressor.compress_for_upload(img)
    stream_bytes = compressor.compress_for_stream(img, quality=55)
    print(f"  -> Screenshot Mode Upload Payload Size: {len(upload_bytes) // 1024} KB", flush=True)
    print(f"  -> Live Stream Mode Frame Payload Size : {len(stream_bytes) // 1024} KB", flush=True)
    assert len(upload_bytes) > 0, "Upload compression failed!"
    assert len(stream_bytes) > 0, "Stream compression failed!"

    # 3. Database Initialization
    print("\n[TEST 3] Testing SQLite Database Initialization...", flush=True)
    init_db()
    print("  -> Database tables initialized successfully.", flush=True)

    # 4. Device Enrollment API
    print("\n[TEST 4] Testing Device Enrollment (X-Enrollment-Key Handshake)...", flush=True)
    db = SessionLocal()
    meta = get_system_metadata()
    enroll_req = EnrollmentRequest(
        hardware_uuid=meta["hardware_uuid"],
        computer_name=meta["computer_name"],
        employee_name=meta["employee_name"],
        os_version=meta["os_version"],
        mac_address="00:11:22:33:44:55",
        ip_address="127.0.0.1"
    )

    enroll_resp = enroll_device(
        payload=enroll_req,
        x_enrollment_key=settings.ORG_ENROLLMENT_KEY,
        db=db
    )
    device_id = enroll_resp.device_id
    device_secret = enroll_resp.device_secret
    print(f"  -> Enrolled Device ID     : {device_id}", flush=True)
    print(f"  -> Device Secret Issued   : {device_secret[:12]}...", flush=True)
    print(f"  -> Config Delivered       : {enroll_resp.config}", flush=True)
    assert device_id is not None and len(device_id) == 36
    assert device_secret.startswith("sec_live_")

    # 5. Screenshot Ingestion API
    print("\n[TEST 5] Testing Screenshot Ingestion & Storage...", flush=True)
    upload_file = UploadFile(filename="screenshot.jpg", file=io.BytesIO(upload_bytes))
    dev = db.query(Device).filter(Device.id == device_id).first()

    ingest_resp = await upload_screenshot(
        file=upload_file,
        captured_at=datetime.utcnow().isoformat(),
        resolution=res_str,
        device=dev,
        db=db
    )
    print(f"  -> Ingest Response: {ingest_resp}", flush=True)
    assert ingest_resp["status"] == "success"
    assert ingest_resp["stored_bytes"] == len(upload_bytes)

    # 6. Admin Live Screen Wall API
    print("\n[TEST 6] Testing Admin Live Screen Wall Grid API...", flush=True)
    wall_resp = list_devices(status_filter=None, db=db)
    print(f"  -> Total Devices on Wall  : {wall_resp['total']}", flush=True)
    print(f"  -> Online Devices Now     : {wall_resp['online_count']}", flush=True)
    assert wall_resp["total"] >= 1
    assert wall_resp["online_count"] >= 1

    target_dev = next(d for d in wall_resp["devices"] if d["device_id"] == device_id)
    print(f"  -> Wall Entry: {target_dev['employee_name']} | Host: {target_dev['computer_name']} | Status: {target_dev['status']}", flush=True)
    print(f"  -> Latest Screenshot URL  : {target_dev['latest_screenshot_url']}", flush=True)
    assert target_dev["status"] == "online"
    assert target_dev["latest_screenshot_url"] is not None

    # 7. Screenshot Retrieval API
    print("\n[TEST 7] Testing Screenshot Retrieval from File Storage...", flush=True)
    file_resp = get_latest_screenshot(device_id=device_id, db=db)
    print(f"  -> File Response Path     : {file_resp.path}", flush=True)
    print(f"  -> File Response Media    : {file_resp.media_type}", flush=True)
    assert os.path.exists(file_resp.path)
    assert file_resp.media_type == "image/jpeg"

    # 8. Dual-Mode Switch Mechanics (Screenshot Mode <-> Live Stream Mode)
    print("\n[TEST 8] Testing Mode Transition (Screenshot Mode <-> Live Stream Mode)...", flush=True)
    # Check initial mode
    assert target_dev["current_mode"] == "screenshot"
    print(f"  -> Initial Mode: {target_dev['current_mode']} (Periodic 5s screenshots)", flush=True)

    # Simulate Admin opening detailed screen view (Request Stream)
    class MockDashboardWS:
        def __init__(self):
            self.messages = []
        async def send_json(self, msg):
            self.messages.append(msg)

    mock_dash = MockDashboardWS()
    await manager.start_device_stream(device_id, mock_dash)
    dev_after_start = db.query(Device).filter(Device.id == device_id).first()
    print(f"  -> Mode after admin opens detail view: {dev_after_start.current_mode} (Live streaming activated)", flush=True)
    assert dev_after_start.current_mode == "streaming"

    # Simulate Admin closing detailed screen view (Exit to Wall)
    await manager.stop_device_stream(device_id)
    dev_after_stop = db.query(Device).filter(Device.id == device_id).first()
    print(f"  -> Mode after admin exits detail view: {dev_after_stop.current_mode} (Reverted to screenshot mode)", flush=True)
    assert dev_after_stop.current_mode == "screenshot"

    db.close()
    cap.close()

    print("\n==================================================", flush=True)
    print("  ALL 8 END-TO-END SYSTEM TESTS PASSED PERFECTLY! ", flush=True)
    print("==================================================", flush=True)

if __name__ == "__main__":
    asyncio.run(run_direct_suite())
