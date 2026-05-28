PYTHON ?= python
POETRY ?= $(PYTHON) -m poetry

.PHONY: install setup-env clean-env migrate run-api run-api-gunicorn run-worker run-worker-dry-run start-worker-real-k8s test-local-apis test-local-k8s tests coverage lint compile clean

install:
	$(PYTHON) -m pip install --upgrade pip poetry
	$(POETRY) install

setup-env: install
	$(POETRY) run python scripts/local_env_guard.py assert-api-not-running
	docker compose up -d postgres
	$(POETRY) run python scripts/wait_for_postgres.py
	$(POETRY) run python -m app.db.migrate
	$(POETRY) run python scripts/kind_env.py ensure
	K8S_SMOKE_TEST_IMAGE=$${K8S_SMOKE_TEST_IMAGE:-hashicorp/http-echo:1.0} WORKER_DRY_RUN=true WORKER_POLL_INTERVAL_SECONDS=0.2 KUBECONFIG_DIR=.local/kube docker compose up -d --build worker
	$(POETRY) run python scripts/local_env_guard.py mark-setup-complete

clean-env:
	docker compose down -v --remove-orphans
	$(POETRY) run python scripts/kind_env.py delete
	$(POETRY) run python scripts/local_env_guard.py clear-setup-marker

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
	K8S_SMOKE_TEST_IMAGE=$${K8S_SMOKE_TEST_IMAGE:-hashicorp/http-echo:1.0} KUBECONFIG_DIR=.local/kube WORKER_DRY_RUN=false docker compose up -d --build worker

test-local-apis:
	$(POETRY) run python scripts/smoke_test_local_api.py

test-local-k8s:
	$(MAKE) start-worker-real-k8s
	$(POETRY) run python scripts/smoke_test_local_k8s.py

tests:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(POETRY) run pytest

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
