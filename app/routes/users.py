"""User account routes.

Planned endpoints:
- POST /v1/users
- GET /v1/users/me
- DELETE /v1/users/me

The authenticated user is resolved from the bearer token for `/me` routes.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import user_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("users", __name__, url_prefix="/v1/users")


@bp.post("")
def create_user() -> tuple[object, int]:
    """Create a new user account."""
    # Routes only unpack HTTP input. Validation, hashing, and persistence live
    # in the service layer so they can be tested without Flask.
    data = require_json_object(request.get_json(silent=True))
    response = user_service.create_user(
        email=require_field(data, "email"),
        password=require_field(data, "password"),
    )
    return jsonify(response), 201


@bp.get("/me")
@require_user_auth
def get_current_user() -> tuple[object, int]:
    """Return the authenticated user's account record."""
    # `require_user_auth` decoded the bearer token and stored the user ID on
    # Flask's request context before this function is called.
    return jsonify(user_service.get_user(current_user_id())), 200


@bp.delete("/me")
@require_user_auth
def delete_current_user() -> tuple[object, int]:
    """Delete the authenticated user's account."""
    # `/me` avoids accepting a user ID from the client, so users can only
    # delete the account represented by their own access token.
    return jsonify(user_service.delete_user(current_user_id())), 200
