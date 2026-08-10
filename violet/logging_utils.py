"""Shared logging setup, so miner/validator/router logs look alike."""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(component: str, level: str | None = None) -> logging.Logger:
    """Configure root logging once and return the component's logger."""
    global _CONFIGURED

    resolved = (level or os.getenv("VIOLET_LOG_LEVEL", "INFO")).upper()
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root = logging.getLogger()
        root.handlers[:] = [handler]
        root.setLevel(resolved)
        # These are chatty at DEBUG and drown out anything useful.
        for noisy in ("websockets", "aiohttp.access", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
        _CONFIGURED = True

    return logging.getLogger(f"violet.{component}")
