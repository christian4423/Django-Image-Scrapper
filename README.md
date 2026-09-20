# Software Developer Interview Task

Hello Christian,

I hope you’re doing well and we’re looking forward to talking with you next week.

For the presentation portion of your interview we’d like you to complete and present on the following task.

## The Problem

Create a web application capable of downloading and displaying all of the images found at a URL. In the provided repository, please create a relational database-backed web application capable of:

1. Allowing the User to enter a URL.
2. Downloading and displaying all of the images found at the URL back to the user.
3. Save the searched URL and references to the downloaded files to a database
4. Generate a navigable list of past searches that when selected will redisplay the previously downloaded files.

## Tools and Guidance

For this task we’re making use of Git version control. While experience with Git is certainly a value add, assessing your familiarity with that software is not the purpose of the exercise. Instead, if used throughout the process, Git will allow us to see your work.

You should use Python to complete this task. In the absence of a strong personal preference, [Django](https://www.djangoproject.com/) would be a great choice.

Functionality is more important than aesthetics. While an attractive front end is appreciated, we are most interested in how you approach the problem and structure your application to address the features outlined above.

Your audience is a group of web software developers. We will be looking at the application source code, HTML, and stylesheets. Be prepared to:

* demonstrate how your application operates

* tell us what choices you made while constructing it

* describe what ideas you may have for enhancing your application were you to continue development on it

* walk through your code

Plan for 20-25 minutes to present your solution, with the remaining time reserved for Q\&A with the panel.

AI use is acceptable for this exercise. Regardless of its use, it is important to fully understand and describe everything about the application and to be able to speak to how it may be improved both functionally and technically. If AI is used, be prepared to speak to how it was used.

## Getting started

1. Open a terminal
2. Navigate to a directory in which you’d like to store your work and checkout the project with the line below.
3. git clone <https://github.com/academic-innovation/christian-matsoukis-developer-interview.git>

# Setting up project:

## Database

For MacOS in

make a file called .pg\_service.conf and .my\_pgpass in \~

\~/.pg\_service.conf contents:

```
[img_downloader]
host=localhost
user=postgres
dbname=img_downloader
port=5432
```

\~/.my\_pgpass contents:

```
localhost:5432:NAME:USER:PASSWORD
```

run `chmod 600` on each

```
chmod 600 ~/.my_pgpass
chmod 600 ~/.pg_service.conf
```

***

In the root of this project, make sure settings.py has

```python
...
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "OPTIONS": {
            "service": "img_downloader",
            "passfile": str(Path.home() / ".my_pgpass"),
        },
    }
}
...
```

Make database

```
psql -U username -d postgres
CREATE DATABASE img_downloader;
\l
```

## Virtual Environment

In project root

```
uv init
uv venv
source .venv/bin/activate
uv pip install Django, psycopg
```

***

# Running the tests

```
python manage.py test
```

43 tests, about half a second, and no network access — `img_downloader/tests/fakes.py`
stands in for the internet, so a 404, a redirect loop, a link-local address or a
dropped connection are all reproducible rather than dependent on some real site
behaving that way today.

The `fetching` tests need no database and run as `SimpleTestCase`. The `ingest`
tests drive the real `fetch_page` and `download_image` over that fake network
instead of stubbing them, so the seam between the two modules is covered: a 404
on one image has to travel from `requests`, through error translation, to a
skipped image on a search that still completes.

***

# Ideas for Further Development

Known limitations and where I would take the application next.

## Orphaned files from rolled-back ingests

`ImageAsset.file.save()` writes to storage the moment it is called, but it runs
inside the `transaction.atomic()` block in `ingest_url`. Storage is not
transactional: when the transaction rolls back, the database row disappears and
the bytes on disk stay, referenced by nothing. The same asymmetry applies in
reverse — `ImageAsset.objects.all().delete()` drops rows and leaves every file
in place.

The usual fix, deferring the write with `transaction.on_commit()`, does not fit
here. `ImageAsset.file` declares `width_field`/`height_field`, so Django reads
the image to populate its dimensions *during* the save; the file has to exist
before the row can be written. Deferring the write would only invert the
failure — a crash between commit and callback would leave a row with no file,
which is worse, because the application would then serve a broken image rather
than waste a few kilobytes of disk.

I would instead treat drift between the two stores as expected and reconcile
it: a management command that walks `MEDIA_ROOT` and deletes any file with no
matching `ImageAsset`. Because the path is derived from the content hash, each
check is one indexed lookup, and the same command cleans up after deletes.
Run it from cron.

## Politeness limits on outbound requests

Images are downloaded in parallel through a `ThreadPoolExecutor` bounded at
`MAX_DOWNLOAD_WORKERS`, but that is a global cap, not a per-host one. It behaves
like a per-host cap in practice, because a page's images almost always come from
one or two origins — but a page that fans out across many hosts could open eight
connections to each of them. Keying a semaphore on the hostname would make the
limit mean what it says.

Relatedly, `robots.txt` is fetched and parsed but only `can_fetch` is consulted.
`Crawl-delay` is sitting right there in the parsed result and is ignored.

## Moving ingest off the request cycle

Even parallelized, fetching a page and its images is too slow to do inside an
HTTP request. `Search.Status` is already shaped for the alternative: `PENDING`
exists precisely so a row can be created and returned immediately while the
work happens elsewhere. A task queue (Celery, RQ, django-q) would take the
`Search` id, and the detail page would poll or subscribe until the status
flips. The current synchronous `ingest_url` then becomes the body of the task.

## Listing which images failed, and why

`Search` records how many images were found, skipped, deduplicated and cut off
by the cap, which is enough for the gallery to explain itself. What it does not
record is *which* URLs failed and for what reason — that only reaches the log.

A `SkippedImage` table (search, source_url, reason, detail) would let a detail
view list them. It was left out on purpose: the counters answer the question a
user actually asks, and a separate table can be added later without changing
what `search.images` means to templates already written against it.

## SVG and other vector formats

SVG is rejected deliberately, at the content-type check, and that is a security
decision rather than a gap. SVG is an active-content format — it can carry
`<script>`, event handlers and external references, all of which a browser
executes — and assets are served back from our own origin, so storing one would
be stored XSS: a page we ingested could run JavaScript against our own users'
sessions. Refusing on the declared type also means the body is never
downloaded. Because a server can mislabel an SVG as `image/png`, the Pillow
decode in `ingest.py` is kept as a second line.

On the Wikipedia test page that accounts for 7 of 59 images, which is a real
cost: those are logos and icons a user would expect to see. Supporting them
safely means either rasterizing at ingest with something like `cairosvg`, or a
separate model backed by a plain `FileField` with the SVG sanitized and served
from a separate origin — not just `FileField` on its own, which would reproduce
exactly the vulnerability above.

## Race on asset creation

`ImageAsset.objects.filter(sha256=digest).first()` followed by a create is
check-then-act. Two ingests of the same image running at once will both miss
and both insert, and one will fail the `sha256` unique constraint. Today
nothing runs concurrently so it cannot happen, but it becomes reachable the
moment downloads are parallelized or a second worker starts. `get_or_create`,
or an explicit `INSERT ... ON CONFLICT DO NOTHING`, closes it.

## Smaller items

- The `robots.txt` cache is an `lru_cache` with no expiry, so a long-running
  process never notices a change. Fine for one-shot ingests, wrong for a
  persistent worker.
- SSRF protection resolves the hostname and then lets `requests` resolve it
  again when connecting, leaving a DNS-rebinding window. Closing it properly
  means pinning the validated IP for the connection.
- `MAX_IMAGES`, the byte caps and the timeouts are module constants; they
  belong in settings so they can be tuned per environment.
