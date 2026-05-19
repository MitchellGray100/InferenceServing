# Inference Serving (MiniTen)

<img width="1112" height="362" alt="image" src="https://github.com/user-attachments/assets/8e06f9a5-c1fb-4f04-98ef-d271c724a9b8" />
<img width="2211" height="1171" alt="image" src="https://github.com/user-attachments/assets/f6f7ab21-b39f-49b1-9acb-072912177f96" />

MiniTen is an OCI-hosted, multi-user inference serving platform for deploying open-source Hugging Face LLMs as vLLM workers on Oracle Kubernetes Engine.

It provides a Baseten-inspired workflow for deploying named model services, managing their lifecycle, and calling them from application code through OpenAI-compatible HTTP APIs.

The core product loop is:

```text
log in → create/select project → deploy named model → call model through API → inspect/start/stop/scale deployment
```

---

## Features

- Multi-user authentication
- Project-scoped API keys
- Named model deployments per project
- Open-source Hugging Face LLM deployment with vLLM
- Kubernetes Deployment, Service, PVC, and HPA orchestration
- PVC-backed Hugging Face model cache
- Readiness and health checks
- Start, stop, inspect, scale, and delete model deployments
- HPA-based autoscaling with configurable min/max replicas
- Postgres-backed deployment job queue for async lifecycle operations
- Idempotency keys for retried control-plane requests
- Structured logging and deployment metadata tracking
- Lightweight inference request analytics
- OpenAI-compatible `/v1/chat/completions` API

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
- SQL schema managed through explicit migration files
- Postgres-backed deployment jobs table
- Postgres-backed idempotency keys table

### Frontend / Dashboard

- HTML
- CSS
- JavaScript
- Flask templates
- Static assets served by Flask

### Model Serving

- vLLM
- Hugging Face model IDs
- Kubernetes-managed vLLM worker pods
- PVC-mounted Hugging Face model cache

### Infrastructure

- Docker
- Docker Compose for local Postgres
- kind or minikube for local Kubernetes development
- Oracle Kubernetes Engine later
- OCI Load Balancer later
- OCI Container Registry later, optional

---

## System Overview

MiniTen separates the platform into a control plane and a data plane.

### Control Plane

The control plane manages users, projects, API keys, deployment metadata, and Kubernetes resources.

Control-plane operations include:

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
inspect logs/status
```

Slow lifecycle operations are written to a Postgres-backed `deployment_jobs` table and processed asynchronously by a Deployment Worker/Reconciler.

### Data Plane

The data plane handles inference traffic.

Inference requests are synchronous and OpenAI-compatible:

```text
Client application
  ↓
POST /v1/chat/completions
  ↓
MiniTen inference service
  ↓
Kubernetes Service for selected model
  ↓
vLLM worker pod
  ↓
response
```

Chat/inference requests do not use the deployment job queue in the MVP because clients expect an immediate or streaming response.

---

## Architecture

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
        +--> Project routes
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
                  +--> Kubernetes Namespace per project
                  +--> Deployment per model version
                  +--> Service per named model deployment
                  +--> PVC per model deployment cache
                  +--> HPA per autoscaled deployment
                  +--> vLLM worker pods
```

For a deployed model, inference traffic flows through the Kubernetes Service, not directly to pod IPs:

```text
Inference Service
  ↓
K8s Service/qwen-small-prod
  ↓
vLLM Worker Pod(s)
```

---

## Deployment Identity

Users interact with models by their project-local deployment name.

Example config:

```yaml
name: qwen-small-prod
model: Qwen/Qwen2.5-0.5B-Instruct
engine: vllm

resources:
  cpu: "2"
  memory: "8Gi"
  gpu: 0

autoscaling:
  enabled: true
  min_replicas: 1
  max_replicas: 3
  target_cpu_utilization: 70

vllm:
  dtype: auto
  max_model_len: 4096
```

The `name` field is the API-facing identifier.

The Hugging Face `model` ID is implementation metadata passed to vLLM.

Users call the deployment by name:

```json
{
  "model": "qwen-small-prod",
  "messages": [
    {
      "role": "user",
      "content": "Explain Kubernetes in one sentence."
    }
  ]
}
```

This allows the same Hugging Face model to be deployed multiple times in one project under different names:

```text
qwen-small-dev
qwen-small-prod
qwen-small-gpu
```

Deployment names are unique within a project.

---

## Example Usage

Deploy a model:

```bash
miniten models deploy Qwen/Qwen2.5-0.5B-Instruct \
  --name qwen-small-prod \
  --cpu 2 \
  --memory 8Gi \
  --gpu 0
```

List deployed models:

```bash
miniten models list
```

Inspect a model:

```bash
miniten models inspect qwen-small-prod
```

Stop a model:

```bash
miniten models stop qwen-small-prod
```

Start a model:

```bash
miniten models start qwen-small-prod
```

Scale a model:

```bash
miniten models scale qwen-small-prod --replicas 3
```

Create an API key:

```bash
miniten api-keys create --name local-dev
```

Call a deployed model from Python:

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

print(response.choices[0].message.content)
```

---

## Kubernetes Resources

For a deployment named `qwen-small-prod`, MiniTen creates resources such as:

```text
Namespace:   miniten-personal
Deployment:  qwen-small-prod-v1
Service:     qwen-small-prod
PVC:         qwen-small-prod-hf-cache
HPA:         qwen-small-prod-v1
Secret:      qwen-small-prod-secrets, optional
```

The inference service forwards traffic to the internal Kubernetes Service:

```text
http://qwen-small-prod.miniten-personal.svc.cluster.local:8000/v1/chat/completions
```

---

## Model Weight Caching

For the MVP, MiniTen uses a PVC-backed Hugging Face cache.

Each vLLM worker mounts a Kubernetes PVC at:

```text
/root/.cache/huggingface
```

Startup flow:

```text
vLLM worker pod starts
  ↓
PVC is mounted at /root/.cache/huggingface
  ↓
vLLM checks local Hugging Face cache
  ↓
if cache hit: load model files from PVC
  ↓
if cache miss: download model files from Hugging Face into PVC
  ↓
load weights into CPU/GPU memory
  ↓
pod becomes ready
```

The PVC is passive storage. The vLLM worker reads and writes the model cache.

Inference requests do not go to Hugging Face. Hugging Face is only used during startup/cache miss.

Future work may add OCI Object Storage as a durable cross-cluster model cache.

---

## Autoscaling

MiniTen supports Kubernetes HPA-based autoscaling.

Example config:

```yaml
autoscaling:
  enabled: true
  min_replicas: 1
  max_replicas: 3
  target_cpu_utilization: 70
```

MiniTen translates this into a Kubernetes `HorizontalPodAutoscaler` for the model deployment.

HPA adjusts the number of vLLM worker pods between the configured replica limits.

For the MVP, autoscaling uses CPU utilization. Future versions may use vLLM metrics, queue depth, in-flight requests, latency, or GPU utilization through Prometheus/KEDA.

---

## API Keys

Users create project-scoped API keys for inference access.

Example:

```text
mt_live_xxx
```

The API key determines the project. The `model` field in the request determines which named deployment inside that project receives the request.

API keys are stored as hashes. Raw API keys are shown only once.

---

## Deployment Jobs

MiniTen uses a Postgres-backed job queue for slow model lifecycle operations.

Job types include:

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
A deployment_jobs row is created
  ↓
Deployment Worker claims the job
  ↓
Deployment Worker calls Kubernetes API
  ↓
Deployment Worker updates model status and writes events
```

This keeps API requests fast and makes Kubernetes operations retryable.

Normal chat requests do not use this queue.

---

## Idempotency

MiniTen supports idempotency keys for control-plane operations.

Example:

```http
POST /projects/proj_123/models
Idempotency-Key: deploy-qwen-small-prod-001
```

If the same request is retried with the same idempotency key, MiniTen returns the original response instead of creating duplicate deployment jobs or Kubernetes resources.

If the same idempotency key is reused with a different request body, MiniTen returns a conflict error.

Idempotency is used for:

```text
deploy model
start model
stop model
scale model
delete model
create API key
```

Idempotency is not used for normal inference requests in the MVP.

---

## Database

Postgres stores MiniTen application metadata.

Core tables include:

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

Postgres stores metadata only.

It does not store model weights, prompts, or model responses.

Model weights are stored by Hugging Face and cached in Kubernetes PVCs.

---

## Local Development

The stack can run locally before deploying to OCI.

Local equivalents:

```text
OKE / Kubernetes       → kind or minikube
OCI Load Balancer      → localhost / port-forward
Postgres               → Docker Compose Postgres
vLLM workers           → Kubernetes pods in kind/minikube
PVC model cache        → local Kubernetes PVC
Hugging Face Hub       → same public Hugging Face Hub
```

Example local setup:

```bash
docker compose up -d postgres
kind create cluster --name miniten
python -m app.db.migrate
flask --app app run --debug
python -m app.services.deployment_worker
```

---

## Repository Structure

```text
miniten/
├── README.md
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
│
├── app/
│   ├── __init__.py
│   ├── config.py
│   │
│   ├── routes/
│   │   ├── auth.py
│   │   ├── users.py
│   │   ├── projects.py
│   │   ├── project_members.py
│   │   ├── api_keys.py
│   │   ├── model_deployments.py
│   │   ├── inference.py
│   │   ├── analytics.py
│   │   └── dashboard.py
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── project_service.py
│   │   ├── api_key_service.py
│   │   ├── model_deployment_service.py
│   │   ├── inference_service.py
│   │   ├── deployment_worker.py
│   │   └── idempotency_service.py
│   │
│   ├── db/
│   │   ├── pool.py
│   │   ├── migrate.py
│   │   ├── sql.py
│   │   └── queries/
│   │
│   ├── k8s/
│   │   ├── client.py
│   │   ├── names.py
│   │   ├── manifests.py
│   │   └── deployment_manager.py
│   │
│   ├── templates/
│   └── static/
│
├── migrations/
├── scripts/
├── examples/
├── docs/
└── tests/
```

---

## MVP Scope

The MVP includes:

- Email/password authentication
- Projects and project memberships
- Project-scoped API keys
- Named vLLM model deployments
- Kubernetes Namespace, Deployment, Service, PVC, and HPA creation
- PVC-backed Hugging Face cache
- Start/stop/scale/delete model lifecycle operations
- Postgres-backed deployment jobs
- Idempotency keys for control-plane retries
- OpenAI-compatible synchronous inference routing
- Basic request logging and analytics

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

## Status

MiniTen is a personal infrastructure project focused on Kubernetes-based model serving, authentication, routing, lifecycle management, and autoscaling for open-source LLM inference.
