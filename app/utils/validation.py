"""Request and resource name validation helpers.

Validation helpers should cover email normalization, password requirements,
project slugs, and Kubernetes-safe model deployment names.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from app.utils.errors import ValidationError


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
DEPLOYMENT_NAME_RE = SLUG_RE
ROLE_VALUES = {"owner", "member", "viewer"}

MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 8
MAX_PROJECT_NAME_LENGTH = 120
MAX_SLUG_LENGTH = 63
MAX_DEPLOYMENT_NAME_LENGTH = 50


def require_json_object(value: Any) -> dict[str, Any]:
    """Validate that a decoded JSON request body is an object."""
    if not isinstance(value, dict):
        raise ValidationError("Request body must be a JSON object.")
    return value


def require_field(data: dict[str, Any], field: str) -> Any:
    """Return a required request field or raise a validation error."""
    value = data.get(field)
    if value is None:
        raise ValidationError(
            f"Missing required field: {field}.",
            details={"field": field},
        )
    return value


def validate_string(
    value: Any,
    field: str,
    *,
    min_length: int = 1,
    max_length: int | None = None,
) -> str:
    """Validate a required string field and return its stripped value."""
    if not isinstance(value, str):
        raise ValidationError(
            f"{field} must be a string.",
            details={"field": field},
        )

    normalized = value.strip()
    if len(normalized) < min_length:
        raise ValidationError(
            f"{field} must not be empty.",
            details={"field": field},
        )

    if max_length is not None and len(normalized) > max_length:
        raise ValidationError(
            f"{field} must be at most {max_length} characters.",
            details={"field": field, "max_length": max_length},
        )

    return normalized


def normalize_email(value: Any) -> str:
    """Normalize and validate an email address."""
    email = validate_string(value, "email", max_length=MAX_EMAIL_LENGTH).lower()

    if not EMAIL_RE.fullmatch(email):
        raise ValidationError(
            "email must be a valid email address.",
            details={"field": "email"},
        )

    return email


def validate_password(value: Any) -> str:
    """Validate a plaintext password from signup/login input."""
    if not isinstance(value, str):
        raise ValidationError(
            "password must be a string.",
            details={"field": "password"},
        )

    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters.",
            details={"field": "password", "min_length": MIN_PASSWORD_LENGTH},
        )

    return value


def validate_project_name(value: Any) -> str:
    """Validate a human-readable project name."""
    return validate_string(
        value,
        "name",
        min_length=1,
        max_length=MAX_PROJECT_NAME_LENGTH,
    )


def slugify(value: str) -> str:
    """Convert a display name into a lowercase URL/Kubernetes-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:MAX_SLUG_LENGTH].strip("-")


def validate_slug(value: Any, field: str = "slug") -> str:
    """Validate a Kubernetes DNS-label-style slug."""
    slug = validate_string(value, field, max_length=MAX_SLUG_LENGTH)

    if not SLUG_RE.fullmatch(slug):
        raise ValidationError(
            f"{field} must use lowercase letters, numbers, and hyphens, and "
            "must start and end with a letter or number.",
            details={"field": field},
        )

    return slug


def validate_deployment_name(value: Any) -> str:
    """Validate a project-local model deployment name.

    The MVP appends internal suffixes such as `-v1` to Kubernetes resources, so
    user-provided names are capped below the Kubernetes 63-character DNS label
    limit.
    """
    name = validate_string(
        value,
        "name",
        max_length=MAX_DEPLOYMENT_NAME_LENGTH,
    )

    if not DEPLOYMENT_NAME_RE.fullmatch(name):
        raise ValidationError(
            "name must use lowercase letters, numbers, and hyphens, and must "
            "start and end with a letter or number.",
            details={"field": "name"},
        )

    return name


def validate_role(value: Any) -> str:
    """Validate a project member role."""
    role = validate_string(value, "role").lower()

    if role not in ROLE_VALUES:
        raise ValidationError(
            "role must be owner, member, or viewer.",
            details={"field": "role", "allowed": sorted(ROLE_VALUES)},
        )

    return role


def validate_uuid(value: Any, field: str) -> str:
    """Validate a UUID string and return its canonical representation."""
    text = validate_string(value, field)

    try:
        return str(UUID(text))
    except ValueError as exc:
        raise ValidationError(
            f"{field} must be a valid UUID.",
            details={"field": field},
        ) from exc


def validate_positive_int(
    value: Any,
    field: str,
    *,
    min_value: int = 1,
    max_value: int | None = None,
) -> int:
    """Validate an integer range used by pagination and resource settings."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{field} must be an integer.",
            details={"field": field},
        )

    if value < min_value:
        raise ValidationError(
            f"{field} must be at least {min_value}.",
            details={"field": field, "min_value": min_value},
        )

    if max_value is not None and value > max_value:
        raise ValidationError(
            f"{field} must be at most {max_value}.",
            details={"field": field, "max_value": max_value},
        )

    return value
