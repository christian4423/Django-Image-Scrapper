from django.urls import path

app_name = "img_downloader"

urlpatterns = [
    "",                      # index        form + recent searches
    "search/",               # create       POST only → redirect
    "search/<int:pk>/",      # detail       the grid
    "searches/",             # history      full list
]
