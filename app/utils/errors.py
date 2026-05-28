"""Consistent API error responses.

All JSON APIs should use the documented error shape:
`{"error": {"type": "...", "message": "..."}}`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import jsonify
from werkzeug import Response


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


def register_error_handlers(app: Any) -> None:
    """Register default Flask handlers for MiniTen API errors."""

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError) -> tuple[Response, int]:
        return api_error_response(error)
