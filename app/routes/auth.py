"""Authentication routes.

Planned endpoints:
- POST /v1/auth/login
- POST /v1/auth/logout

These routes issue and clear user auth tokens for dashboard/control-plane use.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import require_user_auth
from app.services import auth_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("auth", __name__, url_prefix="/v1/auth")


@bp.post("/login")
def login() -> tuple[object, int]:
    """Authenticate a user and return a bearer token."""
    # Keep request parsing here and credential checking in `auth_service` so
    # login behavior is reusable from CLI/dashboard code later.
    data = require_json_object(request.get_json(silent=True))
    response = auth_service.login(
        email=require_field(data, "email"),
        password=require_field(data, "password"),
    )
    return jsonify(response), 200


@bp.post("/logout")
@require_user_auth
def logout() -> tuple[object, int]:
    """End the current user session.

    MVP bearer tokens are stateless, so clients logout by discarding the token.
    The endpoint still exists for a consistent API/dashboard flow.
    """
    # The auth decorator still matters: it prevents anonymous callers from
    # getting a successful logout response that looks like an authenticated flow.
    return jsonify(auth_service.logout()), 200
