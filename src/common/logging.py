"""Logging configuration."""

import logging
import sys
from typing import Optional


def setup_logging(level: str = "WARNING", log_file: Optional[str] = None) -> None:
    """Configure the root logger. Call once at process startup.

    Args:
        level: Log level name — DEBUG, INFO, WARNING, ERROR, CRITICAL.
        log_file: Optional path to write logs in addition to stderr.
    """
    numeric = getattr(logging, level.upper(), logging.WARNING)
    fmt = "%(asctime)s %(name)-35s %(levelname)s %(message)s"
    if log_file:
        # File only — don't pollute the terminal
        handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    else:
        handlers = [logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=numeric, format=fmt, handlers=handlers, force=True)
