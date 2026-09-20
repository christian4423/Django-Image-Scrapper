import hashlib
from PIL import Image
from io import BytesIO

from models import Search, ImageAsset
from django.core.files.base import ContentFile
from django.db import transaction
from fetching import fetch_page, download_image, FetchingError
from parsing import extract_absolute_images
MAX_IMAGES = 60  # Maximum number of images to download per page

"""
create Search at PENDING → fetch_page → extract_absolute_images → cap the count → download concurrently → persist → flip to COMPLETE.
"""
def ingest_url(url: str) -> Search:
    """
    Ingest a URL into the search model.

    Args:
        url: The URL to ingest.

    Returns:
        A Search object representing the ingested URL.
    """
    search = Search(url=url)
    search.status = "PENDING"
    search.page_title = ""  # Initialize page title as empty string
    try:
        fetched_page = fetch_page(url)
    except FetchingError as e:
        search.status = "FAILED"
        search.error_message = str(e)
        search.save()
        raise e
    absolute_images = extract_absolute_images(fetched_page.url, fetched_page.html)
    capped_images = absolute_images[:MAX_IMAGES]# Update image count with capped images
    # download capped images concurrently (implementation not shown)
    downloaded_images = []
    for image_url in capped_images:
        downloaded = download_image(image_url)
        content = downloaded.content
        digest = hashlib.sha256(content).hexdigest()
        downloaded_images.append((downloaded, content, digest))

    with transaction.atomic():
        for downloaded, content, digest in downloaded_images:
            asset = ImageAsset.objects.filter(sha256=digest).first()
            if asset is None:
                pillow_image = Image.open(BytesIO(content))
                filename = f"{pillow_image.filename}{pillow_image.format}"
                asset = ImageAsset(sha256=digest, content_type=downloaded.content_type, byte_size=len(content))
                asset.file.save(filename, ContentFile(content), save=False)
                asset.save()

        search.status = "COMPLETE"
        search.save()
    return search