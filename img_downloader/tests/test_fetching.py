"""
Tests for img_downloader.fetching.

These need no database, so they run as SimpleTestCase. Two properties matter
most here and most of the file is about them: every URL is safety-checked
before it is requested, including each hop of a redirect chain, and every
failure leaves this module as a FetchingError.
"""
import socket
from unittest import mock

import requests
from django.test import SimpleTestCase

from img_downloader import fetching
from img_downloader.fetching import FetchingError, download_image, fetch_page

from .fakes import (
    METADATA_ADDRESS,
    FakeNetworkMixin,
    FakeResponse,
    png,
    robots,
)

PAGE = "https://example.com/"
ROBOTS = "https://example.com/robots.txt"


class FetchPageTests(FakeNetworkMixin, SimpleTestCase):
    def test_fetches_a_page(self):
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(200, {}, b"<html>hi</html>")})

        page = fetch_page(PAGE)

        self.assertEqual(page.url, PAGE)
        self.assertIn("hi", page.html)

    def test_follows_a_redirect_and_reports_where_it_landed(self):
        self.serve({
            ROBOTS: robots(),
            "https://example.com/a": FakeResponse(302, {"Location": "/b"}),
            "https://example.com/b": FakeResponse(200, {}, b"<html>final</html>"),
        })

        page = fetch_page("https://example.com/a")

        # The final URL matters: relative image paths are resolved against it.
        self.assertEqual(page.url, "https://example.com/b")
        self.assertIn("final", page.html)

    def test_rejects_a_redirect_pointing_at_a_private_address(self):
        """The reason redirects are followed by hand instead of by requests."""
        self.serve(
            {
                ROBOTS: robots(),
                "https://example.com/a": FakeResponse(
                    302, {"Location": f"http://{METADATA_ADDRESS}/latest/meta-data/"}
                ),
            },
            resolves_to={METADATA_ADDRESS: METADATA_ADDRESS},
        )

        with self.assertRaisesMessage(FetchingError, "Unsafe IP detected"):
            fetch_page("https://example.com/a")

    def test_gives_up_on_a_redirect_loop(self):
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(302, {"Location": "/"})})

        with self.assertRaisesMessage(FetchingError, "Too many redirects"):
            fetch_page(PAGE)

    def test_a_redirect_status_without_a_location_is_read_as_a_body(self):
        """Documents current behaviour: requests only calls it a redirect if
        there is somewhere to go, so this falls through to the body."""
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(302, {}, b"body")})

        self.assertEqual(fetch_page(PAGE).html, "body")

    def test_refuses_a_page_that_declares_it_is_too_big(self):
        oversized = str(fetching.MAX_HTML_BYTES + 1)
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(200, {"Content-Length": oversized}, b"x")})

        with self.assertRaisesMessage(FetchingError, "Declared content length"):
            fetch_page(PAGE)

    def test_refuses_a_page_that_streams_past_the_limit(self):
        """A lying Content-Length must not get past the running total."""
        body = b"x" * (fetching.MAX_HTML_BYTES + 10)
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(200, {}, body)})

        with self.assertRaisesMessage(FetchingError, "exceeds limit"):
            fetch_page(PAGE)


class RobotsTests(FakeNetworkMixin, SimpleTestCase):
    def test_a_disallow_rule_blocks_the_fetch(self):
        self.serve({
            ROBOTS: robots(b"User-agent: *\nDisallow: /private\n"),
            "https://example.com/private/x": FakeResponse(200, {}, b"secret"),
        })

        with self.assertRaisesMessage(FetchingError, "disallowed by robots.txt"):
            fetch_page("https://example.com/private/x")

    def test_a_401_fails_closed(self):
        """Matches CPython's RobotFileParser: "you may not look" means "you may not fetch"."""
        self.serve({ROBOTS: FakeResponse(401), PAGE: FakeResponse(200, {}, b"ok")})

        with self.assertRaisesMessage(FetchingError, "disallowed by robots.txt"):
            fetch_page(PAGE)

    def test_a_404_allows_the_fetch(self):
        self.serve({ROBOTS: FakeResponse(404), PAGE: FakeResponse(200, {}, b"ok")})

        self.assertEqual(fetch_page(PAGE).html, "ok")

    def test_a_redirected_robots_allows_the_fetch(self):
        """Don't parse a redirect stub as though it were rules."""
        self.serve({
            ROBOTS: FakeResponse(301, {"Location": "/elsewhere"}),
            PAGE: FakeResponse(200, {}, b"ok"),
        })

        self.assertEqual(fetch_page(PAGE).html, "ok")

    def test_an_unreachable_robots_fails_open(self):
        self.serve({ROBOTS: requests.ConnectionError("refused"), PAGE: FakeResponse(200, {}, b"ok")})

        self.assertEqual(fetch_page(PAGE).html, "ok")

    def test_images_do_not_ask_again(self):
        """fetch_page already cleared the origin; 60 images must not mean 60 more requests."""
        session = self.serve({"https://cdn.example.com/a.png": FakeResponse(
            200, {"Content-Type": "image/png"}, png(1))})

        download_image("https://cdn.example.com/a.png")

        self.assertEqual(session.calls, ["https://cdn.example.com/a.png"])


class DownloadImageTests(FakeNetworkMixin, SimpleTestCase):
    url = "https://example.com/a.png"

    def test_rejects_something_that_is_clearly_not_an_image(self):
        self.serve({self.url: FakeResponse(200, {"Content-Type": "text/html"}, b"<html>")})

        with self.assertRaisesMessage(FetchingError, "Unexpected content type"):
            download_image(self.url)

    def test_allows_octet_stream(self):
        """Plenty of servers mislabel images; Pillow makes the real decision later."""
        self.serve({self.url: FakeResponse(200, {"Content-Type": "application/octet-stream"}, png(1))})

        self.assertEqual(download_image(self.url).content, png(1))

    def test_defaults_a_missing_content_type(self):
        self.serve({self.url: FakeResponse(200, {}, png(1))})

        self.assertEqual(download_image(self.url).content_type, "application/octet-stream")

    def test_keeps_the_content_type_verbatim(self):
        self.serve({self.url: FakeResponse(200, {"Content-Type": "image/png; charset=binary"}, png(1))})

        self.assertEqual(download_image(self.url).content_type, "image/png; charset=binary")

    def test_refuses_svg_without_downloading_it(self):
        """
        SVG is active content: served from our origin it would be stored XSS.
        Refusing on the declared type means the body never comes down.
        """
        svg = FakeResponse(200, {"Content-Type": "image/svg+xml"}, b"<svg/>")
        self.serve({self.url: svg})

        with self.assertRaisesMessage(FetchingError, "Refusing active-content image type"):
            download_image(self.url)

        self.assertFalse(svg.body_was_read, "the body was downloaded before being rejected")

    def test_refuses_svg_whatever_the_parameters_say(self):
        self.serve({self.url: FakeResponse(200, {"Content-Type": "IMAGE/SVG+XML; charset=utf-8"}, b"<svg/>")})

        with self.assertRaisesMessage(FetchingError, "Refusing active-content image type"):
            download_image(self.url)

    def test_still_accepts_ordinary_image_types(self):
        """The SVG rule must not catch anything else."""
        for content_type in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            with self.subTest(content_type=content_type):
                self.serve({self.url: FakeResponse(200, {"Content-Type": content_type}, png(1))})
                self.assertEqual(download_image(self.url).content, png(1))

    def test_refuses_an_image_past_the_size_limit(self):
        body = b"x" * (fetching.MAX_IMAGE_BYTES + 10)
        self.serve({self.url: FakeResponse(200, {"Content-Type": "image/png"}, body)})

        with self.assertRaisesMessage(FetchingError, "exceeds limit"):
            download_image(self.url)


class UrlSafetyTests(FakeNetworkMixin, SimpleTestCase):
    """SSRF protection: our server fetches whatever a stranger types in."""

    def test_rejects_an_unsupported_scheme(self):
        self.serve({})

        with self.assertRaisesMessage(FetchingError, "Unsupported URL scheme"):
            fetch_page("ftp://example.com/x")

    def test_rejects_a_url_with_no_hostname(self):
        self.serve({})

        with self.assertRaisesMessage(FetchingError, "Invalid URL"):
            fetch_page("http:///x")

    def test_rejects_a_host_resolving_to_a_private_address(self):
        self.serve({}, resolves_to={"localhost": "127.0.0.1"})

        with self.assertRaisesMessage(FetchingError, "Unsafe IP detected"):
            fetch_page("http://localhost/x")

    def test_rejects_a_host_resolving_to_a_link_local_address(self):
        self.serve({}, resolves_to={"metadata.test": METADATA_ADDRESS})

        with self.assertRaisesMessage(FetchingError, "Unsafe IP detected"):
            fetch_page("http://metadata.test/x")

    def test_a_hostname_that_will_not_resolve_is_a_fetching_error(self):
        with mock.patch.object(fetching.socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaisesMessage(FetchingError, "Unable to resolve hostname"):
                fetching._resolve("nx.example")


class ErrorTranslationTests(FakeNetworkMixin, SimpleTestCase):
    """
    Nothing may escape this module except FetchingError.

    ingest_url catches FetchingError to skip one bad image. If a requests
    exception got through instead, a single 404 would abort a whole search.
    """

    def test_an_http_error_on_a_page(self):
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(404)})

        with self.assertRaises(FetchingError):
            fetch_page(PAGE)

    def test_an_http_error_on_an_image(self):
        self.serve({"https://example.com/404.png": FakeResponse(404, {"Content-Type": "image/png"})})

        with self.assertRaises(FetchingError):
            download_image("https://example.com/404.png")

    def test_a_refused_connection(self):
        self.serve({"https://example.com/x.png": requests.ConnectionError("refused")})

        with self.assertRaises(FetchingError):
            download_image("https://example.com/x.png")

    def test_a_timeout(self):
        self.serve({"https://example.com/x.png": requests.Timeout("slow")})

        with self.assertRaises(FetchingError):
            download_image("https://example.com/x.png")
