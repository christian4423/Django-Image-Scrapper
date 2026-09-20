"""
Four views, one per thing a user can do:

    index    GET   the form, plus the last few searches
    create   POST  take a URL, ingest it, send the user to its page
    detail   GET   one search and the images it found
    history  GET   every search, paginated

All four are function-based. Django's generic class-based views would shorten
`detail` and `history` to a few lines each, but `create` is not a CRUD shape —
it hands the URL to `ingest_url` and lets that decide what rows come out — so
it would stay a hand-written function either way. One paradigm across four
views reads better than two.
"""
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .fetching import FetchingError
from .forms import SearchForm
from .ingest import ingest_url
from .models import Search

# How many past searches the landing page shows before "see all".
RECENT_SEARCHES = 5

# Rows per page on the history list.
HISTORY_PAGE_SIZE = 20


def index(request):
    """The landing page: an empty form and a few recent searches."""
    return render(request, "img_downloader/index.html", _index_context(SearchForm()))


@require_POST
def create(request):
    """
    Take a submitted URL, ingest it, and redirect to the search it produced.

    POST only — the decorator answers a GET with 405 rather than letting a
    crawler or a refresh kick off a download.
    """
    form = SearchForm(request.POST)
    if not form.is_valid():
        # Re-render the landing page with the user's input still in the box
        # and the error beside the field. Form instances carry both.
        return render(request, "img_downloader/index.html", _index_context(form))

    try:
        # Synchronous: the user waits while we fetch the page and its images.
        # Fine for a demo, and `Search.Status.PENDING` is already shaped for
        # the alternative — see "Moving ingest off the request cycle" in the
        # README.
        search = ingest_url(form.cleaned_data["url"])
    except FetchingError as error:
        # Only the page fetch can fail the whole search, and `ingest_url` has
        # already saved that Search as FAILED with this message on it, so the
        # attempt still appears in history. All that is left here is telling
        # the user why, without losing what they typed.
        form.add_error("url", str(error))
        return render(request, "img_downloader/index.html", _index_context(form))

    # Post/Redirect/Get. The browser lands on a plain GET, so refreshing the
    # results page re-reads the search instead of downloading the page again.
    return redirect("img_downloader:detail", pk=search.pk)


def detail(request, pk):
    """One search: its status or error, its counters, and its image grid."""
    search = get_object_or_404(
        # The template walks search.images and reads .asset on each one. Left
        # to itself that is one query per image; prefetching makes it two
        # queries total, however many images the page had.
        Search.objects.prefetch_related("images__asset"),
        pk=pk,
    )
    return render(request, "img_downloader/detail.html", {"search": search})


def history(request):
    """Every search, newest first, in pages."""
    page = Paginator(_searches_with_counts(), HISTORY_PAGE_SIZE).get_page(
        # get_page, not page: it takes whatever is in the query string —
        # missing, "abc", "9999" — and returns a real page instead of raising.
        request.GET.get("page")
    )
    return render(request, "img_downloader/history.html", {"page": page})


def _index_context(form):
    """Context for the landing page, which `create` also re-renders on error."""
    return {
        "form": form,
        "recent_searches": _searches_with_counts()[:RECENT_SEARCHES],
    }


def _searches_with_counts():
    """
    Searches for a list page, newest first (Search.Meta.ordering), each one
    carrying how many images it stored.

    `Search.images_stored` answers the same question, but it is a property
    doing `self.images.count()` — one query per row, so twenty rows on the
    history page would be twenty extra queries. Annotating asks the database
    for all of them in the one query that fetches the rows.
    """
    return Search.objects.annotate(stored_count=Count("images"))
