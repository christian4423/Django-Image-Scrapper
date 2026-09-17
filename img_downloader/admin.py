from django.contrib import admin
from django.utils.html import format_html
from .models import Search, ImageAsset, SearchImage


class SearchImageInline(admin.TabularInline):
    """
    Embeds SearchImage records directly inside the Search change page.
    This creates an inline grid view of all images crawled during that specific search.
    """
    model = SearchImage
    extra = 0  # Prevents empty placeholder rows from cluttering the UI
    raw_id_fields = ("asset",)  # Highly recommended: prevents a dropdown from loading thousands of assets
    fields = ("position", "asset_thumbnail", "source_url")
    readonly_fields = ("asset_thumbnail",)

    @admin.display(description="Preview")
    def asset_thumbnail(self, obj):
        if obj.asset and obj.asset.file:
            return format_html('<img src="{}" style="max-height: 50px; border-radius: 4px;" />', obj.asset.file.url)
        return "-"



@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    """
    Represents the 'Search' context. Registering this with a useful list_display
    gives you an instantaneous, sortable overview of your scraping pipeline's status.
    """
    list_display = ("id", "url", "page_title", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("url", "page_title")
    
    # This embeds the junction table directly on this page
    inlines = [SearchImageInline]


@admin.register(ImageAsset)
class ImageAssetAdmin(admin.ModelAdmin):
    """
    Allows you to verify your deduplicated asset storage. If two different searches 
    find the same image, you will see exactly 1 asset record here with multiple appearances.
    """
    list_display = ("thumb", "sha256_short", "width", "height", "byte_size_kb", "appearance_count", "first_seen_at")
    search_fields = ("sha256",)
    readonly_fields = ("thumb",)

    @admin.display(description="SHA256")
    def sha256_short(self, obj):
        return f"{obj.sha256[:8]}..."

    @admin.display(description="Size")
    def byte_size_kb(self, obj):
        return f"{obj.byte_size / 1024:.1f} KB"

    @admin.display(description="Appearances")
    def appearance_count(self, obj):
        """Leverages the related_name='appearances' to show cross-search popularity."""
        return obj.appearances.count()

    @admin.display(description="Image")
    def thumb(self, obj):
        if obj.file:
            return format_html('<img src="{}" style="max-height: 60px; border-radius: 4px;" />', obj.file.url)
        return "-"


@admin.register(SearchImage)
class SearchImageAdmin(admin.ModelAdmin):
    """
    Registered independently so you can audit the global stream of raw join records 
    outside of the individual search view context if needed.
    """
    list_display = ("id", "search_id", "asset_id", "position", "source_url")
    raw_id_fields = ("search", "asset")
