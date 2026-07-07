"""Structured logging for the service (Phase 9 — Hardening & Observability).

One place to configure process-wide logging. In production (``LOG_JSON=True``)
every record is emitted as a single JSON object to stdout, enriched with the
request/pipeline ``id`` and the currently-executing ``block`` (carried on
context variables so *every* line within a request is tagged without threading
the values through call signatures). Lifecycle lines additionally carry a
``duration_ms`` extra. ``LOG_JSON=False`` falls back to a human formatter.

The two context setters are called by the API middleware (``id``) and the
pipeline orchestrator (``block``); logs emitted anywhere downstream inherit them.
Starlette runs each request in its own task/thread context, so a set made for
one request never leaks into another.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger.json import JsonFormatter

from bulk_insert.config import Settings

__all__: list[str] = [
    "configure_logging",
    "set_log_id",
    "set_log_block",
]

# Request/pipeline-scoped context carried onto every log record. Defaults render
# as "-" so a line emitted outside any request is still well-formed.
_log_id: ContextVar[str] = ContextVar("log_id", default="-")
_log_block: ContextVar[str] = ContextVar("log_block", default="-")


class _ContextFilter(logging.Filter):
    """Stamp the current ``id`` and ``block`` context onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.id = _log_id.get()
        record.block = _log_block.get()
        return True


def set_log_id(value: str) -> None:
    """Bind the correlation id for the current request context."""
    _log_id.set(value)


def set_log_block(value: str) -> None:
    """Bind the currently-executing pipeline block for the current context."""
    _log_block.set(value)


def _build_formatter(json: bool) -> logging.Formatter:
    if json:
        # The named attributes in the format string become top-level JSON keys;
        # `id`/`block` come from the context filter and any `extra=` (e.g.
        # `duration_ms`) is merged in automatically by the JSON formatter.
        return JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(id)s %(block)s",
            rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
        )
    return logging.Formatter("%(asctime)s %(levelname)-8s [%(id)s|%(block)s] %(name)s: %(message)s")


def configure_logging(cfg: Settings) -> None:
    """Configure root logging from settings. Idempotent — safe to call twice.

    Installs a single stdout handler carrying the context filter and the chosen
    formatter, and routes uvicorn's own loggers through the same handler so the
    whole process speaks one format. The uvicorn access logger is quieted because
    the API middleware emits a richer, structured access line instead.
    """
    level = logging.getLevelNamesMapping().get(cfg.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_build_formatter(cfg.LOG_JSON))
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Unify uvicorn's loggers with ours (they ship their own handlers).
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Our middleware is the access log — silence uvicorn's duplicate line.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
