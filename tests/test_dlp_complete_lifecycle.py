import io
import os
import sys
import time
import shutil
import struct
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
from agent.fs_collector import (
    FilesystemEventCollector,
    RemovableVolumeInfo,
    USB_CHANNEL_NAME,
    LOCAL_CHANNEL_NAME,
    parse_recycle_bin_metadata_file,
)
from server.app.core.config import settings
from server.app.db.models import Base, DlpFileBlob
from server.app.db.session import get_db
from server.app.main import app


def _write_win10_recycle_bin_entry(rb_sid_dir: Path, token_id: str, orig_path: Path, payload_bytes: bytes):
    """Create a real Windows 10/11 Version-2 $Ixxxxxx + $Rxxxxxx pair inside a simulated Recycle Bin directory."""
    ext = orig_path.suffix
    i_path = rb_sid_dir / f"$I{token_id}{ext}"
    r_path = rb_sid_dir / f"$R{token_id}{ext}"

    orig_str = str(orig_path.resolve()) + "\x00"
    utf16_bytes = orig_str.encode("utf-16-le")
    char_len = len(orig_str)
    # FILETIME: 100ns intervals since 1601-01-01
    del_filetime = 134_000_000_000_000_000
    header = struct.pack("<QQQI", 2, len(payload_bytes), del_filetime, char_len) + utf16_bytes

    r_path.write_bytes(payload_bytes)
    i_path.write_bytes(header)
    return i_path, r_path


class MockLifecycleClient:
    def __init__(self):
        self.uploaded_files = []
        self.posted_events = []
        self._seq = 0
        self._by_sha = {}

    async def upload_dlp_evidence(
        self,
        file_path: str,
        expected_sha256: str,
        original_filename: str,
        timeout: float = 20.0,
    ) -> dict:
        if expected_sha256 in self._by_sha:
            blob_id = self._by_sha[expected_sha256]
            self.uploaded_files.append({
                "file_path": file_path,
                "sha256": expected_sha256,
                "original_filename": original_filename,
                "blob_id": blob_id,
                "deduplicated": True,
            })
            return {
                "status": "ok",
                "file_id": blob_id,
                "sha256": expected_sha256,
                "deduplicated": True,
            }

        self._seq += 1
        blob_id = f"lc-blob-{self._seq:04d}"
        self._by_sha[expected_sha256] = blob_id
        self.uploaded_files.append({
            "file_path": file_path,
            "sha256": expected_sha256,
            "original_filename": original_filename,
            "blob_id": blob_id,
            "deduplicated": False,
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


def _make_cfg(monitored_dirs, **dlp_overrides) -> AgentConfig:
    cfg = AgentConfig()
    cfg.device_id = "dev-lifecycle-001"
    cfg.device_secret = "sec_lifecycle_test"
    cfg.dlp = {
        "enabled": True,
        "monitored_directories": [str(d) for d in monitored_dirs],
        "monitor_removable_storage": True,
        "evidence_enabled": True,
        "maximum_evidence_size_mb": 25,
        "baseline_hash_max_mb": 100,
        "recycle_bin_recovery_enabled": True,
        "secondary_backup_directories": [],
        "evidence_events": [
            "CREATED",
            "MODIFIED",
            "MOVED",
            "RENAMED",
            "COPIED",
            "USB_TRANSFER",
            "DELETED",
        ],
        "poll_interval_sec": 0.5,
        "max_queue_size": 100,
    }
    cfg.dlp.update(dlp_overrides)
    return cfg


class TestDlpCompleteLifecycle(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv_tmp = tempfile.TemporaryDirectory()
        s_root = Path(cls.srv_tmp.name)
        cls.vault_dir = s_root / "dlp_vault"
        cls.vault_dir.mkdir(parents=True, exist_ok=True)
        settings.DLP_VAULT_DIR = cls.vault_dir

        db_file = s_root / "test_complete_lifecycle.db"
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
        cls.http_client = TestClient(app)
        cls.admin_headers = {"Authorization": f"Bearer {settings.ADMIN_AUTH_TOKEN}"}

        r = cls.http_client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-complete-dlp-001",
                "computer_name": "ENT-WORKSTATION-01",
                "employee_name": "Enterprise User",
                "os_version": "Windows 11",
            },
        )
        assert r.status_code == 200
        cls.enrolled = r.json()

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.srv_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "Documents"
        self.desktop = self.root / "Desktop"
        self.projects = self.root / "Projects"
        self.usb_vol = self.root / "UsbDrive"
        self.sim_rb = self.root / "SimRecycleBin" / "S-1-5-21-TEST-1001"
        for d in (self.docs, self.desktop, self.projects, self.usb_vol, self.sim_rb):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_01_baseline_indexes_metadata_and_sha256_without_uploading(self):
        """Part 12 #1: Existing baseline file is indexed (including SHA-256 & baseline=True) with 0 uploads."""
        existing = self.docs / "Existing.pdf"
        raw = b"%PDF-1.7 Pre-existing corporate specification"
        expected_sha = hashlib.sha256(raw).hexdigest()
        existing.write_bytes(raw)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs], monitor_removable_storage=False)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        count = collector.initialize_baseline()

        self.assertEqual(count, 1)
        self.assertEqual(len(client.uploaded_files), 0)
        self.assertEqual(len(client.posted_events), 0)

        snap_map = collector.get_baseline_snapshot()
        snap = next(iter(snap_map.values()))
        self.assertEqual(snap.name, "Existing.pdf")
        self.assertEqual(snap.size, len(raw))
        self.assertEqual(snap.sha256, expected_sha)
        self.assertTrue(snap.is_baseline)
        self.assertIsNotNone(snap.baseline_timestamp)

    async def test_02_baseline_file_modified_captures_evidence(self):
        """Part 12 #2 & #15: Modifying a pre-existing baseline file generates MODIFIED and uploads evidence."""
        f = self.docs / "Quarterly_Plan.docx"
        f.write_bytes(b"Initial pre-agent baseline version")

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs], monitor_removable_storage=False)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        new_bytes = b"Updated post-agent version with final numbers"
        f.write_bytes(new_bytes)
        os.utime(f, ns=(time.time_ns() + 3_000_000_000, time.time_ns() + 3_000_000_000))

        await collector.run_once()
        self.assertEqual(len(client.uploaded_files), 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "MODIFIED")
        self.assertEqual(ev["metadata"]["evidence_status"], "CAPTURED")
        self.assertTrue(ev["metadata"]["baseline"])
        self.assertEqual(ev["metadata"]["sha256"], hashlib.sha256(new_bytes).hexdigest())

    async def test_03_baseline_file_normal_delete_recovers_from_recycle_bin_end_to_end(self):
        """Part 12 #3: Deleting a pre-existing baseline file to Recycle Bin captures evidence with CAPTURED_FROM_RECYCLE_BIN."""
        saved_url = global_agent_config.server_url
        saved_id = global_agent_config.device_id
        saved_secret = global_agent_config.device_secret
        saved_dlp = dict(global_agent_config.dlp)

        real_async_cls = httpx.AsyncClient

        def asgi_factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return real_async_cls(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

        try:
            orig_doc = self.docs / "PreExisting_Customer_Quote.pdf"
            doc_bytes = b"%PDF-1.7 Confidential Pre-Existing Quote Content Recovered From Recycle Bin"
            expected_sha = hashlib.sha256(doc_bytes).hexdigest()
            orig_doc.write_bytes(doc_bytes)

            global_agent_config.server_url = "http://testserver"
            global_agent_config.device_id = self.enrolled["device_id"]
            global_agent_config.device_secret = self.enrolled["device_secret"]
            global_agent_config.dlp = _make_cfg([self.docs], monitor_removable_storage=False).dlp

            net_client = AgentNetworkClient()
            collector = FilesystemEventCollector(
                client=net_client,
                agent_config=global_agent_config,
                recycle_bin_roots=[str(self.sim_rb)],
            )
            collector.initialize_baseline()

            # Simulate normal Windows Explorer Delete -> moved into $Recycle.Bin ($I + $R) and unlinked from Documents
            i_file, r_file = _write_win10_recycle_bin_entry(self.sim_rb, "RB9001", orig_doc, doc_bytes)
            self.assertIsNotNone(parse_recycle_bin_metadata_file(str(i_file)))
            orig_doc.unlink()

            with patch("agent.client.httpx.AsyncClient", side_effect=asgi_factory):
                await collector.run_once()

            # Ensure file was NOT restored to working folder and remains untouched in Recycle Bin
            self.assertFalse(orig_doc.exists())
            self.assertTrue(r_file.exists())

            # Verify server stored CAPTURED_FROM_RECYCLE_BIN, destination_path="Recycle Bin", and View File works
            res = self.http_client.get(
                "/api/dlp/events",
                headers=self.admin_headers,
                params={"search": "PreExisting_Customer_Quote.pdf"},
            )
            self.assertEqual(res.status_code, 200)
            ev = res.json()["events"][0]
            self.assertEqual(ev["event_type"], "DELETED")
            self.assertEqual(ev["destination_path"], "Recycle Bin")
            self.assertTrue(ev["has_content"])
            self.assertEqual(ev["evidence_status"], "CAPTURED_FROM_RECYCLE_BIN")
            self.assertEqual(ev["metadata"]["sha256"], expected_sha)

            prev = self.http_client.get(
                f"/api/dlp/files/{ev['file_blob_id']}/preview",
                headers=self.admin_headers,
            )
            self.assertEqual(prev.status_code, 200)
            self.assertEqual(prev.json()["sha256"], expected_sha)
        finally:
            global_agent_config.server_url = saved_url
            global_agent_config.device_id = saved_id
            global_agent_config.device_secret = saved_secret
            global_agent_config.dlp = saved_dlp

    async def test_04_permanent_shift_delete_retains_event_and_baseline_metadata(self):
        """Part 12 #4: Permanent Shift+Delete of unarchived baseline file retains SHA-256 & metadata with NOT_CAPTURED_UNAVAILABLE."""
        secret_pdf = self.docs / "Confidential_ShiftDelete.pdf"
        raw = b"%PDF-1.7 Permanently deleted via Shift+Delete"
        expected_sha = hashlib.sha256(raw).hexdigest()
        secret_pdf.write_bytes(raw)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs], monitor_removable_storage=False)
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            recycle_bin_roots=[str(self.sim_rb)],  # Empty Recycle Bin -> bypassed!
        )
        collector.initialize_baseline()

        # Permanent deletion bypassing Recycle Bin
        secret_pdf.unlink()
        await collector.run_once()

        self.assertEqual(len(client.uploaded_files), 0)
        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "DELETED")
        self.assertIsNone(ev["destination_path"])
        self.assertIsNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "NOT_CAPTURED_UNAVAILABLE")
        self.assertEqual(ev["metadata"]["file_size"], len(raw))
        self.assertEqual(ev["metadata"]["sha256"], expected_sha)
        self.assertEqual(ev["metadata"]["deletion_mode"], "permanent_or_bypassed_recycle_bin")

    async def test_05_delete_oversized_file_in_recycle_bin_or_permanent_marks_size_limit(self):
        """Part 12 #5 & #6: Deleting oversized file (> max MB) in Recycle Bin skips upload and sets NOT_CAPTURED_SIZE_LIMIT."""
        big_dwg = self.docs / "Confidential_Design.dwg"
        big_bytes = b"DWG_HEADER_" + (b"X" * (2 * 1024 * 1024 + 8192))  # > 2 MB policy limit
        expected_sha = hashlib.sha256(big_bytes).hexdigest()
        big_dwg.write_bytes(big_bytes)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs], monitor_removable_storage=False, maximum_evidence_size_mb=2)
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            recycle_bin_roots=[str(self.sim_rb)],
        )
        collector.initialize_baseline()

        # Move to Recycle Bin and delete from Documents
        _write_win10_recycle_bin_entry(self.sim_rb, "RB9002", big_dwg, big_bytes)
        big_dwg.unlink()

        await collector.run_once()
        self.assertEqual(len(client.uploaded_files), 0)
        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "DELETED")
        self.assertEqual(ev["destination_path"], "Recycle Bin")
        self.assertIsNone(ev["file_blob_id"])
        self.assertEqual(ev["metadata"]["evidence_status"], "NOT_CAPTURED_SIZE_LIMIT")
        self.assertEqual(ev["metadata"]["maximum_evidence_size_mb"], 2)
        self.assertEqual(ev["metadata"]["sha256"], expected_sha)

    async def test_06_local_copy_and_move_correlation_and_same_name_different_content_guard(self):
        """Part 12 #7, #8, #9, #10, #11, #12: Local COPY, local/cross-volume MOVE, and strict SHA-256 non-collision."""
        src_xlsx = self.docs / "Customer_List.xlsx"
        xlsx_bytes = b"XLSX_CUSTOMER_RECORDS_AAA_1001"
        expected_sha = hashlib.sha256(xlsx_bytes).hexdigest()
        src_xlsx.write_bytes(xlsx_bytes)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, self.desktop, self.projects], monitor_removable_storage=False)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        collector.initialize_baseline()

        # #7: Copy Documents\Customer_List.xlsx -> Desktop\Customer_List.xlsx
        dst_desktop = self.desktop / "Customer_List.xlsx"
        shutil.copy2(src_xlsx, dst_desktop)
        await collector.run_once()

        ev_copy = client.posted_events[-1]
        self.assertEqual(ev_copy["event_type"], "COPIED")
        self.assertEqual(Path(ev_copy["source_path"]).resolve(), src_xlsx.resolve())
        self.assertEqual(Path(ev_copy["destination_path"]).resolve(), dst_desktop.resolve())
        self.assertEqual(ev_copy["channel"], LOCAL_CHANNEL_NAME)
        self.assertEqual(ev_copy["metadata"]["sha256"], expected_sha)
        self.assertEqual(ev_copy["metadata"]["evidence_status"], "CAPTURED")

        # #11: Create Projects\Customer_List.xlsx with SAME filename but DIFFERENT content -> MUST be CREATED, NOT COPIED!
        unrelated_same_name = self.projects / "Customer_List.xlsx"
        unrelated_same_name.write_bytes(b"COMPLETELY_DIFFERENT_WORKBOOK_CONTENT_BBB_9999")
        await collector.run_once()

        ev_diff = client.posted_events[-1]
        self.assertEqual(ev_diff["event_type"], "CREATED")
        self.assertIsNone(ev_diff["source_path"])
        self.assertNotEqual(ev_diff["file_blob_id"], ev_copy["file_blob_id"])

        # #11b: Ambiguous multi-source guard: since BOTH Documents\Customer_List.xlsx and Desktop\Customer_List.xlsx
        # have identical SHA-256 and identical mtime_ns, copying to Projects\Ambiguous_Copy.xlsx must NOT guess source_path,
        # while STILL deduplicating file_blob_id by SHA-256!
        ambig_copy = self.projects / "Ambiguous_Copy.xlsx"
        shutil.copy2(src_xlsx, ambig_copy)
        await collector.run_once()

        ev_ambig = client.posted_events[-1]
        self.assertEqual(ev_ambig["event_type"], "CREATED")
        self.assertIsNone(ev_ambig["source_path"])
        self.assertEqual(ev_ambig["file_blob_id"], ev_copy["file_blob_id"])

        # #12: Unambiguous copy under DIFFERENT filename (A.xlsx -> Copy.xlsx with identical SHA-256) -> COPIED & reuses blob!
        unique_src = self.docs / "A.xlsx"
        unique_src.write_bytes(b"UNIQUE_WORKBOOK_CONTENT_SHA_AAA")
        await collector.run_once()
        ev_a = client.posted_events[-1]

        diff_name_copy = self.projects / "Copy.xlsx"
        shutil.copy2(unique_src, diff_name_copy)
        await collector.run_once()

        ev_dedup = client.posted_events[-1]
        self.assertEqual(ev_dedup["event_type"], "COPIED")
        self.assertEqual(Path(ev_dedup["source_path"]).resolve(), unique_src.resolve())
        self.assertEqual(Path(ev_dedup["destination_path"]).resolve(), diff_name_copy.resolve())
        self.assertEqual(ev_dedup["file_blob_id"], ev_a["file_blob_id"])

        # #8 & #10: Cross-inode MOVE (copy2 + unlink) from Desktop\Customer_List.xlsx -> Projects\Moved_Customer_List.xlsx
        moved_dst = self.projects / "Moved_Customer_List.xlsx"
        shutil.copy2(dst_desktop, moved_dst)
        dst_desktop.unlink()
        await collector.run_once()

        ev_move = client.posted_events[-1]
        self.assertEqual(ev_move["event_type"], "MOVED")
        self.assertEqual(Path(ev_move["source_path"]).resolve(), dst_desktop.resolve())
        self.assertEqual(Path(ev_move["destination_path"]).resolve(), moved_dst.resolve())

    async def test_07_full_multi_hop_history_chain_local_to_desktop_to_usb_to_delete(self):
        """Part 10 & Part 12 #13, #14: Reconstruct full file history (CREATED -> MODIFIED -> COPIED local -> COPIED USB -> USB->Local -> DELETED)."""
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_vol), label="CORP_USB", filesystem="exFAT")]
        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, self.desktop])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
            recycle_bin_roots=[str(self.sim_rb)],
        )
        collector.initialize_baseline()

        # Step 1: CREATED in Documents
        doc_file = self.docs / "Chain_Audit.xlsx"
        doc_file.write_bytes(b"v1-initial")
        await collector.run_once()

        # Step 2: MODIFIED in Documents
        v2_bytes = b"v2-updated-final-audit-sheet"
        doc_file.write_bytes(v2_bytes)
        os.utime(doc_file, ns=(time.time_ns() + 2_000_000_000, time.time_ns() + 2_000_000_000))
        await collector.run_once()

        # Step 3: COPIED Documents -> Desktop
        desk_file = self.desktop / "Chain_Audit.xlsx"
        shutil.copy2(doc_file, desk_file)
        os.utime(desk_file, ns=(time.time_ns() + 5_000_000_000, time.time_ns() + 5_000_000_000))
        await collector.run_once()

        # Step 4: COPIED Desktop -> USB (preferring most recently written Desktop copy!)
        usb_file = self.usb_vol / "Chain_Audit.xlsx"
        shutil.copy2(desk_file, usb_file)
        await collector.run_once()

        # Step 5: USB -> Local copy of a USB-origin file
        usb_inbound = self.usb_vol / "Vendor_Spec.pdf"
        usb_inbound.write_bytes(b"%PDF-1.7 Vendor USB Spec")
        await collector.run_once()

        local_from_usb = self.docs / "Vendor_Spec.pdf"
        shutil.copy2(usb_inbound, local_from_usb)
        await collector.run_once()

        # Step 6: Delete file from USB (permanent, no Recycle Bin on USB flash drives)
        usb_file.unlink()
        await collector.run_once()

        chain = [(e["event_type"], e["file_name"], e["channel"], e["metadata"]["evidence_status"]) for e in client.posted_events]
        self.assertEqual(chain[0], ("CREATED", "Chain_Audit.xlsx", LOCAL_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(chain[1], ("MODIFIED", "Chain_Audit.xlsx", LOCAL_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(chain[2], ("COPIED", "Chain_Audit.xlsx", LOCAL_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(chain[3], ("COPIED", "Chain_Audit.xlsx", USB_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(Path(client.posted_events[3]["source_path"]).resolve(), desk_file.resolve())
        self.assertEqual(Path(client.posted_events[3]["destination_path"]).resolve(), usb_file.resolve())

        self.assertEqual(chain[4], ("CREATED", "Vendor_Spec.pdf", USB_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(chain[5], ("COPIED", "Vendor_Spec.pdf", USB_CHANNEL_NAME, "CAPTURED"))
        self.assertEqual(Path(client.posted_events[5]["source_path"]).resolve(), usb_inbound.resolve())
        self.assertEqual(Path(client.posted_events[5]["destination_path"]).resolve(), local_from_usb.resolve())

        # Note: Chain_Audit.xlsx v2 was ALREADY archived at Step 2, so deleting it on USB retains CAPTURED blob!
        self.assertEqual(chain[6][0], "DELETED")
        self.assertEqual(chain[6][2], USB_CHANNEL_NAME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
