"""Application configuration loaded from environment variables."""

import os


class Config:
    """Runtime settings shared by Flask routes, services, and workers.

    Defaults are local-development values. Public repositories should keep real
    secrets out of source control and override sensitive values through `.env`
    or deployment-specific environment variables.
    """

    # Flask and authentication settings.
    SECRET_KEY = os.getenv("SECRET_KEY", "dev")
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql://miniten:miniten@localhost:5432/miniten",
    )
    API_KEY_HASH_SECRET = os.getenv("API_KEY_HASH_SECRET", "dev")

    # Kubernetes/vLLM deployment defaults.
    K8S_NAMESPACE_PREFIX = os.getenv("K8S_NAMESPACE_PREFIX", "miniten")
    VLLM_IMAGE = os.getenv("VLLM_IMAGE", "vllm/vllm-openai:latest")
    DEFAULT_MODEL_REPLICAS = int(os.getenv("DEFAULT_MODEL_REPLICAS", "1"))
    DEFAULT_HPA_MIN_REPLICAS = int(os.getenv("DEFAULT_HPA_MIN_REPLICAS", "1"))
    DEFAULT_HPA_MAX_REPLICAS = int(os.getenv("DEFAULT_HPA_MAX_REPLICAS", "3"))
    DEFAULT_HPA_TARGET_CPU_UTILIZATION = int(
        os.getenv("DEFAULT_HPA_TARGET_CPU_UTILIZATION", "70")
    )
    DEFAULT_PVC_SIZE = os.getenv("DEFAULT_PVC_SIZE", "20Gi")

    # HTTP timeout for synchronous proxy calls from Flask to vLLM.
    INFERENCE_UPSTREAM_TIMEOUT_SECONDS = int(
        os.getenv("INFERENCE_UPSTREAM_TIMEOUT_SECONDS", "300")
    )
