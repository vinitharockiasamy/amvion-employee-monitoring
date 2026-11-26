import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
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
