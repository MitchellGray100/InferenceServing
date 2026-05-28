"""Inference request routing logic.

Inference is the data-plane path. Project API keys identify the project,
`request.body.model` identifies the project-local deployment, and MiniTen
forwards the request to that deployment's internal vLLM Service.
"""

from __future__ import annotations

import time
from typing import Any

import requests
from flask import current_app

from app.db.pool import transaction
from app.db.sql import load_queries
from app.services import api_key_service
from app.utils.errors import ApiError
from app.utils.validation import require_field, require_json_object, validate_string


queries = load_queries()
VLLM_PORT = 8000
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


def chat_completions(raw_api_key: str, body: Any) -> tuple[dict[str, Any], int]:
    """Proxy a non-streaming OpenAI-compatible chat completion request."""
    # Validate the OpenAI-style request envelope before touching API key or DB
    # state. The model name maps to a project-local MiniTen deployment.
    data = require_json_object(body)
    model_name = validate_string(require_field(data, "model"), "model")

    # TODO: Support streaming
    # Streaming requires a different HTTP response shape and connection
    # handling, so the first MVP pass rejects it explicitly.
    if data.get("stream") is True:
        raise ApiError(
            type="streaming_not_supported",
            message="Streaming responses are not supported yet.",
            status_code=400,
        )

    # The project is inferred from the project API key, not from the request
    # path. That keeps inference endpoints OpenAI-compatible.
    identity = api_key_service.authenticate_project_api_key(raw_api_key)
    deployment = get_deployment_for_inference(identity["projectID"], model_name)
    ensure_deployment_running(deployment)

    # vLLM runs behind a Kubernetes ClusterIP Service. MiniTen forwards the
    # original JSON body so vLLM handles generation parameters directly.
    url = build_vllm_url(deployment, CHAT_COMPLETIONS_PATH)
    started = time.perf_counter()
    status_code = 502
    error_type: str | None = None

    try:
        upstream_response = requests.post(
            url,
            json=data,
            timeout=current_app.config["INFERENCE_UPSTREAM_TIMEOUT_SECONDS"],
        )
        status_code = upstream_response.status_code
        response_body = parse_upstream_json(upstream_response)
        return response_body, status_code
    except requests.Timeout as exc:
        # Surface timeouts as gateway timeouts because MiniTen is acting as a
        # proxy to an upstream model server.
        status_code = 504
        error_type = "upstream_timeout"
        raise ApiError(
            type="upstream_timeout",
            message="Timed out waiting for model response.",
            status_code=504,
        ) from exc
    except requests.RequestException as exc:
        error_type = "upstream_error"
        raise ApiError(
            type="upstream_error",
            message="Model service request failed.",
            status_code=502,
        ) from exc
    except ValueError as exc:
        error_type = "upstream_invalid_response"
        raise ApiError(
            type="upstream_invalid_response",
            message="Model service returned a non-JSON response.",
            status_code=502,
        ) from exc
    finally:
        # Store request metadata only. Prompts and model responses are
        # intentionally not written to this table in the MVP.
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_inference_request(
            project_id=identity["projectID"],
            model_deployment_id=deployment["model_deployment_id"],
            api_key_id=identity["apiKeyID"],
            status_code=status_code,
            latency_ms=latency_ms,
            error_type=error_type,
            request_path=CHAT_COMPLETIONS_PATH,
            method="POST",
            streamed=False,
        )


def list_models(raw_api_key: str) -> dict[str, Any]:
    """Return OpenAI-style model objects for running project deployments."""
    # Authenticate first so the key chooses the project whose models are listed.
    identity = api_key_service.authenticate_project_api_key(raw_api_key)

    with transaction() as conn:
        with conn.cursor() as cur:
            # Only running deployments are returned because `/v1/models` is
            # meant to show models that can receive inference traffic.
            cur.execute(
                queries.get("list_running_model_deployments_for_project"),
                {"project_id": identity["projectID"]},
            )
            rows = cur.fetchall()

    return {
        "object": "list",
        "data": [serialize_openai_model(row) for row in rows],
    }


def get_deployment_for_inference(project_id: str, model_name: str) -> dict[str, Any]:
    """Resolve a project-local model name to a non-deleted deployment row."""
    # The same model name can exist in different projects, so lookup always
    # includes the project resolved from the API key.
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_model_deployment_by_name"),
                {
                    "project_id": project_id,
                    "name": model_name,
                },
            )
            row = cur.fetchone()

    if row is None:
        raise ApiError(
            type="model_not_found",
            message="Model not found.",
            status_code=404,
        )

    return row


def ensure_deployment_running(deployment: dict[str, Any]) -> None:
    """Reject inference to deployments that are not ready for traffic."""
    if deployment["status"] != "running":
        raise ApiError(
            type="model_not_ready",
            message="Model deployment is not running.",
            status_code=409,
        )


def build_vllm_url(deployment: dict[str, Any], path: str) -> str:
    """Build the in-cluster URL for a deployment's vLLM Service."""
    # Kubernetes DNS lets pods call Services by
    # service.namespace.svc.cluster.local inside the cluster.
    service_name = deployment["k8s_service_name"]
    namespace = deployment["k8s_namespace"]
    normalized_path = path if path.startswith("/") else f"/{path}"
    return (
        f"http://{service_name}.{namespace}.svc.cluster.local:"
        f"{VLLM_PORT}{normalized_path}"
    )


def parse_upstream_json(response: Any) -> dict[str, Any]:
    """Parse vLLM JSON responses and reject non-object payloads."""
    payload = response.json()

    # OpenAI-compatible API responses are JSON objects. Returning arrays/scalars
    # would break downstream clients expecting object fields such as `choices`.
    if not isinstance(payload, dict):
        raise ValueError("upstream response must be a JSON object")

    return payload


def record_inference_request(
    *,
    project_id: str,
    model_deployment_id: Any,
    api_key_id: str,
    status_code: int,
    latency_ms: int,
    error_type: str | None,
    request_path: str,
    method: str,
    streamed: bool,
) -> None:
    """Store lightweight request metadata without prompts or responses."""
    # This insert is used for analytics and debugging. It stores routing/status
    # metadata only, keeping user prompts out of the MVP database.
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("create_inference_request"),
                {
                    "project_id": project_id,
                    "model_deployment_id": model_deployment_id,
                    "api_key_id": api_key_id,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "error_type": error_type,
                    "request_path": request_path,
                    "method": method,
                    "streamed": streamed,
                },
            )


def serialize_openai_model(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize a deployment as an OpenAI-compatible model object."""
    # OpenAI's model list uses a Unix timestamp. Tests may use simple strings,
    # so non-datetime values degrade to 0 instead of crashing serialization.
    created_at = row["created_at"]
    created_timestamp = (
        int(created_at.timestamp()) if hasattr(created_at, "timestamp") else 0
    )
    return {
        "id": row["name"],
        "object": "model",
        "created": created_timestamp,
        "owned_by": "miniten",
    }


def missing_project_api_key_error() -> ApiError:
    """Build the standard missing/invalid project API key error."""
    return ApiError(
        type="unauthorized",
        message="Missing or invalid project API key.",
        status_code=401,
    )
