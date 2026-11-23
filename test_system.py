import os
import sys
import time
from datetime import datetime

# Configure test environment
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from fastapi.testclient import TestClient
from server.app.core.config import settings
from server.app.db.session import init_db, SessionLocal
from server.app.db.models import Device, Screenshot
from server.app.main import app
from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor
from agent.config import get_system_metadata

def run_tests():
    print("==================================================", flush=True)
    print("  EMPLOYEE MONITORING SYSTEM — VERIFICATION SUITE", flush=True)
    print("==================================================", flush=True)

    # Test 1: Screen Capture with MSS
    print("\n[TEST 1] Testing Windows MSS Screen Capture...", flush=True)
    cap = ScreenCapture()
    img = cap.capture_primary()
    res_str = cap.get_resolution_str()
    print(f"  -> Successfully captured desktop screen! Size: {img.size}, Mode: {img.mode}, Res: {res_str}", flush=True)
    assert img.size[0] > 0 and img.size[1] > 0, "Captured image has invalid dimensions!"

    # Test 2: Compression with Pillow
    print("\n[TEST 2] Testing Pillow Compression...", flush=True)
    compressor = ImageCompressor(target_width=1280, quality=70)
    upload_bytes = compressor.compress_for_upload(img)
    stream_bytes = compressor.compress_for_stream(img, quality=55)
    print(f"  -> Upload Screenshot Compressed Size: {len(upload_bytes) // 1024} KB", flush=True)
    print(f"  -> Live Stream Frame Compressed Size : {len(stream_bytes) // 1024} KB", flush=True)
    assert len(upload_bytes) > 0, "Compressed upload image is empty!"
    assert len(stream_bytes) > 0, "Compressed stream frame is empty!"

    # Test 3: Database Initialization
    print("\n[TEST 3] Testing Database Initialization...", flush=True)
    init_db()
    print("  -> SQLite database & tables verified.", flush=True)

    # Test 4: FastApi TestClient Verification
    print("\n[TEST 4] Testing Server Endpoints via TestClient...", flush=True)
    client = TestClient(app)
    
    # 4a. Health check
    res = client.get("/health")
    print(f"  -> GET /health: {res.status_code} {res.json()}", flush=True)
    assert res.status_code == 200

    # 4b. Dashboard HTML root
    res = client.get("/")
    print(f"  -> GET / (Dashboard HTML): {res.status_code} (HTML length: {len(res.text)})", flush=True)
    assert res.status_code == 200
    assert "Employee Monitoring System" in res.text
    assert "Live Screen Wall" in res.text

    # 4c. Device Enrollment
    metadata = get_system_metadata()
    enroll_res = client.post(
        "/api/v1/auth/enroll",
        headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
        json=metadata
    )
    print(f"  -> POST /api/v1/auth/enroll: {enroll_res.status_code}", flush=True)
    assert enroll_res.status_code == 200, f"Enrollment failed: {enroll_res.text}"
    enroll_data = enroll_res.json()
    device_id = enroll_data["device_id"]
    device_secret = enroll_data["device_secret"]
    print(f"  -> Enrolled Device ID: {device_id}", flush=True)

    # 4d. Screenshot Upload
    files = {"file": ("screen.jpg", upload_bytes, "image/jpeg")}
    data = {"captured_at": datetime.utcnow().isoformat(), "resolution": res_str}
    upload_res = client.post(
        "/api/v1/agent/screenshot",
        headers={
            "X-Device-Id": device_id,
            "Authorization": f"Bearer {device_secret}"
        },
        files=files,
        data=data
    )
    print(f"  -> POST /api/v1/agent/screenshot: {upload_res.status_code} {upload_res.json()}", flush=True)
    assert upload_res.status_code == 200

    # 4e. Admin Devices List (Live Screen Wall API)
    devices_res = client.get("/api/v1/admin/devices")
    print(f"  -> GET /api/v1/admin/devices: {devices_res.status_code} (Total: {devices_res.json()['total']})", flush=True)
    assert devices_res.status_code == 200
    d_data = devices_res.json()
    assert d_data["total"] >= 1
    assert d_data["online_count"] >= 1
    dev_entry = d_data["devices"][0]
    assert dev_entry["device_id"] == device_id
    assert dev_entry["latest_screenshot_url"] is not None
    print(f"  -> Verified device entry on Live Screen Wall: {dev_entry['employee_name']} ({dev_entry['computer_name']})", flush=True)

    # 4f. Latest Screenshot retrieval
    img_res = client.get(f"/api/v1/admin/devices/{device_id}/screenshot/latest")
    print(f"  -> GET /api/v1/admin/devices/{device_id}/screenshot/latest: {img_res.status_code} Content-Type: {img_res.headers.get('content-type')} (Bytes: {len(img_res.content)})", flush=True)
    assert img_res.status_code == 200
    assert img_res.headers.get("content-type") == "image/jpeg"
    assert len(img_res.content) == len(upload_bytes)

    cap.close()
    print("\n==================================================", flush=True)
    print("  ALL 4 CORE COMPONENT TESTS PASSED SUCCESSFULLY! ", flush=True)
    print("==================================================", flush=True)

if __name__ == "__main__":
    run_tests()
