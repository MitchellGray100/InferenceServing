# Inference Serving (MiniTen)

<img width="1112" height="362" alt="MiniTen logo" src="docs/miniten%20logo.png" />
<img width="2211" height="1171" alt="MiniTen system design diagram" src="docs/MiniTen%20System%20Design.png" />

MiniTen is a planned multi-user inference serving platform for deploying open-source Hugging Face LLMs as named vLLM workers on Kubernetes.

The project is designed to run locally first with `kind` or `minikube`, then later on Oracle Cloud Infrastructure using Oracle Kubernetes Engine.

MiniTen provides a Baseten-inspired workflow for:

```text
create account
  ↓
create/select project
  ↓
deploy named model
  ↓
manage model lifecycle
  ↓
call model through OpenAI-compatible HTTP APIs
```

---

## Project Status

MiniTen is currently a design-stage / MVP-build project.

The repository documents the planned:

- API surface
- database schema
- system design
- Kubernetes resource model
- deployment and inference data flows

Implementation should start with the Flask backend, Postgres schema/migrations, and local development setup before moving to Kubernetes and OKE.

---

## MVP Goals

MiniTen's MVP focuses on:

- User account creation and login
- Project-based isolation
- Project membership and roles
- Project-scoped API keys
- Named model deployments
- Hugging Face model IDs passed to vLLM
- Kubernetes-managed vLLM workers
- OpenAI-compatible inference endpoints
- Lightweight analytics and lifecycle events
- PVC-backed Hugging Face model cache
- Postgres-backed deployment jobs
- Idempotency keys for retried control-plane operations

MiniTen does not run model inference inside Flask. Flask manages metadata, authentication, project access, Kubernetes orchestration, and inference routing. vLLM workers perform the actual inference.

---

## Tech Stack

### Backend

- Python
- Flask
- psycopg 3
- Raw SQL migrations
- Raw SQL query files
- Kubernetes Python client

### Database

- Postgres
- Explicit SQL schema files
- `deployment_jobs` table for asynchronous lifecycle work
- `idempotency_keys` table for retry-safe control-plane operations

### Frontend / Dashboard

- HTML
- CSS
- JavaScript
- Flask templates
- Static files served by Flask

### Model Serving

- vLLM
- Hugging Face model IDs
- Kubernetes-managed vLLM worker pods
- PVC-mounted Hugging Face cache

### Local Infrastructure

- Docker
- Docker Compose for Postgres
- kind or minikube for local Kubernetes

### Future Cloud Target

- Oracle Cloud Infrastructure
- Oracle Kubernetes Engine
- OCI Load Balancer
- OCI Container Registry, optional

---

## API Overview

All public API endpoints are versioned under:

```text
/v1
```

MiniTen has these API groups:

| API Group | Purpose |
|---|---|
| Users API | Create, read, and delete user accounts |
| Auth API | Login/logout and token creation |
| Projects API | Create, list, inspect, and delete projects |
| Project Members API | Manage users inside a project |
| Project API Keys API | Create/revoke project-scoped inference API keys |
| Model Deployment API | Deploy, inspect, update, start, stop, delete, and log models |
| Analytics API | View usage metrics, request history, and lifecycle events |
| Inference API | Call deployed models through OpenAI-compatible endpoints |

---

## Authentication Model

MiniTen uses two authentication modes.

### User Auth Token

Used for dashboard and control-plane operations.

```http
Authorization: Bearer <user_access_token>
```

Used for:

```text
create project
manage members
create API key
deploy model
start/stop model
view analytics
```

### Project API Key

Used by external applications for inference.

```http
Authorization: Bearer <project_api_key>
```

Used for:

```text
POST /v1/chat/completions
GET /v1/models
```

The project API key determines the project. The `model` field in the request body determines which named deployment inside that project receives the request.

---

## Deployment Identity

All model operations use the project-local deployment name.

Example deployment name:

```text
qwen-small-prod
```

The Hugging Face model ID is stored separately as metadata:

```text
Qwen/Qwen2.5-0.5B-Instruct
```

The OpenAI-compatible request uses the MiniTen deployment name:

```json
{
  "model": "qwen-small-prod",
  "messages": []
}
```

This means the same Hugging Face model can be deployed multiple times in the same project under different names:

```text
qwen-small-dev
qwen-small-prod
qwen-small-gpu
```

Deployment names must be unique within a project.

Recommended model name format:

```text
lowercase letters
numbers
hyphens
must start and end with an alphanumeric character
```

---

## Core API Examples

### Create User

```http
POST /v1/users
```

```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

### Login

```http
POST /v1/auth/login
```

```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

### Create Project

```http
POST /v1/projects
Authorization: Bearer <user_access_token>
```

```json
{
  "name": "Personal Models"
}
```

### Deploy Model

```http
POST /v1/projects/{projectID}/models
Authorization: Bearer <user_access_token>
```

```json
{
  "name": "qwen-small-prod",
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "resources": {
    "cpu_request": "2",
    "cpu_limit": "4",
    "memory_request": "8Gi",
    "memory_limit": "16Gi",
    "gpu_count": 0
  },
  "vllm": {
    "image": "vllm/vllm-openai:latest",
    "dtype": "auto",
    "max_model_len": 4096
  },
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 3,
    "target_cpu_utilization": 70
  }
}
```

### Call Model

```http
POST /v1/chat/completions
Authorization: Bearer <project_api_key>
```

```json
{
  "model": "qwen-small-prod",
  "messages": [
    {
      "role": "user",
      "content": "Explain Kubernetes in one sentence."
    }
  ],
  "max_tokens": 128
}
```

The MVP proxies non-streaming chat completions first. Requests with
`"stream": true` return a `streaming_not_supported` error until streaming is
implemented.

---

## System Architecture

```text
External User / Developer App
        |
        v
OCI Load Balancer, or localhost during local development
        |
        v
Flask API / Dashboard
        |
        +--> Auth routes
        +--> User routes
        +--> Project routes
        +--> Project member routes
        +--> API key routes
        +--> Model deployment routes
        +--> Analytics routes
        +--> Inference routes
        |
        +--> Postgres
        |
        +--> Kubernetes API
                  |
                  v
          OKE / kind / minikube cluster
                  |
                  +--> Namespace per project
                  +--> Deployment per model version
                  +--> Service per named model deployment
                  +--> PVC per model deployment cache
                  +--> HPA per autoscaled deployment
                  +--> Secret per deployment, optional
                  +--> vLLM worker pods
```

MiniTen has two conceptual planes:

```text
Control plane = user/project/deployment management
Data plane    = inference routing to vLLM workers
```

---

## Control Plane

The control plane manages platform metadata and Kubernetes lifecycle operations.

Examples:

```text
sign up
log in
create project
create API key
deploy model
start model
stop model
scale model
delete model
view analytics
```

Control-plane state is stored in Postgres.

Slow lifecycle work is stored in the `deployment_jobs` table and processed by a background Deployment Worker/Reconciler.

---

## Data Plane

The data plane handles inference traffic.

Inference requests are synchronous and are not placed in the deployment job queue.

```text
External app
  ↓
POST /v1/chat/completions
  ↓
Flask inference route
  ↓
Validate project API key
  ↓
Resolve API key to project
  ↓
Read request.body.model as deployment name
  ↓
Find model_deployments row by project_id + name
  ↓
Check model is running
  ↓
Forward request to Kubernetes Service
  ↓
vLLM worker returns response
  ↓
Write inference_requests metadata
```

Inference does not go to Hugging Face and does not read the PVC directly. vLLM has already loaded the model into memory.

---

## Kubernetes Resource Model

For each project:

```text
Project: personal
Namespace: miniten-personal
```

For each model deployment:

```text
Deployment name: qwen-small-prod
Model ID: Qwen/Qwen2.5-0.5B-Instruct
Version: v1
```

For the MVP, `v1` is a fixed internal Kubernetes resource suffix for the deployment generation. It is not a user-facing versioning, rollback, or promotion system.

MiniTen creates:

```text
Deployment/qwen-small-prod-v1
Service/qwen-small-prod
PVC/qwen-small-prod-hf-cache
HPA/qwen-small-prod-v1, optional
Secret/qwen-small-prod-secrets, optional
```

The Deployment runs vLLM.

The Service gives the model a stable internal endpoint.

The PVC caches Hugging Face model files.

The HPA controls autoscaling when enabled.

The Secret can provide private model credentials such as a Hugging Face token.

---

## Model Weight Caching

For the MVP, MiniTen uses a PVC-backed Hugging Face cache.

Each vLLM worker mounts a Kubernetes PVC at:

```text
/root/.cache/huggingface
```

First startup / cache miss:

```text
vLLM worker pod starts
  ↓
PVC is mounted at /root/.cache/huggingface
  ↓
vLLM asks Hugging Face libraries for model files
  ↓
Local cache is empty
  ↓
vLLM downloads model files from Hugging Face
  ↓
Downloaded files are written directly into the PVC-mounted cache path
  ↓
vLLM loads model weights into CPU/GPU memory
  ↓
Readiness probe passes
```

Restart / cache hit:

```text
vLLM worker pod restarts
  ↓
Same PVC is mounted
  ↓
Model files are present
  ↓
vLLM loads model from PVC into memory
  ↓
Pod becomes ready
```

Hugging Face is only used on startup/cache miss. It is not used during normal inference.

---

## Autoscaling

MiniTen supports Kubernetes HPA-based autoscaling in the MVP design.

Example autoscaling config:

```json
{
  "enabled": true,
  "min_replicas": 1,
  "max_replicas": 3,
  "target_cpu_utilization": 70
}
```

Autoscaling flow:

```text
Traffic increases
  ↓
CPU utilization rises
  ↓
HPA observes metrics
  ↓
HPA increases replica count
  ↓
Deployment creates more vLLM pods
  ↓
New pods mount PVC cache
  ↓
Pods load model
  ↓
Kubernetes Service load-balances traffic across ready pods
```

Autoscaling is supported in the MVP.

MiniTen uses a shared PVC-backed Hugging Face cache by default. When autoscaling creates more than one replica for a deployment, the configured storage class must support mounting that cache across replicas with a compatible access mode such as `ReadWriteMany`.

If the local or cloud cluster does not support a compatible shared volume mode, the deployment may still run with one replica, but multi-replica autoscaling with a shared cache is not guaranteed.

---

## Database

Postgres stores application metadata.

Core tables:

```text
users
projects
project_members
model_deployments
api_keys
inference_requests
model_events
idempotency_keys
deployment_jobs
```

Postgres does not store model weights, prompts, or model responses.

Model weights live on Hugging Face and are cached in Kubernetes PVCs.

Kubernetes remains the source of truth for live pod and replica state.

---

## Deployment Jobs

`deployment_jobs` stores asynchronous model lifecycle work and the durable history of deployment commands that were requested, attempted, retried, completed, or failed.

MVP deployment assumption: run exactly one Deployment Worker process/pod. The
queue format already preserves command history and stale-job skipping, but
horizontal worker scaling should wait until per-model serialization and worker
heartbeat/lease renewal are implemented.

Job types:

```text
deploy_model
start_model
stop_model
scale_model
delete_model
sync_status
```

Flow:

```text
User requests deploy/start/stop/scale/delete
  ↓
Flask route validates auth and project permissions
  ↓
Model deployment metadata is written to Postgres
  ↓
deployment_jobs row is created
  ↓
Deployment Worker claims the job
  ↓
Deployment Worker calls Kubernetes API
  ↓
Deployment Worker updates model status
  ↓
Deployment Worker writes model_events
  ↓
Job is marked succeeded, retrying, or failed
```

The job queue is for control-plane operations only.

Normal chat/inference requests do not use this queue.

Each model control-plane command increments `model_deployments.desired_generation`
and stores that generation on the queued job. Older jobs remain in history but
are marked `skipped` if a newer desired state has already superseded them.

---

## Idempotency

`idempotency_keys` prevents duplicate side effects from retried control-plane requests.

Used for operations such as:

```text
deploy model
start model
stop model
scale model
delete model
```

Example:

```http
POST /v1/projects/{projectID}/models
Idempotency-Key: deploy-qwen-small-prod-001
```

Retry behavior:

```text
same key + same request body    → return original response
same key + different body       → return 409 Conflict
```

Model deploy/start/stop/scale/delete routes require `Idempotency-Key`.

Idempotency is not used for normal inference requests in the MVP.

---

## Analytics

MiniTen stores lightweight inference request metadata.

The `inference_requests` table can support:

```text
request count
error count
average latency
recent request history
last request time
```

The MVP should not store prompts or model responses.

Lifecycle events are stored in `model_events`.

---

## Local Development

The system is designed to work locally before moving to OCI.

Local equivalents:

```text
OKE / Kubernetes       → kind or minikube
OCI Load Balancer      → localhost / port-forward
Postgres               → Docker Compose Postgres
vLLM workers           → Kubernetes pods in kind/minikube
PVC model cache        → local Kubernetes PVC
Hugging Face Hub       → public Hugging Face Hub
```

Example local workflow:

```bash
make setup-env
make run-api
make test-local-apis
```

Common development commands:

```bash
make install
make setup-env
make clean-env
make migrate
make run-api
make run-worker
make run-worker-dry-run
make start-worker-real-k8s
make test-local-apis
make test-local-k8s
make lint
make tests
make compile
make clean
```

`make setup-env` installs Python dependencies, starts local Postgres through
Docker Compose, waits for it to accept connections, applies migrations,
creates or reuses a local `kind` cluster named `miniten`, writes a Docker
Compose kubeconfig to `.local/kube/config`, and starts the local Deployment
Worker in dry-run mode.
`make clean-env` stops Compose services, removes the local Postgres volume,
deletes the `kind` cluster, and removes the local setup marker.
`make test-local-apis` runs HTTP smoke tests against a running local API, so
start `make run-api` in another terminal first. The smoke test waits for
`GET /readyz` before exercising authenticated endpoints, then verifies queued
deployment jobs are consumed and marked `succeeded`. If `setup-env` restarts
Postgres while the API is already running, restart `make run-api` so the Flask
process opens fresh database connections.

The Makefile enforces the expected order for local development:

```bash
make setup-env
make run-api
```

`make setup-env` fails if the local API port is already running, because setup
may restart Postgres. `make run-api` fails until `setup-env` has completed and
written the local setup marker. `make clean-env` removes that marker.

`make run-api` starts Flask's development server through `app/main.py`, which
works on Windows/Git Bash. Gunicorn depends on Unix-only modules such as
`fcntl`, so `make run-api-gunicorn` is for Linux/WSL-style environments only.
The Docker image uses the Gunicorn path by default, while the Compose `worker`
service overrides the command to run `python -m app.services.deployment_worker`.
`make setup-env` always starts that worker for local development with
`WORKER_DRY_RUN=true`, so local deployment commands are processed without
mutating a Kubernetes cluster. `make run-worker` uses the real Kubernetes
client, and `make run-worker-dry-run` runs a foreground dry-run worker when you
want to inspect worker logs directly.

The setup script requires Docker, `kind`, and `kubectl` on PATH. On Windows,
install the Kubernetes tools with:

```powershell
winget install --id Kubernetes.kind --exact
winget install --id Kubernetes.kubectl --exact
```

For a Docker Compose worker that mutates the real local `kind` cluster, run
`make start-worker-real-k8s`. That command refreshes `.local/kube/config` and
restarts the worker with `WORKER_DRY_RUN=false`. The worker container uses host
networking so kind's localhost API endpoint still matches its TLS certificate.

Real local Kubernetes smoke workflow:

```bash
make setup-env
make run-api
make test-local-k8s
```

`make test-local-k8s` automatically switches the worker into real Kubernetes
mode, deploys a lightweight smoke-test container, waits for the worker to
verify Kubernetes readiness, checks Namespace/PVC/Deployment/Service/HPA
creation, calls the model logs endpoint, deletes the model, and verifies
runtime resources are removed. If `HUGGING_FACE_TOKEN` is set, the worker also
creates and deletes the per-model Secret. This test intentionally avoids
pulling the full `vllm/vllm-openai` image; production-like model loading can be
tested separately by overriding `MINITEN_K8S_TEST_MODEL_ID` and
`K8S_SMOKE_TEST_IMAGE`.

Logging is controlled with `LOG_LEVEL`, defaulting to `INFO`. Use `DEBUG` when
you need lower-level SQL/Kubernetes/auth diagnostics. Logs intentionally avoid
raw API keys, passwords, prompts, model responses, and Hugging Face tokens.

---

## Repository Shape

Current structure:

```text
README.md
.env.example
docker-compose.yml
Dockerfile
pyproject.toml

app/
  __init__.py
  config.py

  routes/
    auth.py
    users.py
    projects.py
    project_members.py
    api_keys.py
    model_deployments.py
    inference.py
    analytics.py
    dashboard.py

  services/
    auth_service.py
    user_service.py
    project_service.py
    api_key_service.py
    model_deployment_service.py
    inference_service.py
    deployment_worker.py
    reconciler.py
    idempotency_service.py

  db/
    pool.py
    migrate.py
    sql.py
    queries/
      users.sql
      projects.sql
      api_keys.sql
      model_deployments.sql
      deployment_jobs.sql
      inference_requests.sql
      model_events.sql
      idempotency_keys.sql

  k8s/
    client.py
    names.py
    manifests.py
    deployment_manager.py

  security/
    passwords.py
    tokens.py
    api_keys.py

  utils/
    errors.py
    validation.py
    time.py

  templates/
  static/

migrations/
  001_initial_schema.sql

scripts/
examples/
docs/
  miniten logo.png
  MiniTen System Design.png
  MiniTen_System_Design.md
  MiniTen_API_Endpoint_Design.md
  MiniTen_Database_Schema.md
tests/
```

---

