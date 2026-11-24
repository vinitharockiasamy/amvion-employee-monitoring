import json
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Device(Base):
    __tablename__ = "devices"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    hardware_uuid = Column(String(128), unique=True, index=True, nullable=False)
    computer_name = Column(String(128), nullable=False)
    employee_name = Column(String(128), nullable=False)
    device_secret_hash = Column(String(256), nullable=False)
    ip_address = Column(String(64), nullable=True)
    mac_address = Column(String(64), nullable=True)
    os_version = Column(String(128), nullable=True)
    status = Column(String(32), default="offline", index=True)  # online, offline
    current_mode = Column(String(32), default="screenshot")      # screenshot, streaming
    last_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    screenshots = relationship("Screenshot", back_populates="device", cascade="all, delete-orphan", order_by="desc(Screenshot.captured_at)")
    sessions = relationship("StreamSession", back_populates="device", cascade="all, delete-orphan")
    dlp_events = relationship("DlpEvent", back_populates="endpoint", cascade="all, delete-orphan", order_by="desc(DlpEvent.timestamp)")

    def to_dict(self):
        latest_screenshot_id = self.screenshots[0].id if self.screenshots else None
        latest_screenshot_url = f"/api/v1/admin/devices/{self.id}/screenshot/latest" if self.screenshots else None
        latest_screenshot_time = self.screenshots[0].captured_at.isoformat() if self.screenshots else None

        return {
            "device_id": self.id,
            "hardware_uuid": self.hardware_uuid,
            "computer_name": self.computer_name,
            "employee_name": self.employee_name,
            "ip_address": self.ip_address,
            "os_version": self.os_version,
            "status": self.status,
            "current_mode": self.current_mode,
            "last_seen_at": (self.last_seen_at.isoformat() + "Z") if self.last_seen_at else None,
            "latest_screenshot_id": latest_screenshot_id,
            "latest_screenshot_url": latest_screenshot_url,
            "latest_screenshot_time": (latest_screenshot_time + "Z") if latest_screenshot_time else None,
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None
        }

class Screenshot(Base):
    __tablename__ = "screenshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    device_id = Column(String(36), ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False)
    file_path = Column(String(512), nullable=False)
    file_size_bytes = Column(Integer, default=0)
    resolution = Column(String(32), nullable=True)
    captured_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    device = relationship("Device", back_populates="screenshots")

class StreamSession(Base):
    __tablename__ = "stream_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    device_id = Column(String(36), ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False)
    admin_id = Column(String(128), default="admin")
    status = Column(String(32), default="active")  # active, closed, failed
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)

    device = relationship("Device", back_populates="sessions")

class DlpFileBlob(Base):
    """SHA-256 deduplicated content storage record for archived DLP files."""
    __tablename__ = "dlp_file_blobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sha256 = Column(String(64), unique=True, index=True, nullable=False)
    original_filename = Column(String(256), nullable=False)
    content_type = Column(String(128), nullable=False, default="application/octet-stream")
    file_size = Column(Integer, nullable=False, default=0)
    storage_path = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    events = relationship("DlpEvent", back_populates="file_blob")

    def to_dict(self):
        return {
            "id": self.id,
            "sha256": self.sha256,
            "original_filename": self.original_filename,
            "content_type": self.content_type,
            "file_size": self.file_size,
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None,
            "download_url": f"/api/dlp/files/{self.id}/download"
        }

class DlpEvent(Base):
    """Enterprise DLP audit event model."""
    __tablename__ = "dlp_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id = Column(String(36), ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False)
    employee_name = Column(String(128), nullable=False)
    event_type = Column(String(48), index=True, nullable=False)
    file_name = Column(String(256), nullable=False)
    file_extension = Column(String(32), nullable=True)
    source_path = Column(Text, nullable=True)
    destination_path = Column(Text, nullable=True)
    channel = Column(String(64), index=True, nullable=False, default="Local Disk")
    application_name = Column(String(128), nullable=True)
    risk_level = Column(String(16), index=True, nullable=False, default="low")
    file_blob_id = Column(String(36), ForeignKey("dlp_file_blobs.id", ondelete="SET NULL"), index=True, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    event_metadata = Column("metadata", Text, nullable=True)

    endpoint = relationship("Device", back_populates="dlp_events")
    file_blob = relationship("DlpFileBlob", back_populates="events")

    def to_dict(self):
        parsed_meta = {}
        if self.event_metadata:
            try:
                parsed_meta = json.loads(self.event_metadata)
            except Exception:
                parsed_meta = {"raw": self.event_metadata}
        comp_name = self.endpoint.computer_name if self.endpoint else None
        has_blob = bool(self.file_blob_id and self.file_blob)

        raw_status = str(parsed_meta.get("evidence_status") or "").strip()
        legacy_map = {
            "CAPTURED": "CAPTURED",
            "archived": "CAPTURED",
            "NOT_CAPTURED_SIZE_LIMIT": "NOT_CAPTURED_SIZE_LIMIT",
            "exceeds_size_limit": "NOT_CAPTURED_SIZE_LIMIT",
            "NOT_CAPTURED_UNAVAILABLE": "NOT_CAPTURED_UNAVAILABLE",
            "locked_or_unavailable": "NOT_CAPTURED_UNAVAILABLE",
            "upload_failed": "NOT_CAPTURED_UNAVAILABLE",
            "NOT_CAPTURED_DISABLED": "NOT_CAPTURED_DISABLED",
            "disabled_by_policy": "NOT_CAPTURED_DISABLED",
            "NO_EVIDENCE": "NO_EVIDENCE",
            "not_applicable": "NO_EVIDENCE",
            "empty_file": "NO_EVIDENCE",
        }
        if has_blob:
            evidence_status = "CAPTURED"
        else:
            evidence_status = legacy_map.get(raw_status, "NO_EVIDENCE")
            if evidence_status == "CAPTURED":
                evidence_status = "NO_EVIDENCE"

        parsed_meta["evidence_status"] = evidence_status
        if not parsed_meta.get("file_path") and (self.destination_path or self.source_path):
            parsed_meta["file_path"] = self.destination_path or self.source_path

        return {
            "id": self.id,
            "endpoint_id": self.endpoint_id,
            "computer_name": comp_name,
            "employee_name": self.employee_name,
            "event_type": self.event_type,
            "file_name": self.file_name,
            "file_extension": self.file_extension or "",
            "source_path": self.source_path,
            "destination_path": self.destination_path,
            "channel": self.channel,
            "application_name": self.application_name,
            "risk_level": self.risk_level,
            "file_blob_id": self.file_blob_id,
            "has_content": has_blob,
            "evidence_status": evidence_status,
            "file_size": self.file_blob.file_size if has_blob else parsed_meta.get("file_size", 0),
            "download_url": f"/api/dlp/files/{self.file_blob_id}/download" if has_blob else None,
            "timestamp": (self.timestamp.isoformat() + "Z") if self.timestamp else None,
            "metadata": parsed_meta
        }
