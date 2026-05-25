"""Structured logging for the worker.

Default output is JSON, one record per line — easier to filter in
RunPod's log viewer, CloudWatch, Loki, or Axiom than free-form prints.
Local development can flip to human-readable text via ``LOG_FORMAT=text``.

A ``job_id`` context var is auto-injected into every log line emitted
from within ``handler()``. Per RunPod's docs: "Include the job ID or
request ID in log entries for traceability." Correlating the 50 log
lines a single job emits is otherwise a timestamp-archaeology exercise.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from typing import Any


# Set by handler.handler() at the top of each request; read by the
# formatter on every emission. ContextVars are asyncio-safe — concurrent
# jobs in the same event loop don't bleed into each other's context.
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mineru_job_id", default=None
)

# Standard LogRecord attributes — anything else on the record was attached
# by the caller via ``extra=`` and is part of the structured payload.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One-line JSON per record.

    Always includes ``ts``, ``level``, ``msg``. Anything passed via
    ``logger.info(..., extra={...})`` is merged at the top level so dashboards
    can filter by ``backend``, ``phase``, ``elapsed_ms``, etc. without regex.
    """

    def format(self, record: logging.LogRecord) -> str:
        ms = int(record.msecs)
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{ms:03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (jid := job_id_var.get()) is not None:
            out["job_id"] = jid
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    """Compact human-readable format for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts: list[str] = []
        if (jid := job_id_var.get()) is not None:
            parts.append(f"job_id={jid}")
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                parts.append(f"{k}={v}")
        if parts:
            base += " " + " ".join(parts)
        return base


_configured = False


def configure(level: str = "INFO") -> None:
    """Install the formatter on the root logger. Idempotent.

    ``LOG_FORMAT`` env var selects ``json`` (default) or ``text``.
    """
    global _configured
    if _configured:
        return
    fmt_name = os.environ.get("LOG_FORMAT", "json").lower()
    formatter: logging.Formatter = (
        TextFormatter() if fmt_name == "text" else JsonFormatter()
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Clear default handlers (e.g. from `runpod` SDK) so we don't double-emit.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Module-level convenience: configure if not yet done, then return a logger."""
    configure()
    return logging.getLogger(name)
