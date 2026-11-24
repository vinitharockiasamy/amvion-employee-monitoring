import os
import io
import re
import csv
import json
import uuid
import hashlib
import zipfile
import mimetypes
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from server.app.core.config import settings
from server.app.core.security import authenticate_admin, authenticate_device_or_admin
from server.app.db.session import get_db
from server.app.db.models import Device, DlpFileBlob, DlpEvent
from server.app.api.websocket import manager

router = APIRouter(prefix="/api/dlp", tags=["Enterprise DLP Audit"])

ALLOWED_RISK_LEVELS = {"low", "medium", "high", "critical"}
ALLOWED_EVENT_TYPES = {
    "CREATED", "MODIFIED", "MOVED", "RENAMED", "COPIED",
    "DELETED", "WEB_UPLOAD", "USB_TRANSFER", "DRIVE_CONNECTED", "DRIVE_DISCONNECTED"
}
DISALLOWED_CONTENT_TYPES = {
    "application/x-msdownload",
    "application/x-dosexec"
}
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ()]")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".mov"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac"}
TEXT_EXTS = {
    ".txt", ".md", ".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".log", ".py", ".js", ".ts", ".html", ".htm", ".css", ".sql", ".bat", ".ps1",
    ".vbs", ".sh", ".toml", ".dxf", ".obj"
}


def _extract_docx_text(file_path: Path, max_chars: int = 100000) -> str:
    paragraphs = []
    with zipfile.ZipFile(file_path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return "(Unable to locate word/document.xml inside .docx archive)"
        root = ET.fromstring(zf.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for p in root.findall(".//w:p", ns):
            runs = [t.text for t in p.findall(".//w:t", ns) if t.text]
            if runs:
                paragraphs.append("".join(runs))
    return "\n\n".join(paragraphs)[:max_chars]


def _extract_xlsx_rows(file_path: Path, max_rows: int = 200) -> dict:
    with zipfile.ZipFile(file_path, "r") as zf:
        shared_strings = []
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall("s:si", ns):
                parts = [t.text for t in si.findall(".//s:t", ns) if t.text]
                shared_strings.append("".join(parts))

        sheets = sorted(
            n for n in zf.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        if not sheets:
            return {"sheet": "Sheet1", "rows": []}

        ws_root = ET.fromstring(zf.read(sheets[0]))
        rows_out = []
        for row_el in ws_root.findall(".//s:sheetData/s:row", ns):
            if len(rows_out) >= max_rows:
                break
            vals = []
            for c_el in row_el.findall("s:c", ns):
                c_type = c_el.attrib.get("t", "")
                v_el = c_el.find("s:v", ns)
                is_el = c_el.find("s:is", ns)
                cell_val = ""
                if c_type == "s" and v_el is not None and v_el.text is not None:
                    idx = int(v_el.text)
                    cell_val = shared_strings[idx] if 0 <= idx < len(shared_strings) else v_el.text
                elif c_type == "inlineStr" and is_el is not None:
                    cell_val = "".join(t.text for t in is_el.findall(".//s:t", ns) if t.text)
                elif v_el is not None and v_el.text is not None:
                    cell_val = v_el.text
                vals.append(cell_val)
            if any(str(x).strip() for x in vals):
                rows_out.append(vals)
        return {"sheet": Path(sheets[0]).stem, "rows": rows_out}


def sanitize_filename(filename: str) -> str:
    """Strip directory traversal components and unsafe control characters."""
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    cleaned = SAFE_FILENAME_RE.sub("_", base)
    cleaned = cleaned.lstrip(".")
    return (cleaned[:240] or "archived_file.bin")


def resolve_safe_vault_path(sha256_hex: str, ext: str) -> Path:
    """Construct a path strictly inside settings.DLP_VAULT_DIR and verify boundary."""
    vault_root = settings.DLP_VAULT_DIR.resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    clean_ext = re.sub(r"[^a-z0-9.]", "", (ext or "").lower())
    if not clean_ext.startswith(".") or len(clean_ext) > 12 or clean_ext == ".":
        clean_ext = ".bin"
    candidate = (vault_root / f"{sha256_hex}{clean_ext}").resolve()
    if candidate.parent != vault_root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid storage path")
    return candidate


class DlpEventCreateRequest(BaseModel):
    idempotency_key: Optional[str] = Field(None, max_length=128)
    endpoint_id: Optional[str] = Field(None, max_length=36)
    employee_name: Optional[str] = Field(None, max_length=128)
    event_type: str = Field(..., min_length=2, max_length=48)
    file_name: str = Field(..., min_length=1, max_length=256)
    file_extension: Optional[str] = Field(None, max_length=32)
    source_path: Optional[str] = Field(None, max_length=2048)
    destination_path: Optional[str] = Field(None, max_length=2048)
    channel: str = Field("Local Disk", min_length=1, max_length=64)
    application_name: Optional[str] = Field(None, max_length=128)
    risk_level: str = Field("low", min_length=2, max_length=16)
    file_blob_id: Optional[str] = Field(None, max_length=36)
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unsupported event_type '{v}'. Allowed: {sorted(ALLOWED_EVENT_TYPES)}")
        return norm

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v: str) -> str:
        norm = v.strip().lower()
        if norm not in ALLOWED_RISK_LEVELS:
            raise ValueError(f"Unsupported risk_level '{v}'. Allowed: {sorted(ALLOWED_RISK_LEVELS)}")
        return norm


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_dlp_file(
    file: UploadFile = File(...),
    expected_sha256: Optional[str] = Form(None),
    caller_device: Optional[Device] = Depends(authenticate_device_or_admin),
    db: Session = Depends(get_db)
):
    """
    Archive one file up to 25 MB into the SHA-256 deduplicated DLP vault.
    Reuses existing DlpFileBlob when an identical SHA-256 already exists.
    """
    safe_name = sanitize_filename(file.filename or "document.bin")
    ext = Path(safe_name).suffix.lower()[:32]

    declared_type = (file.content_type or "").strip().lower()
    if declared_type in DISALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Content type '{declared_type}' is not permitted in DLP archive"
        )

    # Stream-read in chunks enforcing 25 MB hard ceiling without unbounded memory growth
    max_bytes = settings.MAX_DLP_FILE_BYTES
    hasher = hashlib.sha256()
    chunks = []
    total_bytes = 0

    while True:
        chunk = await file.read(1024 * 1024)  # 1 MB chunks
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB"
            )
        hasher.update(chunk)
        chunks.append(chunk)

    if total_bytes == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file uploads are not allowed")

    computed_sha256 = hasher.hexdigest()

    if expected_sha256:
        norm_expected = expected_sha256.strip().lower()
        if not SHA256_HEX_RE.match(norm_expected):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid expected_sha256 format")
        if norm_expected != computed_sha256:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="SHA-256 integrity check failed: uploaded content does not match expected_sha256"
            )

    existing_blob = db.query(DlpFileBlob).filter(DlpFileBlob.sha256 == computed_sha256).first()
    if existing_blob and os.path.exists(existing_blob.storage_path):
        return {
            "file_id": existing_blob.id,
            "sha256": existing_blob.sha256,
            "deduplicated": True,
            "file_size": existing_blob.file_size,
            "content_type": existing_blob.content_type,
            "original_filename": existing_blob.original_filename
        }

    vault_path = resolve_safe_vault_path(computed_sha256, ext)
    with open(vault_path, "wb") as out_f:
        for c in chunks:
            out_f.write(c)

    guessed_mime, _ = mimetypes.guess_type(safe_name)
    final_content_type = declared_type if (declared_type and declared_type != "application/octet-stream") else (guessed_mime or "application/octet-stream")

    if existing_blob:
        existing_blob.storage_path = str(vault_path)
        existing_blob.file_size = total_bytes
        db.commit()
        db.refresh(existing_blob)
        blob = existing_blob
        deduplicated = True
    else:
        blob = DlpFileBlob(
            id=str(uuid.uuid4()),
            sha256=computed_sha256,
            original_filename=safe_name,
            content_type=final_content_type[:128],
            file_size=total_bytes,
            storage_path=str(vault_path),
            created_at=datetime.utcnow()
        )
        db.add(blob)
        db.commit()
        db.refresh(blob)
        deduplicated = False

    return {
        "file_id": blob.id,
        "sha256": blob.sha256,
        "deduplicated": deduplicated,
        "file_size": blob.file_size,
        "content_type": blob.content_type,
        "original_filename": blob.original_filename
    }


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def create_dlp_event(
    payload: DlpEventCreateRequest,
    caller_device: Optional[Device] = Depends(authenticate_device_or_admin),
    db: Session = Depends(get_db)
):
    """
    Validate, store, and broadcast a DLP audit event.
    Enforces authorization so an authenticated device can only submit events for its own endpoint_id.
    """
    if caller_device is not None:
        if payload.endpoint_id and payload.endpoint_id != caller_device.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Device cannot submit DLP events for a different endpoint_id"
            )
        endpoint = caller_device
    else:
        if not payload.endpoint_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="endpoint_id is required when creating events with admin credentials"
            )
        endpoint = db.query(Device).filter(Device.id == payload.endpoint_id).first()
        if not endpoint:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint device not found")

    blob: Optional[DlpFileBlob] = None
    if payload.file_blob_id:
        blob = db.query(DlpFileBlob).filter(DlpFileBlob.id == payload.file_blob_id).first()
        if not blob:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Referenced file_blob_id not found")

    safe_file_name = sanitize_filename(payload.file_name)
    ext = (payload.file_extension or Path(safe_file_name).suffix or "").lower()[:32]
    emp_name = (payload.employee_name or endpoint.employee_name or "Unknown").strip()[:128]

    meta_dict: Dict[str, Any] = dict(payload.metadata) if payload.metadata else {}
    if payload.idempotency_key:
        idem_key = payload.idempotency_key.strip()
        meta_dict["idempotency_key"] = idem_key
        # Idempotency check: prevent duplicate creation on network retry
        recent_candidates = (
            db.query(DlpEvent)
            .filter(
                DlpEvent.endpoint_id == endpoint.id,
                DlpEvent.event_type == payload.event_type,
                DlpEvent.file_name == safe_file_name
            )
            .order_by(DlpEvent.timestamp.desc())
            .limit(25)
            .all()
        )
        for existing_ev in recent_candidates:
            if existing_ev.event_metadata and f'"idempotency_key": "{idem_key}"' in existing_ev.event_metadata:
                return existing_ev.to_dict()

    event_time = payload.timestamp.replace(tzinfo=None) if payload.timestamp else datetime.utcnow()
    serialized_meta = json.dumps(meta_dict) if meta_dict else None

    dlp_event = DlpEvent(
        id=str(uuid.uuid4()),
        endpoint_id=endpoint.id,
        employee_name=emp_name,
        event_type=payload.event_type,
        file_name=safe_file_name,
        file_extension=ext,
        source_path=payload.source_path,
        destination_path=payload.destination_path,
        channel=payload.channel.strip(),
        application_name=payload.application_name.strip() if payload.application_name else None,
        risk_level=payload.risk_level,
        file_blob_id=blob.id if blob else None,
        timestamp=event_time,
        event_metadata=serialized_meta
    )
    db.add(dlp_event)

    endpoint.last_seen_at = datetime.utcnow()
    endpoint.status = "online"
    db.commit()
    db.refresh(dlp_event)

    event_dict = dlp_event.to_dict()
    await manager.broadcast_dlp_event(event_dict)

    return event_dict


@router.get("/events")
def list_dlp_events(
    endpoint: Optional[str] = Query(None, description="Filter by endpoint_id or computer_name"),
    event_type: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search filename, source/destination path, or application"),
    start_date: Optional[datetime] = Query(None, description="ISO-8601 start timestamp (inclusive)"),
    end_date: Optional[datetime] = Query(None, description="ISO-8601 end timestamp (inclusive)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: bool = Depends(authenticate_admin),
    db: Session = Depends(get_db)
):
    """Query DLP audit events with filtering by endpoint, event_type, channel, risk_level, search, and date range."""
    q = db.query(DlpEvent).options(joinedload(DlpEvent.endpoint), joinedload(DlpEvent.file_blob))

    if endpoint and endpoint != "all":
        q = q.join(Device, DlpEvent.endpoint_id == Device.id).filter(
            (DlpEvent.endpoint_id == endpoint) | (Device.computer_name.ilike(f"%{endpoint}%"))
        )
    if event_type and event_type != "all":
        q = q.filter(DlpEvent.event_type == event_type.strip().upper())
    if channel and channel != "all":
        ch_val = channel.strip()
        if ch_val in ("USB/removable_storage", "USB / Removable Storage"):
            q = q.filter(DlpEvent.channel.in_(["USB/removable_storage", "USB / Removable Storage"]))
        else:
            q = q.filter(DlpEvent.channel == ch_val)
    if risk_level and risk_level != "all":
        q = q.filter(DlpEvent.risk_level == risk_level.strip().lower())
    if search and search.strip():
        like_term = f"%{search.strip()}%"
        q = q.filter(
            (DlpEvent.file_name.ilike(like_term)) |
            (DlpEvent.source_path.ilike(like_term)) |
            (DlpEvent.destination_path.ilike(like_term)) |
            (DlpEvent.application_name.ilike(like_term)) |
            (DlpEvent.employee_name.ilike(like_term))
        )
    if start_date:
        q = q.filter(DlpEvent.timestamp >= start_date.replace(tzinfo=None))
    if end_date:
        q = q.filter(DlpEvent.timestamp <= end_date.replace(tzinfo=None))

    total = q.count()
    events = q.order_by(DlpEvent.timestamp.desc()).offset(offset).limit(limit).all()

    summary = {
        "total_events": db.query(DlpEvent).count(),
        "high_risk_count": db.query(DlpEvent).filter(DlpEvent.risk_level.in_(["high", "critical"])).count(),
        "archived_files_count": db.query(DlpFileBlob).count(),
        "external_transfers_count": db.query(DlpEvent).filter(
            DlpEvent.channel.in_([
                "USB/removable_storage",
                "External Drive",
                "Web Upload",
                "Messaging/App",
                "Bluetooth/Phone"
            ])
        ).count(),
        "deleted_files_count": db.query(DlpEvent).filter(DlpEvent.event_type == "DELETED").count()
    }

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
        "events": [e.to_dict() for e in events]
    }


@router.get("/files/{file_id}")
def get_dlp_file_metadata(
    file_id: str,
    _admin: bool = Depends(authenticate_admin),
    db: Session = Depends(get_db)
):
    """Return metadata for an archived DLP file blob."""
    blob = db.query(DlpFileBlob).filter(DlpFileBlob.id == file_id).first()
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file not found")
    return blob.to_dict()


@router.get("/files/{file_id}/download")
def download_dlp_file(
    file_id: str,
    _admin: bool = Depends(authenticate_admin),
    db: Session = Depends(get_db)
):
    """Authenticated download of the original archived file with SHA-256 integrity verification."""
    blob = db.query(DlpFileBlob).filter(DlpFileBlob.id == file_id).first()
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file not found")

    resolved_path = Path(blob.storage_path).resolve()
    vault_root = settings.DLP_VAULT_DIR.resolve()
    if resolved_path.parent != vault_root or not resolved_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file payload missing from vault")

    # Verify SHA-256 integrity prior to serving
    hasher = hashlib.sha256()
    with open(resolved_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    if hasher.hexdigest() != blob.sha256:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored file failed SHA-256 integrity verification"
        )

    safe_download_name = sanitize_filename(blob.original_filename)
    return FileResponse(
        path=str(resolved_path),
        media_type=blob.content_type or "application/octet-stream",
        filename=safe_download_name,
        headers={"X-Content-SHA256": blob.sha256}
    )


@router.get("/files/{file_id}/raw")
def serve_dlp_file_inline(
    file_id: str,
    _admin: bool = Depends(authenticate_admin),
    db: Session = Depends(get_db)
):
    """Authenticated inline file stream for browser previewing (images, PDFs, media)."""
    blob = db.query(DlpFileBlob).filter(DlpFileBlob.id == file_id).first()
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file not found")

    resolved_path = Path(blob.storage_path).resolve()
    vault_root = settings.DLP_VAULT_DIR.resolve()
    if resolved_path.parent != vault_root or not resolved_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file payload missing from vault")

    safe_name = sanitize_filename(blob.original_filename)
    guessed_mime, _ = mimetypes.guess_type(safe_name)
    media = blob.content_type if (blob.content_type and blob.content_type != "application/octet-stream") else (guessed_mime or "application/octet-stream")
    return FileResponse(
        path=str(resolved_path),
        media_type=media,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "X-Content-SHA256": blob.sha256
        }
    )


@router.get("/files/{file_id}/preview")
def preview_dlp_file(
    file_id: str,
    _admin: bool = Depends(authenticate_admin),
    db: Session = Depends(get_db)
):
    """Return structured in-browser preview payload for images, PDFs, Word/Excel, CSV, ZIP, text, or CAD/binary files."""
    blob = db.query(DlpFileBlob).filter(DlpFileBlob.id == file_id).first()
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file not found")

    resolved_path = Path(blob.storage_path).resolve()
    vault_root = settings.DLP_VAULT_DIR.resolve()
    if resolved_path.parent != vault_root or not resolved_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archived file payload missing from vault")

    ext = Path(blob.original_filename).suffix.lower()
    base_info = {
        "id": blob.id,
        "original_filename": blob.original_filename,
        "extension": ext,
        "content_type": blob.content_type,
        "file_size": blob.file_size,
        "sha256": blob.sha256,
        "raw_url": f"/api/dlp/files/{blob.id}/raw",
        "download_url": f"/api/dlp/files/{blob.id}/download"
    }

    if ext in IMAGE_EXTS:
        return {**base_info, "kind": "image"}
    if ext == ".pdf":
        return {**base_info, "kind": "pdf"}
    if ext in VIDEO_EXTS:
        return {**base_info, "kind": "video"}
    if ext in AUDIO_EXTS:
        return {**base_info, "kind": "audio"}

    if ext in (".csv", ".tsv"):
        try:
            raw_bytes = resolved_path.read_bytes()[:512 * 1024]
            text = raw_bytes.decode("utf-8-sig", errors="replace")
            delim = "\t" if ext == ".tsv" else ","
            rows = []
            for idx, r in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
                if idx >= 200:
                    break
                rows.append(r)
            return {**base_info, "kind": "table", "sheet": blob.original_filename, "rows": rows}
        except Exception as e:
            return {**base_info, "kind": "text", "content": f"CSV preview notice: {e}"}

    if ext == ".xlsx":
        try:
            return {**base_info, "kind": "table", **_extract_xlsx_rows(resolved_path)}
        except Exception as e:
            return {**base_info, "kind": "binary", "note": f"Spreadsheet preview note: {e}"}

    if ext == ".docx":
        try:
            doc_text = _extract_docx_text(resolved_path)
            return {**base_info, "kind": "document", "content": doc_text or "(Empty Word document)"}
        except Exception as e:
            return {**base_info, "kind": "binary", "note": f"Document preview note: {e}"}

    if ext == ".zip":
        try:
            with zipfile.ZipFile(resolved_path, "r") as zf:
                entries = [
                    {"name": zi.filename, "size": zi.file_size, "compressed_size": zi.compress_size}
                    for zi in zf.infolist()[:300]
                ]
            return {**base_info, "kind": "archive", "entries": entries}
        except Exception as e:
            return {**base_info, "kind": "binary", "note": f"Archive preview note: {e}"}

    try:
        with open(resolved_path, "rb") as f:
            sample = f.read(256 * 1024)
        if ext in TEXT_EXTS or (b"\x00" not in sample[:4096] and len(sample) > 0):
            return {
                **base_info,
                "kind": "text",
                "content": sample.decode("utf-8-sig", errors="replace"),
                "truncated": blob.file_size > len(sample)
            }
    except Exception:
        pass

    return {
        **base_info,
        "kind": "binary",
        "note": "Binary or proprietary CAD/design file. Click 'Download Original File' to inspect in its native application."
    }
