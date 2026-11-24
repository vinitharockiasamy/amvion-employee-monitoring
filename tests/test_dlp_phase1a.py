import io
import os
import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta

os.environ["TESTING"] = "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from server.app.core.config import settings
from server.app.db.models import Base, DlpFileBlob, DlpEvent
from server.app.db.session import get_db
from server.app.main import app


class TestDlpPhase1A(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(cls.temp_dir.name)
        cls.vault_dir = temp_root / "dlp_vault"
        cls.vault_dir.mkdir(parents=True, exist_ok=True)
        settings.DLP_VAULT_DIR = cls.vault_dir

        db_file = temp_root / "test_dlp.db"
        cls.engine = create_engine(
            f"sqlite:///{db_file.as_posix()}",
            connect_args={"check_same_thread": False}
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
        cls.client = TestClient(app)

        cls.admin_headers = {"Authorization": f"Bearer {settings.ADMIN_AUTH_TOKEN}"}

        # Enroll two managed endpoints using existing enrollment API
        r1 = cls.client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-dlp-test-001",
                "computer_name": "DESIGN-WS-01",
                "employee_name": "Alice Designer",
                "os_version": "Windows 11"
            }
        )
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        cls.dev1_id = data1["device_id"]
        cls.dev1_headers = {
            "X-Device-Id": cls.dev1_id,
            "Authorization": f"Bearer {data1['device_secret']}"
        }

        r2 = cls.client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-dlp-test-002",
                "computer_name": "TICKET-WS-02",
                "employee_name": "Bob Ticketing",
                "os_version": "Windows 11"
            }
        )
        assert r2.status_code == 200, r2.text
        data2 = r2.json()
        cls.dev2_id = data2["device_id"]
        cls.dev2_headers = {
            "X-Device-Id": cls.dev2_id,
            "Authorization": f"Bearer {data2['device_secret']}"
        }

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    def test_01_unauthorized_access_rejected(self):
        """Unauthenticated requests to /api/dlp/* and /ws/dlp must be rejected."""
        res_get = self.client.get("/api/dlp/events")
        self.assertEqual(res_get.status_code, 401)

        res_post_ev = self.client.post("/api/dlp/events", json={
            "event_type": "CREATED",
            "file_name": "secret.pdf"
        })
        self.assertEqual(res_post_ev.status_code, 401)

        res_post_file = self.client.post(
            "/api/dlp/files",
            files={"file": ("doc.txt", b"hello", "text/plain")}
        )
        self.assertEqual(res_post_file.status_code, 401)

        # Device 1 attempting to spoof Device 2's endpoint_id must receive 403 Forbidden
        res_spoof = self.client.post(
            "/api/dlp/events",
            headers=self.dev1_headers,
            json={
                "endpoint_id": self.dev2_id,
                "event_type": "COPIED",
                "file_name": "spoof.xlsx"
            }
        )
        self.assertEqual(res_spoof.status_code, 403)

        # Unauthenticated WebSocket to /ws/dlp must close with policy violation (1008)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/dlp?token=invalid-token") as ws:
                ws.receive_json()

    def test_02_file_upload_metadata_and_sha256_deduplication(self):
        """Identical files uploaded multiple times must be stored once by SHA-256."""
        content = b"Proprietary Jewelry CAD Specification v1.0 - Ring Design #402"
        expected_sha = hashlib.sha256(content).hexdigest()

        # First upload
        r1 = self.client.post(
            "/api/dlp/files",
            headers=self.dev1_headers,
            data={"expected_sha256": expected_sha},
            files={"file": ("ring_design_402.dxf", io.BytesIO(content), "text/plain")}
        )
        self.assertEqual(r1.status_code, 201, r1.text)
        body1 = r1.json()
        self.assertEqual(body1["sha256"], expected_sha)
        self.assertFalse(body1["deduplicated"])
        file_id_1 = body1["file_id"]

        # Second upload with identical content from another endpoint -> deduplicated reuse
        r2 = self.client.post(
            "/api/dlp/files",
            headers=self.dev2_headers,
            files={"file": ("copy_of_ring_402.dxf", io.BytesIO(content), "text/plain")}
        )
        self.assertEqual(r2.status_code, 201, r2.text)
        body2 = r2.json()
        self.assertEqual(body2["file_id"], file_id_1)
        self.assertEqual(body2["sha256"], expected_sha)
        self.assertTrue(body2["deduplicated"])

        # Verify only 1 DlpFileBlob row exists for this SHA-256
        with self.TestingSessionLocal() as db:
            count = db.query(DlpFileBlob).filter(DlpFileBlob.sha256 == expected_sha).count()
            self.assertEqual(count, 1)

        # Metadata endpoint check
        r_meta = self.client.get(f"/api/dlp/files/{file_id_1}", headers=self.admin_headers)
        self.assertEqual(r_meta.status_code, 200)
        meta = r_meta.json()
        self.assertEqual(meta["id"], file_id_1)
        self.assertEqual(meta["sha256"], expected_sha)
        self.assertEqual(meta["file_size"], len(content))

        # Integrity mismatch check (wrong expected_sha256 -> 422)
        r_bad_hash = self.client.post(
            "/api/dlp/files",
            headers=self.dev1_headers,
            data={"expected_sha256": "0" * 64},
            files={"file": ("tampered.txt", io.BytesIO(b"different bytes"), "text/plain")}
        )
        self.assertEqual(r_bad_hash.status_code, 422)

    def test_03_file_25mb_limit_enforced(self):
        """Files larger than 25 MB must be rejected with HTTP 413."""
        oversized_payload = b"A" * (25 * 1024 * 1024 + 1024)
        r = self.client.post(
            "/api/dlp/files",
            headers=self.dev1_headers,
            files={"file": ("huge_archive.zip", io.BytesIO(oversized_payload), "application/zip")}
        )
        self.assertEqual(r.status_code, 413)

    def test_04_authenticated_file_download_and_path_traversal_safety(self):
        """Archived files can be downloaded by authenticated admins with SHA-256 verification."""
        raw_bytes = b"Ticketing VIP Client Export,Seat,Amount\nJohn Doe,A1,4500\n"
        sha = hashlib.sha256(raw_bytes).hexdigest()

        r_up = self.client.post(
            "/api/dlp/files",
            headers=self.dev2_headers,
            files={"file": ("../../etc/vip_clients.csv", io.BytesIO(raw_bytes), "text/csv")}
        )
        self.assertEqual(r_up.status_code, 201)
        up_data = r_up.json()
        file_id = up_data["file_id"]
        # Filename traversal stripped cleanly
        self.assertEqual(up_data["original_filename"], "vip_clients.csv")

        # Unauthenticated download rejected
        r_unauth = self.client.get(f"/api/dlp/files/{file_id}/download")
        self.assertEqual(r_unauth.status_code, 401)

        # Authenticated admin download succeeds and matches exact bytes & SHA-256 header
        r_dl = self.client.get(f"/api/dlp/files/{file_id}/download", headers=self.admin_headers)
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, raw_bytes)
        self.assertEqual(r_dl.headers.get("X-Content-SHA256"), sha)

    def test_05_event_creation_filtering_and_websocket_notification(self):
        """Creating DLP events broadcasts over authenticated WebSocket and supports multi-criteria filtering."""
        sample_bytes = b"Confidential Necklace Render PNG stub"
        r_file = self.client.post(
            "/api/dlp/files",
            headers=self.dev1_headers,
            files={"file": ("necklace_render.png", io.BytesIO(sample_bytes), "image/png")}
        )
        blob_id = r_file.json()["file_id"]

        t_base = datetime(2026, 11, 24, 10, 0, 0)

        with self.client.websocket_connect(f"/ws/dlp?token={settings.ADMIN_AUTH_TOKEN}") as ws:
            # Event 1: High-risk USB transfer on Dev 1
            ev1_res = self.client.post(
                "/api/dlp/events",
                headers=self.dev1_headers,
                json={
                    "event_type": "USB_TRANSFER",
                    "file_name": "necklace_render.png",
                    "file_extension": ".png",
                    "source_path": r"C:\Designs\necklace_render.png",
                    "destination_path": r"E:\Backup\necklace_render.png",
                    "channel": "External Drive",
                    "application_name": "explorer.exe",
                    "risk_level": "high",
                    "file_blob_id": blob_id,
                    "timestamp": t_base.isoformat(),
                    "metadata": {"drive_label": "SANDISK_USB", "file_size": len(sample_bytes)}
                }
            )
            self.assertEqual(ev1_res.status_code, 201, ev1_res.text)
            ws_msg = ws.receive_json()
            self.assertEqual(ws_msg["type"], "DLP_EVENT_CREATED")
            self.assertEqual(ws_msg["event"]["file_name"], "necklace_render.png")
            self.assertEqual(ws_msg["event"]["file_blob_id"], blob_id)
            self.assertEqual(ws_msg["event"]["metadata"]["drive_label"], "SANDISK_USB")

        # Event 2: Low-risk local modification on Dev 2 (2 hours later)
        ev2_res = self.client.post(
            "/api/dlp/events",
            headers=self.dev2_headers,
            json={
                "event_type": "MODIFIED",
                "file_name": "daily_tickets.xlsx",
                "file_extension": ".xlsx",
                "source_path": r"C:\Work\daily_tickets.xlsx",
                "channel": "Local Disk",
                "application_name": "EXCEL.EXE",
                "risk_level": "low",
                "timestamp": (t_base + timedelta(hours=2)).isoformat(),
                "metadata": {"sheet_count": 3}
            }
        )
        self.assertEqual(ev2_res.status_code, 201, ev2_res.text)

        # Filter by endpoint
        f_ep = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={"endpoint": self.dev1_id}
        ).json()
        self.assertEqual(f_ep["total"], 1)
        self.assertEqual(f_ep["events"][0]["endpoint_id"], self.dev1_id)

        # Filter by event_type
        f_type = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={"event_type": "USB_TRANSFER"}
        ).json()
        self.assertEqual(f_type["total"], 1)
        self.assertEqual(f_type["events"][0]["channel"], "External Drive")

        # Filter by channel
        f_chan = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={"channel": "Local Disk"}
        ).json()
        self.assertEqual(f_chan["total"], 1)
        self.assertEqual(f_chan["events"][0]["file_name"], "daily_tickets.xlsx")

        # Filter by risk_level
        f_risk = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={"risk_level": "high"}
        ).json()
        self.assertEqual(f_risk["total"], 1)

        # Filter by date range (only include t_base .. t_base + 30m)
        f_date = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={
                "start_date": (t_base - timedelta(minutes=5)).isoformat(),
                "end_date": (t_base + timedelta(minutes=30)).isoformat()
            }
        ).json()
        self.assertEqual(f_date["total"], 1)
        self.assertEqual(f_date["events"][0]["event_type"], "USB_TRANSFER")


if __name__ == "__main__":
    unittest.main(verbosity=2)
