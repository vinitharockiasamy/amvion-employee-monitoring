import sys
import ctypes
import mss
from PIL import Image

def init_windows_dpi_awareness():
    """Enable Per-Monitor DPI awareness on Windows to prevent blurry or cropped screen captures."""
    if sys.platform == "win32":
        try:
            # Per-monitor DPI aware (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                # System DPI aware fallback (Windows Vista+)
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

# Initialize DPI awareness immediately on import
init_windows_dpi_awareness()

class ScreenCapture:
    def __init__(self):
        try:
            self.sct = mss.mss()
        except Exception:
            self.sct = None

    def capture_primary(self) -> Image.Image:
        """Capture desktop screen using MSS with automatic PIL ImageGrab fallback."""
        if self.sct:
            try:
                # Use virtual screen (monitors[0]) or primary (monitors[1])
                monitor = self.sct.monitors[0]
                sct_img = self.sct.grab(monitor)
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            except Exception as e:
                # Fallback to PIL ImageGrab if MSS encounters GDI BitBlt restrictions
                pass

        try:
            from PIL import ImageGrab
            return ImageGrab.grab().convert("RGB")
        except Exception as e:
            # Emergency fallback: create informative diagnostic frame
            img = Image.new("RGB", (1280, 720), color=(30, 41, 59))
            return img

    def get_resolution_str(self) -> str:
        if self.sct and len(self.sct.monitors) > 0:
            mon = self.sct.monitors[0]
            return f"{mon['width']}x{mon['height']}"
        return "1920x1080"

    def close(self):
        if self.sct:
            self.sct.close()
