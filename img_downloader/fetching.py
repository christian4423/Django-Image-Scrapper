"""
Fetching things from the outside world: one web page, or one image, at a time.

Two rules hold everywhere in this module, and the rest of the app relies on
both of them:

1. Every failure comes out as a `FetchingError`. Callers never need to know
   that `requests` is underneath, or catch its exception types.
2. Every URL is vetted before it is requested — scheme first, then the
   addresses it resolves to — and that includes every hop of a redirect
   chain. See `_reject_unsafe_url`.
"""
import ipaddress
import socket
import threading
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import ParseResult, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

# How we introduce ourselves, both in request headers and when asking
# robots.txt whether a URL is open to us.
USER_AGENT = "ImgDownloader/1.0"

# (seconds to connect, seconds to wait for data)
TIMEOUT = (5, 15)

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5

# Size ceilings. These are the only thing standing between us and a server
# that streams bytes forever, so they are enforced while reading, not just
# from the Content-Length header a server may well be lying about.
MAX_IMAGE_BYTES = 10 * 1024 * 1024       # 10 MB
MAX_HTML_BYTES = 2 * 1024 * 1024         # 2 MB
MAX_ROBOTS_TXT_BYTES = 500 * 1024        # 500 KB


class FetchingError(RuntimeError):
    """Anything that stopped us retrieving a URL. The only error this module raises."""


class FetchedPage:
    """An HTML page. `url` is where we ended up, which may not be where we started."""

    def __init__(self, url: str, html: str):
        self.url = url
        self.html = html


class FetchedImage:
    """Raw image bytes, plus whatever the server said they were."""

    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.content_type = content_type


# --------------------------------------------------------------- what we offer

def fetch_page(url: str) -> FetchedPage:
    """
    Fetch one HTML page.

    Raises FetchingError if the URL is unsafe, robots.txt disallows it, the
    page is too big, or the request fails for any reason.
    """
    with _open_url(url, obey_robots=True) as (final_url, response):
        raw = _read_capped(response, MAX_HTML_BYTES)
        return FetchedPage(final_url, _decode(raw, response))


def download_image(url: str) -> FetchedImage:
    """
    Download one image.

    robots.txt is deliberately not re-checked here: `fetch_page` already
    cleared this origin, and the answer is cached, so asking again for each
    of sixty images would only add sixty requests.

    Raises FetchingError if the URL is unsafe, the response is clearly not an
    image, it is too big, or the request fails for any reason.
    """
    with _open_url(url, obey_robots=False) as (_final_url, response):
        content_type = _image_content_type(response)
        return FetchedImage(_read_capped(response, MAX_IMAGE_BYTES), content_type)


# ------------------------------------------------------------------ requesting

@contextmanager
def _open_url(url: str, *, obey_robots: bool):
    """
    Request `url`, following any redirects ourselves, and hand the caller the
    final `(url, response)` with the body still unread.

    `requests` can follow redirects on its own, but then only the *first* URL
    would pass our safety check — a page redirecting to http://169.254.169.254/
    would sail straight through. Stepping through the chain by hand means
    every hop is checked.

    The response is closed when the caller's `with` block ends, so the caller
    decides how many bytes to read.
    """
    current = url

    for _ in range(MAX_REDIRECTS):
        parsed = urlparse(current)
        _reject_unsafe_url(parsed)
        if obey_robots:
            _require_robots_permission(parsed)

        with _stream(current) as response:
            if response.is_redirect or response.is_permanent_redirect:
                current = _redirect_target(current, response)
                continue

            response.raise_for_status()
            yield current, response
            return

    raise FetchingError("Too many redirects")


@contextmanager
def _stream(url: str):
    """
    GET `url` without reading the body yet, translating every `requests`
    failure into a FetchingError.

    The caller's work happens inside this `try`, so a connection dropped
    halfway through reading the body is converted too, not just a refused
    connection or a 404.
    """
    try:
        with _session().get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        ) as response:
            yield response
    except requests.RequestException as error:
        raise FetchingError(f"Could not fetch {url}: {error}") from error


def _redirect_target(current_url: str, response) -> str:
    """Where a redirect points, resolved against the URL that produced it."""
    location = response.headers.get("Location")
    if not location:
        raise FetchingError("Redirect response missing Location header")
    return urljoin(current_url, location)


_local = threading.local()


def _session() -> requests.Session:
    """
    One `requests.Session` per thread, for connection reuse.

    A Session is not thread-safe, and images are downloaded in parallel, so
    each worker thread gets its own rather than sharing one.
    """
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


# ----------------------------------------------------------------- is it safe?

def _reject_unsafe_url(parsed_url: ParseResult) -> None:
    """
    Raise FetchingError unless this URL is safe for *our server* to request.

    This is SSRF protection. We fetch whatever URL a stranger types in, so
    without this someone could point us at http://localhost:6379/ or at a
    cloud metadata endpoint like http://169.254.169.254/ and read back
    something only our server is able to reach.
    """
    if parsed_url.scheme not in ALLOWED_SCHEMES:
        raise FetchingError(f"Unsupported URL scheme: {parsed_url.scheme}")

    hostname = parsed_url.hostname
    if not hostname:
        raise FetchingError(f"Invalid URL: {parsed_url.geturl()}")

    # A hostname can resolve to several addresses. Every one of them has to be
    # public, or a private address could hide behind a public one.
    for address in _resolve(hostname):
        ip_address = ipaddress.ip_address(address)
        if _is_internal(ip_address):
            raise FetchingError(f"Unsafe IP detected: {ip_address}")


def _resolve(hostname: str) -> list[str]:
    """Every IP address a hostname resolves to."""
    try:
        address_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise FetchingError(f"Unable to resolve hostname: {hostname}")
    return [entry[4][0] for entry in address_info]


def _is_internal(ip_address) -> bool:
    """True for anything on our own network, or otherwise not a public host."""
    return (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_reserved
        or ip_address.is_multicast
    )


# -------------------------------------------------------------------- robots

def _require_robots_permission(parsed_url: ParseResult) -> None:
    """Raise FetchingError if this origin's robots.txt tells us to stay out."""
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    rules = _robots_rules_for_origin(origin)
    if not rules.can_fetch(USER_AGENT, parsed_url.geturl()):
        raise FetchingError(
            f"Fetching disallowed by robots.txt for URL: {parsed_url.geturl()}"
        )


# Known limitation: this cache never expires, so a long-running process will
# not notice robots.txt changing. At this scope — one robots fetch per search
# — that is fine.
@lru_cache(maxsize=128)
def _robots_rules_for_origin(origin: str) -> RobotFileParser:
    """
    Fetch and parse one origin's robots.txt.

    Whether an unreadable robots.txt means "go ahead" or "stay out" depends on
    why it was unreadable, and the choices below follow convention rather than
    taste. An empty ruleset means everything is allowed.
    """
    rules = RobotFileParser()
    robots_url = f"{origin}/robots.txt"
    rules.set_url(robots_url)

    try:
        with _stream(robots_url) as response:
            if response.is_redirect or response.is_permanent_redirect:
                # Fail open: don't read a redirect stub as if it were rules.
                rules.parse([])
                return rules

            if response.status_code in (401, 403):
                # Fail closed, matching CPython's RobotFileParser.read(),
                # which reads "you may not look" as "you may not fetch".
                rules.parse(["User-agent: *", "Disallow: /"])
                return rules

            if response.status_code >= 400:
                # Anything else, 404 included: allow all, per RFC convention.
                rules.parse([])
                return rules

            raw = _read_capped(response, MAX_ROBOTS_TXT_BYTES)
            rules.parse(_decode(raw, response).splitlines())
    except FetchingError:
        # Fail open: if robots.txt cannot be fetched at all, or is absurdly
        # large, that is not the site telling us to stay away.
        rules.parse([])

    return rules


# ------------------------------------------------------------ reading a body

def _read_capped(response, limit: int) -> bytes:
    """
    Read a response body, refusing to buffer more than `limit` bytes.

    Content-Length is checked first because it is free, but it is only a claim
    — the running total below is what actually enforces the limit.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise FetchingError(f"Declared content length exceeds limit of {limit} bytes")

    chunks, total = [], 0
    for chunk in response.iter_content(8192):
        total += len(chunk)
        if total > limit:
            raise FetchingError(f"Downloaded content exceeds limit of {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(raw: bytes, response) -> str:
    """Text from bytes, trusting the server's encoding and never raising."""
    return raw.decode(response.encoding or "utf-8", errors="replace")


def _image_content_type(response) -> str:
    """
    The declared content type, having rejected anything clearly not an image.

    Plenty of servers send images as application/octet-stream, so that is let
    through; Pillow makes the real decision later, when the bytes are decoded.
    """
    declared = response.headers.get("Content-Type") or "application/octet-stream"
    base_type = declared.split(";", 1)[0].strip().lower()
    if not (base_type.startswith("image/") or base_type == "application/octet-stream"):
        raise FetchingError(f"Unexpected content type for image: {declared}")
    return declared
