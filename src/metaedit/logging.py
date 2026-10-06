"""Structured logging. Logs are event streams; secrets never appear in them."""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog


def configure_logging(
    *,
    level: str = "INFO",
    as_json: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog + stdlib logging exactly once at process start.

    ``stream`` defaults to stdout, which is right for a server whose logs *are* its
    output. A command that prints a machine-readable report on stdout passes stderr
    instead, so a caller can pipe the report into ``jq`` without stripping log lines.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    target = stream or sys.stdout
    logging.basicConfig(format="%(message)s", stream=target, level=numeric_level)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if as_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=target),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def redact(value: str, *, keep: int = 4) -> str:
    """Return a fingerprint of an opaque secret, never the secret itself."""
    if not value:
        return "unset"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"
