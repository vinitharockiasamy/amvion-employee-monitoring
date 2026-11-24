import io
import os
import sys
import zipfile
import tempfile
import unittest
from pathlib import Path

os.environ["TESTING"] = "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.app.core.config import settings
from server.app.db.models import Base
from server.app.db.session import get_db
from server.app.main import app


def _build_minimal_docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
            "</w:document>"
        )
        zf.writestr("word/document.xml", xml.encode("utf-8"))
    return buf.getvalue()


def _build_minimal_xlsx(rows: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        row_xml_parts = []
        for idx, row in enumerate(rows, start=1):
            cells = "".join(
                f'<c t="inlineStr"><is><t>{val}</t></is></c>'
                for val in row
            )
            row_xml_parts.append(f'<row r="{idx}">{cells}</row>')
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(row_xml_parts)}</sheetData>"
            "</worksheet>"
        )
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
    return buf.getvalue()


class TestDlpPhase1B(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(cls.temp_dir.name)
        cls.vault_dir = temp_root / "dlp_vault"
        cls.vault_dir.mkdir(parents=True, exist_ok=True)
        settings.DLP_VAULT_DIR = cls.vault_dir

        db_file = temp_root / "test_dlp_1b.db"
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

        r = cls.client.post(
            "/api/v1/auth/enroll",
            headers={"X-Enrollment-Key": settings.ORG_ENROLLMENT_KEY},
            json={
                "hardware_uuid": "hw-dlp-1b-001",
                "computer_name": "DESIGN-STUDIO-01",
                "employee_name": "Priya Jewels",
                "os_version": "Windows 11"
            }
        )
        assert r.status_code == 200
        d = r.json()
        cls.dev_id = d["device_id"]
        cls.dev_headers = {
            "X-Device-Id": cls.dev_id,
            "Authorization": f"Bearer {d['device_secret']}"
        }

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()
        cls.temp_dir.cleanup()

    def test_01_dashboard_renders_dlp_tab_and_preview_modal(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("DLP Audit & File Viewer", html)
        self.assertIn("dlpPreviewModal", html)
        self.assertIn("dlpEventsTableBody", html)

    def test_02_in_browser_preview_across_formats(self):
        # 1. Word .docx preview
        docx_bytes = _build_minimal_docx("Confidential Diamond Bangle Spec #881")
        r_docx = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("bangle_spec.docx", io.BytesIO(docx_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        ).json()
        p_docx = self.client.get(f"/api/dlp/files/{r_docx['file_id']}/preview", headers=self.admin_headers).json()
        self.assertEqual(p_docx["kind"], "document")
        self.assertIn("Confidential Diamond Bangle Spec #881", p_docx["content"])

        # 2. Excel .xlsx preview
        xlsx_bytes = _build_minimal_xlsx([["ClientName", "TicketCode"], ["VIP Corp", "TK-9921"]])
        r_xlsx = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("tickets.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        ).json()
        p_xlsx = self.client.get(f"/api/dlp/files/{r_xlsx['file_id']}/preview", headers=self.admin_headers).json()
        self.assertEqual(p_xlsx["kind"], "table")
        self.assertEqual(p_xlsx["rows"][1], ["VIP Corp", "TK-9921"])

        # 3. Image .png preview & inline raw serving with query token
        png_stub = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        r_png = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("sketch.png", io.BytesIO(png_stub), "image/png")}
        ).json()
        p_png = self.client.get(f"/api/dlp/files/{r_png['file_id']}/preview", headers=self.admin_headers).json()
        self.assertEqual(p_png["kind"], "image")

        # Unauthenticated /raw rejected; ?token= accepted for inline <img>/<iframe src>
        self.assertEqual(self.client.get(f"/api/dlp/files/{r_png['file_id']}/raw").status_code, 401)
        r_raw = self.client.get(f"/api/dlp/files/{r_png['file_id']}/raw?token={settings.ADMIN_AUTH_TOKEN}")
        self.assertEqual(r_raw.status_code, 200)
        self.assertEqual(r_raw.content, png_stub)
        self.assertIn("inline", r_raw.headers.get("content-disposition", ""))

        # 4. Binary CAD .3dm fallback preview
        cad_stub = b"\x00\x01\x02RHINO_3DM_BINARY_CAD_MODEL"
        r_cad = self.client.post(
            "/api/dlp/files",
            headers=self.dev_headers,
            files={"file": ("emerald_ring.3dm", io.BytesIO(cad_stub), "application/octet-stream")}
        ).json()
        p_cad = self.client.get(f"/api/dlp/files/{r_cad['file_id']}/preview", headers=self.admin_headers).json()
        self.assertEqual(p_cad["kind"], "binary")

    def test_03_events_summary_and_search_query(self):
        self.client.post(
            "/api/dlp/events",
            headers=self.dev_headers,
            json={
                "event_type": "DELETED",
                "file_name": "gold_pendant_v2.3dm",
                "source_path": r"C:\CAD\gold_pendant_v2.3dm",
                "channel": "Local Disk",
                "risk_level": "high"
            }
        )
        res = self.client.get(
            "/api/dlp/events",
            headers=self.admin_headers,
            params={"search": "gold_pendant"}
        ).json()
        self.assertEqual(res["total"], 1)
        self.assertGreaterEqual(res["summary"]["deleted_files_count"], 1)
        self.assertGreaterEqual(res["summary"]["high_risk_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
