"""Structured JSON logging configuration for Lambda handlers.

Provides a :class:`StructuredFormatter` that outputs one JSON object per
line (compatible with CloudWatch Logs Insights) and a
:func:`configure_lambda_logging` helper that sets up the root logger with
execution context fields.

Requirements: 10.3.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON log formatter that outputs one JSON object per line.

    Each log entry includes:
    - timestamp (ISO 8601 UTC)
    - level (log level name)
    - message (formatted log message)
    - Any extra fields passed via the ``extra`` dict

    Context fields (cohort_id, current_module, attempt_number) are
    injected by :func:`configure_lambda_logging` and appear in every
    log entry.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a single-line JSON object."""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        # Add context fields injected by the ContextFilter
        for field in ("cohort_id", "current_module", "attempt_number"):
            if hasattr(record, field):
                log_entry[field] = getattr(record, field)

        # Add any extra fields passed via logger.info(..., extra={...})
        # Exclude standard LogRecord attributes and our context fields
        _standard_attrs = {
            "name", "msg", "args", "created", "relativeCreated",
            "thread", "threadName", "msecs", "filename", "funcName",
            "levelno", "lineno", "module", "exc_info", "exc_text",
            "stack_info", "pathname", "processName", "process",
            "message", "levelname", "taskName",
            # Our context fields (already handled above)
            "cohort_id", "current_module", "attempt_number",
        }
        for key, value in record.__dict__.items():
            if key not in _standard_attrs and not key.startswith("_"):
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


class _ContextFilter(logging.Filter):
    """Logging filter that injects execution context into every record."""

    def __init__(
        self,
        cohort_id: str = "",
        current_module: str = "",
        attempt_number: int = 0,
    ) -> None:
        super().__init__()
        self.cohort_id = cohort_id
        self.current_module = current_module
        self.attempt_number = attempt_number

    def filter(self, record: logging.LogRecord) -> bool:
        """Inject context fields into the log record."""
        record.cohort_id = self.cohort_id  # type: ignore[attr-defined]
        record.current_module = self.current_module  # type: ignore[attr-defined]
        record.attempt_number = self.attempt_number  # type: ignore[attr-defined]
        return True


def configure_lambda_logging(
    cohort_id: str = "",
    module: str = "",
    attempt_number: int = 0,
    logger_name: str | None = None,
) -> logging.Logger:
    """Configure structured JSON logging for a Lambda handler.

    Sets up the specified logger (or root logger) with:
    - A :class:`StructuredFormatter` that outputs JSON
    - A :class:`_ContextFilter` that injects cohort_id, current_module,
      and attempt_number into every log entry

    Parameters
    ----------
    cohort_id : str
        Cohort identifier for this execution.
    module : str
        Current GATK-SV module being processed.
    attempt_number : int
        Current retry attempt number.
    logger_name : str | None
        Logger name to configure. If None, configures the root logger.

    Returns
    -------
    logging.Logger
        The configured logger instance.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicate output
    logger.handlers.clear()

    # Remove existing context filters
    for f in list(logger.filters):
        if isinstance(f, _ContextFilter):
            logger.removeFilter(f)

    # Add structured formatter handler
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)

    # Add context filter
    context_filter = _ContextFilter(
        cohort_id=cohort_id,
        current_module=module,
        attempt_number=attempt_number,
    )
    logger.addFilter(context_filter)

    return logger
