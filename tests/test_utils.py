from datetime import UTC, datetime

import pytest
from flask import Flask

from app.utils.errors import ApiError, ValidationError, api_error_response
from app.utils.time import to_iso8601
from app.utils.validation import (
    normalize_email,
    require_json_object,
    slugify,
    validate_deployment_name,
    validate_password,
    validate_positive_int,
    validate_role,
    validate_slug,
    validate_uuid,
)


def test_api_error_serializes_to_documented_shape() -> None:
    error = ApiError("not_found", "Missing.", 404)

    assert error.to_dict() == {
        "error": {
            "type": "not_found",
            "message": "Missing.",
        }
    }


def test_api_error_response_returns_flask_json_tuple() -> None:
    app = Flask(__name__)

    with app.app_context():
        response, status = api_error_response(ApiError("forbidden", "No.", 403))

    assert status == 403
    assert response.get_json() == {
        "error": {
            "type": "forbidden",
            "message": "No.",
        }
    }


def test_normalize_email_strips_and_lowercases() -> None:
    assert normalize_email(" User@Example.COM ") == "user@example.com"


@pytest.mark.parametrize("email", ["not-email", "@example.com", "user@", "user test@example.com"])
def test_normalize_email_rejects_invalid_email(email: str) -> None:
    with pytest.raises(ValidationError):
        normalize_email(email)


def test_validate_password_requires_minimum_length() -> None:
    assert validate_password("password123") == "password123"

    with pytest.raises(ValidationError):
        validate_password("short")


def test_slugify_normalizes_project_name() -> None:
    assert slugify(" Personal Models! ") == "personal-models"


@pytest.mark.parametrize("slug", ["personal-models", "a", "model-123"])
def test_validate_slug_accepts_dns_label_style_values(slug: str) -> None:
    assert validate_slug(slug) == slug


@pytest.mark.parametrize("slug", ["Bad", "-bad", "bad-", "bad_value", ""])
def test_validate_slug_rejects_invalid_values(slug: str) -> None:
    with pytest.raises(ValidationError):
        validate_slug(slug)


@pytest.mark.parametrize("name", ["qwen-small", "tinyllama-dev", "llama-3b-gpu"])
def test_validate_deployment_name_accepts_expected_values(name: str) -> None:
    assert validate_deployment_name(name) == name


@pytest.mark.parametrize("name", ["Qwen Small", "qwen_small", "qwen/small", "-prod", "prod-"])
def test_validate_deployment_name_rejects_invalid_values(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_deployment_name(name)


def test_validate_uuid_returns_canonical_uuid() -> None:
    assert (
        validate_uuid("9D41B65E-1D5A-4F24-A4C6-98F4DF0C2C5E", "userID")
        == "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e"
    )

    with pytest.raises(ValidationError):
        validate_uuid("not-a-uuid", "userID")


def test_validate_role_accepts_known_project_roles() -> None:
    assert validate_role("Owner") == "owner"

    with pytest.raises(ValidationError):
        validate_role("admin")


def test_validate_positive_int_bounds() -> None:
    assert validate_positive_int(5, "limit", max_value=10) == 5

    with pytest.raises(ValidationError):
        validate_positive_int(True, "limit")

    with pytest.raises(ValidationError):
        validate_positive_int(11, "limit", max_value=10)


def test_require_json_object_rejects_non_objects() -> None:
    assert require_json_object({"ok": True}) == {"ok": True}

    with pytest.raises(ValidationError):
        require_json_object([])


def test_to_iso8601_outputs_utc_z_suffix() -> None:
    assert to_iso8601(datetime(2026, 5, 17, 12, 0, tzinfo=UTC)) == "2026-05-17T12:00:00Z"
