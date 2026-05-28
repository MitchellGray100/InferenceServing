"""HTTP smoke tests for a locally running MiniTen API.

The script intentionally exercises the real Flask server and local Postgres
database instead of Flask's in-process test client. Start the API first with
`make run-api`, then run `make test-local-apis` in another terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PASSWORD = "password123"


class SmokeClient:
    """Small HTTP client with status assertions and progress output."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        project_api_key: str | None = None,
        json: dict[str, Any] | None = None,
        expected_status: int | set[int] = 200,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request and assert the expected status code."""
        expected = (
            expected_status
            if isinstance(expected_status, set)
            else {expected_status}
        )
        request_headers = dict(headers or {})

        if token:
            request_headers["Authorization"] = f"Bearer {token}"

        if project_api_key:
            request_headers["Authorization"] = f"Bearer {project_api_key}"

        response = requests.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            headers=request_headers,
            timeout=10,
        )

        if response.status_code not in expected:
            raise RuntimeError(
                f"{method} {path} returned {response.status_code}; "
                f"expected {sorted(expected)}. Body: {response.text}"
            )

        body = response.json() if response.content else {}
        print(f"OK {method} {path} -> {response.status_code}")
        return body


def run_smoke_tests(base_url: str) -> None:
    """Exercise the local API with throwaway users, project, and model state."""
    client = SmokeClient(base_url)
    suffix = uuid.uuid4().hex[:10]
    owner_email = f"owner-{suffix}@example.com"
    member_email = f"member-{suffix}@example.com"
    model_name = f"smoke-{suffix}"

    ensure_api_is_running(client)

    owner = client.request(
        "POST",
        "/v1/users",
        json={"email": owner_email, "password": PASSWORD},
        expected_status=201,
    )
    login = client.request(
        "POST",
        "/v1/auth/login",
        json={"email": owner_email, "password": PASSWORD},
    )
    owner_token = login["access_token"]
    client.request("GET", "/v1/users/me", token=owner_token)

    member = client.request(
        "POST",
        "/v1/users",
        json={"email": member_email, "password": PASSWORD},
        expected_status=201,
    )

    project = client.request(
        "POST",
        "/v1/projects",
        token=owner_token,
        json={"name": f"Smoke Project {suffix}"},
        expected_status=201,
    )
    project_id = project["projectID"]
    client.request("GET", "/v1/projects", token=owner_token)
    client.request("GET", f"/v1/projects/{project_id}", token=owner_token)

    client.request("GET", f"/v1/projects/{project_id}/members", token=owner_token)
    client.request(
        "POST",
        f"/v1/projects/{project_id}/members",
        token=owner_token,
        json={"email": member_email, "role": "member"},
        expected_status=201,
    )
    client.request(
        "PATCH",
        f"/v1/projects/{project_id}/members/{member['userID']}",
        token=owner_token,
        json={"role": "viewer"},
    )
    client.request(
        "DELETE",
        f"/v1/projects/{project_id}/members/{member['userID']}",
        token=owner_token,
    )

    api_key_response = client.request(
        "POST",
        f"/v1/projects/{project_id}/api-keys",
        token=owner_token,
        json={"name": f"Smoke Key {suffix}"},
        expected_status=201,
    )
    api_key = api_key_response["api_key"]
    api_key_id = api_key_response["apiKeyID"]
    client.request("GET", f"/v1/projects/{project_id}/api-keys", token=owner_token)
    client.request("GET", "/v1/models", project_api_key=api_key)
    client.request(
        "POST",
        "/v1/chat/completions",
        project_api_key=api_key,
        json={"model": "missing-model", "messages": []},
        expected_status=404,
    )

    deployment_command = client.request(
        "POST",
        f"/v1/projects/{project_id}/models",
        token=owner_token,
        headers={"Idempotency-Key": f"deploy-{suffix}"},
        json={
            "name": model_name,
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "replicas": 1,
        },
        expected_status=201,
    )
    model_deployment_id = deployment_command["modelDeployment"]["modelDeploymentID"]
    client.request("GET", f"/v1/projects/{project_id}/models", token=owner_token)
    client.request(
        "GET",
        f"/v1/projects/{project_id}/models/{model_deployment_id}",
        token=owner_token,
    )
    client.request(
        "GET",
        f"/v1/projects/{project_id}/analytics/models/{model_name}/metrics",
        token=owner_token,
    )
    client.request(
        "GET",
        f"/v1/projects/{project_id}/analytics/models/{model_name}/requests",
        token=owner_token,
    )
    client.request(
        "GET",
        f"/v1/projects/{project_id}/analytics/models/{model_name}/events",
        token=owner_token,
    )

    client.request(
        "POST",
        f"/v1/projects/{project_id}/models/{model_deployment_id}/start",
        token=owner_token,
        headers={"Idempotency-Key": f"start-{suffix}"},
        expected_status=202,
    )
    client.request(
        "POST",
        f"/v1/projects/{project_id}/models/{model_deployment_id}/stop",
        token=owner_token,
        headers={"Idempotency-Key": f"stop-{suffix}"},
        expected_status=202,
    )
    client.request(
        "POST",
        f"/v1/projects/{project_id}/models/{model_deployment_id}/scale",
        token=owner_token,
        headers={"Idempotency-Key": f"scale-{suffix}"},
        json={"replicas": 2},
        expected_status=202,
    )
    client.request(
        "DELETE",
        f"/v1/projects/{project_id}/models/{model_deployment_id}",
        token=owner_token,
        headers={"Idempotency-Key": f"delete-model-{suffix}"},
        expected_status=202,
    )

    client.request(
        "DELETE",
        f"/v1/projects/{project_id}/api-keys/{api_key_id}",
        token=owner_token,
    )
    client.request("POST", "/v1/auth/logout", token=owner_token)
    client.request("DELETE", f"/v1/projects/{project_id}", token=owner_token)
    client.request("DELETE", "/v1/users/me", token=owner_token)

    member_login = client.request(
        "POST",
        "/v1/auth/login",
        json={"email": member_email, "password": PASSWORD},
    )
    client.request("DELETE", "/v1/users/me", token=member_login["access_token"])

    print(f"Local API smoke test passed for owner user {owner['userID']}.")


def ensure_api_is_running(client: SmokeClient) -> None:
    """Fail with a useful message if the local API is not reachable."""
    last_error: Exception | None = None

    for _ in range(30):
        try:
            client.request("GET", "/readyz")
            return
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1)

    raise RuntimeError(
        "Could not reach the local API. Start it in another terminal with "
        "`make run-api`, then rerun `make test-local-apis`."
    ) from last_error


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description="Smoke test the local MiniTen API.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of the running MiniTen API.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()

    try:
        run_smoke_tests(args.base_url)
    except Exception as exc:
        print(f"Local API smoke test failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
