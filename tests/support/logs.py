"""Capturing the reference ids the disclosure path writes to the log.

``domain.errors`` logs through stdlib ``logging`` rather than a module-level structlog
logger, because ``configure_logging`` sets ``cache_logger_on_first_use`` and a cached
structlog logger is invisible to ``structlog.testing.capture_logs``. These tests need the
opposite of structlog's cached processors: they need to see what was actually emitted.

So the capture goes through pytest's ``caplog``, and this helper reads the log records
structlog's ``PrintLoggerFactory`` receives on the way out.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# `<iso timestamp> [error    ] unexpected_failure reference=abc123 error=RuntimeError ...`
_FIELD = re.compile(r"(\w+)=(?:'([^']*)'|(\S+))")


def _parse(message: str) -> dict[str, Any]:
    return {
        match.group(1): match.group(2) if match.group(2) is not None else match.group(3)
        for match in _FIELD.finditer(message)
    }


@contextmanager
def captured_failure_logs(caplog: Any) -> Iterator[list[dict[str, Any]]]:
    """Every ``unexpected_failure`` record emitted, parsed into fields."""
    records: list[dict[str, Any]] = []
    with caplog.at_level(logging.ERROR, logger="metaedit.domain.errors"):
        yield records
    for record in caplog.records:
        entry = _parse(record.getMessage())
        if record.name == "metaedit.domain.errors" and "unexpected_failure" in record.getMessage():
            entry["exc_info"] = record.exc_info is not None
            records.append(entry)
