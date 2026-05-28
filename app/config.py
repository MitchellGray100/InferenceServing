"""Application configuration loaded from environment variables."""

import os

from dotenv import load_dotenv


# Load local `.env` values before the Config class evaluates environment
# variables. Real deployment environments should provide these values directly.
load_dotenv()


class Config:
    """Runtime settings shared by Flask routes, services, and workers.

    Defaults are local-development values. Public repositories should keep real
    secrets out of source control and override sensitive values through `.env`
    or deployment-specific environment variables.
    """

    # Flask and authentication settings.
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me-32-bytes")
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql://miniten:miniten@localhost:5432/miniten",
    )
    API_KEY_HASH_SECRET = os.getenv(
        "API_KEY_HASH_SECRET",
        "dev-api-key-hash-secret-change-me-32-bytes",
    )

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
    HUGGING_FACE_TOKEN = os.getenv("HUGGING_FACE_TOKEN")
    K8S_SMOKE_TEST_IMAGE = os.getenv(
        "K8S_SMOKE_TEST_IMAGE",
        "hashicorp/http-echo:1.0",
    )

    # HTTP timeout for synchronous proxy calls from Flask to vLLM.
    INFERENCE_UPSTREAM_TIMEOUT_SECONDS = int(
        os.getenv("INFERENCE_UPSTREAM_TIMEOUT_SECONDS", "300")
    )

    # Runtime process defaults.
    API_HOST = os.getenv("API_HOST", "0.0.0.0")
    API_PORT = int(os.getenv("API_PORT", "8000"))
    API_DEBUG = os.getenv("API_DEBUG", "true").lower() in {"1", "true", "yes", "on"}
    WEB_CONCURRENCY = int(os.getenv("WEB_CONCURRENCY", "2"))
    WORKER_POLL_INTERVAL_SECONDS = float(
        os.getenv("WORKER_POLL_INTERVAL_SECONDS", "2.0")
    )
    WORKER_READINESS_TIMEOUT_SECONDS = float(
        os.getenv("WORKER_READINESS_TIMEOUT_SECONDS", "600")
    )
    WORKER_READINESS_POLL_INTERVAL_SECONDS = float(
        os.getenv("WORKER_READINESS_POLL_INTERVAL_SECONDS", "5.0")
    )
    WORKER_DRY_RUN = os.getenv("WORKER_DRY_RUN", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
