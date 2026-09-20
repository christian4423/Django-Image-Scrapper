"""
Turning a URL the user typed into a Search row with its images attached.

The whole flow, in order:

    create the Search (PENDING)
      -> fetch the page
      -> find every image URL in it
      -> download those images, in parallel
      -> save each one as an ImageAsset and link it to the Search
      -> mark the Search COMPLETE

Only the page fetch can fail the whole search. An individual image that will
not download, or whose bytes turn out not to be an image, is counted and
skipped — a page is still worth keeping when a few of its images are missing.
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import NamedTuple

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, UnidentifiedImageError

from .fetching import FetchingError, download_image, fetch_page
from .models import ImageAsset, Search, SearchImage
from .parsing import extract_absolute_images, extract_title

logger = logging.getLogger(__name__)

# How many images we are willing to take from one page.
MAX_IMAGES = 60

# Downloading is almost entirely network wait, so threads are the right tool
# and the GIL is not in the way. Kept deliberately small: a page's images
# usually come from one or two origins, so the pool size doubles as the
# politeness limit for those hosts.
MAX_DOWNLOAD_WORKERS = 8

# Pillow's format names, and the file extension we store each one under.
FORMAT_EXTENSIONS = {
    "JPEG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "TIFF": "tiff",
}

# Pillow's way of saying "these bytes are not a raster image I can handle".
# Listed by name rather than catching OSError — which UnidentifiedImageError
# happens to subclass — so that a genuine storage failure still propagates
# instead of being quietly counted as a skipped image.
UNUSABLE_IMAGE_ERRORS = (UnidentifiedImageError, Image.DecompressionBombError)


class DownloadedImage(NamedTuple):
    """One image we successfully retrieved, before it becomes an ImageAsset."""

    source_url: str
    content: bytes
    content_type: str
    digest: str  # sha256 hex: both the image's identity and its filename


def ingest_url(url: str) -> Search:
    """
    Download every image on the page at `url` and record them against a Search.

    Returns the completed Search. Raises FetchingError if the page itself
    could not be fetched, having first marked the Search FAILED.
    """
    # Saved straight away so the row exists — and can be shown as in-progress
    # or failed — no matter what happens below.
    search = Search.objects.create(url=url, status=Search.Status.PENDING)

    try:
        page = fetch_page(url)
    except FetchingError as error:
        search.status = Search.Status.FAILED
        search.error_message = str(error)
        search.save()
        raise

    search.page_title = extract_title(page.html)
    search.save()

    # Resolved against the page's *final* URL, which may differ from `url` if
    # we were redirected, so relative paths point where the browser would go.
    found_urls = extract_absolute_images(page.html, page.url)
    attempted_urls = found_urls[:MAX_IMAGES]

    downloads = _download_all(attempted_urls, search.pk)
    failed_downloads = len(attempted_urls) - len(downloads)

    with transaction.atomic():
        assets, newly_stored_ids = _save_assets(downloads, search.pk)
        unreadable = len(downloads) - len(assets)
        duplicates = _link_to_search(search, assets)

        # One row is linked per distinct asset, so these ids are exactly the
        # images this search can show; the ones we did not have to create are
        # the ones an earlier search had already paid for.
        stored_ids = {asset.pk for _source_url, asset in assets}

        # images_found is the total before the cap, so images_over_cap can
        # tell "the page only had 12" from "we stopped looking at 60".
        search.images_found = len(found_urls)
        search.images_skipped = failed_downloads + unreadable
        search.images_duplicate = duplicates
        search.images_reused = len(stored_ids - newly_stored_ids)
        search.status = Search.Status.COMPLETE
        search.save()

    if search.images_skipped or search.images_duplicate or search.images_reused:
        logger.info(
            "search %s: %s found -> %s stored (%s reused), %s skipped, %s duplicate, %s over cap",
            search.pk, search.images_found, search.images_stored, search.images_reused,
            search.images_skipped, search.images_duplicate, search.images_over_cap,
        )
    return search


def _download_all(image_urls: list[str], search_pk: int) -> list[DownloadedImage]:
    """
    Download every URL at once, dropping the ones that fail.

    The result keeps the order of `image_urls` minus the failures, which is
    what SearchImage.position is built from further down.
    """
    # These workers only make HTTP requests — they never touch the ORM — so no
    # Django database connection is ever shared between threads.
    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(download_image, image_url) for image_url in image_urls]

    downloads = []
    for image_url, future in zip(image_urls, futures):
        try:
            fetched = future.result()
        except FetchingError as error:
            # A dead link, a timeout or an oversized file is a fact about one
            # image, not about the page. Note it and carry on.
            logger.warning("search %s: skipping %s (%s)", search_pk, image_url, error)
            continue

        downloads.append(
            DownloadedImage(
                source_url=image_url,
                content=fetched.content,
                content_type=fetched.content_type,
                digest=hashlib.sha256(fetched.content).hexdigest(),
            )
        )
    return downloads


def _save_assets(
    downloads: list[DownloadedImage], search_pk: int
) -> tuple[list[tuple[str, ImageAsset]], set[int]]:
    """
    Turn each download into a stored ImageAsset, in order.

    Returns (source_url, asset) pairs rather than bare assets, because the
    same asset can be reached from several URLs and we want to remember which
    one we saw it at. Images Pillow cannot read are left out.

    Also returns the ids of the assets this call had to create. Everything
    else was already in storage, which is what images_reused counts — and it
    has to be tracked here rather than inferred later, because the *second*
    copy of a repeated image on this same page also finds an existing asset
    without that meaning we saved any work.
    """
    saved = []
    newly_stored_ids = set()
    for download in downloads:
        # Assets are content-addressed, so if we already hold these exact
        # bytes — from this page or from a search months ago — reuse the row
        # instead of storing a second copy.
        asset = ImageAsset.objects.filter(sha256=download.digest).first()

        if asset is None:
            try:
                asset = _store_new_asset(download)
            except UNUSABLE_IMAGE_ERRORS as error:
                logger.warning(
                    "search %s: undecodable %s (%s)", search_pk, download.source_url, error
                )
                continue
            newly_stored_ids.add(asset.pk)

        saved.append((download.source_url, asset))
    return saved, newly_stored_ids


def _store_new_asset(download: DownloadedImage) -> ImageAsset:
    """
    Write one image's bytes to storage and return the saved ImageAsset.

    Raises one of UNUSABLE_IMAGE_ERRORS if the bytes are not a readable image,
    which happens before anything is written.
    """
    # Opening reads the header only. That is enough to reject SVG and other
    # non-raster content that got past the Content-Type check, and to trip
    # Pillow's decompression-bomb guard, without decoding the whole image.
    with Image.open(BytesIO(download.content)) as image:
        image_format = image.format

    asset = ImageAsset(
        sha256=download.digest,
        content_type=download.content_type,
        byte_size=len(download.content),
    )
    filename = f"{download.digest}.{_extension_for(image_format)}"
    asset.file.save(filename, ContentFile(download.content), save=False)
    asset.save()
    return asset


def _extension_for(image_format: str | None) -> str:
    """The extension to store a Pillow format under, e.g. "JPEG" -> "jpg"."""
    if image_format is None:
        return "bin"
    return FORMAT_EXTENSIONS.get(image_format, image_format.lower())


def _link_to_search(search: Search, assets: list[tuple[str, ImageAsset]]) -> int:
    """
    Create the SearchImage rows joining this search to the images it found.

    Returns how many were dropped as duplicates. The same bytes often appear
    at several URLs on one page — a logo in the header and again in the footer
    — and unique_search_asset allows only one row per (search, asset), so the
    first URL we saw it at wins.
    """
    rows: list[SearchImage] = []
    seen_asset_ids = set()
    duplicates = 0

    for source_url, asset in assets:
        if asset.pk in seen_asset_ids:
            duplicates += 1
            continue
        seen_asset_ids.add(asset.pk)

        rows.append(
            SearchImage(
                search=search,
                asset=asset,
                source_url=source_url,
                # Numbered across stored rows only, so Meta.ordering gives
                # back document order with no gaps where something was
                # skipped or deduplicated.
                position=len(rows),
            )
        )

    SearchImage.objects.bulk_create(rows)
    return duplicates
