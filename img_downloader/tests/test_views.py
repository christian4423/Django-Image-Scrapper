"""
Tests for img_downloader.views.

Django's test client drives these end to end — routing, the view, the form and
the template all run — so what is asserted is what a browser would actually be
sent. The only thing still faked is the network: `create` calls `ingest_url`
inside the request, so these reuse fakes.py exactly as the ingest tests do.

Three things here are worth more than their line count:

  * `create` is POST-only, and the 405 case is the only thing that proves
    @require_POST is still on it. A GET that quietly succeeded would let a
    browser refresh, or a crawler, start a download.
  * Rejected input must leave nothing behind. Every validation test asserts
    that no Search row was created and — because these run against an empty
    fake internet — that no request went out either.
  * `detail` and `history` are each written in a particular shape to avoid an
    N+1 query: prefetch_related on one, annotate on the other. HTML assertions
    would pass just as happily against the slow version, so both are pinned
    with assertNumQueries at two different row counts. A count that grows with
    the data is the failure those tests exist to catch.
"""
import hashlib

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from img_downloader.forms import SearchForm
from img_downloader.models import ImageAsset, Search, SearchImage
from img_downloader.views import HISTORY_PAGE_SIZE, HISTORY_RECENT_SEARCHES

from .fakes import (
    FakeNetworkMixin,
    FakeResponse,
    TemporaryMediaMixin,
    html_page,
    png,
    png_response,
    robots,
)

PAGE = "https://example.com/"
ROBOTS = "https://example.com/robots.txt"


def image_url(name):
    return f"https://example.com/{name}"


class WebTestCase(TestCase):
    """Routes by name, never by literal path, so urls.py stays free to move."""

    def setUp(self):
        super().setUp()
        self.index_url = reverse("img_downloader:index")
        self.create_url = reverse("img_downloader:create")
        self.history_url = reverse("img_downloader:history")

    def detail_url(self, pk):
        return reverse("img_downloader:detail", args=[pk])

    def make_search(self, url=PAGE, status=Search.Status.COMPLETE, **fields):
        return Search.objects.create(url=url, status=status, **fields)


class AssetBuildingMixin(TemporaryMediaMixin):
    """
    Builds Searches with images attached, without going near the network.

    The view tests that care about rendering and query counts need rows, not a
    download; driving `ingest_url` for them would test ingest a second time and
    make a template regression look like a fetching bug.
    """

    def make_asset(self, seed):
        body = png(seed)
        digest = hashlib.sha256(body).hexdigest()
        asset = ImageAsset(sha256=digest, content_type="image/png", byte_size=len(body))
        asset.file.save(f"{digest}.png", ContentFile(body), save=False)
        asset.save()
        return asset

    def make_search_with_images(self, count, **fields):
        search = self.make_search(**fields)
        SearchImage.objects.bulk_create([
            SearchImage(
                search=search,
                asset=self.make_asset(seed),
                source_url=image_url(f"{seed}.png"),
                position=position,
            )
            # Seeds are globally unique, so two searches in one test never
            # collide on the sha256 unique constraint.
            for position, seed in enumerate(range(ImageAsset.objects.count(), count))
        ])
        return search


class IndexTests(WebTestCase):
    def test_renders_the_form(self):
        response = self.client.get(self.index_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "img_downloader/index.html")
        self.assertIsInstance(response.context["form"], SearchForm)
        # The form posts to `create`, not back to itself.
        self.assertContains(response, f'action="{self.create_url}"')

    def test_says_so_when_there_is_no_history_yet(self):
        response = self.client.get(self.index_url)

        self.assertContains(response, "No recent searches available.")

    def test_lists_recent_searches_newest_first(self):
        older = self.make_search(url="https://example.com/older", page_title="Older")
        newer = self.make_search(url="https://example.com/newer", page_title="Newer")

        response = self.client.get(self.index_url)

        self.assertEqual(
            [search.pk for search in response.context["recent_searches"]],
            [newer.pk, older.pk],
        )
        self.assertContains(response, self.detail_url(newer.pk))

    def test_shows_only_the_most_recent_handful(self):
        for index in range(HISTORY_RECENT_SEARCHES + 3):
            self.make_search(url=f"https://example.com/{index}")

        response = self.client.get(self.index_url)

        self.assertEqual(len(response.context["recent_searches"]), HISTORY_RECENT_SEARCHES)


class CreateMethodTests(WebTestCase):
    """The route exists for one verb."""

    def test_a_get_is_refused(self):
        response = self.client.get(self.create_url)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(Search.objects.count(), 0)


class CreateValidationTests(FakeNetworkMixin, WebTestCase):
    """
    Input the form refuses never reaches ingest.

    The fake internet installed here is empty, so any outbound request fails
    the test loudly rather than silently reaching the real network — which is
    the assertion that matters: rejected input costs nothing.
    """

    def setUp(self):
        super().setUp()
        self.serve({})

    def assertRejected(self, url, expected_fragment):
        response = self.client.post(self.create_url, {"url": url})

        self.assertEqual(response.status_code, 200, "a rejected URL re-renders, it does not redirect")
        self.assertTemplateUsed(response, "img_downloader/index.html")
        errors = response.context["form"].errors["url"]
        self.assertIn(expected_fragment, " ".join(errors))
        self.assertEqual(Search.objects.count(), 0, "a rejected URL reached the database")
        return response

    def test_rejects_something_that_is_not_a_url(self):
        response = self.assertRejected("not a url", "Enter a valid URL")

        # The error is rendered, not merely present in the context.
        self.assertContains(response, "Enter a valid URL")

    def test_rejects_an_empty_submission(self):
        self.assertRejected("", "This field is required")

    def test_rejects_a_scheme_we_will_not_fetch(self):
        """clean_url, turning what would be a fetch failure into a field error."""
        self.assertRejected(
            "ftp://example.com/files/",
            "Only http:// and https:// addresses can be downloaded.",
        )

    def test_rejects_a_url_too_long_for_the_column(self):
        """max_length matches Search.url, so this is a field error, not a database one."""
        self.assertRejected(f"https://example.com/{'a' * 2000}", "at most 2000 characters")

    def test_keeps_what_the_user_typed(self):
        response = self.client.post(self.create_url, {"url": "ftp://example.com/files/"})

        self.assertContains(response, 'value="ftp://example.com/files/"')


class CreateTests(FakeNetworkMixin, AssetBuildingMixin, WebTestCase):
    """POST a URL, ingest it, and send the browser somewhere it can refresh."""

    def serve_page(self, images, title="Test Page"):
        self.serve({
            ROBOTS: robots(),
            PAGE: FakeResponse(200, {"Content-Type": "text/html"}, html_page(images, title)),
            **images,
        })

    def test_a_good_url_redirects_to_the_search_it_created(self):
        self.serve_page({image_url("a.png"): png_response(1)}, title="Two Cats")

        response = self.client.post(self.create_url, {"url": PAGE})

        search = Search.objects.get()
        # assertRedirects follows the Location header, so this also proves the
        # page the user lands on renders — the whole Post/Redirect/Get round
        # trip, not just the 302.
        self.assertRedirects(response, self.detail_url(search.pk))
        self.assertEqual(search.status, Search.Status.COMPLETE)
        self.assertEqual(search.page_title, "Two Cats")
        self.assertEqual(search.images.count(), 1)

    def test_fills_in_a_missing_scheme(self):
        """A user types example.com; the form assumes https rather than refusing."""
        # Routed without the trailing slash on purpose: assume_scheme prefixes
        # the scheme and changes nothing else, and that is what gets fetched.
        bare_host = "https://example.com"
        self.serve({
            ROBOTS: robots(),
            bare_host: FakeResponse(200, {"Content-Type": "text/html"}, html_page([])),
        })

        response = self.client.post(self.create_url, {"url": "example.com"})

        search = Search.objects.get()
        self.assertRedirects(response, self.detail_url(search.pk))
        self.assertEqual(search.url, bare_host)

    def test_a_page_that_404s_is_reported_and_still_recorded(self):
        self.serve({ROBOTS: robots(), PAGE: FakeResponse(404)})

        response = self.client.post(self.create_url, {"url": PAGE})

        self.assertEqual(response.status_code, 200, "a failed fetch re-renders, it does not redirect")
        self.assertTemplateUsed(response, "img_downloader/index.html")
        self.assertIn("404", " ".join(response.context["form"].errors["url"]))

        # ingest_url saved the attempt before re-raising, so it is not lost:
        # the row exists, and the page just rendered already links to it.
        search = Search.objects.get(url=PAGE)
        self.assertEqual(search.status, Search.Status.FAILED)
        self.assertContains(response, self.detail_url(search.pk))

    def test_an_unfetchable_host_is_reported_the_same_way(self):
        """SSRF protection reaching the user as an ordinary error message."""
        self.serve({}, resolves_to={"localhost": "127.0.0.1"})

        response = self.client.post(self.create_url, {"url": "http://localhost/admin"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Unsafe IP detected", " ".join(response.context["form"].errors["url"]))
        self.assertEqual(Search.objects.get().status, Search.Status.FAILED)


class DetailTests(AssetBuildingMixin, WebTestCase):
    def test_renders_a_search_and_its_images(self):
        search = self.make_search_with_images(3, page_title="Two Cats")

        response = self.client.get(self.detail_url(search.pk))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "img_downloader/detail.html")
        self.assertContains(response, "Two Cats")
        self.assertContains(response, "Complete")
        for image in search.images.all():
            self.assertContains(response, image.asset.file.url)

    def test_shows_the_error_instead_of_counters_when_the_search_failed(self):
        search = self.make_search(
            status=Search.Status.FAILED, error_message="Unsafe IP detected: 127.0.0.1"
        )

        response = self.client.get(self.detail_url(search.pk))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unsafe IP detected: 127.0.0.1")
        self.assertNotContains(response, "Overcap", msg_prefix="counters shown for a failed search")

    def test_a_search_that_stored_nothing_still_renders(self):
        search = self.make_search(images_found=2, images_skipped=2)

        response = self.client.get(self.detail_url(search.pk))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Images are available for this search.")

    def test_a_missing_search_is_a_404(self):
        response = self.client.get(self.detail_url(999999))

        self.assertEqual(response.status_code, 404)

    def test_the_query_count_does_not_grow_with_the_image_count(self):
        """
        What prefetch_related("images__asset") buys: one query for the search,
        one for its rows, one for their assets — however many images there are.
        Without it the template's `image.asset` would add one query per image.
        """
        one_image = self.make_search_with_images(1)
        many_images = self.make_search_with_images(12)

        with self.assertNumQueries(3):
            self.client.get(self.detail_url(one_image.pk))

        with self.assertNumQueries(3):
            self.client.get(self.detail_url(many_images.pk))


class HistoryTests(WebTestCase):
    def test_says_so_when_there_is_nothing_to_show(self):
        response = self.client.get(self.history_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No search history available.")

    def test_lists_searches_newest_first_with_their_stored_counts(self):
        older = self.make_search(url="https://example.com/older")
        newer = self.make_search(url="https://example.com/newer")

        response = self.client.get(self.history_url)

        page = response.context["page"]
        self.assertEqual([search.pk for search in page], [newer.pk, older.pk])
        # stored_count is the annotation the template reads; the property of
        # the same name would be a query per row.
        self.assertEqual([search.stored_count for search in page], [0, 0])

    def test_splits_a_long_history_into_pages(self):
        overflow = 3
        for index in range(HISTORY_PAGE_SIZE + overflow):
            self.make_search(url=f"https://example.com/{index}")

        first = self.client.get(self.history_url)
        second = self.client.get(self.history_url, {"page": 2})

        self.assertEqual(len(first.context["page"].object_list), HISTORY_PAGE_SIZE)
        self.assertEqual(len(second.context["page"].object_list), overflow)
        self.assertEqual(second.context["page"].paginator.count, HISTORY_PAGE_SIZE + overflow)

    def test_a_page_number_that_is_not_a_number_falls_back_to_the_first(self):
        """get_page, not page: a hand-edited query string is not a server error."""
        self.make_search()

        response = self.client.get(self.history_url, {"page": "abc"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].number, 1)

    def test_a_page_number_past_the_end_falls_back_to_the_last(self):
        self.make_search()

        response = self.client.get(self.history_url, {"page": 999})

        self.assertEqual(response.status_code, 200)
        page = response.context["page"]
        self.assertEqual(page.number, page.paginator.num_pages)

    def test_the_query_count_does_not_grow_with_the_row_count(self):
        """
        What the Count annotation buys. `Search.images_stored` answers the same
        question with one query per row, so the history page would cost a query
        per search listed; annotating folds them into the query that fetches
        the rows, leaving the count (one) plus the rows (one).
        """
        self.make_search(url="https://example.com/only")

        with self.assertNumQueries(2):
            self.client.get(self.history_url)

        for index in range(HISTORY_PAGE_SIZE - 1):
            self.make_search(url=f"https://example.com/{index}")

        with self.assertNumQueries(2):
            self.client.get(self.history_url)
