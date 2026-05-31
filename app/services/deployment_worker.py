"""Deployment job worker entrypoint.

The worker consumes queued `deployment_jobs`, applies Kubernetes lifecycle
changes, records `model_events`, and marks jobs succeeded, retrying, or failed.
It is deliberately separate from Flask request handlers so API requests only
commit desired state and durable commands.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.config import Config
from app.db.pool import transaction
from app.db.sql import load_queries
from app.k8s import client as k8s_client
from app.k8s import deployment_manager


queries = load_queries()
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
logger = logging.getLogger(__name__)

SUCCESS_EVENT_TYPES = {
    "deploy_model": "model_running",
    "update_model": "model_updated",
    "start_model": "model_started",
    "stop_model": "model_stopped",
    "hard_restart_model": "model_hard_restarted",
    "scale_model": "model_scaled",
    "delete_model": "model_deleted",
    "sync_status": "model_status_synced",
}
SUCCESS_MESSAGES = {
    "deploy_model": "Model deployment applied to Kubernetes.",
    "update_model": "Model deployment settings applied to Kubernetes.",
    "start_model": "Model deployment started.",
    "stop_model": "Model deployment stopped.",
    "hard_restart_model": "Model deployment hard restarted.",
    "scale_model": "Model deployment scaled.",
    "delete_model": "Model deployment deleted.",
    "sync_status": "Model deployment status reconciled from Kubernetes.",
}
RUNNING_STATUSES = {
    "deploy_model": "running",
    "update_model": "running",
    "start_model": "running",
    "hard_restart_model": "running",
}
STOPPED_STATUSES = {"stop_model": "stopped"}
SKIPPED_STATUS = "skipped"
LEASE_LOST_STATUS = "lease_lost"

FAILURE_CATEGORIES = {
    "imagepullbackoff": "image_pull",
    "errimagepull": "image_pull",
    "invalidimagename": "image_pull",
    "oomkilled": "insufficient_memory",
    "insufficient memory": "insufficient_memory",
    "failed to infer device type": "gpu_unavailable",
    "no cuda": "gpu_unavailable",
    "cuda": "gpu_unavailable",
    "nvidia": "gpu_unavailable",
    "hf_token": "model_download_auth",
    "hugging face": "model_download_auth",
    "401": "model_download_auth",
    "403": "model_download_auth",
    "chat template": "invalid_model_or_chat_template",
    "tokenizer": "invalid_model_or_chat_template",
}


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


class LostModelOperationLease(RuntimeError):
    """Raised when a worker no longer owns a model operation lease."""


def default_worker_id() -> str:
    """Return a stable-ish worker identifier for DB locks and debugging."""
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    """Atomically claim the next queued/retrying deployment job."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("claim_next_deployment_job"),
                {
                    "locked_by": worker_id,
                    "lease_seconds": Config.WORKER_LEASE_SECONDS,
                },
            )
            return cur.fetchone()


def claim_next_project_cleanup_job(worker_id: str) -> dict[str, Any] | None:
    """Atomically claim the next queued/retrying project cleanup job."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("claim_next_project_cleanup_job"),
                {
                    "locked_by": worker_id,
                    "lease_seconds": Config.WORKER_LEASE_SECONDS,
                },
            )
            return cur.fetchone()


def process_next_job(
    clients: Any,
    *,
    worker_id: str | None = None,
) -> JobResult:
    """Claim and process one job, returning whether work was found."""
    active_worker_id = worker_id or default_worker_id()

    # Deployment jobs are user-visible lifecycle commands, so workers always
    # drain that queue before best-effort namespace cleanup work.
    job = claim_next_job(active_worker_id)

    if job is not None:
        logger.info(
            "Processing deployment job %s of type %s.",
            job["deployment_job_id"],
            job["job_type"],
        )
        status = process_claimed_job(clients, job)
        logger.info(
            "Finished deployment job %s with status %s.",
            job["deployment_job_id"],
            status,
        )
        return JobResult(
            processed=True,
            deployment_job_id=str(job["deployment_job_id"]),
            status=status,
        )

    cleanup_job = claim_next_project_cleanup_job(active_worker_id)
    if cleanup_job is None:
        return JobResult(processed=False)

    logger.info(
        "Processing project cleanup job %s for namespace %s.",
        cleanup_job["project_cleanup_job_id"],
        cleanup_job["k8s_namespace"],
    )
    status = process_claimed_project_cleanup_job(clients, cleanup_job)
    logger.info(
        "Finished project cleanup job %s with status %s.",
        cleanup_job["project_cleanup_job_id"],
        status,
    )
    return JobResult(
        processed=True,
        deployment_job_id=str(cleanup_job["project_cleanup_job_id"]),
        status=status,
    )


def process_claimed_project_cleanup_job(clients: Any, job: dict[str, Any]) -> str:
    """Delete a project namespace for a claimed project cleanup job."""
    try:
        if Config.WORKER_DRY_RUN:
            # Dry-run mode is for local API smoke tests. Marking cleanup
            # succeeded keeps test queues moving without touching Kubernetes.
            logger.info(
                "Dry-run worker skipped project namespace cleanup job_id=%s namespace=%s.",
                job["project_cleanup_job_id"],
                job["k8s_namespace"],
            )
        else:
            k8s_client.delete_namespace(clients, job["k8s_namespace"])
        mark_project_cleanup_job_succeeded(job)
        return "succeeded"
    except Exception as exc:
        mark_project_cleanup_job_failed_or_retrying(job, exc)
        return "failed" if should_project_cleanup_fail_permanently(job) else "retrying"


def process_claimed_job(
    clients: Any,
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
        if is_noop_job(job):
            mark_job_skipped(job)
            return SKIPPED_STATUS

        # Kubernetes calls can take minutes. The heartbeat thread keeps the
        # model-level lease alive while the worker waits for pods/readiness.
        # The pre/post assertions fence off stale workers that wake up after a
        # newer job has taken over the same model.
        with maintained_model_operation_lease(job):
            assert_job_lease_owned(job)
            dispatch_job(clients, job, deployment)
            assert_job_lease_owned(job)
        mark_job_succeeded(job, deployment)
        return "succeeded"
    except LostModelOperationLease:
        logger.warning(
            "Deployment job %s lost its model operation lease; leaving final state for the current lease owner.",
            job.get("deployment_job_id"),
        )
        return LEASE_LOST_STATUS
    except Exception as exc:
        try:
            mark_job_failed_or_retrying(job, exc)
        except LostModelOperationLease:
            logger.warning(
                "Deployment job %s lost its model operation lease before failure could be recorded.",
                job.get("deployment_job_id"),
            )
            return LEASE_LOST_STATUS
        return "failed" if should_fail_permanently(job) else "retrying"


def fetch_deployment_for_job(job: dict[str, Any]) -> dict[str, Any]:
    """Load the current model deployment row for a claimed job."""
    model_deployment_id = job.get("model_deployment_id")

    if model_deployment_id is None:
        raise RuntimeError("deployment job is missing model_deployment_id")

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get(
                    "get_model_deployment_by_id_including_deleted"
                    if job.get("job_type") == "delete_model"
                    else "get_model_deployment_by_id"
                ),
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
    return int(job["desired_generation"]) != int(deployment["desired_generation"])


def is_noop_job(job: dict[str, Any]) -> bool:
    """Return whether a queued lifecycle job was already satisfied."""
    payload = job.get("payload") or {}
    job_type = job["job_type"]

    if job_type == "start_model":
        return payload.get("previous_status") == "running"

    if job_type == "stop_model":
        return payload.get("previous_status") == "stopped"

    if job_type == "scale_model":
        previous_replicas = payload.get("previous_replicas")
        requested_replicas = payload.get("replicas")
        if previous_replicas is None or requested_replicas is None:
            return False
        return int(previous_replicas) == int(requested_replicas)

    return False


def dispatch_job(
    clients: Any,
    job: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    """Call the Kubernetes operation for a claimed deployment job."""
    job_type = job["job_type"]

    if Config.WORKER_DRY_RUN:
        # In dry-run mode the database/job state machine is exercised without
        # creating pods. This is what lets `test-local-apis` run quickly.
        logger.info(
            "Dry-run worker skipped Kubernetes mutation job_id=%s job_type=%s model_deployment_id=%s.",
            job["deployment_job_id"],
            job_type,
            deployment["model_deployment_id"],
        )
        return

    if job_type == "deploy_model":
        deployment_manager.apply_model_deployment(
            clients,
            deployment,
            hugging_face_token=Config.HUGGING_FACE_TOKEN,
        )
        return

    if job_type == "update_model":
        deployment_manager.apply_model_deployment(
            clients,
            deployment,
            hugging_face_token=Config.HUGGING_FACE_TOKEN,
        )
        return

    if job_type == "start_model":
        deployment_manager.apply_model_deployment(
            clients,
            deployment,
            hugging_face_token=Config.HUGGING_FACE_TOKEN,
        )
        return

    if job_type == "hard_restart_model":
        # Hard restart intentionally removes all model runtime resources before
        # reapplying desired state. It is the escape hatch for broken pods where
        # ordinary stop/start cannot recover cleanly.
        deployment_manager.delete_model_deployment(clients, deployment)
        deployment_manager.apply_model_deployment(
            clients,
            deployment,
            hugging_face_token=Config.HUGGING_FACE_TOKEN,
        )
        return

    if job_type == "stop_model":
        deployment_manager.stop_model_deployment(clients, deployment)
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

    if job_type == "sync_status":
        # Sync is read-only against Kubernetes. It reconciles MiniTen's durable
        # status with current pod readiness without changing the Deployment.
        status = deployment_manager.inspect_model_readiness(
            clients,
            deployment,
            expected_replicas=int(deployment.get("replicas") or 0),
        )
        if status["failed"]:
            deployment["_synced_status"] = "failed"
        elif status["ready"]:
            deployment["_synced_status"] = (
                "running" if int(deployment.get("replicas") or 0) > 0 else "stopped"
            )
        else:
            deployment["_synced_status"] = "deploying"
        return

    raise RuntimeError(f"unsupported deployment job type: {job_type}")


def maintained_model_operation_lease(job: dict[str, Any]) -> Any:
    """Return a context manager that heartbeats a model operation lease."""
    return ModelOperationLeaseMaintenance(job)


class ModelOperationLeaseMaintenance:
    """Refresh a model operation lease while a worker performs Kubernetes work."""

    def __init__(self, job: dict[str, Any]) -> None:
        """Capture the claimed job and initialize heartbeat coordination state."""
        self.job = job
        self.stop_event = threading.Event()
        self.lease_lost_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "ModelOperationLeaseMaintenance":
        """Start lease heartbeats before slow Kubernetes work begins."""
        if not job_has_model_operation_lease(self.job):
            return self

        # Refresh once before starting the heartbeat so a nearly expired claim
        # is extended before Kubernetes work begins.
        refresh_job_lease(self.job)
        self.thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"model-operation-lease-{self.job['deployment_job_id']}",
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> bool:
        """Stop heartbeats and report lease loss to the caller."""
        # Stop the heartbeat before final job writes. The caller performs one
        # last lease assertion inside the transaction that records the result.
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.lease_lost_event.is_set() and args[0] is None:
            raise LostModelOperationLease("model operation lease was lost")
        return False

    def _heartbeat_loop(self) -> None:
        """Refresh the model operation lease until work completes or ownership is lost."""
        interval = max(1.0, float(Config.WORKER_LEASE_HEARTBEAT_SECONDS))
        while not self.stop_event.wait(interval):
            try:
                refresh_job_lease(self.job)
            except LostModelOperationLease:
                self.lease_lost_event.set()
                logger.warning(
                    "Lost model operation lease during heartbeat deployment_job_id=%s.",
                    self.job.get("deployment_job_id"),
                )
                return
            except Exception:
                logger.exception(
                    "Failed to heartbeat model operation lease deployment_job_id=%s.",
                    self.job.get("deployment_job_id"),
                )


def job_has_model_operation_lease(job: dict[str, Any]) -> bool:
    """Return whether a claimed job carries a fenced model operation lease."""
    return bool(job.get("model_deployment_id") and job.get("model_operation_lease_token"))


def model_operation_lease_params(job: dict[str, Any]) -> dict[str, Any]:
    """Return SQL parameters identifying a claimed model operation lease."""
    return {
        "model_deployment_id": job["model_deployment_id"],
        "deployment_job_id": job["deployment_job_id"],
        "lease_token": job["model_operation_lease_token"],
        "locked_by": job.get("locked_by") or default_worker_id(),
        "lease_seconds": Config.WORKER_LEASE_SECONDS,
    }


def refresh_job_lease(job: dict[str, Any]) -> None:
    """Extend a model operation lease or raise if this worker no longer owns it."""
    if not job_has_model_operation_lease(job):
        return

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("heartbeat_model_operation_lease"),
                model_operation_lease_params(job),
            )
            if cur.fetchone() is None:
                raise LostModelOperationLease("model operation lease is no longer owned")


def assert_job_lease_owned(job: dict[str, Any]) -> None:
    """Raise when a fenced model operation lease is missing or no longer current."""
    if not job_has_model_operation_lease(job):
        return

    with transaction() as conn:
        with conn.cursor() as cur:
            assert_job_lease_owned_with_cursor(cur, job)


def assert_job_lease_owned_with_cursor(cur: Any, job: dict[str, Any]) -> None:
    """Verify model lease ownership inside an existing transaction."""
    if not job_has_model_operation_lease(job):
        return

    cur.execute(
        queries.get("verify_model_operation_lease"),
        model_operation_lease_params(job),
    )
    if cur.fetchone() is None:
        raise LostModelOperationLease("model operation lease is no longer owned")


def release_job_lease_with_cursor(cur: Any, job: dict[str, Any]) -> None:
    """Release a model operation lease inside the caller's transaction."""
    if not job_has_model_operation_lease(job):
        return

    # Release is token-scoped. If another worker has already stolen the lease,
    # this update affects no rows and cannot clear the newer owner.
    cur.execute(
        queries.get("release_model_operation_lease"),
        model_operation_lease_params(job),
    )


def mark_job_succeeded(job: dict[str, Any], deployment: dict[str, Any]) -> None:
    """Persist deployment/event/job success state after Kubernetes work."""
    job_type = job["job_type"]

    with transaction() as conn:
        with conn.cursor() as cur:
            assert_job_lease_owned_with_cursor(cur, job)
            if job_type == "delete_model":
                # Delete keeps the deployment row soft-deleted so job history
                # and analytics remain inspectable after Kubernetes cleanup.
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
            elif job_type == "sync_status":
                deployment = update_deployment_status_with_cursor(
                    cur,
                    job["model_deployment_id"],
                    infer_synced_status(deployment),
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
            release_job_lease_with_cursor(cur, job)


def infer_synced_status(deployment: dict[str, Any]) -> str:
    """Return a conservative DB status after explicit status reconciliation."""
    if deployment.get("_synced_status"):
        return deployment["_synced_status"]
    if deployment.get("status") in {"deleting", "deleted"}:
        return deployment["status"]
    return "running" if int(deployment.get("replicas") or 0) > 0 else "stopped"


def mark_job_skipped(job: dict[str, Any]) -> None:
    """Mark a stale job skipped without mutating Kubernetes or deployment state."""
    with transaction() as conn:
        with conn.cursor() as cur:
            assert_job_lease_owned_with_cursor(cur, job)
            cur.execute(
                queries.get("mark_deployment_job_skipped"),
                {"deployment_job_id": job["deployment_job_id"]},
            )
            release_job_lease_with_cursor(cur, job)


def mark_job_failed_or_retrying(job: dict[str, Any], exc: Exception) -> None:
    """Persist retry/failure state after a job raises."""
    permanent_failure = should_fail_permanently(job)
    failure = classify_failure(exc)
    query_name = (
        "mark_deployment_job_failed"
        if permanent_failure
        else "mark_deployment_job_retrying"
    )

    with transaction() as conn:
        with conn.cursor() as cur:
            assert_job_lease_owned_with_cursor(cur, job)
            deployment = None

            if job.get("model_deployment_id") is not None:
                current = fetch_deployment_for_job_with_cursor(cur, job)
                if current is not None and is_stale_job(job, current):
                    # A newer desired_generation won the race while this job
                    # was failing. Skip instead of overwriting newer state.
                    cur.execute(
                        queries.get("mark_deployment_job_skipped"),
                        {"deployment_job_id": job["deployment_job_id"]},
                    )
                    release_job_lease_with_cursor(cur, job)
                    return

                mark_deployment_failed = permanent_failure and deployment_failure_is_terminal(
                    failure
                )
                deployment = (
                    update_deployment_status_with_cursor(
                        cur,
                        job["model_deployment_id"],
                        "failed",
                    )
                    if mark_deployment_failed
                    else current
                )

            if deployment is not None and permanent_failure and mark_deployment_failed:
                create_model_event_with_cursor(
                    cur,
                    deployment,
                    "model_failed",
                    "Deployment job failed.",
                    {
                        "deployment_job_id": str(job["deployment_job_id"]),
                        "job_type": job["job_type"],
                        "error": failure["message"],
                        "category": failure["category"],
                        "hint": failure["hint"],
                        "will_retry": not permanent_failure,
                    },
                )

            cur.execute(
                queries.get(query_name),
                {
                    "deployment_job_id": job["deployment_job_id"],
                    "last_error": truncate_error(
                        f"{failure['category']}: {failure['message']}"
                    ),
                },
            )
            release_job_lease_with_cursor(cur, job)


def fetch_deployment_for_job_with_cursor(
    cur: Any,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    """Load the current deployment row inside an existing transaction."""
    cur.execute(
        queries.get("get_model_deployment_by_id"),
        {
            "project_id": job["project_id"],
            "model_deployment_id": job["model_deployment_id"],
        },
    )
    return cur.fetchone()


def should_fail_permanently(job: dict[str, Any]) -> bool:
    """Return whether the next failed attempt should exhaust the job."""
    return int(job["attempts"]) + 1 >= int(job["max_attempts"])


def deployment_failure_is_terminal(failure: dict[str, str]) -> bool:
    """Return whether a failed job should mark the deployment itself failed."""
    return failure["category"] != "readiness_timeout"


def should_project_cleanup_fail_permanently(job: dict[str, Any]) -> bool:
    """Return whether the next failed project cleanup attempt should exhaust the job."""
    return int(job["attempts"]) + 1 >= int(job["max_attempts"])


def mark_project_cleanup_job_succeeded(job: dict[str, Any]) -> None:
    """Mark a project namespace cleanup job as succeeded."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get("mark_project_cleanup_job_succeeded"),
                {"project_cleanup_job_id": job["project_cleanup_job_id"]},
            )


def mark_project_cleanup_job_failed_or_retrying(
    job: dict[str, Any],
    exc: Exception,
) -> None:
    """Record retry/failure state for a project namespace cleanup job."""
    query_name = (
        "mark_project_cleanup_job_failed"
        if should_project_cleanup_fail_permanently(job)
        else "mark_project_cleanup_job_retrying"
    )
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.get(query_name),
                {
                    "project_cleanup_job_id": job["project_cleanup_job_id"],
                    "last_error": truncate_error(str(exc) or exc.__class__.__name__),
                },
            )


def classify_failure(exc: Exception) -> dict[str, str]:
    """Classify worker failures into stable, user-actionable categories."""
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    category = "unknown"

    # The worker sees errors from Kubernetes, vLLM, image pulls, and model
    # downloads. Keep classification string-based so tests do not need concrete
    # Kubernetes/vLLM exception types.
    for needle, candidate in FAILURE_CATEGORIES.items():
        if needle in lowered:
            category = candidate
            break
    if category == "unknown" and "timed out waiting for kubernetes model readiness" in lowered:
        category = "readiness_timeout"

    return {
        "category": category,
        "message": message,
        "hint": failure_hint(category),
    }


def failure_hint(category: str) -> str:
    """Return a short operator hint for a worker failure category."""
    hints = {
        "image_pull": "Check the managed image name, tag availability, and registry access.",
        "insufficient_memory": "Increase memory limits or choose a smaller model/context length.",
        "insufficient_cpu": "Increase CPU capacity or reduce requested CPU.",
        "gpu_unavailable": "Schedule on a node with advertised GPU resources or use a CPU deployment.",
        "model_download_auth": "Set a Hugging Face token for gated or rate-limited models.",
        "invalid_model_or_chat_template": "Use an instruction/chat model with a compatible tokenizer chat template.",
        "readiness_timeout": "Inspect pod logs and events; the container did not become ready in time.",
        "pod_failure": "Inspect pod status, events, and logs for the Kubernetes failure reason.",
        "unknown": "Inspect worker logs, Kubernetes events, and pod logs for the root cause.",
    }
    return hints[category]


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
    clients: Any = None,
    *,
    config: WorkerConfig | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Continuously poll for deployment jobs until the process is stopped."""
    worker_config = config or WorkerConfig(
        worker_id=default_worker_id(),
        poll_interval_seconds=Config.WORKER_POLL_INTERVAL_SECONDS,
    )
    k8s_clients = clients
    if k8s_clients is None and not Config.WORKER_DRY_RUN:
        k8s_clients = k8s_client.create_clients()
    should_stop = should_stop or (lambda: False)

    logger.info(
        "Starting deployment worker %s with %.2fs poll interval dry_run=%s.",
        worker_config.worker_id,
        worker_config.poll_interval_seconds,
        Config.WORKER_DRY_RUN,
    )

    while not should_stop():
        result = process_next_job(k8s_clients, worker_id=worker_config.worker_id)

        if not result.processed:
            time.sleep(worker_config.poll_interval_seconds)

    logger.info("Stopping deployment worker %s.", worker_config.worker_id)


def setup_logging() -> None:
    """Configure process-level logging for the worker CLI."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def build_shutdown_event() -> threading.Event:
    """Return an event set by SIGINT/SIGTERM for graceful worker shutdown."""
    stop_event = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        """Set the shared stop event when the process receives a shutdown signal."""
        logger.info("Received signal %s; shutdown requested.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    return stop_event


def main() -> None:
    """Console entrypoint used by `python -m app.services.deployment_worker`."""
    setup_logging()
    stop_event = build_shutdown_event()
    run_forever(should_stop=stop_event.is_set)


if __name__ == "__main__":
    main()
