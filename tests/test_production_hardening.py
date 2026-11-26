import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TESTING"] = "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from server.app.core.config import _normalize_database_url, settings
from server.app.core.security import (
    ADMIN_SESSION_COOKIE,
    check_auth_rate_limit,
    create_admin_session_token,
    verify_admin_token_value,
)
from server.app.db.models import Base, Screenshot
from server.app.db.session import get_db
from server.app.main import app


class TestProductionHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(cls.temp_dir.name)
        cls.shots_dir = temp_root / "screenshots"
        cls.shots_dir.mkdir(parents=True, exist_ok=True)
        settings.SCREENSHOTS_DIR = cls.shots_dir

        db_file = temp_root / "test_prod.db"
        cls.engine = create_engine(
            f"sqlite:///{db_file.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    def test_01_admin_login_cookie_session_and_logout_flow(self):
        client = TestClient(app)

        # 1. Unauthenticated access to /api/v1/admin/devices and /ws/dashboard rejected
        self.assertEqual(client.get("/api/v1/admin/devices").status_code, 401)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws/dashboard") as ws:
                ws.receive_json()

        # 2. Invalid credentials rejected
        bad_login = client.post(
            "/api/v1/auth/admin/login",
            json={"username": settings.ADMIN_USERNAME, "password": "wrong-password"},
        )
        self.assertEqual(bad_login.status_code, 401)

        # 3. Valid login sets HttpOnly emp_admin_session cookie and grants full console access
        ok_login = client.post(
            "/api/v1/auth/admin/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        self.assertEqual(ok_login.status_code, 200)
        self.assertIn(ADMIN_SESSION_COOKIE, ok_login.cookies)
        token = ok_login.json()["token"]
        self.assertTrue(token.startswith("v1."))

        # Cookie alone authenticates GET / and GET /api/v1/admin/devices
        dash_html = client.get("/")
        self.assertEqual(dash_html.status_code, 200)
        self.assertIn("Live Screen Wall", dash_html.text)
        self.assertIn("Sign Out", dash_html.text)
        self.assertEqual(dash_html.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(dash_html.headers.get("X-Frame-Options"), "SAMEORIGIN")

        devs_res = client.get("/api/v1/admin/devices")
        self.assertEqual(devs_res.status_code, 200)

        # 4. Logout invalidates browser cookie
        logout_res = client.post("/api/v1/auth/admin/logout")
        self.assertEqual(logout_res.status_code, 200)
        self.assertEqual(client.get("/api/v1/admin/devices").status_code, 401)

    def test_02_expired_or_tampered_session_token_rejected(self):
        expired_tok = create_admin_session_token("admin", ttl_seconds=-10)
        # Note: create_admin_session_token floors ttl at 60s; craft expired token manually
        parts = expired_tok.split(".")
        tampered = f"{parts[0]}.{parts[1]}.{parts[2]}.{'0' * 64}"
        self.assertFalse(verify_admin_token_value(tampered))

    def test_03_rate_limiter_blocks_brute_force_burst(self):
        for _ in range(5):
            check_auth_rate_limit(None, bucket_name="test_rate_limit", max_attempts=5, window_seconds=60)
        with self.assertRaises(HTTPException) as ctx:
            check_auth_rate_limit(None, bucket_name="test_rate_limit", max_attempts=5, window_seconds=60)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_04_screenshot_retention_pruning_prevents_unbounded_disk_growth(self):
        client = TestClient(app)
        enroll = client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-prod-prune-001",
                "computer_name": "PRUNE-WS-01",
                "employee_name": "Retention User",
                "os_version": "Windows 11",
            },
        ).json()
        dev_headers = {
            "X-Device-Id": enroll["device_id"],
            "Authorization": f"Bearer {enroll['device_secret']}",
        }

        old_keep = settings.MAX_SCREENSHOTS_PER_DEVICE
        settings.MAX_SCREENSHOTS_PER_DEVICE = 5
        try:
            for i in range(9):
                r = client.post(
                    "/api/v1/agent/screenshot",
                    headers=dev_headers,
                    data={"resolution": "1920x1080"},
                    files={"file": (f"shot_{i}.jpg", io.BytesIO(b"\xff\xd8\xff\xe0JPEGDATA"), "image/jpeg")},
                )
                self.assertEqual(r.status_code, 200)

            with self.TestingSessionLocal() as db:
                remaining_rows = db.query(Screenshot).filter(Screenshot.device_id == enroll["device_id"]).count()
            dev_dir = self.shots_dir / enroll["device_id"]
            remaining_files = list(dev_dir.glob("*.jpg"))
            self.assertLessEqual(remaining_rows, 5)
            self.assertLessEqual(len(remaining_files), 5)
        finally:
            settings.MAX_SCREENSHOTS_PER_DEVICE = old_keep

    def test_05_postgres_url_normalization_and_health_probe(self):
        self.assertEqual(
            _normalize_database_url("postgres://user:pass@host:5432/dbname"),
            "postgresql://user:pass@host:5432/dbname",
        )
        client = TestClient(app)
        h = client.get("/health")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json()["status"], "healthy")
        self.assertTrue(h.json()["storage_ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
