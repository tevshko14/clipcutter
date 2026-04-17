"""Logging configuration for ClipCutter.

Writes to both stdout (for the terminal that launched the app) AND a rotating
file at ~/.clipcutter/clipcutter.log, so background-thread failures remain
debuggable after a crash or window close.

Usage:
    from cc_log import log
    log.info("something happened")
    log.error("something broke: %s", exc)
"""

import logging
from logging.handlers import RotatingFileHandler

from cc_config import APP_DIR

_LOG_PATH = APP_DIR / "clipcutter.log"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("clipcutter")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't double-emit through the root logger

    if logger.handlers:  # already configured (re-import)
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Rotating file: 2 MB per file, keep last 3 — plenty for a desktop app.
    fh = RotatingFileHandler(_LOG_PATH, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Mirror to stdout so the launcher's launch.log captures it too.
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = _build_logger()
