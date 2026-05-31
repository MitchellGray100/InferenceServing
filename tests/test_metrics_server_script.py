from __future__ import annotations

import subprocess

from scripts import metrics_server


def metrics_server_deployment(*, args: list[str] | None = None) -> dict:
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "metrics-server",
                            "args": args or [],
                        }
                    ]
                }
            }
        }
    }


def test_has_container_arg_detects_existing_kind_patch() -> None:
    deployment = metrics_server_deployment(
        args=["--secure-port=10250", "--kubelet-insecure-tls"]
    )

    assert metrics_server.has_container_arg(
        deployment, metrics_server.KIND_INSECURE_TLS_ARG
    )


def test_ensure_metrics_server_skips_apply_and_patch_when_installed(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(metrics_server, "require_tool", lambda name: None)
    monkeypatch.setattr(metrics_server, "deployment_exists", lambda context: True)
    monkeypatch.setattr(
        metrics_server,
        "read_deployment",
        lambda context: metrics_server_deployment(
            args=[metrics_server.KIND_INSECURE_TLS_ARG]
        ),
    )

    def fake_run(
        args: list[str], *, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

    monkeypatch.setattr(metrics_server, "run", fake_run)

    metrics_server.ensure_metrics_server("miniten")

    assert not any("apply" in call for call in calls)
    assert not any("patch" in call for call in calls)
    assert any("rollout" in call for call in calls)
