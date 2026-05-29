"""Consistent API error responses.

All JSON APIs should use the documented error shape:
`{"error": {"type": "...", "message": "..."}}`.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug import Response
from werkzeug.exceptions import HTTPException, InternalServerError


logger = logging.getLogger(__name__)


@dataclass
class ApiError(Exception):
    """Application error that can be serialized as a documented JSON response."""

    type: str
    message: str
    status_code: int = 400
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the documented error response body."""
        body: dict[str, Any] = {
            "error": {
                "type": self.type,
                "message": self.message,
            }
        }

        if self.details:
            body["error"]["details"] = self.details

        return body


class ValidationError(ApiError):
    """Error raised when request input fails validation."""

    def __init__(
        self,
        message: str = "Invalid request.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            type="validation_error",
            message=message,
            status_code=400,
            details=details,
        )


def error_response(
    error_type: str,
    message: str,
    status_code: int = 400,
    *,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Build a Flask JSON error response."""
    return jsonify(ApiError(error_type, message, status_code, details).to_dict()), status_code


def api_error_response(error: ApiError) -> tuple[Response, int]:
    """Serialize an ApiError into a Flask response tuple."""
    return jsonify(error.to_dict()), error.status_code


def dashboard_error_response(
    *,
    status_code: int,
    message: str,
) -> tuple[str, int]:
    """Render an HTML error page for dashboard requests."""
    return (
        render_template(
            "dashboard/error.html",
            status_code=status_code,
            message=message,
            is_authenticated=bool(session.get("access_token")),
        ),
        status_code,
    )


def is_api_request() -> bool:
    """Return whether the current request should receive JSON errors."""
    return request.path.startswith(("/v1/", "/healthz", "/readyz"))


def register_error_handlers(app: Any) -> None:
    """Register default Flask handlers for MiniTen API errors."""

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError) -> tuple[Response, int] | tuple[str, int]:
        logger.info(
            "Application error returned type=%s status=%s.",
            error.type,
            error.status_code,
        )
        if not is_api_request():
            if error.type == "user_not_found":
                session.clear()
                flash("Your session expired. Log in again.", "warning")
                return redirect(url_for("dashboard.login"))
            return dashboard_error_response(
                status_code=error.status_code,
                message=error.message,
            )
        return api_error_response(error)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response, int] | tuple[str, int]:
        status_code = error.code or 500
        if is_api_request():
            return error_response(
                error.name.lower().replace(" ", "_"),
                error.description,
                status_code,
            )
        return dashboard_error_response(
            status_code=status_code,
            message=error.description,
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> tuple[Response, int] | tuple[str, int]:
        wrapped = InternalServerError()
        logger.exception("Unhandled application error.", exc_info=error)
        if is_api_request():
            return error_response(
                "internal_server_error",
                wrapped.description,
                500,
            )
        return dashboard_error_response(
            status_code=500,
            message=wrapped.description,
        )
