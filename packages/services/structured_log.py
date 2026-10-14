"""One-line JSON logs that Cloud Logging ingests as structured entries.

Cloud Run forwards stdout/stderr; a JSON line with "severity" and "message" becomes a
structured LogEntry, and "logging.googleapis.com/trace" ties it to the request trace.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

_TRACE_HEADER = re.compile(r"([0-9a-f]{32})(?:/[0-9]{1,20})?(?:;o=[01])?")


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            entry.update(fields)
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__  # type only: no payload echo
        return json.dumps(entry, ensure_ascii=False, default=str)


def structured_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(CloudLoggingFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def well_formed_trace_header(header: str | None) -> bool:
    """True for an X-Cloud-Trace-Context value safe to log or forward as is."""
    return _TRACE_HEADER.fullmatch(header or "") is not None


def trace_field(project: str, header: str | None) -> dict[str, str]:
    """Map an X-Cloud-Trace-Context header to the LogEntry trace field, if well formed."""
    match = _TRACE_HEADER.fullmatch(header or "")
    if not match or not project:
        return {}
    return {"logging.googleapis.com/trace": f"projects/{project}/traces/{match.group(1)}"}
