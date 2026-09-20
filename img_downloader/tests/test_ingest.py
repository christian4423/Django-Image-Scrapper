"""
Tests for img_downloader.ingest.

These drive the real fetch_page and download_image over the fake network from
fakes.py, rather than stubbing them out, so the seam between the two modules
is covered too — a 404 on one image has to travel all the way from requests,
through fetching's error translation, to a skipped image and a search that
still completes.
"""
import hashlib
import logging
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from img_downloader import ingest
from img_downloader.fetching import FetchingError
from img_downloader.ingest import ingest_url
from img_downloader.models import ImageAsset, Search, SearchImage

from .fakes import (
    SVG,
    FakeNetworkMixin,
    FakeResponse,
    html_page,
    png,
    png_response,
    robots,
)

PAGE = "https://example.com/"
ROBOTS = "https://example.com/robots.txt"


def image_url(name):
    return f"https://example.com/{name}"


class IngestTestCase(FakeNetworkMixin, TestCase):
    """Shared setup: a scratch MEDIA_ROOT, a quiet logger, and a served page."""

    def setUp(self):
        super().setUp()

        media_root = tempfile.mkdtemp(prefix="img-downloader-tests-")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        overridden = override_settings(MEDIA_ROOT=media_root)
        overridden.enable()
        self.addCleanup(overridden.disable)
        self.media_root = Path(media_root)

        # Skips are expected in most of these tests; a handler here keeps
        # logging's last-resort printer off the test output. assertLogs still
        # works, because it swaps the handler list out for its own.
        handler = logging.NullHandler()
        ingest.logger.addHandler(handler)
        self.addCleanup(ingest.logger.removeHandler, handler)

    def serve_page(self, images, title="Test Page"):
        """`images` maps URL to response (or exception). Returns the page URL."""
        self.serve({
            ROBOTS: robots(),
            PAGE: FakeResponse(200, {"Content-Type": "text/html"}, html_page(images, title)),
            **images,
        })
        return PAGE

    def stored_urls(self, search):
        return [si.source_url for si in search.images.all()]

    def positions(self, search):
        return [si.position for si in search.images.all()]


class HappyPathTests(IngestTestCase):
    def test_links_each_image_to_the_search(self):
        self.serve_page({
            image_url("a.png"): png_response(1),
            image_url("b.png"): png_response(2),
        }, title="Two Cats")

        search = ingest_url(PAGE)

        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(search.page_title, "Two Cats")
        self.assertEqual(self.stored_urls(search), [image_url("a.png"), image_url("b.png")])
        self.assertEqual(self.positions(search), [0, 1])

    def test_stores_each_asset_under_its_own_hash(self):
        body = png(3)
        digest = hashlib.sha256(body).hexdigest()
        self.serve_page({image_url("a.png"): FakeResponse(200, {"Content-Type": "image/png"}, body)})

        ingest_url(PAGE)

        asset = ImageAsset.objects.get(sha256=digest)
        self.assertEqual(asset.file.name, f"assets/{digest[0:2]}/{digest[2:4]}/{digest}.png")
        self.assertEqual(asset.byte_size, len(body))
        self.assertEqual((asset.width, asset.height), (8, 8))

    def test_reuses_an_asset_across_searches(self):
        """Content addressing: the same image on two pages is stored once."""
        self.serve_page({image_url("a.png"): png_response(4)})
        first = ingest_url(PAGE)

        self.serve_page({image_url("a.png"): png_response(4)})
        second = ingest_url(PAGE)

        self.assertEqual(ImageAsset.objects.count(), 1)
        self.assertEqual(first.images.count(), 1)
        self.assertEqual(second.images.count(), 1)
        self.assertEqual(SearchImage.objects.count(), 2)

    def test_deduplicates_identical_bytes_on_one_page(self):
        """A logo in the header and again in the footer is one row, not two."""
        self.serve_page({
            image_url("logo.png"): png_response(5),
            image_url("middle.png"): png_response(6),
            image_url("logo-again.png"): png_response(5),
        })

        search = ingest_url(PAGE)

        self.assertEqual(self.stored_urls(search), [image_url("logo.png"), image_url("middle.png")])
        self.assertEqual(search.images_duplicate, 1)

    def test_reclaims_an_orphaned_file_sitting_at_the_hash_path(self):
        """
        A rolled-back ingest can leave a file behind with no database row.
        Storing those bytes again must reuse that exact path rather than
        sidestep it with a suffixed copy, or the filename stops being the hash.
        """
        body = png(7)
        digest = hashlib.sha256(body).hexdigest()
        expected = f"assets/{digest[0:2]}/{digest[2:4]}/{digest}.png"

        orphan = self.media_root / expected
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(body)

        self.serve_page({image_url("a.png"): FakeResponse(200, {"Content-Type": "image/png"}, body)})
        ingest_url(PAGE)

        self.assertEqual(ImageAsset.objects.get(sha256=digest).file.name, expected)
        on_disk = [str(p.relative_to(self.media_root))
                   for p in self.media_root.rglob("*") if p.is_file()]
        self.assertEqual(on_disk, [expected], "a second copy was written")


class PartialFailureTests(IngestTestCase):
    """A page is worth keeping even when some of its images are not."""

    def test_a_404_on_one_image_does_not_sink_the_search(self):
        self.serve_page({
            image_url("ok.png"): png_response(8),
            image_url("gone.png"): FakeResponse(404, {"Content-Type": "image/png"}),
        })

        search = ingest_url(PAGE)

        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(self.stored_urls(search), [image_url("ok.png")])
        self.assertEqual(search.images_skipped, 1)

    def test_an_svg_is_refused_and_counted_as_skipped(self):
        """
        SVG is declined at the content-type check, so its body is never
        downloaded — but it still counts as a skipped image, because the page
        really did reference an image we are not showing.
        """
        svg = FakeResponse(200, {"Content-Type": "image/svg+xml"}, SVG)
        self.serve_page({image_url("photo.png"): png_response(9), image_url("logo.svg"): svg})

        search = ingest_url(PAGE)

        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(search.images.count(), 1)
        self.assertEqual(search.images_skipped, 1)
        self.assertEqual(ImageAsset.objects.count(), 1)
        self.assertFalse(svg.body_was_read, "the SVG was downloaded before being rejected")

    def test_an_svg_mislabelled_as_png_is_still_not_stored(self):
        """
        The content-type rule is the first line, not the only one. A server can
        label an SVG image/png, and then Pillow refusing the bytes is what
        keeps active content out of our storage.
        """
        self.serve_page({
            image_url("photo.png"): png_response(10),
            image_url("sneaky.png"): FakeResponse(200, {"Content-Type": "image/png"}, SVG),
        })

        search = ingest_url(PAGE)

        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(search.images.count(), 1)
        self.assertEqual(search.images_skipped, 1)
        self.assertEqual(ImageAsset.objects.count(), 1)

    def test_completes_with_nothing_when_every_image_fails(self):
        self.serve_page({
            image_url("gone.png"): FakeResponse(404, {"Content-Type": "image/png"}),
            image_url("logo.svg"): FakeResponse(200, {"Content-Type": "image/svg+xml"}, SVG),
        })

        search = ingest_url(PAGE)

        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(search.images.count(), 0)
        self.assertEqual(search.error_message, "", "a partial failure is not a search failure")

    def test_says_in_the_log_what_it_skipped_and_why(self):
        self.serve_page({image_url("gone.png"): FakeResponse(404, {"Content-Type": "image/png"})})

        with self.assertLogs("img_downloader.ingest", level="WARNING") as captured:
            ingest_url(PAGE)

        self.assertIn("gone.png", captured.output[0])
        self.assertIn("404", captured.output[0])


class SearchFailureTests(IngestTestCase):
    """Only the page fetch can fail the whole search."""

    def test_an_unsafe_url_marks_the_search_failed(self):
        self.serve({}, resolves_to={"localhost": "127.0.0.1"})

        with self.assertRaises(FetchingError):
            ingest_url("http://localhost/admin")

        search = Search.objects.get(url="http://localhost/admin")
        self.assertEqual(search.status, Search.Status.FAILED)
        self.assertIn("Unsafe IP detected", search.error_message)

    def test_a_404_page_does_not_leave_the_search_pending(self):
        """The likeliest user mistake must not produce a stuck row."""
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(404)})

        with self.assertRaises(FetchingError):
            ingest_url(PAGE)

        search = Search.objects.get(url=PAGE)
        self.assertEqual(search.status, Search.Status.FAILED)
        self.assertIn("404", search.error_message)

    def test_a_failed_search_records_no_counts(self):
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(500)})

        with self.assertRaises(FetchingError):
            ingest_url(PAGE)

        search = Search.objects.get(url=PAGE)
        self.assertEqual(
            (search.images_found, search.images_skipped, search.images_duplicate), (0, 0, 0)
        )


class CounterTests(IngestTestCase):
    """found = stored + skipped + duplicate + over_cap, in every case."""

    def assertCountersPartition(self, search):
        parts = (
            search.images_stored,
            search.images_skipped,
            search.images_duplicate,
            search.images_over_cap,
        )
        self.assertEqual(sum(parts), search.images_found, f"{parts} does not sum to found")
        self.assertTrue(all(part >= 0 for part in parts), f"negative counter in {parts}")
        return parts

    def test_a_mixed_page(self):
        self.serve_page({
            image_url("a.png"): png_response(10),
            image_url("gone.png"): FakeResponse(404, {"Content-Type": "image/png"}),
            image_url("logo.svg"): FakeResponse(200, {"Content-Type": "image/svg+xml"}, SVG),
            image_url("b.png"): png_response(11),
            image_url("a-again.png"): png_response(10),
            image_url("b-again.png"): png_response(11),
        })

        search = ingest_url(PAGE)

        self.assertEqual(search.images_found, 6)
        self.assertEqual(self.assertCountersPartition(search), (2, 2, 2, 0))

    def test_a_clean_page(self):
        self.serve_page({image_url(f"c{i}.png"): png_response(20 + i) for i in range(4)})

        search = ingest_url(PAGE)

        self.assertEqual(self.assertCountersPartition(search), (4, 0, 0, 0))

    def test_a_page_over_the_cap(self):
        self.serve_page({image_url(f"d{i}.png"): png_response(30 + i) for i in range(12)})

        with mock.patch.object(ingest, "MAX_IMAGES", 5):
            search = ingest_url(PAGE)

        self.assertEqual(search.images_found, 12, "found is the total before the cap")
        self.assertEqual(self.assertCountersPartition(search), (5, 0, 0, 7))


class ConcurrencyTests(IngestTestCase):
    def test_downloads_in_parallel_and_keeps_document_order(self):
        count, delay = 16, 0.15
        images = {image_url(f"i{i:02d}.png"): png_response(40 + i, delay=delay)
                  for i in range(count)}
        dead = [image_url("i03.png"), image_url("i09.png")]
        for url in dead:
            images[url] = FakeResponse(404, {"Content-Type": "image/png"}, delay=delay)
        self.serve_page(images)

        started = time.monotonic()
        search = ingest_url(PAGE)
        elapsed = time.monotonic() - started

        serial = count * delay
        self.assertLess(elapsed, serial / 3, f"{elapsed:.2f}s suggests downloads ran serially")

        survivors = [url for url in images if url not in dead]
        self.assertEqual(self.stored_urls(search), survivors, "document order was lost")
        self.assertEqual(self.positions(search), list(range(len(survivors))))
