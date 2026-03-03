"""
app/core/logging.py

Configures structured, human-readable logging for the application.

Design notes:
- Single stdout handler — plays nicely with Docker / CloudWatch / ECS
- Timestamp + level + logger name in every line for easy grep
- Call configure_logging() once at application startup (in main.py lifespan)
- Use get_logger(__name__) in every module for namespaced loggers

Future: swap StreamHandler for a JSON formatter (python-json-logger)
        when shipping to a log aggregator (Datadog, ELK, CloudWatch Insights).
"""

import logging
import sys
from typing import Optional


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: Optional[str] = None) -> None:
    """
    Set up the root logger with a consistent format.

    Should be called exactly once during application startup.
    Subsequent calls are safe but redundant — handlers are not duplicated
    because we clear the root logger's handler list first.

    Args:
        level: Override log level string (e.g. "DEBUG"). Falls back to the
               LOG_LEVEL env var via Settings if not provided.
    """
    from app.core.config import get_settings  # local import to avoid circular dep

    if level is None:
        level = get_settings().LOG_LEVEL

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()  # prevent duplicate handlers on reload
    root.setLevel(numeric_level)
    root.addHandler(handler)

    # Quieten noisy third-party loggers in non-debug mode
    if numeric_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("faiss").setLevel(logging.WARNING)
        logging.getLogger("pdfminer").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Return a namespaced logger.

    Usage:
        logger = get_logger(__name__)
        logger.info("Starting service ...")
    """
    return logging.getLogger(name)
