from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jetriever_trace_id", default=""
)
AUDIT_LOGGER = logging.getLogger("jetriever.audit")


def configure_audit_logging(path: Path, max_bytes: int, backup_count: int) -> None:
    """Configure one rotating JSONL audit sink for this process."""

    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(AUDIT_LOGGER.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename).resolve() == resolved
        ):
            return
        AUDIT_LOGGER.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        resolved,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    AUDIT_LOGGER.addHandler(handler)
    AUDIT_LOGGER.setLevel(logging.INFO)
    AUDIT_LOGGER.propagate = False


def new_trace_id() -> str:
    return uuid.uuid4().hex


def hash_identifier(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def audit_event(event: str, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
        "trace_id": TRACE_ID.get() or None,
        **fields,
    }
    AUDIT_LOGGER.info(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)
    )
