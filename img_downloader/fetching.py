import requests
import socket
import ipaddress
import threading
from functools import lru_cache
from urllib.robotparser import RobotFileParser
from urllib.parse import ParseResult, urljoin, urlparse
# Functions for fetching web pages and downloading images.
# Designed for one url at a time
# identifier for the user agent
USER_AGENT = "ImgDownloader/1.0"

TIMEOUT = (5, 15)

_local = threading.local()

def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session

SCHEMES = ["http", "https"]
MAX_REDIRECTS = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB limit
MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MB limit
MAX_ROBOTS_TXT_BYTES = 500 * 1024  # 500 KB limit for robots.txt

class FetchingError(RuntimeError):
    """Custom exception for fetching errors."""
    pass

class FetchedPage:
    url: str
    html: str

    def __init__(self, url: str, html: str):
        self.url = url
        self.html = html

class FetchedImage:
    content: bytes
    content_type: str

    def __init__(self, b: bytes, s: str):
        self.content = b
        self.content_type = s

def _read_capped(response, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise FetchingError(f"Declared content length exceeds limit of {limit} bytes")          # cheap bail, before downloading

    chunks, total = [], 0
    for chunk in response.iter_content(8192):
        total += len(chunk)
        if total > limit:
            raise FetchingError(f"Downloaded content exceeds limit of {limit} bytes")      # the actual guard
        chunks.append(chunk)
    return b"".join(chunks)


# Known limitation: this cache never expires, so long-running processes won't
# notice robots.txt changes; at this scope, one robots fetch per search is fine.
@lru_cache(maxsize=128)
def _fetch_robots_txt_cached(origin: str) -> RobotFileParser:
    robots_url = f"{origin}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        with _session().get(
            robots_url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.is_redirect or response.is_permanent_redirect:
                # Fail open on redirects: don't parse the redirect stub as robots rules.
                rp.parse([])
                return rp
            if response.status_code in (401, 403):
                # Fail closed to match CPython's RobotFileParser.read() convention,
                # which treats 401/403 as "disallow all".
                rp.parse(["User-agent: *", "Disallow: /"])
                return rp
            if response.status_code >= 400:
                # Any other error (e.g. 404): treat as "allow all", per RFC convention.
                rp.parse([])
                return rp
            raw = _read_capped(response, MAX_ROBOTS_TXT_BYTES)
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        rp.parse(text.splitlines())
    except (requests.RequestException, FetchingError):
        # Fail open: if robots.txt can't be fetched safely/at all, allow fetching.
        rp.parse([])
    return rp

def _fetch_robots_txt(parsed_url):
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    rp = _fetch_robots_txt_cached(origin)
    if not rp.can_fetch(USER_AGENT, parsed_url.geturl()):
        raise FetchingError(f"Fetching disallowed by robots.txt for URL: {parsed_url.geturl()}")
    return rp

def fetch_page(url: str) -> FetchedPage:
    """
    Fetch the HTML content of a web page.

    Args:
        url: The URL of the web page to fetch.

    Returns:
        The HTML content of the page as a string.
    """
    headers = {"User-Agent": USER_AGENT}
    current = url

    for _ in range(MAX_REDIRECTS):
        parsed_url = urlparse(current)
        _handle_SSRF(parsed_url)
        _fetch_robots_txt(parsed_url)

        with _session().get(current, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise FetchingError("Redirect response missing Location header")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            raw = _read_capped(response, MAX_HTML_BYTES)
            html = raw.decode(response.encoding or "utf-8", errors="replace")
            return FetchedPage(current, html)

    raise FetchingError("Too many redirects")
def download_image(url: str) -> FetchedImage:  
    """
    Download an image from a URL and return its content as a FetchedImage object.

    Args:
        url: The URL of the image to download.

    Returns:
        A FetchedImage object containing the content of the image and its content type.
    """
    headers = {"User-Agent": USER_AGENT}
    current = url

    for _ in range(MAX_REDIRECTS):
        parsed_url = urlparse(current)
        _handle_SSRF(parsed_url)

        # (robots.txt already checked/cached for this origin in fetch_page)
        with _session().get(current, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise FetchingError("Redirect response missing Location header")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get('Content-Type')
            if not content_type:
                content_type = "application/octet-stream"
            base_type = content_type.split(";", 1)[0].strip().lower()
            if not (base_type.startswith("image/") or base_type == "application/octet-stream"):
                raise FetchingError(f"Unexpected content type for image: {content_type}")
            content = _read_capped(response, limit=MAX_IMAGE_BYTES)  # 10 MB limit
            return FetchedImage(content, content_type)

    raise FetchingError("Too many redirects")

def _handle_SSRF(parsed_url: ParseResult) -> bool:
    """
    Handle Server-Side Request Forgery (SSRF) protection.

    Args:
        parsed_url: The parsed URL to check for SSRF vulnerabilities.
    Returns:
        True if the URL passes the SSRF checks, False otherwise.
    Raises:
        FetchingError: If the URL is deemed unsafe.
    """
    if parsed_url.scheme not in SCHEMES: 
        raise FetchingError(f"Unsupported URL scheme: {parsed_url.scheme}")
    hostname = parsed_url.hostname
    if not hostname:
        raise FetchingError(f"Invalid URL: {parsed_url.geturl()}")
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise FetchingError(f"Unable to resolve hostname: {hostname}")
    for entry in addr_info:
        ip = entry[4][0]
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
            raise FetchingError(f"Unsafe IP detected: {ip}")

    return True
