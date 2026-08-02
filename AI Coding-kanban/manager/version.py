"""What version of the system this is.

Two different numbers, and the difference between them is the whole point of the update:

    system version   the release of THIS code — `VERSION`, tagged in git.
    schema version   how far the WORKING FOLDER has been migrated (see manager/migrations).

An update moves the first one and then runs the migrations that move the second one to
match. Both must be answerable offline, which is why the version is a file and not a
network call.
"""
from __future__ import annotations

import os

SYSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_PATH = os.path.join(SYSTEM_DIR, "VERSION")
DEFAULTS_DIR = os.path.join(SYSTEM_DIR, "workers.default")
MANAGER_DEFAULT_PATH = os.path.join(SYSTEM_DIR, "MANAGER.default.md")


def system_version() -> str:
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"
