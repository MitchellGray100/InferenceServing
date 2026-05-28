"""OpenAI-compatible inference routes.

Inference routes use project API keys, resolve `request.body.model` as the
project-local deployment name, and proxy non-streaming requests to vLLM.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services import inference_service


bp = Blueprint("inference", __name__, url_prefix="/v1")


@bp.post("/chat/completions")
def chat_completions() -> tuple[object, int]:
    """Proxy a non-streaming chat completion request to vLLM."""
    # Inference uses project API keys instead of user dashboard tokens. The key
    # determines the project, and the request body's `model` selects the
    # project-local deployment.
    body, status_code = inference_service.chat_completions(
        raw_api_key=get_project_api_key(),
        body=request.get_json(silent=True),
    )
    return jsonify(body), status_code


@bp.get("/models")
def list_models() -> tuple[object, int]:
    """List running model deployments visible to the project API key."""
    # `/v1/models` mirrors OpenAI's shape, but the contents are MiniTen
    # deployment names from the project attached to the API key.
    return jsonify(inference_service.list_models(get_project_api_key())), 200


def get_project_api_key() -> str:
    """Extract a project API key from the Authorization bearer header."""
    # Keep the parser strict so malformed headers fail the same way as missing
    # or revoked keys.
    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        raise inference_service.missing_project_api_key_error()

    return token.strip()
