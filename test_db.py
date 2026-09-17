#!/usr/bin/env python
"""Test that Django can connect to the database configured in setting.py."""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "setting")

# Raw connectivity check only, so we skip django.setup() (avoids needing INSTALLED_APPS to be importable).
from django.db import connection


def main() -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version();")
            (version,) = cursor.fetchone()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1

    print("Connection succeeded.")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
