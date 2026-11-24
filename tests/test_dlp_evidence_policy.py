import io
import os
import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TESTING"] = "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.config import AgentConfig, config as global_agent_config
from agent.client import AgentNetworkClient
from agent.fs_collector import FilesystemEventCollector
from server.app.core.config import settings
from server.app.db.models import Base, DlpFileBlob
from server.app.db.session import get_db
from server.app.main import app


class MockPolicyClient:
    def __init__(self):
        self.uploaded_files = []
        self.posted_events = []
        self._seq = 0

    async def upload_dlp_evidence(
        self,
        file_path: str,
        expected_sha256: str,
        original_filename: str,
        timeout: float = 20.0,
    ) -> dict:
        self._seq += 1
        blob_id = f"policy-blob-{self._seq:04d}"
        self.uploaded_files.append({
            "file_path": file_path,
            "sha256": expected_sha256,
            "original_filename": original_filename,
            "blob_id": blob_id,
        })
        return {
            "status": "ok",
            "file_id": blob_id,
            "sha256": expected_sha256,
            "deduplicated": False,
        }

    async def post_dlp_event(self, event_payload: dict, timeout: float = 12.0) -> dict:
        self.posted_events.append(event_payload)
        return {"status": "ok", "event": event_payload}


def _policy_cfg(monitored_dirs, **dlp_overrides) -> AgentConfig:
    cfg = AgentConfig()
    cfg.device_id = "dev-policy-test-001"
    cfg.device_secret = "sec_policy_test"
    cfg.dlp = {
        "enabled": True,
        "monitored_directories": [str(d) for d in monitored_dirs],
        "monitor_removable_storage": False,
        "evidence_enabled": True,
        "maximum_evidence_size_mb": 25,
        "evidence_events": ["CREATED", "MODIFIED", "MOVED", "RENAMED", "COPIED", "DELETED"],
        "poll_interval_sec": 0.5,
        "max_queue_size": 50,
    }
    cfg.dlp.update(dlp_overrides)
    return cfg


class TestDlpEvidencePolicy(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_tmp = tempfile.TemporaryDirectory()
        s_root = Path(cls.server_tmp.name)
        cls.vault_dir = s_root / "dlp_vault"
        cls.vault_dir.mkdir(parents=True, exist_ok=True)
        settings.DLP_VAULT_DIR = cls.vault_dir

        db_file = s_root / "test_evidence_policy.db"
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
        cls.client = TestClient(app)
        cls.admin_headers = {"Authorization": f"Bearer {settings.ADMIN_AUTH_TOKEN}"}

        r = cls.client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-evidence-policy-001",
                "computer_name": "POLICY-WS-01",
                "employee_name": "Policy Auditor",
                "os_version": "Windows 11",
            },
        )
        assert r.status_code == 200
        cls.enrolled = r.json()
        cls.dev_headers = {
            "X-Device-Id": cls.enrolled["device_id"],
            "Authorization": f"Bearer {cls.enrolled['device_secret']}",
        }

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.server_tmp.cleanup()

    def setUp(self):
        self.ws_tmp = tempfile.TemporaryDirectory()
        self.docs_dir = Path(self.ws_tmp.name) / "Documents"
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.ws_tmp.cleanup()

    async def test_01_normal_file_within_limit_captures_evidence(self):
        """1. Normal file <= configured limit uploads evidence and sets evidence_status = CAPTURED."""
        mock_client = MockPolicyClient()
        cfg = _policy_cfg([self.docs_dir], maximum_evidence_size_mb=25)
        self.assertEqual(cfg.dlp["maximum_evidence_size_mb"], 25)
        collector = FilesystemEventCollector(client=mock_client, agent_config=cfg)
        collector.initialize_baseline()

        f = self.docs_dir / "within_limit_report.pdf"
        content = b"%PDF-1.7 normal policy test document"
        f.write_bytes(content)

        await collector.run_once()
        self.assertEqual(len(mock_client.uploaded_files), 1)
        ev = mock_client.posted_events[0]
        self.assertIsNotNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "CAPTURED")
        self.assertEqual(ev["metadata"]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(Path(ev["metadata"]["file_path"]).resolve(), f.resolve())

    async def test_02_file_above_configured_limit_retains_event_and_metadata_without_upload(self):
        """2 & 5. File > configured limit skips upload, retains SHA-256 & full metadata, and remains searchable on server."""
        saved_url = global_agent_config.server_url
        saved_id = global_agent_config.device_id
        saved_secret = global_agent_config.device_secret
        saved_dlp = dict(global_agent_config.dlp)

        real_async_cls = httpx.AsyncClient

        def asgi_factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return real_async_cls(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

        try:
            global_agent_config.server_url = "http://testserver"
            global_agent_config.device_id = self.enrolled["device_id"]
            global_agent_config.device_secret = self.enrolled["device_secret"]
            # Configure 2 MB policy limit via existing DLP config
            global_agent_config.dlp = _policy_cfg([self.docs_dir], maximum_evidence_size_mb=2).dlp

            net_client = AgentNetworkClient()
            collector = FilesystemEventCollector(client=net_client, agent_config=global_agent_config)
            collector.initialize_baseline()

            large_file = self.docs_dir / "oversized_cad_assembly_2026.3dm"
            large_bytes = b"CAD3DM-" * 350_000  # ~2.45 MB (> 2 MB configured limit)
            expected_sha = hashlib.sha256(large_bytes).hexdigest()
            large_file.write_bytes(large_bytes)

            with patch("agent.client.httpx.AsyncClient", side_effect=asgi_factory):
                await collector.run_once()

            # Verify event is stored, searchable in API, has full metadata + SHA-256, and NOT_CAPTURED_SIZE_LIMIT
            res = self.client.get(
                "/api/dlp/events",
                headers=self.admin_headers,
                params={"search": "oversized_cad_assembly_2026"},
            )
            self.assertEqual(res.status_code, 200)
            events = res.json()["events"]
            self.assertEqual(len(events), 1)

            ev = events[0]
            self.assertFalse(ev["has_content"])
            self.assertIsNone(ev["file_blob_id"])
            self.assertEqual(ev["evidence_status"], "NOT_CAPTURED_SIZE_LIMIT")
            self.assertEqual(ev["metadata"]["evidence_status"], "NOT_CAPTURED_SIZE_LIMIT")
            self.assertEqual(ev["metadata"]["maximum_evidence_size_mb"], 2)
            self.assertEqual(ev["file_size"], len(large_bytes))
            self.assertEqual(ev["file_extension"], ".3dm")
            self.assertEqual(ev["metadata"]["sha256"], expected_sha)
        finally:
            global_agent_config.server_url = saved_url
            global_agent_config.device_id = saved_id
            global_agent_config.device_secret = saved_secret
            global_agent_config.dlp = saved_dlp

    async def test_03_evidence_disabled_retains_event_and_sha256(self):
        """3 & 5. When evidence_enabled is False, event and SHA-256 metadata are retained with NOT_CAPTURED_DISABLED."""
        mock_client = MockPolicyClient()
        cfg = _policy_cfg([self.docs_dir], evidence_enabled=False)
        collector = FilesystemEventCollector(client=mock_client, agent_config=cfg)
        collector.initialize_baseline()

        f = self.docs_dir / "policy_disabled_note.docx"
        payload = b"DOCX_CONTENT_WHEN_EVIDENCE_DISABLED"
        f.write_bytes(payload)

        await collector.run_once()
        self.assertEqual(len(mock_client.uploaded_files), 0)
        self.assertEqual(len(mock_client.posted_events), 1)
        ev = mock_client.posted_events[0]
        self.assertIsNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "NOT_CAPTURED_DISABLED")
        self.assertEqual(ev["metadata"]["sha256"], hashlib.sha256(payload).hexdigest())

    async def test_04_baseline_does_not_upload_existing_files_and_missing_file_retains_event(self):
        """4 & 6. Baseline indexes pre-existing file metadata without uploading; deleting pre-existing file retains metadata."""
        pre_existing = self.docs_dir / "legacy_archive_before_agent.xlsx"
        pre_bytes = b"Pre-existing spreadsheet bytes before agent startup"
        pre_existing.write_bytes(pre_bytes)

        mock_client = MockPolicyClient()
        cfg = _policy_cfg([self.docs_dir])
        collector = FilesystemEventCollector(client=mock_client, agent_config=cfg)
        indexed_count = collector.initialize_baseline()

        # Baseline must index existing file metadata and perform ZERO evidence uploads or events
        self.assertEqual(indexed_count, 1)
        self.assertEqual(len(mock_client.uploaded_files), 0)
        self.assertEqual(len(mock_client.posted_events), 0)

        # Deleting that pre-existing file records full baseline metadata with NOT_CAPTURED_UNAVAILABLE
        pre_existing.unlink()
        await collector.run_once()

        self.assertEqual(len(mock_client.uploaded_files), 0)
        self.assertEqual(len(mock_client.posted_events), 1)
        ev = mock_client.posted_events[0]
        self.assertEqual(ev["event_type"], "DELETED")
        self.assertEqual(ev["file_name"], "legacy_archive_before_agent.xlsx")
        self.assertEqual(ev["file_extension"], ".xlsx")
        self.assertEqual(ev["metadata"]["file_size"], len(pre_bytes))
        self.assertEqual(Path(ev["source_path"]).resolve(), pre_existing.resolve())
        self.assertEqual(ev["metadata"]["evidence_status"], "NOT_CAPTURED_UNAVAILABLE")

    async def test_05_sha256_content_identity_distinguishes_same_filename_different_content(self):
        """7. Two files with identical filename but different content create distinct blobs; identical content reuses blob."""
        content_v1 = b"Contract Version 1 - Draft Terms"
        content_v2 = b"Contract Version 2 - Completely Different Final Terms"

        # Same filename "contract.txt", different bytes -> MUST NOT collide
        r1 = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("contract.txt", io.BytesIO(content_v1), "text/plain")},
        )
        r2 = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("contract.txt", io.BytesIO(content_v2), "text/plain")},
        )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        b1 = r1.json()
        b2 = r2.json()
        self.assertNotEqual(b1["file_id"], b2["file_id"])
        self.assertNotEqual(b1["sha256"], b2["sha256"])
        self.assertFalse(b1["deduplicated"])
        self.assertFalse(b2["deduplicated"])

        # Different filename "backup_copy.txt", identical bytes to content_v1 -> MUST deduplicate by SHA-256
        r3 = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("backup_copy.txt", io.BytesIO(content_v1), "text/plain")},
        )
        self.assertEqual(r3.status_code, 201)
        b3 = r3.json()
        self.assertEqual(b3["file_id"], b1["file_id"])
        self.assertTrue(b3["deduplicated"])

        with self.TestingSessionLocal() as db:
            v1_rows = db.query(DlpFileBlob).filter(DlpFileBlob.sha256 == b1["sha256"]).count()
            v2_rows = db.query(DlpFileBlob).filter(DlpFileBlob.sha256 == b2["sha256"]).count()
            self.assertEqual(v1_rows, 1)
            self.assertEqual(v2_rows, 1)

    async def test_06_dashboard_js_contains_evidence_not_captured_presentation(self):
        """Dashboard script exposes 'View File' and 'Evidence Not Captured' with clear size-limit reason."""
        js_res = self.client.get("/static/js/dashboard.js")
        self.assertEqual(js_res.status_code, 200)
        self.assertIn("Evidence Not Captured", js_res.text)
        self.assertIn("MB evidence limit", js_res.text)
        self.assertIn("NOT_CAPTURED_SIZE_LIMIT", js_res.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
