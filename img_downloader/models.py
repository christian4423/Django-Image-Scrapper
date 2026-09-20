import os
from django.contrib.admin import TabularInline
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models import UniqueConstraint

# Assets are named after their own sha256, so a name collision means the path
# already holds byte-identical content. Django's default storage renames on
# collision (foo.jpg -> foo_a1b2c3.jpg) to avoid clobbering an unrelated
# upload; here that stores a redundant second copy and breaks the invariant
# that a file's name IS its hash. Overwriting is always safe when the name is
# derived from the content.
asset_storage = FileSystemStorage(allow_overwrite=True)

class Search(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        COMPLETE = 'COMPLETE', 'Complete'
        FAILED = 'FAILED', 'Failed'
        
    url = models.URLField(max_length=2000)
    page_title = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Counts of what happened to the images on the page, so a template can
    # explain a short gallery instead of silently showing fewer images than
    # the page has. Together with images.count() these partition images_found:
    #
    #     found = stored + skipped + duplicate + over_cap
    #
    # "stored" is deliberately not a column — it is images.count(), and a
    # denormalised copy could only ever disagree with the rows themselves.
    images_found = models.PositiveIntegerField(
        default=0,
        help_text="Image URLs found in the page, before the MAX_IMAGES cap.",
    )
    images_skipped = models.PositiveIntegerField(
        default=0,
        help_text="Attempted but unusable: download failed, or not a decodable image.",
    )
    images_duplicate = models.PositiveIntegerField(
        default=0,
        help_text="Byte-identical to another image on the same page, so stored once.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} - {self.status}"

    @property
    def images_stored(self) -> int:
        """How many images this search can actually display."""
        return self.images.count()

    @property
    def images_over_cap(self) -> int:
        """Found but never attempted, because MAX_IMAGES cut the list short."""
        return self.images_found - (
            self.images_stored + self.images_skipped + self.images_duplicate
        )


def get_asset_upload_path(instance, filename):
    """
    Generates a deterministic path for content-addressed storage.
    Shards on the first two byte-pairs of the sha256 hash to prevent 
    directory performance degradation (e.g., assets/ab/cd/abcdef1234...png).
    """
    ext = os.path.splitext(filename)[1].lower()
    h = instance.sha256
    # Shard into two levels of directories using the first 4 hex characters
    return f"assets/{h[0:2]}/{h[2:4]}/{h}{ext}"


class ImageAsset(models.Model):
    # Enforces database-level uniqueness and provides a lookup index in one shot
    sha256 = models.CharField(max_length=64, unique=True)
    
    # Leverages Pillow for implicit image validation and auto-populating dimensions
    file = models.ImageField(
        upload_to=get_asset_upload_path,
        storage=asset_storage,
        width_field="width",
        height_field="height"
    )
    
    # Populated by Pillow on save
    width = models.PositiveIntegerField(blank=True, null=True)
    height = models.PositiveIntegerField(blank=True, null=True)
    
    content_type = models.CharField(max_length=100, blank=True)
    byte_size = models.PositiveIntegerField()  # Safe up to 2.1GB
    first_seen_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Asset {self.sha256[:8]}"


class SearchImage(models.Model):
    # Asymmetric on_delete strategy prevents data loss while protecting assets
    search = models.ForeignKey(
        Search,  
        on_delete=models.CASCADE,
        related_name="images"
    )
    asset = models.ForeignKey(
        ImageAsset,
        on_delete=models.PROTECT,
        related_name="appearances"
    )
    
    # Crucial distinction: Many locations/URLs can point to a single byte identity
    source_url = models.URLField(max_length=2000)
    position = models.PositiveIntegerField()
    class Meta:
        ordering = ["position"]
        # Modern alternative to unique_together for constraint compliance
        constraints = [
            UniqueConstraint(
                fields=["search", "asset"], 
                name="unique_search_asset"
            )
        ]

    def __str__(self):
        return f"Search {self.search_id} - Asset {self.asset_id} at pos {self.position}"
