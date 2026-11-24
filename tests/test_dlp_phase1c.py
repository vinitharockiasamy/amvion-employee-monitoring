import os
import sys
import time
import hashlib
import asyncio
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
from agent.fs_collector import (
    FilesystemEventCollector,
    compute_file_sha256,
    generate_idempotency_key,
    is_ignored_path,
)
from server.app.core.config import settings
from server.app.db.models import Base
from server.app.db.session import get_db
from server.app.main import app


class MockDlpClient:
    """Lightweight mock of AgentNetworkClient's DLP methods for isolated unit testing."""

    def __init__(self, fail_uploads: bool = False, fail_events: bool = False):
        self.fail_uploads = fail_uploads
        self.fail_events = fail_events
        self.uploaded_files = []
        self.posted_events = []
        self._counter = 0

    async def upload_dlp_evidence(
        self,
        file_path: str,
        expected_sha256: str,
        original_filename: str,
        timeout: float = 20.0,
    ) -> dict:
        if self.fail_uploads:
            return {"status": "retry", "code": 503, "detail": "Service unavailable"}
        self._counter += 1
        blob_id = f"blob-mock-{self._counter:04d}"
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
        if self.fail_events:
            return {"status": "retry", "code": 503, "detail": "Server unreachable"}
        self.posted_events.append(event_payload)
        return {"status": "ok", "event": event_payload}


def _make_test_config(monitored_dirs, **dlp_overrides) -> AgentConfig:
    cfg = AgentConfig()
    cfg.device_id = "dev-unit-test-001"
    cfg.device_secret = "sec_unit_test"
    cfg.dlp = {
        "enabled": True,
        "monitored_directories": [str(d) for d in monitored_dirs],
        "monitor_removable_storage": True,
        "evidence_enabled": True,
        "maximum_evidence_size_mb": 25,
        "evidence_events": ["CREATED", "MODIFIED", "MOVED", "RENAMED", "DELETED"],
        "poll_interval_sec": 0.5,
        "max_queue_size": 10,
    }
    cfg.dlp.update(dlp_overrides)
    return cfg


class TestDlpPhase1CUnit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs_dir = self.root / "Documents"
        self.downloads_dir = self.root / "Downloads"
        self.unmonitored_dir = self.root / "UnmonitoredPrivate"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.unmonitored_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_01_created_event(self):
        """1. Detecting a newly created file inside a monitored directory."""
        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        target = self.docs_dir / "quarterly_budget.xlsx"
        payload_bytes = b"Q4 Revenue Sheet Data"
        target.write_bytes(payload_bytes)

        count = await collector.run_once()
        self.assertEqual(count, 1)
        self.assertEqual(len(client.posted_events), 1)

        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "CREATED")
        self.assertEqual(ev["file_name"], "quarterly_budget.xlsx")
        self.assertEqual(ev["file_extension"], ".xlsx")
        self.assertIsNone(ev["source_path"])
        self.assertEqual(Path(ev["destination_path"]).resolve(), target.resolve())
        self.assertEqual(ev["metadata"]["sha256"], hashlib.sha256(payload_bytes).hexdigest())
        self.assertIsNotNone(ev["file_blob_id"])

    async def test_02_modified_event(self):
        """2. Detecting modification of an existing file."""
        existing = self.docs_dir / "notes.txt"
        existing.write_bytes(b"Initial draft")

        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        updated_bytes = b"Initial draft - updated with final figures and signoff"
        existing.write_bytes(updated_bytes)
        os.utime(existing, ns=(time.time_ns() + 5_000_000_000, time.time_ns() + 5_000_000_000))

        count = await collector.run_once()
        self.assertEqual(count, 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "MODIFIED")
        self.assertEqual(Path(ev["source_path"]).resolve(), existing.resolve())
        self.assertIsNone(ev["destination_path"])
        self.assertEqual(ev["metadata"]["sha256"], hashlib.sha256(updated_bytes).hexdigest())

    async def test_03_renamed_event(self):
        """3. Detecting a file rename within the same directory (old_path -> new_path)."""
        src = self.docs_dir / "draft_spec.docx"
        src.write_bytes(b"Architecture specification v1")

        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        dst = self.docs_dir / "final_spec.docx"
        src.rename(dst)

        count = await collector.run_once()
        self.assertEqual(count, 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "RENAMED")
        self.assertEqual(ev["file_name"], "final_spec.docx")
        self.assertEqual(Path(ev["source_path"]).resolve(), src.resolve())
        self.assertEqual(Path(ev["destination_path"]).resolve(), dst.resolve())

    async def test_04_moved_event(self):
        """4. Detecting a file move across monitored directories (Documents -> Downloads)."""
        src = self.docs_dir / "client_export.csv"
        src.write_bytes(b"id,name\n1,EnterpriseCustomer\n")

        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir, self.downloads_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        dst = self.downloads_dir / "client_export.csv"
        src.rename(dst)

        count = await collector.run_once()
        self.assertEqual(count, 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "MOVED")
        self.assertEqual(Path(ev["source_path"]).resolve(), src.resolve())
        self.assertEqual(Path(ev["destination_path"]).resolve(), dst.resolve())

    async def test_05_deleted_event(self):
        """5. Detecting deletion of a monitored file without crashing."""
        victim = self.docs_dir / "obsolete_plan.pdf"
        victim.write_bytes(b"%PDF-1.4 sample content")

        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        victim.unlink()

        count = await collector.run_once()
        self.assertEqual(count, 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "DELETED")
        self.assertEqual(ev["file_name"], "obsolete_plan.pdf")
        self.assertEqual(Path(ev["source_path"]).resolve(), victim.resolve())
        self.assertIsNone(ev["destination_path"])

    async def test_06_sha256_calculation(self):
        """6. Accurate streaming SHA-256 calculation across multi-chunk content."""
        sample = (b"EnterpriseDLPBlock-" * 120_000)  # ~2.28 MB (> 1 MB chunk size)
        expected = hashlib.sha256(sample).hexdigest()
        fpath = self.docs_dir / "large_design.dxf"
        fpath.write_bytes(sample)

        sha, size_read, err = compute_file_sha256(str(fpath), chunk_size=256 * 1024)
        self.assertIsNone(err)
        self.assertEqual(size_read, len(sample))
        self.assertEqual(sha, expected)

    async def test_07_evidence_upload_for_eligible_files(self):
        """7. Eligible files within size limit upload evidence first and link file_blob_id."""
        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir], maximum_evidence_size_mb=5)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        f = self.docs_dir / "ring_cad.3dm"
        data = b"3DM_CAD_MODEL_PAYLOAD_991"
        f.write_bytes(data)

        await collector.run_once()
        self.assertEqual(len(client.uploaded_files), 1)
        self.assertEqual(client.uploaded_files[0]["original_filename"], "ring_cad.3dm")
        self.assertEqual(client.posted_events[0]["file_blob_id"], client.uploaded_files[0]["blob_id"])
        self.assertEqual(client.posted_events[0]["metadata"]["evidence_status"], "archived")

    async def test_08_evidence_skipped_when_exceeding_max_size(self):
        """8. Files exceeding maximum_evidence_size_mb skip upload but still emit DLP event."""
        client = MockDlpClient()
        # Set 1 MB limit for fast test execution
        cfg = _make_test_config([self.docs_dir], maximum_evidence_size_mb=1)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        oversized = self.docs_dir / "raw_render_bundle.zip"
        oversized.write_bytes(b"Z" * (1024 * 1024 + 4096))  # > 1 MB

        await collector.run_once()
        self.assertEqual(len(client.uploaded_files), 0)
        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertIsNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "exceeds_size_limit")
        self.assertIn("sha256", ev["metadata"])

    async def test_09_locked_or_inaccessible_file_does_not_crash(self):
        """9. PermissionError / locked file during hashing still submits event without crashing."""
        client = MockDlpClient()
        cfg = _make_test_config([self.docs_dir])
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        locked_file = self.docs_dir / "locked_ledger.xlsx"
        locked_file.write_bytes(b"Locked content")

        with patch("agent.fs_collector.open", side_effect=PermissionError("File locked by another process")):
            count = await collector.run_once()

        self.assertEqual(count, 1)
        self.assertEqual(len(client.uploaded_files), 0)
        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertIsNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "locked_or_unavailable")

    async def test_10_api_failure_does_not_crash_and_recovers_with_bounded_queue(self):
        """10. Transient API failure queues events up to max_queue_size without crashing and flushes on recovery."""
        client = MockDlpClient(fail_uploads=True, fail_events=True)
        cfg = _make_test_config([self.docs_dir], max_queue_size=3)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        # Create 5 files while server is unreachable -> bounded queue caps at 3
        for idx in range(5):
            (self.docs_dir / f"offline_doc_{idx}.txt").write_bytes(f"doc-{idx}".encode())

        count = await collector.run_once()
        self.assertEqual(count, 5)
        self.assertEqual(collector.pending_queue_size, 3)
        self.assertEqual(len(client.posted_events), 0)

        # Restore server availability -> queued items flush cleanly with their evidence
        client.fail_uploads = False
        client.fail_events = False
        await collector.run_once()

        self.assertEqual(collector.pending_queue_size, 0)
        self.assertEqual(len(client.posted_events), 3)
        self.assertTrue(all(ev["file_blob_id"] is not None for ev in client.posted_events))

    async def test_11_deterministic_idempotency_key_generation(self):
        """11. Idempotency key is deterministic for identical attributes and distinct across changes."""
        k1 = generate_idempotency_key(
            endpoint_id="dev-1",
            event_type="CREATED",
            source_path=None,
            destination_path=r"C:\Users\Alice\Documents\spec.pdf",
            file_size=4096,
            mtime_ns=1700000000000,
            sha256="a" * 64,
        )
        k2 = generate_idempotency_key(
            endpoint_id="dev-1",
            event_type="CREATED",
            source_path=None,
            destination_path=r"c:\users\alice\documents\spec.pdf",
            file_size=4096,
            mtime_ns=1700000000000,
            sha256="a" * 64,
        )
        k3 = generate_idempotency_key(
            endpoint_id="dev-1",
            event_type="MODIFIED",
            source_path=r"C:\Users\Alice\Documents\spec.pdf",
            destination_path=None,
            file_size=8192,
            mtime_ns=1700000099999,
            sha256="b" * 64,
        )
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertTrue(k1.startswith("dlp-"))

    async def test_12_configured_directory_filtering_and_noise_exclusion(self):
        """12. Unmonitored directories and OS/temp files are ignored while logical + absolute dirs work."""
        # Test logical user folder resolution + explicit absolute directory
        fake_home = self.root / "SimulatedUserHome"
        (fake_home / "Desktop").mkdir(parents=True, exist_ok=True)

        client = MockDlpClient()
        cfg = _make_test_config(["Desktop", str(self.docs_dir)])

        with patch("pathlib.Path.home", return_value=fake_home):
            collector = FilesystemEventCollector(client=client, agent_config=cfg)
            collector.initialize_baseline()

            # Write into unmonitored directory -> must NOT be reported
            (self.unmonitored_dir / "private.txt").write_bytes(b"out of scope")

            # Write obvious temporary/system noise -> must NOT be reported
            (self.docs_dir / "~$temp_lock.docx").write_bytes(b"office lock")
            (self.docs_dir / "download.crdownload").write_bytes(b"partial")
            (self.docs_dir / "desktop.ini").write_bytes(b"[.ShellClassInfo]")
            self.assertTrue(is_ignored_path(Path("~$temp_lock.docx")))

            # Write real user files in logical Desktop and explicit Documents -> MUST be reported
            (fake_home / "Desktop" / "presentation.pptx").write_bytes(b"Slide deck bytes")
            (self.docs_dir / "contract.pdf").write_bytes(b"%PDF-1.7 contract")

            count = await collector.run_once()

        self.assertEqual(count, 2)
        names = sorted(ev["file_name"] for ev in client.posted_events)
        self.assertEqual(names, ["contract.pdf", "presentation.pptx"])


class TestDlpPhase1CEndToEndIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Full end-to-end acceptance test connecting FilesystemEventCollector + AgentNetworkClient
    directly to the FastAPI DLP Vault, Event API, and Dashboard Preview endpoints.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(cls.temp_dir.name)
        cls.vault_dir = temp_root / "e2e_vault"
        cls.vault_dir.mkdir(parents=True, exist_ok=True)
        settings.DLP_VAULT_DIR = cls.vault_dir

        db_file = temp_root / "test_dlp_1c_e2e.db"
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
        cls.sync_client = TestClient(app)
        cls.admin_headers = {"Authorization": f"Bearer {settings.ADMIN_AUTH_TOKEN}"}

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    async def test_13_end_to_end_agent_collector_to_server_and_dashboard(self):
        workspace_root = Path(self.temp_dir.name) / "endpoint_workspace"
        monitored_docs = workspace_root / "Documents"
        monitored_downloads = workspace_root / "Downloads"
        monitored_docs.mkdir(parents=True, exist_ok=True)
        monitored_downloads.mkdir(parents=True, exist_ok=True)

        # Enroll endpoint via real /api/v1/auth/enroll
        enroll_res = self.sync_client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-phase1c-e2e-001",
                "computer_name": "CORP-LAPTOP-09",
                "employee_name": "Vinith Enterprise",
                "os_version": "Windows 11 Pro",
            },
        )
        self.assertEqual(enroll_res.status_code, 200, enroll_res.text)
        enrolled = enroll_res.json()

        saved_url = global_agent_config.server_url
        saved_id = global_agent_config.device_id
        saved_secret = global_agent_config.device_secret
        saved_dlp = dict(global_agent_config.dlp)

        real_async_client_cls = httpx.AsyncClient

        def asgi_client_factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return real_async_client_cls(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )

        try:
            global_agent_config.server_url = "http://testserver"
            global_agent_config.device_id = enrolled["device_id"]
            global_agent_config.device_secret = enrolled["device_secret"]
            global_agent_config.dlp = {
                "enabled": True,
                "monitored_directories": [str(monitored_docs), str(monitored_downloads)],
                "monitor_removable_storage": True,
                "evidence_enabled": True,
                "maximum_evidence_size_mb": 25,
                "evidence_events": ["CREATED", "MODIFIED", "MOVED", "RENAMED", "DELETED"],
                "poll_interval_sec": 1.0,
                "max_queue_size": 100,
            }

            net_client = AgentNetworkClient()
            collector = FilesystemEventCollector(client=net_client, agent_config=global_agent_config)
            collector.initialize_baseline()

            with patch("agent.client.httpx.AsyncClient", side_effect=asgi_client_factory):
                # Step A: Create a test file in monitored Documents
                doc_path = monitored_docs / "board_strategy_2026.txt"
                doc_bytes = b"Confidential 2026 Enterprise Roadmap & Pricing Table"
                expected_sha = hashlib.sha256(doc_bytes).hexdigest()
                doc_path.write_bytes(doc_bytes)

                await collector.run_once()

                # Step B: Rename file in Documents
                renamed_path = monitored_docs / "board_strategy_final.txt"
                doc_path.rename(renamed_path)
                await collector.run_once()

                # Step C: Move file to Downloads
                moved_path = monitored_downloads / "board_strategy_final.txt"
                renamed_path.rename(moved_path)
                await collector.run_once()

                # Step D: Delete file from Downloads
                moved_path.unlink()
                await collector.run_once()

            # Verify events arrived in /api/dlp/events for the dashboard
            events_resp = self.sync_client.get(
                "/api/dlp/events",
                headers=self.admin_headers,
                params={"endpoint": enrolled["device_id"]},
            )
            self.assertEqual(events_resp.status_code, 200)
            body = events_resp.json()
            self.assertEqual(body["total"], 4)

            by_type = {e["event_type"]: e for e in body["events"]}
            self.assertEqual(set(by_type.keys()), {"CREATED", "RENAMED", "MOVED", "DELETED"})

            created_ev = by_type["CREATED"]
            self.assertTrue(created_ev["has_content"])
            self.assertIsNotNone(created_ev["file_blob_id"])
            self.assertEqual(created_ev["metadata"]["sha256"], expected_sha)

            # Verify dashboard preview endpoint serves exact archived text content
            blob_id = created_ev["file_blob_id"]
            preview_resp = self.sync_client.get(
                f"/api/dlp/files/{blob_id}/preview",
                headers=self.admin_headers,
            )
            self.assertEqual(preview_resp.status_code, 200)
            preview_data = preview_resp.json()
            self.assertEqual(preview_data["kind"], "text")
            self.assertIn("Confidential 2026 Enterprise Roadmap", preview_data["content"])

        finally:
            global_agent_config.server_url = saved_url
            global_agent_config.device_id = saved_id
            global_agent_config.device_secret = saved_secret
            global_agent_config.dlp = saved_dlp


if __name__ == "__main__":
    unittest.main(verbosity=2)
