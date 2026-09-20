import re
from collections.abc import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

_SRCSET_DESCRIPTOR = re.compile(r"^(\d+(?:\.\d+)?)([wx])$")


def extract_title(html_content: str) -> str:
    """Extract the title from one image's source document.

    Call this once for each fetched image/source page when the result should
    be associated with individual images; the title is document-level data.
    """
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")
    title = soup.title
    return title.get_text(strip=True) if title else ""


def extract_absolute_images(html_content: str, base_url: str) -> list[str]:
    """
    Extract unique absolute http/https image URLs from an HTML document.

    Sources, in output order:
      1. <meta property="og:image" content="...">
      2. <img> and <picture><source> tags, in document order.
         For srcset attributes the highest-resolution candidate is used.

    Relative URLs are resolved against ``base_url``. Non-http(s) results
    (data:, javascript:, mailto:, ...) are dropped.
    """
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    raw_urls = [*_og_image_urls(soup), *_image_tag_urls(soup)]
    return _absolutize_and_filter(raw_urls, base_url)


def _og_image_urls(soup: BeautifulSoup) -> Iterator[str]:
    """Yield og:image URLs as written in the document."""
    for meta in soup.find_all("meta", property="og:image", content=True):
        yield meta["content"]


def _image_tag_urls(soup: BeautifulSoup) -> Iterator[str]:
    """Yield one URL per <img> / <picture><source> tag, in document order."""
    for tag in soup.find_all(["img", "source"]):
        if tag.name == "source" and not _is_picture_source(tag):
            continue

        url = _best_srcset_url(tag.get("srcset", "")) or tag.get("src")
        if url:
            yield url


def _is_picture_source(tag: Tag) -> bool:
    """<source> is also used by <video>/<audio>; only <picture> sources are images."""
    return tag.parent is not None and tag.parent.name == "picture"


def _best_srcset_url(srcset: str) -> str | None:
    """
    Return the URL of the highest-resolution candidate in a srcset string.

    A srcset looks like ``"a.jpg 480w, b.jpg 800w"`` or ``"a.jpg 1x, b.jpg 2x"``.
    A candidate with no descriptor counts as 1x. Width (w) and density (x)
    descriptors are compared on the same numeric scale, which is fine
    because a single srcset only ever uses one kind.
    """
    best_url = None
    best_size = -1.0

    for url, descriptor in _parse_srcset(srcset):
        size = _descriptor_size(descriptor) if descriptor else 1.0
        if size > best_size:
            best_url, best_size = url, size

    return best_url


def _parse_srcset(srcset: str) -> Iterator[tuple[str, str | None]]:
    """
    Yield ``(url, descriptor)`` pairs from a srcset string.

    Do NOT split on commas: URLs may contain them (CDN transform paths like
    ``.../w_800,h_600/photo.jpg``). Following the HTML spec, a URL is a run
    of non-whitespace characters; only the descriptor that follows it is
    terminated by a comma. A comma glued to the end of a URL
    (``"a.jpg,b.jpg 2x"`` is one URL; ``"a.jpg, b.jpg 2x"`` is two) is the
    candidate separator and is stripped.
    """
    pos, end = 0, len(srcset)

    while pos < end:
        # Skip separators between candidates.
        while pos < end and (srcset[pos].isspace() or srcset[pos] == ","):
            pos += 1
        if pos >= end:
            break

        # URL: everything up to the next whitespace.
        start = pos
        while pos < end and not srcset[pos].isspace():
            pos += 1
        url = srcset[start:pos]

        if url.endswith(","):
            # "a.jpg, b.jpg 2x" — comma ends this candidate, no descriptor.
            yield url.rstrip(","), None
            continue

        # Descriptor: everything up to the next comma.
        start = pos
        while pos < end and srcset[pos] != ",":
            pos += 1
        descriptor = srcset[start:pos].split()

        yield url, (descriptor[0] if descriptor else None)


def _descriptor_size(descriptor: str) -> float:
    """Turn '800w' or '2x' into 800.0 or 2.0. Unrecognised descriptors count as 1x."""
    match = _SRCSET_DESCRIPTOR.match(descriptor.lower())
    return float(match.group(1)) if match else 1.0


def _absolutize_and_filter(urls: list[str], base_url: str) -> list[str]:
    """Resolve against base_url, keep only http(s), dedupe preserving order."""
    absolute = (urljoin(base_url, url.strip()) for url in urls)
    kept = (url for url in absolute if url.lower().startswith(("http://", "https://")))
    return list(dict.fromkeys(kept))