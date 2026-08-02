"""A log file that outlives the terminal that started the server.

The incident that motivated this had to be reconstructed from raw Claude session transcripts
under ~/.claude/projects/, because uvicorn logged to a stdout that died with the terminal and
there was no log file anywhere. Whoever debugs the next one deserves better.

    <workspace>/logs/manager.log      rotated at 5MB, 5 kept

It goes in the WORKING FOLDER, not the system folder: it is about this project's board, it
must survive an update, and it must exist however the server was started — by `./run.sh`, by
launchd, or by uvicorn directly. Console output is kept as well, for when you are watching.
"""
from __future__ import annotations

import logging
import logging.handlers
import os

LOG_DIRNAME = "logs"
LOG_FILENAME = "manager.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5

_configured = False


def log_path(data_dir: str) -> str:
    return os.path.join(data_dir, LOG_DIRNAME, LOG_FILENAME)


def setup_logging(data_dir: str, level: int = logging.INFO) -> str:
    """Attach a rotating file handler to the root logger, so uvicorn's records land in it
    too — its access log is exactly what you want when reconstructing a death."""
    global _configured
    path = log_path(data_dir)
    if _configured:
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    _configured = True
    return path
