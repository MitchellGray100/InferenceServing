"""Account API key routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.security.tokens import current_user_id, require_user_auth
from app.services import account_api_key_service
from app.utils.validation import require_field, require_json_object


bp = Blueprint("account_api_keys", __name__, url_prefix="/v1/account/api-keys")


@bp.post("")
@require_user_auth
def create_account_api_key() -> tuple[object, int]:
    """Create an account API key and return the raw key once."""
    data = require_json_object(request.get_json(silent=True))
    response = account_api_key_service.create_account_api_key(
        current_user_id(),
        require_field(data, "name"),
    )
    return jsonify(response), 201


@bp.get("")
@require_user_auth
def list_account_api_keys() -> tuple[object, int]:
    """List display-safe account API key metadata."""
    return jsonify(account_api_key_service.list_account_api_keys(current_user_id())), 200


@bp.delete("/<account_api_key_id>")
@require_user_auth
def revoke_account_api_key(account_api_key_id: str) -> tuple[object, int]:
    """Revoke one account API key."""
    response = account_api_key_service.revoke_account_api_key(
        current_user_id(),
        account_api_key_id,
    )
    return jsonify(response), 200
