"""Choose a request's photos and turn them into inline Graph hosted contents.

MaintainX file URLs are presigned S3 links that expire after 60 minutes, so photos are
downloaded in the same run that fetched the record and are never stored. Graph caps each
hosted content at 4 MB and ~20% of store phone photos are bigger, so they are downscaled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import PauseFeed, TransientError
from .graph import HostedImage
from .ports import ImageProcessor

log = logging.getLogger(__name__)

DEFAULT_MAX_IMAGES = 6
DEFAULT_MAX_PIXELS = 1600
DEFAULT_MAX_BYTES = 3_500_000  # headroom under Graph's 4 MB per hosted content


@dataclass(frozen=True)
class MediaItem:
    url: str
    mime_type: str
    key: str  # S3 object path: the same photo carries the same key on request and work order


@dataclass(frozen=True)
class PreparedImages:
    images: list[HostedImage]
    extra: int  # photos not shown inline (over the cap or unprocessable)

    @property
    def placeholder_html(self) -> str:
        return " ".join(
            f'<img src="../hostedContents/{image.temp_id}/$value" alt="Photo {image.temp_id}">'
            for image in self.images
        )


def collect_media(*records: dict[str, Any] | None) -> list[MediaItem]:
    """Images from each record's thumbnail (the first photo, stored separately) and
    attachments, in order, without duplicates."""
    seen: set[str] = set()
    items: list[MediaItem] = []
    for record in records:
        if not record:
            continue
        for file in _files(record):
            url = file.get("url")
            mime = file.get("mimeType") or ""
            if not url or not mime.startswith("image/"):
                continue
            key = urlsplit(url).path
            if key in seen:
                continue
            seen.add(key)
            items.append(MediaItem(url=url, mime_type=mime, key=key))
    return items


def prepare_images(
    items: Iterable[MediaItem],
    download: Callable[[str], tuple[bytes, str]],
    processor: ImageProcessor | None,
    *,
    max_images: int = DEFAULT_MAX_IMAGES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PreparedImages:
    images: list[HostedImage] = []
    extra = 0
    for item in items:
        if len(images) >= max_images:
            extra += 1
            continue
        # One bad photo (deleted file, corrupt or enormous image) must never block the post.
        try:
            data, mime = download(item.url)
            if processor is not None:
                fitted = processor.fit(data, mime or item.mime_type, max_pixels, max_bytes)
            else:
                fitted = (data, mime or item.mime_type) if len(data) <= max_bytes else None
        except PauseFeed:
            raise
        except TransientError as exc:
            if exc.status is None or exc.status == 429 or exc.status >= 500:
                raise  # an S3 hiccup: retry the whole post later with fresh URLs
            log.warning("maintenance feed skipped a photo: S3 answered %s", exc.status)
            fitted = None
        except Exception as exc:
            log.warning("maintenance feed skipped a photo: %s", type(exc).__name__)
            fitted = None
        if fitted is None:
            extra += 1
            continue
        images.append(HostedImage(str(len(images) + 1), fitted[0], fitted[1]))
    return PreparedImages(images=images, extra=extra)


def _files(record: dict[str, Any]) -> list[dict[str, Any]]:
    files = []
    if isinstance(record.get("thumbnail"), dict):
        files.append(record["thumbnail"])
    files.extend(f for f in record.get("attachments") or [] if isinstance(f, dict))
    return files
