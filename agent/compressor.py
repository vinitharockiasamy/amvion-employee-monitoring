import io
from PIL import Image

class ImageCompressor:
    def __init__(self, target_width: int = 1280, quality: int = 70):
        self.target_width = target_width
        self.quality = quality

    def compress_for_upload(self, image: Image.Image) -> bytes:
        """Resize and compress PIL Image to JPEG bytes for screenshot upload."""
        w, h = image.size
        if w > self.target_width:
            target_height = int(h * (self.target_width / w))
            resized = image.resize((self.target_width, target_height), Image.Resampling.BILINEAR)
        else:
            resized = image

        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=self.quality, optimize=False)
        return buffer.getvalue()

    def compress_for_stream(self, image: Image.Image, quality: int = 55) -> bytes:
        """Fast JPEG compression optimized for 15 FPS live streaming."""
        w, h = image.size
        # Downscale slightly more for live streaming low-latency transfer (e.g. 1024 or 1280)
        max_w = 1024
        if w > max_w:
            target_h = int(h * (max_w / w))
            resized = image.resize((max_w, target_h), Image.Resampling.NEAREST)
        else:
            resized = image

        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()
