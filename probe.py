from img_downloader.ingest import ingest_url
from img_downloader.models import ImageAsset

s = ingest_url("https://en.wikipedia.org/wiki/Cat")
print("search:", s.id, s.status, repr(s.page_title))
print("images:", s.images.count())
for si in s.images.select_related("asset")[:10]:
    a = si.asset
    print(f"  {si.position:>3}  {a.sha256[:8]}  {a.width}x{a.height}  {a.byte_size:>8}B  {si.source_url[:60]}")
print("assets total:", ImageAsset.objects.count())
