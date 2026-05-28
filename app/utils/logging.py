"""Logging configuration helpers.

Logs should describe control-plane behavior without recording secrets,
passwords, API keys, prompts, model responses, or Hugging Face tokens.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from flask import Flask, g, request


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure process-wide logging."""
    logging.basicConfig(level=level, format=LOG_FORMAT)


def register_request_logging(app: Flask) -> None:
    """Register basic HTTP request logging for the Flask app."""
    logger = logging.getLogger("app.http")

    @app.before_request
    def log_request_started() -> None:
        g.request_started_at = time.perf_counter()
        logger.info(
            "HTTP request started method=%s path=%s remote_addr=%s",
            request.method,
            request.path,
            request.remote_addr,
        )

    @app.after_request
    def log_request_finished(response: Any) -> Any:
        started_at = getattr(g, "request_started_at", None)
        latency_ms = (
            int((time.perf_counter() - started_at) * 1000)
            if started_at is not None
            else None
        )
        logger.info(
            "HTTP request finished method=%s path=%s status=%s latency_ms=%s",
            request.method,
            request.path,
            response.status_code,
            latency_ms,
        )
        return response
