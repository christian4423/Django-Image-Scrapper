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
