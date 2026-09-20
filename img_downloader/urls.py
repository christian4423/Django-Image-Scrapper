from django.urls import path

from . import views

# Namespaced, so templates and redirects name a route as
# "img_downloader:detail" and never hard-code a path.
app_name = "img_downloader"

urlpatterns = [
    path("", views.index, name="index"),
    # The verb lives in the method, not the path: POST here creates a search.
    # No clash with the route below — that one only matches an integer.
    path("search/", views.create, name="create"),
    path("search/<int:pk>/", views.detail, name="detail"),
    path("searches/", views.history, name="history"),
]
