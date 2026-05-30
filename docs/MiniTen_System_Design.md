# MiniTen System Design

## 1. Overview

MiniTen is a multi-user inference serving platform for deploying open-source Hugging Face LLMs as vLLM workers on Kubernetes.

The Flask API exposes unauthenticated operational probes:

```text
GET /healthz
GET /readyz
```

`/healthz` reports process liveness. `/readyz` verifies the API can query
Postgres and should be used by local smoke tests, Docker/CI checks, and future
Kubernetes readiness probes for the control plane API.

The platform lets users:

- Sign up and log in.
- Create or select a project.
- Deploy a Hugging Face model under a project-local deployment name.
- Manage model lifecycle operations such as inspect, start, stop, scale, and delete.
- Create project-scoped API keys.
- Call deployed models through OpenAI-compatible HTTP APIs.
- Track model status, request metadata, logs, and lifecycle events.

MiniTen is designed to run locally first with `kind` or `minikube`, then later on Oracle Cloud Infrastructure using Oracle Kubernetes Engine.

---

## 2. Core Product Flow

```text
User logs in
  ↓
User creates/selects project
  ↓
User deploys named model
  ↓
MiniTen creates Kubernetes resources
  ↓
vLLM worker starts and loads model
  ↓
User creates API key
  ↓
External app calls /v1/chat/completions
  ↓
MiniTen routes request to the correct vLLM worker
  ↓
Model response is returned
```

Example deployment:

```bash
miniten models deploy Qwen/Qwen2.5-0.5B-Instruct \
  --name qwen-small-prod \
  --cpu 2 \
  --memory 8Gi \
  --gpu 0
```

Example inference call:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.miniten.dev/v1",
    api_key="mt_live_xxx",
)

response = client.chat.completions.create(
    model="qwen-small-prod",
    messages=[
        {"role": "user", "content": "Explain Kubernetes in one sentence."}
    ],
)
```

The `model` field uses the MiniTen deployment name, not the raw Hugging Face model ID.

---

## 3. Tech Stack

## Backend

- Python
- Flask
- psycopg 3
- Raw SQL migrations
- Raw SQL query files
- Kubernetes Python client

## Database

- Postgres
- Explicit SQL schema files
- Postgres-backed deployment job queue

## Frontend / Dashboard

- HTML
- CSS
- JavaScript
- Flask templates
- Static files served by Flask

## Model Serving

- vLLM
- Hugging Face model IDs
- vLLM OpenAI-compatible server
- Kubernetes-managed vLLM worker pods

## Kubernetes / Infrastructure

- Docker
- Docker Compose for local Postgres
- kind or minikube for local Kubernetes
- Kubernetes Deployments
- Kubernetes Services
- Kubernetes PVCs
- Kubernetes HPAs
- Kubernetes Secrets
- Oracle Kubernetes Engine later
- OCI Load Balancer later
- OCI Container Registry later, optional

---

## 4. High-Level Architecture

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

## 5. Major Components

## 5.1 Flask API / Dashboard

The Flask app serves both the API and the dashboard for the MVP.

It handles:

- HTML dashboard pages.
- Static CSS/JavaScript.
- JSON API routes.
- Authentication.
- Project authorization.
- Deployment lifecycle requests.
- API key management.
- Inference routing.
- Request logging.

Recommended route modules:

```text
app/routes/auth.py
app/routes/users.py
app/routes/projects.py
app/routes/project_members.py
app/routes/api_keys.py
app/routes/model_deployments.py
app/routes/inference.py
app/routes/analytics.py
app/routes/dashboard.py
```

Recommended service modules:

```text
app/services/auth_service.py
app/services/user_service.py
app/services/project_service.py
app/services/api_key_service.py
app/services/model_deployment_service.py
app/services/inference_service.py
app/services/deployment_worker.py
```

Recommended support modules:

```text
app/db/pool.py
app/db/migrate.py
app/db/sql.py
app/db/queries/

app/k8s/client.py
app/k8s/names.py
app/k8s/manifests.py
app/k8s/deployment_manager.py

app/security/passwords.py
app/security/tokens.py
app/security/api_keys.py

app/utils/errors.py
app/utils/validation.py
app/utils/time.py
```

---

## 5.2 Postgres

Postgres stores MiniTen application metadata.

It does not store:

- Model weights.
- Full prompts.
- Full model responses.
- Live Kubernetes state as the source of truth.

Core tables:

```text
users
projects
project_members
model_deployments
api_keys
inference_requests
model_events
deployment_jobs
```

Postgres is the source of truth for product metadata. Kubernetes is the source of truth for live infrastructure state.

---

## 5.3 Kubernetes Cluster

The Kubernetes cluster runs model-serving infrastructure.

Local development uses:

```text
kind or minikube
```

Cloud deployment later uses:

```text
Oracle Kubernetes Engine
```

For each project:

```text
Project → Kubernetes Namespace
```

For each model deployment:

```text
Deployment name → Kubernetes Deployment + Service + PVC + optional HPA + optional Secret
```

Example:

```text
Project: personal
Namespace: miniten-personal

MiniTen deployment name: qwen-small-prod
K8s Deployment: qwen-small-prod-v1
K8s Service: qwen-small-prod
K8s PVC: qwen-small-prod-hf-cache
K8s HPA: qwen-small-prod-v1
```

For the MVP, `v1` is a fixed internal Kubernetes resource suffix for the deployment generation. It is not a user-facing versioning, rollback, or promotion system.

---

## 5.4 vLLM Workers

Each deployed model runs as one or more vLLM worker pods. Clients provide the
Hugging Face model id and resource settings; MiniTen chooses the container image
as an internal provisioning detail.

GPU-backed deployments use the standard vLLM image:

```text
vllm/vllm-openai:latest
```

CPU-only deployments use the CPU-specific vLLM image:

```text
vllm/vllm-openai-cpu:latest-x86_64
```

The model is selected through runtime arguments:

```text
--model Qwen/Qwen2.5-0.5B-Instruct
--host 0.0.0.0
--port 8000
--dtype auto
--max-model-len 4096
```

The same MiniTen-selected image can serve many different Hugging Face models in
the matching CPU or GPU runtime mode.

---

## 5.5 Kubernetes Service

A Kubernetes Service is not a MiniTen microservice.

It is a Kubernetes networking object that gives a stable internal address to vLLM pods.

Inference traffic flows through the Kubernetes Service:

```text
Inference route
  ↓
K8s Service/qwen-small-prod
  ↓
vLLM Worker Pod(s)
```

The internal URL looks like:

```text
http://qwen-small-prod.miniten-personal.svc.cluster.local:8000/v1/chat/completions
```

---

## 5.6 PVC Model Cache

MiniTen uses a Kubernetes PVC as a persistent Hugging Face cache for model files.

Each vLLM pod mounts the PVC at:

```text
/root/.cache/huggingface
```

The PVC is passive storage. It does not download anything by itself.

The vLLM worker reads and writes model files through the mounted filesystem path.

---

## 5.7 Deployment Worker

Model lifecycle operations can be slow. Deploying a model requires creating Kubernetes resources, starting pods, downloading model weights, and waiting for readiness.

MiniTen uses a Postgres-backed `deployment_jobs` table so the API can return quickly and retain a durable history of deployment commands.

MiniTen can run multiple Deployment Worker processes/pods. Workers claim queued
jobs from Postgres and use fenced per-model operation leases with heartbeats so
slow Kubernetes operations for one model cannot overlap with a stale worker
that wakes up later. Different models can still be processed in parallel.

Local development starts the worker during `make setup-env` with
`WORKER_DRY_RUN=true`. In dry-run mode the worker still claims
`deployment_jobs`, checks `desired_generation`, updates `model_deployments`,
marks jobs `succeeded`/`skipped`/`failed`, and writes `model_events`, but it
does not call the Kubernetes API. This lets local smoke tests verify the
control-plane queue without mutating Kubernetes. `make setup-env` also creates
or reuses a local `kind` cluster named `miniten` and writes a Docker-friendly
kubeconfig to `.local/kube/config`. The Compose worker uses host networking so
kind's localhost API endpoint still matches its TLS certificate. Real local
Kubernetes runs use `make test-local-k8s`, which switches the Compose worker to
`WORKER_DRY_RUN=false` before deploying a smoke-test model.

The real local Kubernetes smoke test uses a lightweight OpenAI-compatible
Python HTTP container instead of the full vLLM image. It validates Kubernetes
resource creation/deletion, the model logs endpoint, project API key inference,
request metadata logging, and analytics reads. Because `make run-api` executes
Flask on the host, the smoke test opens a temporary `kubectl port-forward` and
local debug inference routing uses `INFERENCE_LOCAL_PORT_FORWARD_URL`; deployed
API containers use Kubernetes Service DNS directly.

`make test-local-vllm` is the slower production-like smoke path. It deploys a
real vLLM image, waits for a small Hugging Face model to load, calls
`/v1/chat/completions`, verifies analytics metadata, and prints Kubernetes pod,
event, describe, and log diagnostics when startup or readiness fails. The local
command uses `vllm/vllm-openai-cpu:latest-x86_64` and
`MINITEN_VLLM_TEST_DEVICE=cpu` by default because Docker Desktop/kind usually
does not expose GPUs to pods and the normal `vllm/vllm-openai` image is the GPU
path. The default test model is a small instruct model because
`/v1/chat/completions` requires a tokenizer with a chat template. GPU validation
should use the normal vLLM image and set
`MINITEN_VLLM_TEST_DEVICE=cuda` only after the Kubernetes node advertises GPU
resources. MiniTen passes this value through the `VLLM_TARGET_DEVICE`
environment variable, because vLLM reads it while constructing CLI defaults and
the CPU image does not accept a `--device` CLI flag. CPU deployments also pass
MiniTen's internal `VLLM_CPU_MEMORY_UTILIZATION` setting so vLLM's CPU KV-cache
reservation fits inside local Docker Desktop/kind memory. The local vLLM smoke
test also uses a short context length and a larger memory limit than the fast
smoke test because CPU vLLM has meaningful startup overhead. Model pods use `imagePullPolicy:
IfNotPresent` so restarts reuse images already present in the node image store.
In kind, that cache is inside the kind node container's containerd store, not
necessarily the host Docker Images view. Local cleanup preserves this cache by
default: `make clean-env` resets Compose/Postgres and the setup marker but keeps
the kind cluster. `make clean-kind` intentionally deletes the kind cluster and
its image cache, and `make clean-all` performs both cleanup paths.

`make test-local-vllm-gpu` is the dedicated local GPU smoke path. It reuses the
real vLLM smoke test but sets `gpu_count=1`, `VLLM_TARGET_DEVICE=cuda`, and the
managed GPU image path. Before deploying, it verifies Docker can see a
vLLM-compatible GPU and verifies the active Kubernetes context advertises
allocatable `nvidia.com/gpu`.

Docker Desktop kind is intentionally rejected for this GPU path. Docker Desktop
can run GPU containers with `docker run --gpus all`, but kind launches pods
through containerd inside the kind node container. Docker Desktop's NVIDIA
runtime injection does not propagate into that nested runtime, so vLLM pods can
be scheduled but fail CUDA/NVML initialization. GPU smoke tests should run
against a Linux/WSL Kubernetes cluster configured with NVIDIA container
runtime/device-plugin support.
The GPU smoke preflight also checks Docker-visible GPU compute capability before
deploying because current prebuilt vLLM GPU images require modern NVIDIA GPU
architectures. Pascal-era cards such as GTX 1080 Ti fail this preflight and
should use the CPU vLLM smoke path unless a custom Pascal-compatible vLLM image
is supplied.

The Deployment Worker:

- Polls `deployment_jobs`.
- Claims queued jobs.
- Compares job `desired_generation` with the current model deployment generation.
- Marks stale jobs `skipped` without changing Kubernetes.
- Calls the Kubernetes API.
- Reapplies Kubernetes resources for `deploy_model`, `update_model`, and `start_model`.
- Force-recreates runtime Kubernetes resources for `hard_restart_model` by deleting
  the Deployment, Service, HPA, and Secret, then applying the deployment again
  while retaining the model cache PVC.
- Waits for Kubernetes readiness before marking deploy/update/start/scale jobs successful.
- Detects pod scheduling, pod readiness, Service existence, Deployment availability, and common failed pod states such as image pull and crash-loop errors.
- Classifies worker failures into stable categories such as `image_pull`,
  `insufficient_memory`, `insufficient_cpu`, `gpu_unavailable`,
  `model_download_auth`, `invalid_model_or_chat_template`,
  `readiness_timeout`, and `pod_failure`.
- Updates `model_deployments`.
- Writes `model_events`.
- Retries failed jobs when appropriate and stores the category-prefixed error in
  `deployment_jobs.last_error`.

Status synchronization is handled by explicit `sync_status` deployment jobs.
Those jobs are processed by the Deployment Worker, which reads live Kubernetes
readiness and updates MiniTen's durable status without changing desired
generation or mutating Kubernetes resources.

The API also exposes `GET /v1/projects/{projectID}/models/{modelDeploymentID}/status`
for on-demand troubleshooting. It joins durable model metadata, recent
deployment jobs, best-effort live Kubernetes readiness, pod summaries, and a
short recent log snippet. Kubernetes inspection failures do not fail the status
endpoint; they are returned as `kubernetes.available=false` so operators can
still see the database state and latest command history.

---

## 6. Deployment Identity

Every model deployment has two identifiers:

```text
deployment name = user-facing project-local model name
model_id        = Hugging Face model ID passed to vLLM
```

Example:

```text
deployment name: qwen-small-prod
model_id: Qwen/Qwen2.5-0.5B-Instruct
```

The deployment name is used for:

- Dashboard actions.
- CLI commands.
- API calls.
- OpenAI-compatible `model` field.
- Kubernetes resource naming.

The Hugging Face model ID is stored as metadata and passed to vLLM.

MiniTen owns container image selection. Users provide model IDs and deployment
settings, not arbitrary image names. CPU-only deployments use the managed CPU
vLLM image, GPU deployments use the managed GPU vLLM image, and local debug
smoke tests can select the lightweight smoke image only through MiniTen's
internal smoke model ID. The API rejects unsupported fields, invalid
CPU/memory quantities, invalid dtype values, and autoscaling settings where the
desired replica count falls outside min/max bounds.

Deployment names must be unique within a project:

```sql
UNIQUE(project_id, name)
```

The same Hugging Face model can be deployed multiple times with different names:

```text
qwen-small-dev
qwen-small-prod
qwen-small-gpu
```

---

## 7. Control Plane Data Flow

Control-plane operations manage platform and infrastructure state.

Examples:

- Sign up.
- Log in.
- Create project.
- Create API key.
- Deploy model.
- Start model.
- Stop model.
- Scale model.
- Delete model.

## 7.1 User Signup/Login Flow

```text
Browser
  ↓
Flask auth route
  ↓
Validate request
  ↓
Hash password or verify password
  ↓
Read/write users table
  ↓
Return session/JWT
```

Relevant tables:

```text
users
project_members, if creating default project
projects, if creating default project
```

---

## 7.2 Create Project Flow

```text
Browser/API client
  ↓
Flask projects route
  ↓
Validate authenticated user
  ↓
Create projects row
  ↓
Create project_members owner row
  ↓
Return project
```

Relevant tables:

```text
projects
project_members
```

The Kubernetes namespace can be created immediately or lazily when the first model is deployed.

For the MVP, namespace creation can happen during the first model deployment.

---

## 7.3 Create API Key Flow

```text
User
  ↓
POST /v1/projects/{projectID}/api-keys
  ↓
Validate project permissions
  ↓
Generate raw API key
  ↓
Hash API key with server-secret HMAC
  ↓
Store HMAC hash and visible prefix in api_keys
  ↓
Return raw key once
```

Relevant table:

```text
api_keys
```

Raw API keys are never stored. MiniTen stores only a visible lookup prefix and a server-secret HMAC of the full raw key.

---

## 7.4 Deploy Model Flow

The deploy flow is asynchronous.

```text
User requests model deploy
  ↓
Flask model deployment route
  ↓
Validate user and project permissions
  ↓
Validate deployment name is unique in project
  ↓
  ↓
Create model_deployments row
  ↓
Create deployment_jobs row
  ↓
Return queued/deploying response
```

Then the Deployment Worker processes the job:

```text
Deployment Worker
  ↓
Claim deploy_model job from deployment_jobs
  ↓
Create/update Kubernetes Namespace
  ↓
Create PVC for model cache
  ↓
Create Kubernetes Deployment
  ↓
Create Kubernetes Service
  ↓
Create HPA if autoscaling enabled
  ↓
Create Secret if HF token is provided
  ↓
Update model_deployments status
  ↓
Write model_events
  ↓
Mark job succeeded or failed
```

Relevant tables:

```text
model_deployments
deployment_jobs
model_events
```

Relevant Kubernetes resources:

```text
Namespace
Deployment
Service
PVC
HPA
Secret
```

---

## 7.5 Start Model Flow

```text
User clicks Start
  ↓
Flask route validates permissions
  ↓
Create start_model deployment_jobs row
  ↓
Deployment Worker claims job
  ↓
Patch Kubernetes Deployment replicas to 1
  ↓
Update model status to loading/running
```

If HPA is enabled, the start operation may restore `min_replicas` rather than manually setting a fixed replica count.

---

## 7.6 Stop Model Flow

```text
User clicks Stop
  ↓
Flask route validates permissions
  ↓
Create stop_model deployment_jobs row
  ↓
Deployment Worker claims job
  ↓
Patch Kubernetes Deployment replicas to 0
  ↓
Update model status to stopped
```

Stopping a model does not delete its metadata or PVC cache.

---

## 7.7 Hard Restart Model Flow

```text
User clicks Hard restart
  ↓
Flask route validates permissions
  ↓
Create hard_restart_model deployment_jobs row
  ↓
Deployment Worker claims job
  ↓
Delete runtime Kubernetes resources
  ↓
Reapply Deployment, Service, HPA, Secret, and related manifests
  ↓
Update model status to running
```

Hard restart is intended for recovery when normal stop/start cannot cleanly
unstick a deployment. It keeps the model cache PVC by default so model weights
do not need to be re-downloaded unless the cache itself is removed separately.

---

## 7.8 Scale Model Flow

```text
User requests scale
  ↓
Flask route validates permissions
  ↓
Create scale_model deployment_jobs row
  ↓
Deployment Worker claims job
  ↓
Patch Kubernetes Deployment or HPA config
  ↓
Update model_deployments metadata
```

Recommended MVP behavior:

```text
If autoscaling is disabled:
  allow manual replica scaling

If autoscaling is enabled:
  require updating HPA min/max settings instead of manual replica count
```

---

## 8. Data Plane Inference Flow

Inference requests are synchronous.

They are not placed in the deployment job queue.

```text
External app
  ↓
POST /v1/chat/completions
  ↓
Flask inference route
  ↓
Validate API key
  ↓
Resolve API key to project
  ↓
Read request.body.model as deployment name
  ↓
Find model_deployments row by project_id + name
  ↓
Check model is running
  ↓
Build Kubernetes Service URL
  ↓
Forward request to vLLM Service
  ↓
Return vLLM response
  ↓
Write inference_requests metadata
```

Relevant tables:

```text
api_keys
model_deployments
inference_requests
```

Relevant Kubernetes resource:

```text
Service/qwen-small-prod
```

Inference does not go to Hugging Face.

Inference does not read the PVC directly.

vLLM has already loaded the model into memory.

---

## 9. Model Weight Data Flow

For the MVP, MiniTen uses a PVC-backed Hugging Face cache.

## 9.1 First Startup / Cache Miss

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
  ↓
Kubernetes Service can send traffic to the pod
```

There is no separate upload into PVC. The PVC is mounted as a filesystem path, so downloads are written directly there.

## 9.2 Restart / Cache Hit

```text
vLLM worker pod restarts
  ↓
Same PVC is mounted
  ↓
vLLM checks /root/.cache/huggingface
  ↓
Model files are present
  ↓
vLLM loads model from PVC into memory
  ↓
Pod becomes ready
```

This avoids re-downloading from Hugging Face.

## 9.3 Normal Inference

```text
Inference Service
  ↓
K8s Service
  ↓
vLLM worker
  ↓
Model already loaded in memory
  ↓
Response
```

Hugging Face is not involved during normal inference.

---

## 10. Autoscaling Flow

MiniTen supports Kubernetes HPA-based autoscaling.

User config:

```yaml
autoscaling:
  enabled: true
  min_replicas: 1
  max_replicas: 3
  target_cpu_utilization: 70
```

MiniTen translates this into a Kubernetes HPA.

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

MVP storage rule:

MiniTen uses a shared PVC-backed Hugging Face cache by default. When autoscaling creates more than one replica for a deployment, the configured storage class must support mounting that cache across replicas with a compatible access mode such as `ReadWriteMany`.

If the local or cloud cluster does not support a compatible shared volume mode, the deployment may still run with one replica, but multi-replica autoscaling with a shared cache is not guaranteed.

---



Example:

```http
POST /projects/proj_123/models
```

First request:

```text
Request arrives
  ↓
  ↓
  ↓
Run operation
  ↓
Store response
  ↓
Return response
```

Retry with same key and same request body:

```text
Request arrives
  ↓
  ↓
Request hash matches
  ↓
Return stored response
  ↓
Do not create another job
```

Retry with same key and different body:

```text
Request arrives
  ↓
  ↓
Request hash differs
  ↓
Return 409 Conflict
```

Used for:

```text
deploy model
start model
stop model
scale model
delete model
```

prevent older, already-queued jobs from applying after a newer command has been
requested for the same model deployment.

response would require storing the raw API key.

Not used for:

```text
/v1/chat/completions
```

---

## 12. Deployment Job Queue Flow

MiniTen uses Postgres as a durable queue and command history for lifecycle operations.

## 12.1 Job Creation

```text
Flask route
  ↓
Validate request
  ↓
Insert or update model_deployments
  ↓
Insert deployment_jobs row
  ↓
Return quickly
```

## 12.2 Job Claiming

The Deployment Worker claims one job atomically:

```sql
WITH next_job AS (
  SELECT deployment_job_id
  FROM deployment_jobs
  WHERE status IN ('queued', 'retrying')
    AND (locked_at IS NULL OR locked_at < NOW() - INTERVAL '5 minutes')
  ORDER BY created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE deployment_jobs
SET
  status = 'running',
  locked_by = $1,
  locked_at = NOW(),
  updated_at = NOW()
WHERE deployment_job_id = (SELECT deployment_job_id FROM next_job)
RETURNING *;
```

## 12.3 Job Completion

```text
Worker succeeds
  ↓
Update Kubernetes resources
  ↓
Update model_deployments
  ↓
Write model_events
  ↓
Mark job succeeded
```

## 12.4 Job Failure

```text
Worker fails
  ↓
Increment attempts
  ↓
Store last_error
  ↓
If attempts remain: status = retrying
  ↓
If attempts exhausted: status = failed
```

---

## 13. Kubernetes Resource Model

For each project:

```text
Project: personal
Namespace: miniten-personal
```

For each deployment:

```text
Deployment name: qwen-small-prod
Model ID: Qwen/Qwen2.5-0.5B-Instruct
Version: v1
```

For the MVP, `v1` is a fixed internal Kubernetes resource suffix for the deployment generation. It is not a user-facing versioning, rollback, or promotion system.

Create:

```text
Deployment/qwen-small-prod-v1
Service/qwen-small-prod
PVC/qwen-small-prod-hf-cache
HPA/qwen-small-prod-v1, optional
Secret/qwen-small-prod-secrets, optional
```

The Deployment runs vLLM.

The Service gives a stable internal endpoint.

The PVC caches model weights.

The HPA controls replica count.

The Secret provides optional Hugging Face token access.

---

## 14. Health Checks

Each vLLM worker should use startup and readiness probes.

```yaml
startupProbe:
  httpGet:
    path: /health
    port: 8000
  failureThreshold: 60
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
```

Startup probe gives vLLM time to download/cache/load model weights.

Readiness probe prevents traffic from reaching a pod before the model is ready.

---

## 15. Status Mapping

MiniTen maps Kubernetes state to product-level statuses.

```text
Kubernetes/resources being created → deploying
Pod exists but model not ready      → loading
Ready pod exists                    → running
Deployment replicas = 0             → stopped
Pod crash loop or failed job         → failed
Delete requested                    → deleting
Deleted or soft-deleted             → deleted
```

Allowed `model_deployments.status` values:

```text
deploying
loading
running
stopped
failed
deleting
deleted
```

---

## 16. Local Development Data Flow

Local development mirrors the cloud design.

```text
Browser / curl / OpenAI SDK
  ↓
localhost Flask app
  ↓
Postgres in Docker Compose
  ↓
kind or minikube Kubernetes API
  ↓
vLLM pod in local cluster
  ↓
PVC cache in local cluster
  ↓
Hugging Face on cache miss
```

Local equivalents:

```text
OKE / Kubernetes       → kind or minikube
OCI Load Balancer      → localhost / port-forward
Postgres               → Docker Compose Postgres
vLLM workers           → Kubernetes pods in kind/minikube
PVC model cache        → local Kubernetes PVC
Hugging Face Hub       → public Hugging Face Hub
```

---

## 17. Cloud Deployment Data Flow

Cloud deployment uses the same application logic.

```text
Browser / Developer App
  ↓
OCI Load Balancer
  ↓
Flask app running on OKE
  ↓
Postgres
  ↓
OKE Kubernetes API
  ↓
vLLM workers
  ↓
PVC cache
  ↓
Hugging Face on cache miss
```

Future cloud improvements may include:

```text
OCI Container Registry
Managed Postgres
OCI Object Storage model cache
Prometheus/Grafana
KEDA
GPU node pools
```

---

## 18. Security Model

## Users

- Users authenticate with email/password.
- Passwords are hashed with Argon2id or bcrypt.
- Users belong to projects through `project_members`.

## Projects

- Projects are the main isolation boundary.
- Each project maps to a Kubernetes namespace.
- Deployment names are unique within a project.

## API Keys

- API keys are project-scoped.
- Raw API keys are shown only once.
- Only server-secret API key HMACs are stored.
- API keys are used for inference requests.

## Kubernetes

- Users do not receive Kubernetes credentials.
- The Flask backend/worker uses Kubernetes credentials.
- Model deployments are isolated by namespace.

---

## 19. Observability

MiniTen tracks lightweight metadata for operations and inference.

## Model Events

`model_events` records deployment lifecycle history:

```text
deploy_requested
k8s_namespace_created
k8s_deployment_created
k8s_service_created
hpa_created
model_loading
model_running
model_stopped
model_started
model_scaled
model_failed
model_deleted
```

## Inference Requests

`inference_requests` records:

```text
project_id
model_deployment_id
api_key_id
status_code
latency_ms
error_type
request_path
method
streamed
created_at
```

Prompts and responses are not stored in the MVP.

---

## 20. Out of Scope for MVP

The MVP does not include:

- Billing
- Quotas
- Kafka
- Redis
- Async chat jobs
- Server-side chat memory
- OAuth/SSO
- Fine-tuning
- Batch inference
- OCI Object Storage model cache
- Custom Docker model builds
- Multi-node tensor parallelism
- Advanced GPU scheduling
- Prometheus/KEDA autoscaling

---

## 21. Final Mental Model

```text
Flask manages the product.
Postgres stores product metadata.
Deployment jobs make lifecycle operations asynchronous.
The Deployment Worker talks to Kubernetes.
Kubernetes runs vLLM workers.
Kubernetes Services route inference traffic.
PVCs cache Hugging Face model files.
vLLM serves model responses.
Hugging Face is only used on startup/cache miss.
```

MiniTen is a control plane for named vLLM model deployments. It does not run model inference inside Flask. Flask authenticates users, manages metadata, creates Kubernetes resources, and routes inference traffic to vLLM workers.
