PYTHON ?= python
POETRY ?= $(PYTHON) -m poetry
WORKER_REPLICAS ?= 2

.PHONY: install setup-env setup-web open-dashboard clean-env start-kind stop-kind clean-kind clean-all install-metrics-server migrate run-api run-api-gunicorn run-worker run-worker-dry-run start-worker-real-k8s test test-local-apis test-local-k8s test-local-vllm test-local-vllm-gpu tests coverage lint compile clean

install:
	$(PYTHON) -m pip install --upgrade pip poetry
	$(POETRY) install

setup-env: install
	$(POETRY) run python scripts/local_env_guard.py assert-api-not-running
	$(POETRY) run python scripts/local_env_guard.py assert-docker-ready
	docker compose up -d postgres
	$(POETRY) run python scripts/wait_for_postgres.py
	$(POETRY) run python -m app.db.migrate
	$(MAKE) start-kind
	$(POETRY) run python scripts/kind_env.py ensure
	$(MAKE) install-metrics-server
	K8S_SMOKE_TEST_IMAGE=$${K8S_SMOKE_TEST_IMAGE:-python:3.12-alpine} WORKER_DRY_RUN=true WORKER_POLL_INTERVAL_SECONDS=0.2 KUBECONFIG_DIR=.local/kube docker compose up -d --build --force-recreate --scale worker=$(WORKER_REPLICAS) worker
	$(POETRY) run python scripts/local_env_guard.py mark-setup-complete

setup-web: setup-env
	$(MAKE) start-worker-real-k8s
	$(POETRY) run python scripts/start_dashboard.py

open-dashboard:
	$(POETRY) run python scripts/start_dashboard.py

clean-env:
	$(POETRY) run python scripts/stop_local_api.py
	docker compose down -v --remove-orphans
	$(MAKE) stop-kind
	$(POETRY) run python scripts/local_env_guard.py clear-setup-marker

start-kind:
	$(POETRY) run python scripts/kind_env.py start

stop-kind:
	$(POETRY) run python scripts/kind_env.py stop

clean-kind:
	$(POETRY) run python scripts/kind_env.py delete
	$(POETRY) run python scripts/local_env_guard.py clear-setup-marker

clean-all: clean-env clean-kind

install-metrics-server:
	$(POETRY) run python scripts/metrics_server.py

migrate:
	$(POETRY) run python -m app.db.migrate

run-api:
	$(POETRY) run python scripts/local_env_guard.py assert-setup-complete
	$(POETRY) run python -m app.main

ifeq ($(OS),Windows_NT)
run-api-gunicorn:
	@echo "Gunicorn does not run on native Windows because it depends on Unix-only modules such as fcntl."
	@echo "Use 'make run-api' on Windows, or run Gunicorn inside Docker/WSL/Linux."
	@exit 1
else
run-api-gunicorn:
	$(POETRY) run gunicorn --bind 0.0.0.0:8000 --workers 2 wsgi:app
endif

run-worker:
	$(POETRY) run python -m app.services.deployment_worker

run-worker-dry-run:
	WORKER_DRY_RUN=true $(POETRY) run python -m app.services.deployment_worker

start-worker-real-k8s:
	$(POETRY) run python scripts/kind_env.py ensure
	$(MAKE) install-metrics-server
	K8S_SMOKE_TEST_IMAGE=$${K8S_SMOKE_TEST_IMAGE:-python:3.12-alpine} KUBECONFIG_DIR=.local/kube WORKER_DRY_RUN=false docker compose up -d --build --force-recreate --scale worker=$(WORKER_REPLICAS) worker

test-local-apis:
	K8S_SMOKE_TEST_IMAGE=$${K8S_SMOKE_TEST_IMAGE:-python:3.12-alpine} WORKER_DRY_RUN=true WORKER_POLL_INTERVAL_SECONDS=0.2 KUBECONFIG_DIR=.local/kube docker compose up -d --build --force-recreate --scale worker=$(WORKER_REPLICAS) worker
	$(POETRY) run python scripts/smoke_test_local_api.py

test-local-k8s:
	$(MAKE) start-worker-real-k8s
	$(POETRY) run python scripts/smoke_test_local_k8s.py

test-local-vllm:
	VLLM_DEVICE=$${MINITEN_VLLM_TEST_DEVICE:-cpu} $(MAKE) start-worker-real-k8s
	$(POETRY) run python scripts/smoke_test_local_vllm.py

test-local-vllm-gpu:
	$(POETRY) run python scripts/check_local_gpu_k8s.py
	$(MAKE) install-metrics-server
	MINITEN_VLLM_TEST_MODEL_ID=$${MINITEN_VLLM_GPU_TEST_MODEL_ID:-HuggingFaceTB/SmolLM2-135M-Instruct} MINITEN_VLLM_TEST_GPU_COUNT=$${MINITEN_VLLM_GPU_TEST_GPU_COUNT:-1} MINITEN_VLLM_TEST_DEVICE=$${MINITEN_VLLM_GPU_TEST_DEVICE:-cuda} VLLM_DEVICE=$${MINITEN_VLLM_GPU_TEST_DEVICE:-cuda} KUBECONFIG_DIR=$${KUBECONFIG_DIR:-.local/kube} WORKER_DRY_RUN=false docker compose up -d --build --force-recreate --scale worker=$(WORKER_REPLICAS) worker
	MINITEN_VLLM_TEST_MODEL_ID=$${MINITEN_VLLM_GPU_TEST_MODEL_ID:-HuggingFaceTB/SmolLM2-135M-Instruct} MINITEN_VLLM_TEST_GPU_COUNT=$${MINITEN_VLLM_GPU_TEST_GPU_COUNT:-1} MINITEN_VLLM_TEST_DEVICE=$${MINITEN_VLLM_GPU_TEST_DEVICE:-cuda} $(POETRY) run python scripts/smoke_test_local_vllm.py

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(POETRY) run pytest

tests:
	$(MAKE) test
	$(MAKE) test-local-apis
	$(MAKE) test-local-k8s
	$(MAKE) test-local-vllm

coverage:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(POETRY) run coverage run -m pytest
	$(POETRY) run coverage report -m

lint:
	$(POETRY) run ruff check app tests scripts

compile:
	$(POETRY) run python -m compileall app tests

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
