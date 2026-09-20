"""
The one thing a user gives us: a URL.

A plain Form, not a ModelForm on Search. A ModelForm would say "this form is
responsible for creating the row", and it is not — `ingest_url` creates the
Search, fills in the title and the counters, and decides whether it ends
COMPLETE or FAILED. The form's job stops at "is this a URL we are willing to
try", which keeps one owner for each side of that line.
"""
from urllib.parse import urlsplit

from django import forms

from .fetching import ALLOWED_SCHEMES


class SearchForm(forms.Form):
    url = forms.URLField(
        label="Page URL",
        # Matches Search.url, so an over-long URL comes back as a field error
        # instead of letting the database raise on save.
        max_length=2000,
        # A user types "example.com", not "https://example.com". Django fills
        # in the missing scheme rather than calling the input invalid.
        assume_scheme="https",
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://example.com",
                "autofocus": True,
                "autocomplete": "off",
            }
        ),
    )

    def clean_url(self):
        """
        Reject schemes we will not fetch.

        URLField's validator accepts ftp:// and ftps:// as well as http(s).
        `fetching` refuses those anyway, so checking here against the same
        ALLOWED_SCHEMES keeps one source of truth and turns what would be a
        fetch failure into an ordinary "fix this field" message.

        A clean_<field> method runs after that field's own validators and only
        if they passed, so a plainly malformed URL still gets one message
        rather than this one stacked on top of it.
        """
        url = self.cleaned_data["url"]
        if urlsplit(url).scheme not in ALLOWED_SCHEMES:
            raise forms.ValidationError(
                "Only http:// and https:// addresses can be downloaded."
            )
        return url
