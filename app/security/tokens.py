"""User access token helpers.

User auth tokens are used for dashboard and control-plane operations. Project
API keys are handled separately in `app.security.api_keys`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any, TypeVar

import jwt
from flask import current_app, g, request

from app.utils.errors import ApiError
from app.utils.time import utc_now


TOKEN_TYPE = "user_access"
ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_TTL = timedelta(hours=8)
F = TypeVar("F", bound=Callable[..., Any])


def create_access_token(
    user_id: str,
    *,
    expires_delta: timedelta = DEFAULT_ACCESS_TOKEN_TTL,
) -> str:
    """Create a signed bearer token for dashboard/control-plane requests."""
    now = utc_now()
    # `sub` identifies the user, `type` prevents using other JWT kinds here,
    # and `exp` bounds how long a leaked token remains useful.
    payload = {
        "sub": user_id,
        "type": TOKEN_TYPE,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a user access token."""
    try:
        # PyJWT validates the signature and expiration using SECRET_KEY.
        payload = jwt.decode(
            token,
            current_app.config["SECRET_KEY"],
            algorithms=[ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise unauthorized_error() from exc

    # Reject validly-signed tokens that are not MiniTen user access tokens.
    if payload.get("type") != TOKEN_TYPE or not payload.get("sub"):
        raise unauthorized_error()

    return payload


def get_bearer_token() -> str:
    """Extract the bearer token from the Authorization header."""
    # The API accepts only `Authorization: Bearer <token>` so auth behavior is
    # predictable across all protected routes.
    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")

    if scheme.lower() != "bearer" or not token:
        raise unauthorized_error()

    return token.strip()


def current_user_id() -> str:
    """Return the authenticated user_id stored by require_user_auth."""
    # Route handlers should call this after `@require_user_auth`; without the
    # decorator there is no trusted user ID on the request context.
    user_id = getattr(g, "current_user_id", None)

    if not user_id:
        raise unauthorized_error()

    return str(user_id)


def require_user_auth(view: F) -> F:
    """Flask route decorator requiring a valid user bearer token."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        # Decode once at the route boundary and store only the user identifier
        # needed by downstream services.
        token = get_bearer_token()
        payload = decode_access_token(token)
        g.current_user_id = payload["sub"]
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def unauthorized_error() -> ApiError:
    """Build the standard invalid/missing user token error."""
    return ApiError(
        type="unauthorized",
        message="Missing or invalid access token.",
        status_code=401,
    )
