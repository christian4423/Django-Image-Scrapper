import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, UnidentifiedImageError
from io import BytesIO

from .models import Search, ImageAsset, SearchImage
from django.core.files.base import ContentFile
from django.db import transaction
from .fetching import fetch_page, download_image, FetchingError
from .parsing import extract_absolute_images, extract_title

logger = logging.getLogger(__name__)

MAX_IMAGES = 60  # Maximum number of images to download per page

# Images are fetched in parallel: the work is almost entirely network wait, so
# threads are the right tool and the GIL is not in the way. Kept deliberately
# small — a page's images usually come from one or two origins, so the pool
# size doubles as the politeness limit for those hosts.
MAX_DOWNLOAD_WORKERS = 8
FORMAT_EXTENSIONS = {
    "JPEG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "TIFF": "tiff",
}

# Pillow's way of saying "these bytes aren't a raster image I can handle".
# Caught by name rather than by OSError (which UnidentifiedImageError subclasses)
# so a genuine storage failure in asset.file.save still propagates.
UNUSABLE_IMAGE_ERRORS = (UnidentifiedImageError, Image.DecompressionBombError)

"""
create Search at PENDING → fetch_page → extract_absolute_images → cap the count → download concurrently → persist → flip to COMPLETE.
"""
def ingest_url(url: str) -> Search:
    """
    Ingest a URL into the search model.

    A page is worth keeping even when some of its images aren't: individual
    images that fail to download or decode are skipped and logged, and the
    Search still completes with whatever was retrievable. Only a failure to
    fetch the page itself marks the Search FAILED.

    Args:
        url: The URL to ingest.

    Returns:
        A Search object representing the ingested URL.
    """
    search = Search(url=url)
    search.status = Search.Status.PENDING
    search.page_title = ""  # Initialize page title as empty string
    search.save()  # Save the initial PENDING state
    try:
        fetched_page = fetch_page(url)
    except FetchingError as e:
        search.status = Search.Status.FAILED
        search.error_message = str(e)
        search.save()
        raise e
    search.page_title = extract_title(fetched_page.html)
    search.save()
    absolute_images = extract_absolute_images(fetched_page.html, fetched_page.url)
    capped_images = absolute_images[:MAX_IMAGES]# Update image count with capped images

    # Workers only do HTTP — no ORM calls — so there is no question of sharing a
    # Django connection across threads. requests.Session is already thread-local
    # in fetching.py, so each worker gets its own connection pool.
    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(download_image, image_url) for image_url in capped_images]

    # Consuming the futures in submission order keeps results in document order,
    # which is what SearchImage.position is derived from below.
    downloaded_images = []
    skipped = 0
    duplicates = 0
    for image_url, future in zip(capped_images, futures):
        try:
            downloaded = future.result()
        except FetchingError as e:
            # A dead link, a timeout or an oversized file is a fact about one
            # image, not about the page. Drop it and keep going.
            logger.warning("search %s: skipping %s (%s)", search.pk, image_url, e)
            skipped += 1
            continue
        content = downloaded.content
        digest = hashlib.sha256(content).hexdigest()
        downloaded_images.append((image_url, downloaded, content, digest))

    with transaction.atomic():
        search_images = []
        seen_asset_ids = set()
        for source_url, downloaded, content, digest in downloaded_images:
            asset = ImageAsset.objects.filter(sha256=digest).first()
            if asset is None:
                try:
                    # Header-only read, so it rejects SVG and other non-raster
                    # content that got past the content-type check, and trips
                    # Pillow's decompression-bomb guard, before we touch storage.
                    with Image.open(BytesIO(content)) as pillow_image:
                        image_format = pillow_image.format
                except UNUSABLE_IMAGE_ERRORS as e:
                    logger.warning("search %s: undecodable %s (%s)", search.pk, source_url, e)
                    skipped += 1
                    continue

                extension = (
                    FORMAT_EXTENSIONS.get(image_format, image_format.lower())
                    if image_format is not None
                    else "bin"
                )
        
                filename = f"{digest}.{extension}"
                asset = ImageAsset(sha256=digest, content_type=downloaded.content_type, byte_size=len(content))
                asset.file.save(filename, ContentFile(content), save=False)
                asset.save()

            # The same bytes can appear at several URLs on one page (e.g. a logo
            # in the header and the footer). unique_search_asset allows only one
            # row per (search, asset), so keep the first URL we saw it at.
            if asset.pk in seen_asset_ids:
                duplicates += 1
                continue
            seen_asset_ids.add(asset.pk)

            search_images.append(
                SearchImage(
                    search=search,
                    asset=asset,
                    source_url=source_url,
                    # Contiguous over stored rows, so Meta.ordering gives back
                    # document order with no gaps where duplicates were dropped.
                    position=len(search_images),
                )
            )

        SearchImage.objects.bulk_create(search_images)

        # images_found is the uncapped total, so images_over_cap can tell the
        # difference between "the page only had 12" and "we stopped at 60".
        search.images_found = len(absolute_images)
        search.images_skipped = skipped
        search.images_duplicate = duplicates
        search.status = Search.Status.COMPLETE
        search.save()

    if skipped or duplicates:
        logger.info(
            "search %s: %s found -> %s stored, %s skipped, %s duplicate, %s over cap",
            search.pk, len(absolute_images), len(search_images), skipped, duplicates,
            len(absolute_images) - len(capped_images),
        )
    return search
