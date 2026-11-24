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

    async def test_08_ntfs_junction_does_not_poison_sibling_monitored_root(self):
        """Regression #1: NTFS junction inside Documents (e.g. Documents\\My Pictures -> Pictures) must never skip Pictures."""
        import subprocess

        downloads = self.docs.parent / "Downloads"
        pictures = self.docs.parent / "Pictures"
        downloads.mkdir(exist_ok=True)
        pictures.mkdir(exist_ok=True)

        existing_pic = pictures / "Existing_Photo.png"
        existing_pic.write_bytes(b"\x89PNG\r\n\x1a\nEXISTING_PHOTO_DATA")
        dl_doc = downloads / "Travel_Commercial_2026.docx"
        dl_doc.write_bytes(b"PK\x03\x04TRAVEL_COMMERCIAL_2026_DOCX")

        # Create real Windows NTFS junction Documents\My Pictures -> Pictures
        junction_path = self.docs / "My Pictures"
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_path), str(pictures)],
                check=True,
                capture_output=True,
            )

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, downloads, pictures], monitor_removable_storage=False)
        collector = FilesystemEventCollector(client=client, agent_config=cfg)
        count = collector.initialize_baseline()

        # Both Downloads\Travel_Commercial_2026.docx AND Pictures\Existing_Photo.png MUST be indexed (never 0 in Pictures!)
        snap = collector.get_baseline_snapshot()
        indexed_paths = {Path(s.path).resolve() for s in snap.values()}
        self.assertEqual(count, 2)
        self.assertIn(existing_pic.resolve(), indexed_paths)
        self.assertIn(dl_doc.resolve(), indexed_paths)

        # Cut + Paste (same-volume rename/move) from Downloads -> Pictures must emit MOVED, NOT blind DELETED!
        moved_pic_doc = pictures / "Travel_Commercial_2026.docx"
        dl_doc.rename(moved_pic_doc)
        await collector.run_once()

        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "MOVED")
        self.assertEqual(Path(ev["source_path"]).resolve(), dl_doc.resolve())
        self.assertEqual(Path(ev["destination_path"]).resolve(), moved_pic_doc.resolve())
        self.assertEqual(ev["metadata"]["evidence_status"], "CAPTURED")
        if junction_path.exists() or os.path.lexists(junction_path):
            os.rmdir(junction_path)

    async def test_09_explorer_preserved_mtime_multihop_copy_and_locked_copy_deferral(self):
        """Regression #2: Multi-hop copy2 preserving identical mtime_ns + Explorer '- Copy' + WinError 32 mid-copy lock deferral."""
        downloads = self.docs.parent / "Downloads"
        downloads.mkdir(exist_ok=True)
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_vol), label="CORP_USB", filesystem="NTFS")]

        src_doc = self.docs / "Quarterly_Plan.docx"
        payload = b"PK\x03\x04QUARTERLY_PLAN_2026_EXACT_BYTES"
        src_doc.write_bytes(payload)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, self.desktop, downloads])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Hop 1: Documents -> Desktop (shutil.copy2 preserves identical mtime_ns, NO os.utime!)
        hop1_desk = self.desktop / "Quarterly_Plan.docx"
        shutil.copy2(src_doc, hop1_desk)
        await collector.run_once()
        self.assertEqual(client.posted_events[-1]["event_type"], "COPIED")
        self.assertEqual(Path(client.posted_events[-1]["source_path"]).resolve(), src_doc.resolve())
        self.assertEqual(Path(client.posted_events[-1]["destination_path"]).resolve(), hop1_desk.resolve())

        # Hop 2: Desktop -> Downloads (identical mtime_ns on BOTH Documents and Desktop; must pick Desktop!)
        hop2_dl = downloads / "Quarterly_Plan.docx"
        shutil.copy2(hop1_desk, hop2_dl)
        await collector.run_once()
        self.assertEqual(client.posted_events[-1]["event_type"], "COPIED")
        self.assertEqual(Path(client.posted_events[-1]["source_path"]).resolve(), hop1_desk.resolve())
        self.assertEqual(Path(client.posted_events[-1]["destination_path"]).resolve(), hop2_dl.resolve())

        # Hop 3: Downloads -> USB (identical mtime_ns on Documents, Desktop, Downloads; must pick Downloads!)
        hop3_usb = self.usb_vol / "Quarterly_Plan.docx"
        shutil.copy2(hop2_dl, hop3_usb)
        await collector.run_once()
        self.assertEqual(client.posted_events[-1]["event_type"], "COPIED")
        self.assertEqual(Path(client.posted_events[-1]["source_path"]).resolve(), hop2_dl.resolve())
        self.assertEqual(Path(client.posted_events[-1]["destination_path"]).resolve(), hop3_usb.resolve())
        self.assertEqual(client.posted_events[-1]["channel"], USB_CHANNEL_NAME)

        # Same-folder Ctrl+C / Ctrl+V duplicate ("Quarterly_Plan - Copy.docx" inside Desktop)
        same_folder_dup = self.desktop / "Quarterly_Plan - Copy.docx"
        shutil.copy2(hop1_desk, same_folder_dup)
        await collector.run_once()
        self.assertEqual(client.posted_events[-1]["event_type"], "COPIED")
        self.assertEqual(Path(client.posted_events[-1]["source_path"]).resolve(), hop1_desk.resolve())
        self.assertEqual(Path(client.posted_events[-1]["destination_path"]).resolve(), same_folder_dup.resolve())

        # Mid-copy WinError 32 sharing violation deferral: locked on cycle 1 -> deferred -> unlocked on cycle 2 -> COPIED!
        locked_src = self.docs / "Locked_Source_Spec.docx"
        locked_src.write_bytes(b"PK\x03\x04UNIQUE_LOCKED_COPY_PAYLOAD_2026")
        await collector.run_once()

        locked_target = downloads / "Locked_Source_Spec.docx"
        shutil.copy2(locked_src, locked_target)
        events_before = len(client.posted_events)

        from agent import fs_collector as fsc_mod
        real_hash_fn = fsc_mod.compute_file_sha256

        def simulated_sharing_violation(p, *a, **kw):
            if Path(p).resolve() == locked_target.resolve():
                return None, None, "PermissionError: [WinError 32] The process cannot access the file"
            return real_hash_fn(p, *a, **kw)

        with patch("agent.fs_collector.compute_file_sha256", side_effect=simulated_sharing_violation):
            await collector.run_once()
        # Deferred on cycle 1 rather than emitting degraded CREATED / NOT_CAPTURED_UNAVAILABLE!
        self.assertEqual(len(client.posted_events), events_before)

        # Cycle 2: Explorer write lock released -> correlated as COPIED + CAPTURED!
        await collector.run_once()
        self.assertEqual(len(client.posted_events), events_before + 1)
        self.assertEqual(client.posted_events[-1]["event_type"], "COPIED")
        self.assertEqual(Path(client.posted_events[-1]["destination_path"]).resolve(), locked_target.resolve())
        self.assertEqual(client.posted_events[-1]["metadata"]["evidence_status"], "CAPTURED")

    async def test_10_realistic_operations_across_all_nine_file_extensions(self):
        """Section 2: Verify CREATE, MODIFY, RENAME, COPY, MOVE, and DELETE across PDF, DOCX, JPG, PNG, MP4, ZIP, EXE, DWG, and custom extension."""
        sample_specs = [
            ("Spec_Doc.pdf", ".pdf", b"%PDF-1.7\nCorporate engineering spec\n"),
            ("Board_Notes.docx", ".docx", b"PK\x03\x04DOCX_OOXML_ARCHIVE_PAYLOAD"),
            ("Site_Photo.jpg", ".jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF_PHOTO_DATA"),
            ("Diagram_Export.png", ".png", b"\x89PNG\r\n\x1a\nPNG_IMAGE_PAYLOAD"),
            ("Training_Clip.mp4", ".mp4", b"\x00\x00\x00\x18ftypmp42MP4_VIDEO_STREAM"),
            ("Release_Bundle.zip", ".zip", b"PK\x03\x04ZIP_BUNDLE_BINARY_BYTES"),
            ("Setup_Tool.exe", ".exe", b"MZ\x90\x00PE_EXECUTABLE_BINARY_HEADER"),
            ("Floorplan_2026.dwg", ".dwg", b"AC1032_AUTOCAD_DWG_DRAWING_BINARY"),
            ("Telemetry_Snapshot.amvcustom", ".amvcustom", b"\xde\xad\xbe\xefARBITRARY_CUSTOM_EXTENSION_DATA"),
        ]

        downloads = self.docs.parent / "Downloads"
        downloads.mkdir(exist_ok=True)

        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, self.desktop, downloads], monitor_removable_storage=False)
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            recycle_bin_roots=[str(self.sim_rb)],
        )
        collector.initialize_baseline()

        for fname, ext, initial_bytes in sample_specs:
            stem = Path(fname).stem
            # 1. CREATE
            f_path = self.docs / fname
            f_path.write_bytes(initial_bytes)
            await collector.run_once()
            ev_create = client.posted_events[-1]
            self.assertEqual(ev_create["event_type"], "CREATED")
            self.assertEqual(ev_create["file_name"], fname)
            self.assertEqual(ev_create["file_extension"], ext)
            self.assertEqual(ev_create["metadata"]["file_size"], len(initial_bytes))
            self.assertEqual(ev_create["metadata"]["sha256"], hashlib.sha256(initial_bytes).hexdigest())
            self.assertEqual(ev_create["metadata"]["evidence_status"], "CAPTURED")

            # 2. MODIFY
            updated_bytes = initial_bytes + b"_REV2_MODIFIED"
            f_path.write_bytes(updated_bytes)
            os.utime(f_path, ns=(time.time_ns() + 2_000_000_000, time.time_ns() + 2_000_000_000))
            await collector.run_once()
            ev_mod = client.posted_events[-1]
            self.assertEqual(ev_mod["event_type"], "MODIFIED")
            self.assertEqual(ev_mod["file_extension"], ext)
            self.assertEqual(ev_mod["metadata"]["sha256"], hashlib.sha256(updated_bytes).hexdigest())
            self.assertEqual(ev_mod["metadata"]["evidence_status"], "CAPTURED")

            # 3. RENAME (same folder)
            renamed_name = f"{stem}_Final{ext}"
            renamed_path = self.docs / renamed_name
            f_path.rename(renamed_path)
            await collector.run_once()
            ev_ren = client.posted_events[-1]
            self.assertEqual(ev_ren["event_type"], "RENAMED")
            self.assertEqual(Path(ev_ren["source_path"]).resolve(), f_path.resolve())
            self.assertEqual(Path(ev_ren["destination_path"]).resolve(), renamed_path.resolve())
            self.assertEqual(ev_ren["file_extension"], ext)

            # 4. COPY (Documents -> Desktop)
            copied_path = self.desktop / renamed_name
            shutil.copy2(renamed_path, copied_path)
            await collector.run_once()
            ev_copy = client.posted_events[-1]
            self.assertEqual(ev_copy["event_type"], "COPIED")
            self.assertEqual(Path(ev_copy["source_path"]).resolve(), renamed_path.resolve())
            self.assertEqual(Path(ev_copy["destination_path"]).resolve(), copied_path.resolve())
            self.assertEqual(ev_copy["file_extension"], ext)
            self.assertEqual(ev_copy["metadata"]["sha256"], hashlib.sha256(updated_bytes).hexdigest())

            # 5. MOVE (Desktop -> Downloads)
            moved_path = downloads / renamed_name
            copied_path.rename(moved_path)
            await collector.run_once()
            ev_move = client.posted_events[-1]
            self.assertEqual(ev_move["event_type"], "MOVED")
            self.assertEqual(Path(ev_move["source_path"]).resolve(), copied_path.resolve())
            self.assertEqual(Path(ev_move["destination_path"]).resolve(), moved_path.resolve())
            self.assertEqual(ev_move["file_extension"], ext)

            # 6. DELETE (Downloads copy deleted; original in Documents also cleaned up)
            moved_path.unlink()
            await collector.run_once()
            ev_del = client.posted_events[-1]
            self.assertEqual(ev_del["event_type"], "DELETED")
            self.assertEqual(Path(ev_del["source_path"]).resolve(), moved_path.resolve())
            self.assertIsNone(ev_del["destination_path"])
            self.assertEqual(ev_del["file_extension"], ext)
            # Content was already archived on MODIFY, so blob remains CAPTURED
            self.assertEqual(ev_del["metadata"]["evidence_status"], "CAPTURED")

    async def test_11_copy_and_move_local_and_removable_full_metadata_and_direction(self):
        """Sections 3 & 4: Verify LOCAL<->LOCAL, LOCAL->REMOVABLE, and REMOVABLE->LOCAL COPY & MOVE fields and source/destination ordering."""
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_vol), label="AUDIT_USB", filesystem="exFAT")]
        client = MockLifecycleClient()
        cfg = _make_cfg([self.docs, self.desktop, self.projects])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )

        # Seed 1 local baseline file and 1 USB baseline file before baseline initialization
        local_seed = self.docs / "Architecture_Blueprint.dwg"
        local_bytes = b"AC1032_LOCAL_BLUEPRINT_SEED_DATA_2026"
        local_sha = hashlib.sha256(local_bytes).hexdigest()
        local_seed.write_bytes(local_bytes)

        usb_seed = self.usb_vol / "Supplier_Archive.zip"
        usb_bytes = b"PK\x03\x04USB_SUPPLIER_ARCHIVE_BYTES_2026"
        usb_sha = hashlib.sha256(usb_bytes).hexdigest()
        usb_seed.write_bytes(usb_bytes)

        indexed = collector.initialize_baseline()
        self.assertEqual(indexed, 2)
        self.assertEqual(len(client.uploaded_files), 0)

        def _assert_full_fields(ev, expected_type, exp_src: Path, exp_dst: Path, exp_ext: str, exp_size: int, exp_sha: str, exp_chan: str):
            self.assertEqual(ev["event_type"], expected_type)
            self.assertEqual(ev["file_name"], exp_dst.name)
            self.assertEqual(ev["file_extension"], exp_ext)
            self.assertEqual(ev["metadata"]["file_size"], exp_size)
            self.assertEqual(Path(ev["source_path"]).resolve(), exp_src.resolve())
            self.assertEqual(Path(ev["destination_path"]).resolve(), exp_dst.resolve())
            self.assertNotEqual(Path(ev["source_path"]).resolve(), Path(ev["destination_path"]).resolve())
            self.assertEqual(ev["metadata"]["sha256"], exp_sha)
            self.assertEqual(ev["endpoint_id"], "dev-lifecycle-001")
            self.assertTrue(bool(ev["timestamp"]))
            self.assertEqual(ev["channel"], exp_chan)
            self.assertEqual(ev["metadata"]["evidence_status"], "CAPTURED")

        # 1. LOCAL -> LOCAL COPY (baseline file Documents -> Desktop)
        desk_copy = self.desktop / "Architecture_Blueprint.dwg"
        shutil.copy2(local_seed, desk_copy)
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "COPIED", local_seed, desk_copy, ".dwg", len(local_bytes), local_sha, LOCAL_CHANNEL_NAME
        )

        # 2. LOCAL -> REMOVABLE COPY (Desktop -> USB)
        usb_copy = self.usb_vol / "Architecture_Blueprint.dwg"
        shutil.copy2(desk_copy, usb_copy)
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "COPIED", desk_copy, usb_copy, ".dwg", len(local_bytes), local_sha, USB_CHANNEL_NAME
        )
        self.assertEqual(client.posted_events[-1]["metadata"]["transfer_direction"], "local_to_removable")

        # 3. REMOVABLE -> LOCAL COPY (USB baseline file -> Projects)
        proj_from_usb_copy = self.projects / "Supplier_Archive.zip"
        shutil.copy2(usb_seed, proj_from_usb_copy)
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "COPIED", usb_seed, proj_from_usb_copy, ".zip", len(usb_bytes), usb_sha, USB_CHANNEL_NAME
        )
        self.assertEqual(client.posted_events[-1]["metadata"]["transfer_direction"], "removable_to_local")

        # 4. LOCAL -> LOCAL MOVE (Desktop -> Projects)
        local_move_src = self.desktop / "Local_Move_Item.pdf"
        move_bytes = b"%PDF-1.7_LOCAL_TO_LOCAL_MOVE_TEST"
        move_sha = hashlib.sha256(move_bytes).hexdigest()
        local_move_src.write_bytes(move_bytes)
        await collector.run_once()

        local_move_dst = self.projects / "Local_Move_Item.pdf"
        local_move_src.rename(local_move_dst)
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "MOVED", local_move_src, local_move_dst, ".pdf", len(move_bytes), move_sha, LOCAL_CHANNEL_NAME
        )

        # 5. LOCAL -> REMOVABLE MOVE (Projects -> USB via cross-volume copy2 + unlink)
        usb_move_dst = self.usb_vol / "Local_Move_Item.pdf"
        shutil.copy2(local_move_dst, usb_move_dst)
        local_move_dst.unlink()
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "MOVED", local_move_dst, usb_move_dst, ".pdf", len(move_bytes), move_sha, USB_CHANNEL_NAME
        )
        self.assertEqual(client.posted_events[-1]["metadata"]["transfer_direction"], "local_to_removable")

        # 6. REMOVABLE -> LOCAL MOVE (USB -> Documents via cross-volume copy2 + unlink)
        back_to_local_dst = self.docs / "Local_Move_Item.pdf"
        shutil.copy2(usb_move_dst, back_to_local_dst)
        usb_move_dst.unlink()
        await collector.run_once()
        _assert_full_fields(
            client.posted_events[-1], "MOVED", usb_move_dst, back_to_local_dst, ".pdf", len(move_bytes), move_sha, USB_CHANNEL_NAME
        )
        self.assertEqual(client.posted_events[-1]["metadata"]["transfer_direction"], "removable_to_local")

    @unittest.skipUnless(os.name == "nt", "Windows Explorer Shell.Application COM requires Windows")
    async def test_12_windows_explorer_shell_com_copyhere_and_movehere_and_dashboard_api(self):
        """Sections 7, 9 & Real Explorer: Validate real Windows Shell.Application CopyHere/MoveHere on baseline files and verify /api/dlp/events fields."""
        import subprocess

        saved_url = global_agent_config.server_url
        saved_id = global_agent_config.device_id
        saved_secret = global_agent_config.device_secret
        saved_dlp = dict(global_agent_config.dlp)
        real_async_cls = httpx.AsyncClient

        def asgi_factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return real_async_cls(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

        def shell_op(src_file: Path, dst_folder: Path, method: str):
            ps = (
                f"$sh = New-Object -ComObject Shell.Application; "
                f"$f = $sh.NameSpace('{str(dst_folder)}'); "
                f"if ($null -eq $f) {{ throw 'Namespace null' }}; "
                f"$f.{method}('{str(src_file)}', 16)"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True)

        try:
            # Pre-existing baseline file before agent starts
            baseline_doc = self.docs / "Explorer_Baseline_Contract.docx"
            initial_bytes = b"PK\x03\x04REAL_WINDOWS_EXPLORER_BASELINE_DOCX_2026"
            initial_sha = hashlib.sha256(initial_bytes).hexdigest()
            baseline_doc.write_bytes(initial_bytes)

            global_agent_config.server_url = "http://testserver"
            global_agent_config.device_id = self.enrolled["device_id"]
            global_agent_config.device_secret = self.enrolled["device_secret"]
            global_agent_config.dlp = _make_cfg(
                [self.docs, self.desktop, self.projects],
                monitor_removable_storage=False,
            ).dlp

            net_client = AgentNetworkClient()
            collector = FilesystemEventCollector(
                client=net_client,
                agent_config=global_agent_config,
                recycle_bin_roots=[str(self.sim_rb)],
            )
            b_count = collector.initialize_baseline()
            self.assertEqual(b_count, 1)

            with patch("agent.client.httpx.AsyncClient", side_effect=asgi_factory):
                # 1. Real Windows Explorer CopyHere (Documents -> Desktop)
                shell_op(baseline_doc, self.desktop, "CopyHere")
                await collector.run_once()

                # 2. Real Windows Explorer MoveHere (Desktop -> Projects)
                desk_copy = self.desktop / "Explorer_Baseline_Contract.docx"
                shell_op(desk_copy, self.projects, "MoveHere")
                await collector.run_once()

                # 3. Modify original baseline file in Documents
                mod_bytes = initial_bytes + b"_AMENDED_CLAUSE"
                mod_sha = hashlib.sha256(mod_bytes).hexdigest()
                baseline_doc.write_bytes(mod_bytes)
                os.utime(baseline_doc, ns=(time.time_ns() + 3_000_000_000, time.time_ns() + 3_000_000_000))
                await collector.run_once()

                # 4. Permanently delete moved copy in Projects
                proj_moved = self.projects / "Explorer_Baseline_Contract.docx"
                proj_moved.unlink()
                await collector.run_once()

            # Query /api/dlp/events dashboard endpoint and verify exact schema & fields for COPY, MOVE, MODIFY, DELETE
            res = self.http_client.get(
                "/api/dlp/events",
                headers=self.admin_headers,
                params={"search": "Explorer_Baseline_Contract.docx"},
            )
            self.assertEqual(res.status_code, 200)
            events = list(reversed(res.json()["events"]))
            self.assertEqual([e["event_type"] for e in events], ["COPIED", "MOVED", "MODIFIED", "DELETED"])

            for ev in events:
                for req_key in (
                    "id",
                    "event_type",
                    "file_name",
                    "file_extension",
                    "file_size",
                    "source_path",
                    "destination_path",
                    "endpoint_id",
                    "computer_name",
                    "employee_name",
                    "channel",
                    "timestamp",
                    "evidence_status",
                    "has_content",
                    "metadata",
                ):
                    self.assertIn(req_key, ev)

            ev_copied, ev_moved, ev_modified, ev_deleted = events
            self.assertEqual(Path(ev_copied["source_path"]).resolve(), baseline_doc.resolve())
            self.assertEqual(Path(ev_copied["destination_path"]).resolve(), desk_copy.resolve())
            self.assertEqual(ev_copied["evidence_status"], "CAPTURED")
            self.assertTrue(ev_copied["has_content"])
            self.assertEqual(ev_copied["metadata"]["sha256"], initial_sha)

            self.assertEqual(Path(ev_moved["source_path"]).resolve(), desk_copy.resolve())
            self.assertEqual(Path(ev_moved["destination_path"]).resolve(), proj_moved.resolve())
            self.assertEqual(ev_moved["evidence_status"], "CAPTURED")
            self.assertEqual(ev_moved["metadata"]["sha256"], initial_sha)

            self.assertEqual(Path(ev_modified["source_path"]).resolve(), baseline_doc.resolve())
            self.assertEqual(ev_modified["evidence_status"], "CAPTURED")
            self.assertEqual(ev_modified["metadata"]["sha256"], mod_sha)

            self.assertEqual(Path(ev_deleted["source_path"]).resolve(), proj_moved.resolve())
            self.assertIsNone(ev_deleted["destination_path"])
            # Content of initial_sha was already archived during COPIED, so DELETED retains CAPTURED evidence!
            self.assertEqual(ev_deleted["evidence_status"], "CAPTURED")
            self.assertEqual(ev_deleted["file_blob_id"], ev_copied["file_blob_id"])
        finally:
            global_agent_config.server_url = saved_url
            global_agent_config.device_id = saved_id
            global_agent_config.device_secret = saved_secret
            global_agent_config.dlp = saved_dlp


if __name__ == "__main__":
    unittest.main(verbosity=2)
