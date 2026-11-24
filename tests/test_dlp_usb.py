import os
import sys
import time
import shutil
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
    WindowsRemovableVolumeDetector,
    DRIVE_REMOVABLE,
    DRIVE_FIXED,
    USB_CHANNEL_NAME,
)
from server.app.core.config import settings
from server.app.db.models import Base
from server.app.db.session import get_db
from server.app.main import app


class MockUsbDlpClient:
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
        blob_id = f"usb-blob-{self._seq:04d}"
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


def _build_usb_config(local_dirs, **overrides) -> AgentConfig:
    cfg = AgentConfig()
    cfg.device_id = "dev-usb-test-001"
    cfg.device_secret = "sec_usb_test"
    cfg.dlp = {
        "enabled": True,
        "monitored_directories": [str(d) for d in local_dirs],
        "monitor_removable_storage": True,
        "evidence_enabled": True,
        "maximum_evidence_size_mb": 25,
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
        "max_queue_size": 50,
    }
    cfg.dlp.update(overrides)
    return cfg


class TestUsbRemovableDlp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_docs = self.root / "LocalDocuments"
        self.usb_mount = self.root / "SimulatedUSBVolume"
        self.local_docs.mkdir(parents=True, exist_ok=True)
        self.usb_mount.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_01_dynamic_removable_volume_detection_without_hardcoded_letters(self):
        """1. WindowsRemovableVolumeDetector dynamically discovers DRIVE_REMOVABLE & hot-plugged USB drives."""
        detector = WindowsRemovableVolumeDetector()
        drive_table = {
            "C:\\": (DRIVE_FIXED, "OS", "NTFS"),
            "D:\\": (DRIVE_FIXED, "INTERNAL_DATA", "NTFS"),
        }

        with (
            patch("agent.fs_collector.os.name", "nt"),
            patch("agent.fs_collector._list_windows_drive_roots", side_effect=lambda: list(drive_table.keys())),
            patch("agent.fs_collector._query_windows_volume_details", side_effect=lambda r: drive_table[r]),
            patch("agent.fs_collector.os.path.exists", return_value=True),
        ):
            # Initial baseline: only C:\ and internal D:\ -> 0 removable volumes
            initial = detector.discover()
            self.assertEqual(initial, [])

            # Plug in USB flash drive mounted dynamically at M:\ and external USB SSD at P:\
            drive_table["M:\\"] = (DRIVE_REMOVABLE, "KINGSTON_USB", "exFAT")
            drive_table["P:\\"] = (DRIVE_FIXED, "WD_PASSPORT", "NTFS")

            discovered = detector.discover()
            mounts = {v.mount_point: (v.label, v.filesystem) for v in discovered}
            self.assertEqual(set(mounts.keys()), {"M:\\", "P:\\"})
            self.assertEqual(mounts["M:\\"], ("KINGSTON_USB", "exFAT"))
            self.assertEqual(mounts["P:\\"], ("WD_PASSPORT", "NTFS"))

    async def test_02_removable_volume_registration_and_baseline_indexing(self):
        """2. Newly connected removable volume emits DRIVE_CONNECTED and indexes pre-existing USB files without flood."""
        # Pre-populate USB stick with existing files before connection
        (self.usb_mount / "existing_usb_readme.txt").write_bytes(b"Pre-existing USB content")

        mounted = []
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Connect removable volume dynamically
        mounted.append(RemovableVolumeInfo(mount_point=str(self.usb_mount), label="SANDISK_64G", filesystem="exFAT"))
        await collector.run_once()

        self.assertEqual(len(collector.active_removable_volumes), 1)
        self.assertEqual(len(client.posted_events), 1)
        ev = client.posted_events[0]
        self.assertEqual(ev["event_type"], "DRIVE_CONNECTED")
        self.assertEqual(ev["channel"], USB_CHANNEL_NAME)
        self.assertEqual(ev["metadata"]["removable_volume"], str(self.usb_mount))
        self.assertEqual(ev["metadata"]["volume_label"], "SANDISK_64G")

    async def test_03_removable_volume_disconnect_stops_monitoring_cleanly(self):
        """3. Disconnecting removable volume emits DRIVE_DISCONNECTED, stops monitoring, and avoids false DELETED storm."""
        (self.usb_mount / "keep_on_stick.pdf").write_bytes(b"%PDF-1.4 on usb")
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="LEXAR_USB", filesystem="FAT32")]

        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()
        self.assertEqual(len(collector.active_removable_volumes), 1)

        # Disconnect volume
        mounted.clear()
        await collector.run_once()

        self.assertEqual(len(collector.active_removable_volumes), 0)
        self.assertEqual(len(client.posted_events), 1)
        self.assertEqual(client.posted_events[0]["event_type"], "DRIVE_DISCONNECTED")
        self.assertEqual(client.posted_events[0]["channel"], USB_CHANNEL_NAME)

        # Subsequent activity on that unmounted folder must NOT be monitored
        (self.usb_mount / "after_unplug.txt").write_bytes(b"should not be seen")
        await collector.run_once()
        self.assertEqual(len(client.posted_events), 1)

    async def test_04_file_activity_directly_on_removable_storage(self):
        """4. File create, modify, rename, and delete directly on removable storage emit USB/removable_storage events."""
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="CORP_USB", filesystem="exFAT")]
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Create on USB
        f_usb = self.usb_mount / "field_audit.csv"
        f_usb.write_bytes(b"id,status\n1,ok\n")
        await collector.run_once()

        # Modify on USB
        f_usb.write_bytes(b"id,status\n1,ok\n2,updated\n")
        os.utime(f_usb, ns=(time.time_ns() + 4_000_000_000, time.time_ns() + 4_000_000_000))
        await collector.run_once()

        # Rename on USB
        f_renamed = self.usb_mount / "field_audit_final.csv"
        f_usb.rename(f_renamed)
        await collector.run_once()

        # Delete on USB
        f_renamed.unlink()
        await collector.run_once()

        types = [e["event_type"] for e in client.posted_events]
        self.assertEqual(types, ["CREATED", "MODIFIED", "RENAMED", "DELETED"])
        for ev in client.posted_events:
            self.assertEqual(ev["channel"], USB_CHANNEL_NAME)
            self.assertEqual(ev["metadata"]["removable_volume"], str(self.usb_mount))

    async def test_05_local_to_removable_move_and_copy_detection(self):
        """5. Moving or copying files from monitored local folder -> removable USB records both paths and USB channel."""
        src_move = self.local_docs / "design_blueprint.dxf"
        src_move.write_bytes(b"DXF_BLUEPRINT_DATA_001")

        src_copy = self.local_docs / "pricing_sheet.xlsx"
        src_copy.write_bytes(b"XLSX_PRICING_CONFIDENTIAL_2026")

        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="USB_DRIVE", filesystem="exFAT")]
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Simulate real cross-volume move (copy2 creates new inode + unlink old file)
        dst_move = self.usb_mount / "design_blueprint.dxf"
        shutil.copy2(src_move, dst_move)
        src_move.unlink()

        # Simulate cross-volume copy (source remains in LocalDocuments)
        dst_copy = self.usb_mount / "pricing_sheet.xlsx"
        shutil.copy2(src_copy, dst_copy)

        await collector.run_once()

        by_name = {e["file_name"]: e for e in client.posted_events}
        self.assertIn("design_blueprint.dxf", by_name)
        self.assertIn("pricing_sheet.xlsx", by_name)

        ev_move = by_name["design_blueprint.dxf"]
        self.assertEqual(ev_move["event_type"], "MOVED")
        self.assertEqual(Path(ev_move["source_path"]).resolve(), src_move.resolve())
        self.assertEqual(Path(ev_move["destination_path"]).resolve(), dst_move.resolve())
        self.assertEqual(ev_move["channel"], USB_CHANNEL_NAME)
        self.assertEqual(ev_move["metadata"]["transfer_direction"], "local_to_removable")

        ev_copy = by_name["pricing_sheet.xlsx"]
        self.assertEqual(ev_copy["event_type"], "COPIED")
        self.assertEqual(Path(ev_copy["source_path"]).resolve(), src_copy.resolve())
        self.assertEqual(Path(ev_copy["destination_path"]).resolve(), dst_copy.resolve())
        self.assertEqual(ev_copy["channel"], USB_CHANNEL_NAME)
        self.assertEqual(ev_copy["metadata"]["transfer_direction"], "local_to_removable")

    async def test_06_removable_to_local_move_and_copy_detection(self):
        """6. Moving or copying files from removable USB -> monitored local folder records USB -> Local movement."""
        usb_move_src = self.usb_mount / "inbound_vendor_cad.3dm"
        usb_move_src.write_bytes(b"VENDOR_3DM_MODEL_PAYLOAD")

        usb_copy_src = self.usb_mount / "inbound_invoice.pdf"
        usb_copy_src.write_bytes(b"%PDF-1.7 INBOUND_USB_INVOICE")

        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="VENDOR_USB", filesystem="NTFS")]
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Cross-volume move from USB -> LocalDocuments
        local_move_dst = self.local_docs / "inbound_vendor_cad.3dm"
        shutil.copy2(usb_move_src, local_move_dst)
        usb_move_src.unlink()

        # Cross-volume copy from USB -> LocalDocuments
        local_copy_dst = self.local_docs / "inbound_invoice.pdf"
        shutil.copy2(usb_copy_src, local_copy_dst)

        await collector.run_once()

        by_name = {e["file_name"]: e for e in client.posted_events}
        ev_move = by_name["inbound_vendor_cad.3dm"]
        self.assertEqual(ev_move["event_type"], "MOVED")
        self.assertEqual(Path(ev_move["source_path"]).resolve(), usb_move_src.resolve())
        self.assertEqual(Path(ev_move["destination_path"]).resolve(), local_move_dst.resolve())
        self.assertEqual(ev_move["channel"], USB_CHANNEL_NAME)
        self.assertEqual(ev_move["metadata"]["transfer_direction"], "removable_to_local")

        ev_copy = by_name["inbound_invoice.pdf"]
        self.assertEqual(ev_copy["event_type"], "COPIED")
        self.assertEqual(Path(ev_copy["source_path"]).resolve(), usb_copy_src.resolve())
        self.assertEqual(Path(ev_copy["destination_path"]).resolve(), local_copy_dst.resolve())
        self.assertEqual(ev_copy["channel"], USB_CHANNEL_NAME)
        self.assertEqual(ev_copy["metadata"]["transfer_direction"], "removable_to_local")

    async def test_07_usb_evidence_upload_and_25mb_ceiling(self):
        """7 & 8. Eligible USB files upload evidence; USB files > 25 MB skip upload with exceeds_size_limit."""
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="AUDIT_USB", filesystem="exFAT")]
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs], maximum_evidence_size_mb=25)
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Eligible USB file (small)
        small_usb = self.usb_mount / "usb_summary.txt"
        small_bytes = b"Eligible USB evidence document"
        small_usb.write_bytes(small_bytes)

        # Oversized USB file (> 25 MB)
        large_usb = self.usb_mount / "huge_usb_dump.bin"
        large_usb.write_bytes(b"X" * (25 * 1024 * 1024 + 2048))

        await collector.run_once()

        self.assertEqual(len(client.uploaded_files), 1)
        self.assertEqual(client.uploaded_files[0]["original_filename"], "usb_summary.txt")

        by_name = {e["file_name"]: e for e in client.posted_events}
        self.assertIsNotNone(by_name["usb_summary.txt"]["file_blob_id"])
        self.assertEqual(by_name["usb_summary.txt"]["metadata"]["evidence_status"], "CAPTURED")

        self.assertIsNone(by_name["huge_usb_dump.bin"]["file_blob_id"])
        self.assertEqual(by_name["huge_usb_dump.bin"]["metadata"]["evidence_status"], "NOT_CAPTURED_SIZE_LIMIT")
        self.assertEqual(by_name["huge_usb_dump.bin"]["channel"], USB_CHANNEL_NAME)

    async def test_08_abrupt_disconnected_volume_errors_do_not_crash(self):
        """9. OSError (device not ready / abrupt pull) during scan or hash never crashes collector."""
        mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="FLAKY_USB", filesystem="FAT32")]
        client = MockUsbDlpClient()
        cfg = _build_usb_config([self.local_docs])
        collector = FilesystemEventCollector(
            client=client,
            agent_config=cfg,
            volume_detector=lambda: list(mounted),
        )
        collector.initialize_baseline()

        # Create a file, then simulate abrupt USB I/O WinError 21 during open/read
        (self.usb_mount / "abrupt.txt").write_bytes(b"yanked mid-read")
        with patch("agent.fs_collector.open", side_effect=OSError(21, "The device is not ready")):
            await collector.run_once()

        self.assertEqual(len(client.posted_events), 1)
        self.assertEqual(client.posted_events[0]["metadata"]["evidence_status"], "NOT_CAPTURED_UNAVAILABLE")

        # Simulate volume vanishing physically before detector updates
        shutil.rmtree(self.usb_mount)
        await collector.run_once()  # Must not crash or emit false DELETED events
        self.assertEqual(len(client.posted_events), 1)

    async def test_09_usb_duplicate_event_prevention_end_to_end(self):
        """10. Retrying identical USB event payload with same idempotency_key does not create duplicate DB rows."""
        temp_db_dir = tempfile.TemporaryDirectory()
        try:
            t_root = Path(temp_db_dir.name)
            vault = t_root / "vault"
            vault.mkdir(parents=True, exist_ok=True)
            settings.DLP_VAULT_DIR = vault

            engine = create_engine(
                f"sqlite:///{(t_root / 'usb_dedup.db').as_posix()}",
                connect_args={"check_same_thread": False},
            )
            TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Base.metadata.create_all(bind=engine)

            def override_get_db():
                db = TestingSession()
                try:
                    yield db
                finally:
                    db.close()

            app.dependency_overrides[get_db] = override_get_db
            sync_client = TestClient(app)
            admin_headers = {"Authorization": f"Bearer {settings.ADMIN_AUTH_TOKEN}"}

            enroll = sync_client.post(
                "/api/v1/auth/enroll",
                headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
                json={
                    "hardware_uuid": "hw-usb-dedup-001",
                    "computer_name": "USB-TEST-PC",
                    "employee_name": "USB Tester",
                    "os_version": "Windows 11",
                },
            ).json()

            mounted = [RemovableVolumeInfo(mount_point=str(self.usb_mount), label="DEDUP_USB", filesystem="exFAT")]
            mock_client = MockUsbDlpClient()
            cfg = _build_usb_config([self.local_docs])
            cfg.device_id = enroll["device_id"]
            collector = FilesystemEventCollector(
                client=mock_client,
                agent_config=cfg,
                volume_detector=lambda: list(mounted),
            )
            collector.initialize_baseline()

            (self.usb_mount / "dedup_check.txt").write_bytes(b"Idempotent USB test payload")
            await collector.run_once()

            payload = dict(mock_client.posted_events[0])
            payload["file_blob_id"] = None  # Submit event metadata twice with same idempotency_key
            dev_headers = {
                "X-Device-Id": enroll["device_id"],
                "Authorization": f"Bearer {enroll['device_secret']}",
            }

            r1 = sync_client.post("/api/dlp/events", headers=dev_headers, json=payload)
            r2 = sync_client.post("/api/dlp/events", headers=dev_headers, json=payload)
            self.assertEqual(r1.status_code, 201)
            self.assertEqual(r2.status_code, 201)
            self.assertEqual(r1.json()["id"], r2.json()["id"])

            listed = sync_client.get(
                "/api/dlp/events",
                headers=admin_headers,
                params={"endpoint": enroll["device_id"]},
            ).json()
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["events"][0]["channel"], USB_CHANNEL_NAME)
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            temp_db_dir.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
