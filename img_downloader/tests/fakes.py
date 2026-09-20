"""
A pretend internet for the test suite.

No test in this package touches the real network. `FakeNetworkMixin.serve()`
hands `fetching` a session that answers from a dictionary of URLs, and a DNS
lookup that returns whatever the test says, so every case — a 404, a redirect
loop, a link-local address, a connection reset — is reproducible and offline.
"""
import time
from io import BytesIO
from unittest import mock

import requests
from PIL import Image
from requests.structures import CaseInsensitiveDict

from img_downloader import fetching

#: Any address outside the reserved ranges; stands in for a real host.
PUBLIC_ADDRESS = "93.184.216.34"

#: A cloud metadata endpoint — the address SSRF protection exists to block.
METADATA_ADDRESS = "169.254.169.254"

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class FakeResponse:
    """Enough of `requests.Response` for `fetching` to work against."""

    def __init__(self, status_code=200, headers=None, body=b"", encoding="utf-8", delay=0.0):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self.encoding = encoding
        self.delay = delay
        self._body = body
        #: Set once iter_content runs, so a test can prove a rejected response
        #: was never downloaded.
        self.body_was_read = False

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in REDIRECT_STATUSES

    @property
    def is_permanent_redirect(self):
        return "location" in self.headers and self.status_code in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def iter_content(self, chunk_size):
        self.body_was_read = True
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSession:
    """Answers GETs from a {url: FakeResponse} map, recording what was asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            raise AssertionError(
                f"test made an unexpected request to {url}\nrouted: {sorted(self.routes)}"
            )
        if isinstance(route, Exception):
            raise route
        if route.delay:
            time.sleep(route.delay)
        return route


class FakeNetworkMixin:
    """Swaps `fetching`'s HTTP session and DNS lookups out for the duration of a test."""

    def serve(self, routes, resolves_to=None):
        """
        Install `routes` as the whole internet.

        `resolves_to` overrides DNS for particular hostnames, e.g.
        {"evil.test": METADATA_ADDRESS}. Everything else looks public.
        """
        session = FakeSession(routes)
        overrides = resolves_to or {}

        self.patch(fetching, "_session", lambda: session)
        self.patch(
            fetching,
            "_resolve",
            lambda hostname: [overrides.get(hostname, PUBLIC_ADDRESS)],
        )

        # robots.txt answers are cached per origin for the life of the process,
        # so one test's rules must not leak into the next.
        fetching._robots_rules_for_origin.cache_clear()
        self.addCleanup(fetching._robots_rules_for_origin.cache_clear)
        return session

    def patch(self, target, attribute, replacement):
        """mock.patch.object that undoes itself when the test finishes."""
        patcher = mock.patch.object(target, attribute, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)


def robots(body=b"User-agent: *\nAllow: /\n", **kwargs):
    """A robots.txt response. Defaults to allowing everything."""
    return FakeResponse(200, {"Content-Type": "text/plain"}, body, **kwargs)


def png(seed=0, size=(8, 8), **kwargs):
    """Real PNG bytes. Different seeds give different bytes, so different hashes."""
    buffer = BytesIO()
    colour = (seed * 7 % 256, seed * 13 % 256, seed * 29 % 256)
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def png_response(seed=0, **kwargs):
    return FakeResponse(200, {"Content-Type": "image/png"}, png(seed), **kwargs)


#: Valid SVG. A real image to a browser, unreadable to Pillow — which is the
#: single most common reason a real page's images get skipped.
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'


def html_page(image_urls, title="Test Page"):
    tags = "".join(f'<img src="{url}">' for url in image_urls)
    return f"<html><head><title>{title}</title></head><body>{tags}</body></html>".encode()
