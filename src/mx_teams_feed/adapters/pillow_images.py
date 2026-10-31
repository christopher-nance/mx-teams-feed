"""ImageProcessor backed by Pillow (`pip install mx-teams-feed[images]`).

HEIC photos are decoded too when `pillow-heif` is installed (V3 already pins it).
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # optional HEIC support
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - depends on the host's environment
    pass

_QUALITIES = (85, 75, 65, 50)
# A 200 MP phone mode exists; anything bigger than this is refused before it is decoded.
DEFAULT_MAX_SOURCE_PIXELS = 60_000_000


class PillowImageProcessor:
    def __init__(self, max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS):
        self._max_source_pixels = max_source_pixels

    def fit(
        self, data: bytes, mime_type: str, max_pixels: int, max_bytes: int
    ) -> tuple[bytes, str] | None:
        try:
            with warnings.catch_warnings():
                # Oversized sources are refused by our own pixel cap below.
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                return self._fit(data, max_pixels, max_bytes)
        except Exception:  # DecompressionBombError, plugin ValueErrors, truncated files, …
            return None

    def _fit(self, data: bytes, max_pixels: int, max_bytes: int) -> tuple[bytes, str] | None:
        try:
            image = Image.open(io.BytesIO(data))
        except UnidentifiedImageError:
            return None
        width, height = image.size
        if width * height > self._max_source_pixels:
            return None
        if image.format == "JPEG":
            image.draft("RGB", (max_pixels, max_pixels))  # decode at reduced scale: less memory
        image.load()
        image = ImageOps.exif_transpose(image)
        image = _to_rgb(image)
        image.thumbnail((max_pixels, max_pixels), Image.Resampling.LANCZOS)
        for quality in _QUALITIES:
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True)
            if buffer.tell() <= max_bytes:
                return buffer.getvalue(), "image/jpeg"
        return None


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")
