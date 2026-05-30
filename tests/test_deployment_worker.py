"""Deployment worker tests."""

from app.services import deployment_worker


JOB_ID = "3ef7d993-cb61-4392-b36b-2ed2e1d88af1"
PROJECT_ID = "a2fc41b7-862e-4060-b466-2376f29227bb"
MODEL_DEPLOYMENT_ID = "bf3dc090-5bb4-46f6-964d-6cd8375ddf56"


def deployment_row() -> dict[str, object]:
    return {
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "project_id": PROJECT_ID,
        "name": "qwen-small-prod",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "k8s_namespace": "miniten-personal",
        "k8s_deployment_name": "qwen-small-prod-v1",
        "k8s_service_name": "qwen-small-prod",
        "k8s_hpa_name": "qwen-small-prod-v1",
        "replicas": 3,
        "desired_generation": 2,
        "autoscaling_enabled": True,
    }


def job_row(
    job_type: str = "deploy_model",
    *,
    attempts: int = 0,
    max_attempts: int = 3,
) -> dict[str, object]:
    return {
        "deployment_job_id": JOB_ID,
        "project_id": PROJECT_ID,
        "model_deployment_id": MODEL_DEPLOYMENT_ID,
        "job_type": job_type,
        "desired_generation": 2,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "payload": {},
    }


def test_process_next_job_returns_no_work_when_queue_empty(monkeypatch) -> None:
    monkeypatch.setattr(deployment_worker, "claim_next_job", lambda worker_id: None)

    result = deployment_worker.process_next_job(FakeClients(), worker_id="worker-1")

    assert result.processed is False
    assert result.deployment_job_id is None


def test_process_next_job_claims_and_processes(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_worker,
        "claim_next_job",
        lambda worker_id: job_row("deploy_model"),
    )
    monkeypatch.setattr(
        deployment_worker,
        "process_claimed_job",
        lambda clients, job: "succeeded",
    )

    result = deployment_worker.process_next_job(FakeClients(), worker_id="worker-1")

    assert result.processed is True
    assert result.deployment_job_id == JOB_ID
    assert result.status == "succeeded"


def test_process_claimed_job_success(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        deployment_worker,
        "fetch_deployment_for_job",
        lambda job: deployment_row(),
    )
    monkeypatch.setattr(
        deployment_worker,
        "dispatch_job",
        lambda clients, job, deployment: calls.append("dispatch"),
    )
    monkeypatch.setattr(
        deployment_worker,
        "mark_job_succeeded",
        lambda job, deployment: calls.append("success"),
    )

    status = deployment_worker.process_claimed_job(FakeClients(), job_row())

    assert status == "succeeded"
    assert calls == ["dispatch", "success"]


def test_process_claimed_job_skips_stale_generation(monkeypatch) -> None:
    calls = []
    stale_job = job_row()
    stale_job["desired_generation"] = 1

    monkeypatch.setattr(
        deployment_worker,
        "fetch_deployment_for_job",
        lambda job: deployment_row(),
    )
    monkeypatch.setattr(
        deployment_worker,
        "dispatch_job",
        lambda clients, job, deployment: calls.append("dispatch"),
    )
    monkeypatch.setattr(
        deployment_worker,
        "mark_job_skipped",
        lambda job: calls.append("skipped"),
    )

    status = deployment_worker.process_claimed_job(FakeClients(), stale_job)

    assert status == "skipped"
    assert calls == ["skipped"]


def test_process_claimed_job_skips_noop_lifecycle_job(monkeypatch) -> None:
    calls = []
    noop_job = job_row("stop_model")
    noop_job["payload"] = {"previous_status": "stopped"}

    monkeypatch.setattr(
        deployment_worker,
        "fetch_deployment_for_job",
        lambda job: deployment_row(),
    )
    monkeypatch.setattr(
        deployment_worker,
        "dispatch_job",
        lambda clients, job, deployment: calls.append("dispatch"),
    )
    monkeypatch.setattr(
        deployment_worker,
        "mark_job_skipped",
        lambda job: calls.append("skipped"),
    )

    status = deployment_worker.process_claimed_job(FakeClients(), noop_job)

    assert status == "skipped"
    assert calls == ["skipped"]


def test_is_stale_job_compares_desired_generation() -> None:
    assert deployment_worker.is_stale_job(job_row(), deployment_row()) is False

    stale_job = job_row()
    stale_job["desired_generation"] = 1

    assert deployment_worker.is_stale_job(stale_job, deployment_row()) is True


def test_is_noop_job_detects_already_satisfied_commands() -> None:
    start = job_row("start_model")
    start["payload"] = {"previous_status": "running"}
    stop = job_row("stop_model")
    stop["payload"] = {"previous_status": "stopped"}
    scale = job_row("scale_model")
    scale["payload"] = {"previous_replicas": 2, "replicas": 2}

    assert deployment_worker.is_noop_job(start) is True
    assert deployment_worker.is_noop_job(stop) is True
    assert deployment_worker.is_noop_job(scale) is True
    assert deployment_worker.is_noop_job(job_row("deploy_model")) is False


def test_fetch_deployment_for_job_success(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=deployment_row())
    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)

    deployment = deployment_worker.fetch_deployment_for_job(job_row())

    assert deployment["model_deployment_id"] == MODEL_DEPLOYMENT_ID


def test_fetch_deployment_for_job_rejects_missing_model_id() -> None:
    job = job_row()
    job["model_deployment_id"] = None

    try:
        deployment_worker.fetch_deployment_for_job(job)
    except RuntimeError as exc:
        assert "missing model_deployment_id" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_fetch_deployment_for_job_rejects_missing_deployment(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=None)
    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)

    try:
        deployment_worker.fetch_deployment_for_job(job_row())
    except RuntimeError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_process_claimed_job_retries_before_max_attempts(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        deployment_worker,
        "fetch_deployment_for_job",
        lambda job: deployment_row(),
    )

    def dispatch_job(clients, job, deployment):
        raise RuntimeError("temporary Kubernetes failure")

    monkeypatch.setattr(deployment_worker, "dispatch_job", dispatch_job)
    monkeypatch.setattr(
        deployment_worker,
        "mark_job_failed_or_retrying",
        lambda job, exc: calls.append(str(exc)),
    )

    status = deployment_worker.process_claimed_job(
        FakeClients(),
        job_row(attempts=1, max_attempts=3),
    )

    assert status == "retrying"
    assert calls == ["temporary Kubernetes failure"]


def test_process_claimed_job_fails_at_max_attempts(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_worker,
        "fetch_deployment_for_job",
        lambda job: deployment_row(),
    )

    def dispatch_job(clients, job, deployment):
        raise RuntimeError("permanent failure")

    monkeypatch.setattr(deployment_worker, "dispatch_job", dispatch_job)
    monkeypatch.setattr(
        deployment_worker,
        "mark_job_failed_or_retrying",
        lambda job, exc: None,
    )

    status = deployment_worker.process_claimed_job(
        FakeClients(),
        job_row(attempts=2, max_attempts=3),
    )

    assert status == "failed"


def test_dispatch_job_calls_apply_for_deploy_and_start(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", False)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "apply_model_deployment",
        lambda clients, deployment, hugging_face_token=None: calls.append(
            deployment["name"]
        ),
    )

    deployment_worker.dispatch_job(FakeClients(), job_row("deploy_model"), deployment_row())
    deployment_worker.dispatch_job(FakeClients(), job_row("update_model"), deployment_row())
    deployment_worker.dispatch_job(FakeClients(), job_row("start_model"), deployment_row())

    assert calls == ["qwen-small-prod", "qwen-small-prod", "qwen-small-prod"]


def test_dispatch_job_hard_restart_deletes_then_applies(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", False)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "delete_model_deployment",
        lambda clients, deployment: calls.append(("delete", deployment["name"])),
    )
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "apply_model_deployment",
        lambda clients, deployment, hugging_face_token=None: calls.append(
            ("apply", deployment["name"])
        ),
    )

    deployment_worker.dispatch_job(
        FakeClients(),
        job_row("hard_restart_model"),
        deployment_row(),
    )

    assert calls == [
        ("delete", "qwen-small-prod"),
        ("apply", "qwen-small-prod"),
    ]


def test_dispatch_job_sync_status_sets_synced_status(monkeypatch) -> None:
    deployment = deployment_row()
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", False)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "inspect_model_readiness",
        lambda clients, deployment, expected_replicas: {
            "failed": False,
            "ready": True,
        },
    )

    deployment_worker.dispatch_job(FakeClients(), job_row("sync_status"), deployment)

    assert deployment["_synced_status"] == "running"


def test_dispatch_job_calls_stop_and_scale(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", False)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "stop_model_deployment",
        lambda clients, deployment: calls.append("stop"),
    )
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "scale_model_deployment",
        lambda clients, deployment, replicas: calls.append(("scale", replicas)),
    )

    deployment_worker.dispatch_job(FakeClients(), job_row("stop_model"), deployment_row())
    deployment_worker.dispatch_job(FakeClients(), job_row("scale_model"), deployment_row())

    assert calls == ["stop", ("scale", 3)]


def test_dispatch_job_calls_delete(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", False)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "delete_model_deployment",
        lambda clients, deployment: calls.append(deployment["name"]),
    )

    deployment_worker.dispatch_job(FakeClients(), job_row("delete_model"), deployment_row())

    assert calls == ["qwen-small-prod"]


def test_dispatch_job_dry_run_skips_kubernetes(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(deployment_worker.Config, "WORKER_DRY_RUN", True)
    monkeypatch.setattr(
        deployment_worker.deployment_manager,
        "apply_model_deployment",
        lambda clients, deployment, hugging_face_token=None: calls.append("apply"),
    )

    deployment_worker.dispatch_job(FakeClients(), job_row("deploy_model"), deployment_row())

    assert calls == []


def test_should_fail_permanently() -> None:
    assert deployment_worker.should_fail_permanently(job_row(attempts=1, max_attempts=3)) is False
    assert deployment_worker.should_fail_permanently(job_row(attempts=2, max_attempts=3)) is True


def test_truncate_error() -> None:
    assert deployment_worker.truncate_error("short") == "short"
    assert deployment_worker.truncate_error("abcdef", max_length=5) == "ab..."


def test_classify_failure_returns_actionable_category() -> None:
    failure = deployment_worker.classify_failure(
        RuntimeError("Pod model is waiting with ImagePullBackOff")
    )

    assert failure["category"] == "image_pull"
    assert "registry" in failure["hint"]


def test_classify_failure_detects_chat_template_issue() -> None:
    failure = deployment_worker.classify_failure(
        RuntimeError("default chat template is no longer allowed")
    )

    assert failure["category"] == "invalid_model_or_chat_template"


def test_update_deployment_status_with_cursor() -> None:
    cursor = FakeCursor(fetchone=deployment_row())

    row = deployment_worker.update_deployment_status_with_cursor(
        cursor,
        MODEL_DEPLOYMENT_ID,
        "running",
    )

    assert row["model_deployment_id"] == MODEL_DEPLOYMENT_ID
    assert cursor.executed[0]["status"] == "running"


def test_mark_job_skipped(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=job_row())
    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)

    deployment_worker.mark_job_skipped(job_row())

    assert fake.cursor.executed[0]["deployment_job_id"] == JOB_ID


def test_failed_stale_job_does_not_overwrite_newer_deployment(monkeypatch) -> None:
    deployment = deployment_row()
    deployment["desired_generation"] = 3
    fake = FakeTransaction(fetchone=deployment)
    job = job_row(attempts=0, max_attempts=3)
    job["desired_generation"] = 2

    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)

    deployment_worker.mark_job_failed_or_retrying(job, RuntimeError("old failure"))

    assert fake.cursor.executed[-1]["deployment_job_id"] == JOB_ID
    assert all(params.get("status") != "failed" for params in fake.cursor.executed)


def test_retrying_job_does_not_mark_deployment_failed(monkeypatch) -> None:
    fake = FakeTransaction(fetchone=deployment_row())
    monkeypatch.setattr(deployment_worker, "transaction", fake.transaction)

    deployment_worker.mark_job_failed_or_retrying(
        job_row(attempts=0, max_attempts=3),
        RuntimeError("temporary Kubernetes failure"),
    )

    assert fake.cursor.executed[-1]["deployment_job_id"] == JOB_ID
    assert all(params.get("status") != "failed" for params in fake.cursor.executed)


class FakeClients:
    pass


class FakeCursor:
    def __init__(self, fetchone=None):
        self._fetchone = fetchone
        self.executed = []

    def execute(self, sql, params):
        self.executed.append(params)

    def fetchone(self):
        return self._fetchone


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return CursorContext(self._cursor)


class CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        return False


class FakeTransaction:
    def __init__(self, fetchone=None):
        self.cursor = FakeCursor(fetchone)

    def transaction(self):
        return TransactionContext(self.cursor)


class TransactionContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return FakeConnection(self.cursor)

    def __exit__(self, *args):
        return False
