import io

import pytest
from PIL import Image

from mx_teams_feed.adapters.pillow_images import PillowImageProcessor
from mx_teams_feed.media import collect_media, prepare_images

S3 = "https://maintainx-uploads-production.s3.us-west-2.amazonaws.com"


def _file(name, mime="image/jpeg"):
    return {"url": f"{S3}/{name}?X-Amz-Expires=3600&sig={name}", "mimeType": mime, "fileName": name}


class TestCollectMedia:
    def test_thumbnail_first_then_attachments(self):
        request = {"thumbnail": _file("a.jpg"), "attachments": [_file("b.jpg"), _file("c.jpg")]}
        assert [m.key for m in collect_media(request)] == ["/a.jpg", "/b.jpg", "/c.jpg"]

    def test_duplicates_by_object_key_are_dropped(self):
        request = {"thumbnail": _file("a.jpg"), "attachments": [_file("b.jpg")]}
        work_order = {"thumbnail": _file("a.jpg"), "attachments": [_file("b.jpg"), _file("d.jpg")]}
        assert [m.key for m in collect_media(request, work_order)] == ["/a.jpg", "/b.jpg", "/d.jpg"]

    def test_non_images_are_skipped(self):
        request = {"attachments": [_file("x.pdf", "application/pdf"), _file("y.png", "image/png")]}
        assert [m.key for m in collect_media(request)] == ["/y.png"]

    def test_missing_media_is_empty(self):
        assert collect_media({"thumbnail": None, "attachments": None}) == []


class _Processor:
    def __init__(self, fail_for=()):
        self.fail_for = fail_for

    def fit(self, data, mime_type, max_pixels, max_bytes):
        if data in self.fail_for:
            return None
        return b"small-" + data, "image/jpeg"


def _download_map(mapping):
    def download(url):
        name = url.split("/")[-1].split("?")[0]
        return mapping[name], "image/jpeg"

    return download


class TestPrepareImages:
    def test_processes_and_numbers_images(self):
        items = collect_media({"attachments": [_file("a.jpg"), _file("b.jpg")]})
        prepared = prepare_images(
            items, _download_map({"a.jpg": b"A", "b.jpg": b"B"}), _Processor()
        )
        assert [(i.temp_id, i.data) for i in prepared.images] == [
            ("1", b"small-A"),
            ("2", b"small-B"),
        ]
        assert prepared.extra == 0
        assert prepared.placeholder_html == (
            '<img src="../hostedContents/1/$value" alt="Photo 1"> '
            '<img src="../hostedContents/2/$value" alt="Photo 2">'
        )

    def test_caps_the_number_of_images(self):
        items = collect_media({"attachments": [_file(f"{n}.jpg") for n in "abcd"]})
        data = {f"{n}.jpg": n.encode() for n in "abcd"}
        prepared = prepare_images(items, _download_map(data), _Processor(), max_images=2)
        assert len(prepared.images) == 2
        assert prepared.extra == 2

    def test_unprocessable_image_counts_as_extra(self):
        items = collect_media({"attachments": [_file("a.jpg"), _file("b.jpg")]})
        processor = _Processor(fail_for={b"A"})
        prepared = prepare_images(items, _download_map({"a.jpg": b"A", "b.jpg": b"B"}), processor)
        assert [i.data for i in prepared.images] == [b"small-B"]
        assert prepared.images[0].temp_id == "1"
        assert prepared.extra == 1

    def test_without_a_processor_only_small_images_are_used(self):
        items = collect_media({"attachments": [_file("a.jpg"), _file("b.jpg")]})
        download = _download_map({"a.jpg": b"x" * 10, "b.jpg": b"x" * 100})
        prepared = prepare_images(items, download, None, max_bytes=50)
        assert len(prepared.images) == 1
        assert prepared.extra == 1


def _jpeg(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


class TestPillowProcessor:
    def test_downscales_large_photos_to_the_pixel_limit(self):
        data, mime = PillowImageProcessor().fit(_jpeg(4032, 3024), "image/jpeg", 1600, 3_500_000)
        assert mime == "image/jpeg"
        assert max(Image.open(io.BytesIO(data)).size) == 1600

    def test_converts_png_with_alpha_to_jpeg(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (800, 600), (0, 0, 255, 128)).save(buffer, "PNG")
        data, mime = PillowImageProcessor().fit(buffer.getvalue(), "image/png", 1600, 3_500_000)
        assert mime == "image/jpeg"
        assert Image.open(io.BytesIO(data)).mode == "RGB"

    def test_returns_none_when_it_cannot_fit(self):
        assert PillowImageProcessor().fit(_jpeg(400, 400), "image/jpeg", 1600, 10) is None

    def test_returns_none_for_non_images(self):
        assert PillowImageProcessor().fit(b"%PDF-1.7", "application/pdf", 1600, 3_500_000) is None


@pytest.mark.parametrize("orientation,expected", [(6, (300, 400)), (1, (400, 300))])
def test_pillow_applies_exif_rotation(orientation, expected):
    image = Image.new("RGB", (400, 300))
    exif = image.getexif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    data, _ = PillowImageProcessor().fit(buffer.getvalue(), "image/jpeg", 1600, 3_500_000)
    assert Image.open(io.BytesIO(data)).size == expected


class TestPerPhotoFailures:
    def test_a_photo_that_fails_to_download_counts_as_extra(self):
        from mx_teams_feed.errors import NotFound

        def download(url):
            if "a.jpg" in url:
                raise NotFound("gone")
            return b"B", "image/jpeg"

        items = collect_media({"attachments": [_file("a.jpg"), _file("b.jpg")]})
        prepared = prepare_images(items, download, _Processor())
        assert [i.data for i in prepared.images] == [b"small-B"]
        assert prepared.extra == 1

    def test_a_processor_crash_counts_as_extra(self):
        class Crashing:
            def fit(self, *args):
                raise ValueError("bad file")

        items = collect_media({"attachments": [_file("a.jpg")]})
        prepared = prepare_images(items, _download_map({"a.jpg": b"A"}), Crashing())
        assert (prepared.images, prepared.extra) == ([], 1)


def test_pillow_refuses_decompression_bombs_instead_of_raising():
    # ~200 MP in a tiny file: Pillow raises DecompressionBombError (not an OSError).
    buffer = io.BytesIO()
    Image.new("1", (16320, 12240)).save(buffer, "PNG")
    assert PillowImageProcessor().fit(buffer.getvalue(), "image/png", 1600, 3_500_000) is None


def test_pillow_refuses_very_large_sources_before_decoding():
    buffer = io.BytesIO()
    Image.new("1", (10000, 9000)).save(buffer, "PNG")  # 90 MP: under Pillow's limit
    processor = PillowImageProcessor(max_source_pixels=50_000_000)
    assert processor.fit(buffer.getvalue(), "image/png", 1600, 3_500_000) is None


class TestTransientDownloads:
    def _items(self):
        return collect_media({"attachments": [_file("a.jpg")]})

    def _failing(self, status):
        from mx_teams_feed.errors import TransientError

        def download(url):
            raise TransientError("s3", status=status)

        return download

    def test_s3_outage_retries_the_post_rather_than_dropping_the_photo(self):
        from mx_teams_feed.errors import TransientError

        with pytest.raises(TransientError):
            prepare_images(self._items(), self._failing(503), _Processor())

    def test_timeout_retries_the_post(self):
        from mx_teams_feed.errors import TransientError

        with pytest.raises(TransientError):
            prepare_images(self._items(), self._failing(None), _Processor())

    def test_refused_object_is_skipped(self):
        # S3 answers 403 for a deleted object too; that will never get better.
        prepared = prepare_images(self._items(), self._failing(403), _Processor())
        assert (prepared.images, prepared.extra) == ([], 1)
