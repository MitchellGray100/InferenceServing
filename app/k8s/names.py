"""Kubernetes-safe resource naming helpers.

Model deployment names are user-facing and project-local. Kubernetes resource
names derived from them must fit DNS label constraints, leave room for internal
suffixes such as `-v1`, and stay deterministic so the deployment worker can
reconcile the same desired resources on every retry.
"""

from __future__ import annotations

import re

from app.utils.errors import ApiError


DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
MAX_DNS_LABEL_LENGTH = 63
MODEL_GENERATION_SUFFIX = "-v1"
PVC_SUFFIX = "-hf-cache"
SECRET_SUFFIX = "-secrets"


def validate_dns_label(value: str, field: str = "name") -> str:
    """Validate a Kubernetes DNS label and return it unchanged."""
    if not DNS_LABEL_RE.fullmatch(value):
        raise ApiError(
            type="validation_error",
            message=(
                f"{field} must be a Kubernetes DNS label: lowercase letters, "
                "numbers, and hyphens, starting and ending with alphanumeric."
            ),
            status_code=400,
        )

    return value


def append_suffix(base_name: str, suffix: str, field: str = "name") -> str:
    """Append a suffix while preserving Kubernetes' 63-character limit."""
    validate_dns_label(base_name, field)

    if len(suffix) >= MAX_DNS_LABEL_LENGTH:
        raise ValueError("suffix is too long for a Kubernetes DNS label")

    max_base_length = MAX_DNS_LABEL_LENGTH - len(suffix)
    trimmed_base = base_name[:max_base_length].rstrip("-")
    return validate_dns_label(f"{trimmed_base}{suffix}", field)


def build_model_resource_names(k8s_namespace: str, deployment_name: str) -> dict[str, str]:
    """Build deterministic resource names for one model deployment.

    The Service keeps the stable user-facing deployment name. The Deployment and
    HPA include a fixed `-v1` generation suffix so a future rollout system can
    create `-v2` while the Service continues routing by the stable model name.
    """
    validate_dns_label(k8s_namespace, "k8s_namespace")
    validate_dns_label(deployment_name, "deployment_name")

    return {
        "k8s_namespace": k8s_namespace,
        "k8s_deployment_name": append_suffix(deployment_name, MODEL_GENERATION_SUFFIX),
        "k8s_service_name": deployment_name,
        "k8s_hpa_name": append_suffix(deployment_name, MODEL_GENERATION_SUFFIX),
        "k8s_pvc_name": append_suffix(deployment_name, PVC_SUFFIX),
        "k8s_secret_name": append_suffix(deployment_name, SECRET_SUFFIX),
    }
