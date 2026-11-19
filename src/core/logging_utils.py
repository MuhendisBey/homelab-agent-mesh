"""Logging hygiene utilities: secret URL/token redaction and noisy healthcheck suppression."""

from __future__ import annotations

import logging
import re

_PRIVATE_PATH_RE = re.compile(r"/private_[A-Za-z0-9_-]+")
_BEARER_TOKEN_RE = re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:api_key|key|token|access_token)=)[^&\s\"']+",
    re.IGNORECASE,
)

_HTTP_CLIENT_LOGGERS: tuple[str, ...] = ("httpx", "httpx2", "httpcore")
_UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact_sensitive_text(text: str) -> str:
    """Scrub secret URL paths, bearer tokens, and secret query parameters from text."""
    cleaned = _PRIVATE_PATH_RE.sub("/private_***", text)
    cleaned = _BEARER_TOKEN_RE.sub(r"\1 ***", cleaned)
    cleaned = _QUERY_SECRET_RE.sub(r"\1***", cleaned)
    return cleaned


class SecretRedactingFilter(logging.Filter):
    """Scrub secret tokens and private endpoint paths from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        redacted = redact_sensitive_text(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        if isinstance(record.exc_text, str) and record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        return True


class HealthCheckAccessFilter(logging.Filter):
    """Suppress successful Docker healthcheck access log spam while retaining failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        return not ("/api/v1/health" in message and " 200" in message)


def _attach_filter_once(target: logging.Logger | logging.Handler, flt: logging.Filter) -> None:
    if not any(isinstance(existing, type(flt)) for existing in target.filters):
        target.addFilter(flt)


def configure_logging(level: str) -> None:
    """Configure application logging with secret redaction and reduced HTTP client noise."""
    app_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=app_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    secret_filter = SecretRedactingFilter()
    health_filter = HealthCheckAccessFilter()

    root_logger = logging.getLogger()
    _attach_filter_once(root_logger, secret_filter)
    for handler in root_logger.handlers:
        _attach_filter_once(handler, secret_filter)

    for logger_name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(logger_name)
        _attach_filter_once(uv_logger, secret_filter)
        for handler in uv_logger.handlers:
            _attach_filter_once(handler, secret_filter)

    _attach_filter_once(logging.getLogger("uvicorn.access"), health_filter)

    http_level = max(app_level, logging.WARNING)
    for logger_name in _HTTP_CLIENT_LOGGERS:
        http_logger = logging.getLogger(logger_name)
        http_logger.setLevel(http_level)
        _attach_filter_once(http_logger, secret_filter)
