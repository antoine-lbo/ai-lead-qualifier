"""
Structured logging configuration for the AI Lead Qualifier.

Provides JSON-formatted logs with correlation IDs, performance
tracking, and configurable log levels per module.
"""

import logging
import logging.config
import json
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from src.config import settings


# ─── JSON Formatter ──────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Formats log records as structured JSON for log aggregation."""

    def __init__(self, service_name: str = "ai-lead-qualifier"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add correlation ID if present
        if hasattr(record, "correlation_id"):
            log_entry["correlation_id"] = record.correlation_id

        # Add request context if present
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id

        # Add extra fields
        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data

        # Add exception info
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        # Add performance metrics if present
        if hasattr(record, "duration_ms"):
            log_entry["performance"] = {
                "duration_ms": record.duration_ms,
            }

        return json.dumps(log_entry, default=str)

# ─── Context Logger ──────────────────────────────────────


class ContextLogger:
    """Logger wrapper that automatically includes context in log records."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._context: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> "ContextLogger":
        """Add context fields that will be included in all subsequent logs."""
        new_logger = ContextLogger(self._logger)
        new_logger._context = {**self._context, **kwargs}
        return new_logger

    def _log(self, level: int, msg: str, *args, **kwargs):
        """Internal log method that injects context."""
        extra = kwargs.pop("extra", {})
        extra["extra_data"] = {**self._context, **extra.get("extra_data", {})}

        if "correlation_id" in self._context:
            extra["correlation_id"] = self._context["correlation_id"]
        if "request_id" in self._context:
            extra["request_id"] = self._context["request_id"]

        kwargs["extra"] = extra
        self._logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs):
        kwargs["exc_info"] = kwargs.get("exc_info", True)
        self._log(logging.ERROR, msg, *args, **kwargs)

# ─── Performance Tracking ────────────────────────────────


@contextmanager
def log_performance(logger: ContextLogger, operation: str, **extra):
    """Context manager to log operation duration.

    Usage:
        with log_performance(logger, "qualify_lead", lead_id="123"):
            result = await qualify(lead)
    """
    start = time.perf_counter()
    logger.info(f"Starting {operation}", extra={"extra_data": extra})

    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            f"Completed {operation}",
            extra={
                "duration_ms": duration_ms,
                "extra_data": {**extra, "status": "success"},
            },
        )
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(
            f"Failed {operation}: {e}",
            extra={
                "duration_ms": duration_ms,
                "extra_data": {**extra, "status": "error", "error": str(e)},
            },
            exc_info=True,
        )
        raise


# ─── Log Filters ─────────────────────────────────────────


class SensitiveDataFilter(logging.Filter):
    """Redacts sensitive data from log records."""

    SENSITIVE_KEYS = {
        "api_key", "password", "token", "secret",
        "authorization", "credit_card", "ssn", "email",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            record.extra_data = self._redact(record.extra_data)
        return True

    def _redact(self, data: dict) -> dict:
        """Recursively redact sensitive fields."""
        redacted = {}
        for key, value in data.items():
            if any(s in key.lower() for s in self.SENSITIVE_KEYS):
                redacted[key] = "***REDACTED***"
            elif isinstance(value, dict):
                redacted[key] = self._redact(value)
            else:
                redacted[key] = value
        return redacted

# ─── Setup Functions ──────────────────────────────────────


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": JSONFormatter,
            "service_name": "ai-lead-qualifier",
        },
        "console": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "filters": {
        "sensitive_data": {
            "()": SensitiveDataFilter,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "console",
            "filters": ["sensitive_data"],
        },
        "json_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/app.jsonl",
            "maxBytes": 10_485_760,  # 10 MB
            "backupCount": 5,
            "formatter": "json",
            "filters": ["sensitive_data"],
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/error.jsonl",
            "maxBytes": 10_485_760,
            "backupCount": 10,
            "formatter": "json",
            "level": "ERROR",
            "filters": ["sensitive_data"],
        },
    },
    "loggers": {
        "src.qualifier": {"level": "INFO"},
        "src.enrichment": {"level": "INFO"},
        "src.router": {"level": "INFO"},
        "src.rate_limiter": {"level": "WARNING"},
        "src.webhooks": {"level": "INFO"},
        "src.crm": {"level": "INFO"},
        "src.batch": {"level": "INFO"},
        "uvicorn": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "json_file", "error_file"],
    },
}

def setup_logging(
    log_level: Optional[str] = None,
    json_output: Optional[bool] = None,
) -> None:
    """Initialize logging configuration.

    Args:
        log_level: Override log level (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, use JSON formatter for console output.
                     Defaults to True in production, False in development.
    """
    import os

    # Create logs directory
    os.makedirs("logs", exist_ok=True)

    # Apply config
    logging.config.dictConfig(LOGGING_CONFIG)

    # Override log level if specified
    if log_level:
        logging.getLogger().setLevel(getattr(logging, log_level.upper()))

    # Use JSON formatter for console in production
    env = getattr(settings, "ENVIRONMENT", os.getenv("ENVIRONMENT", "development"))
    use_json = json_output if json_output is not None else (env == "production")

    if use_json:
        console_handler = logging.getLogger().handlers[0]
        console_handler.setFormatter(JSONFormatter())

    logger = get_logger(__name__)
    logger.info(
        f"Logging initialized",
        extra={
            "extra_data": {
                "environment": env,
                "log_level": logging.getLogger().level,
                "json_output": use_json,
            }
        },
    )


def get_logger(name: str) -> ContextLogger:
    """Get a context-aware logger for a module.

    Args:
        name: Module name (typically __name__).

    Returns:
        ContextLogger with structured logging support.

    Usage:
        logger = get_logger(__name__)
        logger = logger.bind(lead_id="123", source="webhook")
        logger.info("Processing lead")
    """
    return ContextLogger(logging.getLogger(name))


def get_correlation_id() -> str:
    """Generate a unique correlation ID for request tracing."""
    return str(uuid.uuid4())
