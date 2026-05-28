"""Deployment job worker entrypoint.

The worker consumes queued `deployment_jobs`, applies Kubernetes lifecycle
changes, records `model_events`, and marks jobs succeeded, retrying, or failed.
It is deliberately separate from Flask request handlers so API requests only
commit desired state and durable commands.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Any

from app.db.pool import transaction
from app.db.sql import load_queries
from app.k8s import client as k8s_client
from app.k8s import deployment_manager


queries = load_queries()
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

SUCCESS_EVENT_TYPES = {
    "deploy_model": "model_running",
    "start_model": "model_started",
    "stop_model": "model_stopped",
    "scale_model": "model_scaled",
    "delete_model": "model_deleted",
}
SUCCESS_MESSAGES = {
    "deploy_model": "Model deployment applied to Kubernetes.",
    "start_model": "Model deployment started.",
    "stop_model": "Model deployment stopped.",
    "scale_model": "Model deployment scaled.",
    "delete_model": "Model deployment deleted.",
}
RUNNING_STATUSES = {"deploy_model": "running", "start_model": "running"}
STOPPED_STATUSES = {"stop_model": "stopped"}
SKIPPED_STATUS = "skipped"


@dataclass(frozen=True)
class WorkerConfig:
    """Runtime knobs for a deployment worker process."""

    worker_id: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS


@dataclass(frozen=True)
class JobResult:
    """Result of one worker polling iteration."""

    processed: bool
    deployment_job_id: str | None = None
    status: str | None = None


def default_worker_id() -> str:
    """Return a stable-ish worker identifier for DB locks and debugging."""
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    """Atomically claim the next queued/retrying deployment job."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("claim_next_deployment_job"),
                {"locked_by": worker_id},
            )
            return cur.fetchone()


def process_next_job(
    clients: k8s_client.KubernetesClients,
    *,
    worker_id: str | None = None,
) -> JobResult:
    """Claim and process one job, returning whether work was found."""
    job = claim_next_job(worker_id or default_worker_id())

    if job is None:
        return JobResult(processed=False)

    status = process_claimed_job(clients, job)
    return JobResult(
        processed=True,
        deployment_job_id=str(job["deployment_job_id"]),
        status=status,
    )


def process_claimed_job(
    clients: k8s_client.KubernetesClients,
    job: dict[str, Any],
) -> str:
    """Process a job already marked `running` by `claim_next_job`.

    Kubernetes calls happen outside a database transaction. Once they complete,
    a short transaction records the resulting deployment status, lifecycle
    event, and job status.
    """
    try:
        deployment = fetch_deployment_for_job(job)
        if is_stale_job(job, deployment):
            mark_job_skipped(job)
            return SKIPPED_STATUS

        dispatch_job(clients, job, deployment)
        mark_job_succeeded(job, deployment)
        return "succeeded"
    except Exception as exc:
        mark_job_failed_or_retrying(job, exc)
        return "failed" if should_fail_permanently(job) else "retrying"


def fetch_deployment_for_job(job: dict[str, Any]) -> dict[str, Any]:
    """Load the current model deployment row for a claimed job."""
    model_deployment_id = job.get("model_deployment_id")

    if model_deployment_id is None:
        raise RuntimeError("deployment job is missing model_deployment_id")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("get_model_deployment_by_id"),
                {
                    "project_id": job["project_id"],
                    "model_deployment_id": model_deployment_id,
                },
            )
            deployment = cur.fetchone()

    if deployment is None:
        raise RuntimeError("model deployment not found for deployment job")

    return deployment


def is_stale_job(job: dict[str, Any], deployment: dict[str, Any]) -> bool:
    """Return whether a newer desired generation superseded this job."""
    return int(job.get("desired_generation", 1)) != int(
        deployment.get("desired_generation", 1)
    )


def dispatch_job(
    clients: k8s_client.KubernetesClients,
    job: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    """Call the Kubernetes operation for a claimed deployment job."""
    job_type = job["job_type"]

    if job_type == "deploy_model":
        deployment_manager.apply_model_deployment(clients, deployment)
        return

    if job_type == "start_model":
        deployment_manager.apply_model_deployment(clients, deployment)
        return

    if job_type == "stop_model":
        deployment_manager.scale_model_deployment(clients, deployment, 0)
        return

    if job_type == "scale_model":
        deployment_manager.scale_model_deployment(
            clients,
            deployment,
            int(deployment["replicas"]),
        )
        return

    if job_type == "delete_model":
        deployment_manager.delete_model_deployment(clients, deployment)
        return

    raise RuntimeError(f"unsupported deployment job type: {job_type}")


def mark_job_succeeded(job: dict[str, Any], deployment: dict[str, Any]) -> None:
    """Persist deployment/event/job success state after Kubernetes work."""
    job_type = job["job_type"]

    with transaction() as conn:
        with conn.cursor() as cur:
            if job_type == "delete_model":
                cur.execute(
                    queries.get("mark_model_deployment_deleted"),
                    {"model_deployment_id": job["model_deployment_id"]},
                )
                deployment = cur.fetchone()
            elif job_type in RUNNING_STATUSES:
                deployment = update_deployment_status_with_cursor(
                    cur,
                    job["model_deployment_id"],
                    RUNNING_STATUSES[job_type],
                )
            elif job_type in STOPPED_STATUSES:
                deployment = update_deployment_status_with_cursor(
                    cur,
                    job["model_deployment_id"],
                    STOPPED_STATUSES[job_type],
                )

            create_model_event_with_cursor(
                cur,
                deployment,
                SUCCESS_EVENT_TYPES[job_type],
                SUCCESS_MESSAGES[job_type],
                {"deployment_job_id": str(job["deployment_job_id"])},
            )
            cur.execute(
                queries.get("mark_deployment_job_succeeded"),
                {"deployment_job_id": job["deployment_job_id"]},
            )


def mark_job_skipped(job: dict[str, Any]) -> None:
    """Mark a stale job skipped without mutating Kubernetes or deployment state."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("mark_deployment_job_skipped"),
                {"deployment_job_id": job["deployment_job_id"]},
            )


def mark_job_failed_or_retrying(job: dict[str, Any], exc: Exception) -> None:
    """Persist retry/failure state after a job raises."""
    permanent_failure = should_fail_permanently(job)
    query_name = (
        "mark_deployment_job_failed"
        if permanent_failure
        else "mark_deployment_job_retrying"
    )

    with transaction() as conn:
        with conn.cursor() as cur:
            deployment = None

            if job.get("model_deployment_id") is not None:
                deployment = update_deployment_status_with_cursor(
                    cur,
                    job["model_deployment_id"],
                    "failed",
                )

            if deployment is not None:
                create_model_event_with_cursor(
                    cur,
                    deployment,
                    "model_failed",
                    "Deployment job failed.",
                    {
                        "deployment_job_id": str(job["deployment_job_id"]),
                        "job_type": job["job_type"],
                        "error": str(exc),
                        "will_retry": not permanent_failure,
                    },
                )

            cur.execute(
                queries.get(query_name),
                {
                    "deployment_job_id": job["deployment_job_id"],
                    "last_error": truncate_error(str(exc)),
                },
            )


def should_fail_permanently(job: dict[str, Any]) -> bool:
    """Return whether the next failed attempt should exhaust the job."""
    return int(job["attempts"]) + 1 >= int(job["max_attempts"])


def update_deployment_status_with_cursor(
    cur: Any,
    model_deployment_id: Any,
    status: str,
) -> dict[str, Any] | None:
    """Update deployment status using the caller's transaction."""
    cur.execute(
        queries.get("update_model_deployment_status"),
        {
            "model_deployment_id": model_deployment_id,
            "status": status,
        },
    )
    return cur.fetchone()


def create_model_event_with_cursor(
    cur: Any,
    deployment: dict[str, Any],
    event_type: str,
    message: str,
    metadata: dict[str, Any],
) -> None:
    """Insert a lifecycle event using the caller's transaction."""
    from psycopg.types.json import Jsonb

    cur.execute(
        queries.get("create_model_event"),
        {
            "model_deployment_id": deployment["model_deployment_id"],
            "project_id": deployment["project_id"],
            "event_type": event_type,
            "message": message,
            "metadata": Jsonb(metadata),
        },
    )


def truncate_error(message: str, max_length: int = 2000) -> str:
    """Keep stored job errors bounded while preserving useful context."""
    if len(message) <= max_length:
        return message

    return f"{message[:max_length - 3]}..."


def run_forever(
    clients: k8s_client.KubernetesClients | None = None,
    *,
    config: WorkerConfig | None = None,
) -> None:
    """Continuously poll for deployment jobs until the process is stopped."""
    worker_config = config or WorkerConfig(worker_id=default_worker_id())
    k8s_clients = clients or k8s_client.create_clients()

    while True:
        result = process_next_job(k8s_clients, worker_id=worker_config.worker_id)

        if not result.processed:
            time.sleep(worker_config.poll_interval_seconds)


def main() -> None:
    """Console entrypoint used by `python -m app.services.deployment_worker`."""
    run_forever()


if __name__ == "__main__":
    main()
