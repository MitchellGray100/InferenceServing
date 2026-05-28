# MiniTen Database Schema Reference

This document describes the MiniTen database schema.

The database stores product-level metadata: users, projects, memberships, deployments, API keys, request logs, and model lifecycle events.

Kubernetes stores live infrastructure state. Hugging Face stores model weights. Postgres stores MiniTen's application metadata.

---

## Schema Overview

Final MVP tables:

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

Core relationship flow:

```text
users
  └── project_members
        └── projects
              ├── model_deployments
              │     ├── inference_requests
              │     └── model_events
              └── api_keys
                    └── inference_requests

users
  └── idempotency_keys

projects
  └── idempotency_keys

model_deployments
  └── deployment_jobs
```

Important design rules:

- Users are identified by email.
- Projects are the main isolation boundary.
- Each project maps to one Kubernetes namespace.
- Model deployments are identified by a project-local deployment name.
- The Hugging Face model ID is stored as metadata and passed to vLLM.
- API keys are project-scoped.
- Raw API keys are never stored.
- Request prompts and model responses are not stored.
- Kubernetes state is mirrored into application status fields, but Kubernetes remains the source of truth for live pod/replica state.
- Idempotency keys prevent duplicate side effects from retried control-plane requests.
- Deployment jobs make model lifecycle operations asynchronous and retryable.

---

## Postgres Extension

The schema uses `gen_random_uuid()` for primary keys.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

---

# 1. `users`

## Purpose

Stores account and login information for MiniTen users.

This table answers:

> Who is this person?

Project access is not stored directly on the user. Project membership lives in `project_members`.

## Table Definition

```sql
CREATE TABLE users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  email TEXT NOT NULL UNIQUE,
  hashed_password TEXT NOT NULL,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `user_id` | `UUID` | Yes | Primary key. Stable internal user identifier. |
| `email` | `TEXT` | Yes | Unique email address used for login and project invitations. |
| `hashed_password` | `TEXT` | Yes | Secure password hash. Never store plaintext passwords. |
| `created_at` | `TIMESTAMP` | Yes | Time the account was created. |
| `last_login_at` | `TIMESTAMP` | No | Last successful login time. Nullable for newly created users. |

## Important Notes

- Normalize emails before storing: trim whitespace and lowercase.
- Use Argon2id or bcrypt for password hashing.
- Do not store project IDs in this table.
- Do not store raw API keys in this table.

---

# 2. `projects`

## Purpose

Stores project-level metadata.

A project is the main isolation boundary for users, model deployments, API keys, request logs, and Kubernetes namespaces.

Each project maps to exactly one Kubernetes namespace.

## Table Definition

```sql
CREATE TABLE projects (
  project_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Human-readable project name shown in the dashboard
  name TEXT NOT NULL,

  -- URL-safe unique project identifier
  -- Example: "personal-models"
  slug TEXT NOT NULL UNIQUE,

  -- Kubernetes namespace for all model deployments in this project
  -- Example: "miniten-personal-models"
  k8s_namespace TEXT NOT NULL UNIQUE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `project_id` | `UUID` | Yes | Primary key. Stable internal project identifier. |
| `name` | `TEXT` | Yes | Human-readable project name shown in the dashboard. |
| `slug` | `TEXT` | Yes | URL-safe unique project identifier. |
| `k8s_namespace` | `TEXT` | Yes | Kubernetes namespace used for this project's model resources. |
| `created_at` | `TIMESTAMP` | Yes | Time the project was created. |

## Important Notes

- `name` does not need to be globally unique.
- `slug` should be globally unique.
- `k8s_namespace` should be globally unique.
- The namespace can be generated from the slug. Example: `personal-models` becomes `miniten-personal-models`.
- If two projects have the same name, generate different slugs, such as `personal-models` and `personal-models-x7k2`.

---

# 3. `project_members`

## Purpose

Join table between `users` and `projects`.

This supports many users per project, many projects per user, and project-level roles.

This table answers:

> Which users have access to this project, and what are they allowed to do?

## Table Definition

```sql
CREATE TABLE project_members (
  project_member_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  role TEXT NOT NULL DEFAULT 'member'
    CHECK (role IN ('owner', 'member', 'viewer')),

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  -- A user can only be a member of the same project once
  UNIQUE(project_id, user_id)
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `project_member_id` | `UUID` | Yes | Primary key for the membership row. |
| `project_id` | `UUID` | Yes | Project the user belongs to. |
| `user_id` | `UUID` | Yes | User who belongs to the project. |
| `role` | `TEXT` | Yes | User's role in the project. |
| `created_at` | `TIMESTAMP` | Yes | Time the user was added to the project. |

## Role Values

Allowed roles:

```text
owner
member
viewer
```

Permissions:

| Role | Permissions |
|---|---|
| `owner` | Full project access, including member management. |
| `member` | Deploy, inspect, start, stop, scale, delete models, view logs, create API keys. |
| `viewer` | View models, logs, and metadata. No infrastructure changes. |

## Important Notes

- `UNIQUE(project_id, user_id)` prevents duplicate memberships.
- `ON DELETE CASCADE` removes memberships when a user or project is deleted.
- The role `CHECK` constraint prevents invalid values like `admin`, `Owner`, or `superuser`.

---

# 4. `model_deployments`

## Purpose

Stores named model deployments inside projects.

This is one of the most important tables.

Each row represents a user-facing model service, such as:

```text
qwen-small-prod
tinyllama-dev
llama-3b-gpu
```

The deployment name is what users use in dashboard actions, CLI commands, and OpenAI-compatible API requests.

The Hugging Face model ID is stored separately as implementation metadata.

## Table Definition

```sql
CREATE TABLE model_deployments (
  model_deployment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  -- User-facing deployment name within the project
  name TEXT NOT NULL,

  -- Hugging Face model ID passed to vLLM
  model_id TEXT NOT NULL,

  status TEXT NOT NULL DEFAULT 'deploying'
    CHECK (status IN (
      'deploying',
      'loading',
      'running',
      'stopped',
      'failed',
      'deleting',
      'deleted'
    )),

  -- Kubernetes resource names
  k8s_namespace TEXT NOT NULL,
  k8s_deployment_name TEXT NOT NULL,
  k8s_service_name TEXT NOT NULL,
  k8s_hpa_name TEXT,

  -- Runtime configuration
  replicas INTEGER NOT NULL DEFAULT 1,

  cpu_request TEXT,
  cpu_limit TEXT,
  memory_request TEXT,
  memory_limit TEXT,
  gpu_count INTEGER NOT NULL DEFAULT 0,

  -- vLLM configuration
  vllm_image TEXT NOT NULL DEFAULT 'vllm/vllm-openai:latest',
  vllm_dtype TEXT NOT NULL DEFAULT 'auto',
  vllm_max_model_len INTEGER NOT NULL DEFAULT 4096,

  -- Autoscaling configuration
  autoscaling_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  min_replicas INTEGER,
  max_replicas INTEGER,
  target_cpu_utilization INTEGER,

  created_by_user_id UUID NOT NULL REFERENCES users(user_id),

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at TIMESTAMP,

  -- Deployment names must be unique within a project
  UNIQUE(project_id, name)
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `model_deployment_id` | `UUID` | Yes | Primary key for the deployment. |
| `project_id` | `UUID` | Yes | Project that owns this deployment. |
| `name` | `TEXT` | Yes | User-facing deployment name, unique inside the project. |
| `model_id` | `TEXT` | Yes | Hugging Face model ID passed to vLLM. |
| `status` | `TEXT` | Yes | Current platform-level deployment status. |
| `k8s_namespace` | `TEXT` | Yes | Kubernetes namespace where resources are created. |
| `k8s_deployment_name` | `TEXT` | Yes | Kubernetes Deployment name. |
| `k8s_service_name` | `TEXT` | Yes | Kubernetes Service name. |
| `k8s_hpa_name` | `TEXT` | No | Kubernetes HPA name, if autoscaling is enabled. |
| `replicas` | `INTEGER` | Yes | Desired replica count when autoscaling is disabled. |
| `cpu_request` | `TEXT` | No | Kubernetes CPU request. Example: `2`. |
| `cpu_limit` | `TEXT` | No | Kubernetes CPU limit. Example: `4`. |
| `memory_request` | `TEXT` | No | Kubernetes memory request. Example: `8Gi`. |
| `memory_limit` | `TEXT` | No | Kubernetes memory limit. Example: `16Gi`. |
| `gpu_count` | `INTEGER` | Yes | Number of GPUs requested by the model worker. |
| `vllm_image` | `TEXT` | Yes | vLLM container image. |
| `vllm_dtype` | `TEXT` | Yes | vLLM dtype setting. Example: `auto`, `float16`, `bfloat16`. |
| `vllm_max_model_len` | `INTEGER` | Yes | Maximum model context length passed to vLLM. |
| `autoscaling_enabled` | `BOOLEAN` | Yes | Whether HPA should be enabled. |
| `min_replicas` | `INTEGER` | No | HPA minimum replicas. |
| `max_replicas` | `INTEGER` | No | HPA maximum replicas. |
| `target_cpu_utilization` | `INTEGER` | No | HPA target CPU utilization percentage. |
| `created_by_user_id` | `UUID` | Yes | User who created the deployment. |
| `created_at` | `TIMESTAMP` | Yes | Time the deployment was created. |
| `updated_at` | `TIMESTAMP` | Yes | Last time deployment metadata changed. |
| `deleted_at` | `TIMESTAMP` | No | Soft-delete timestamp, if used. |

## Status Values

Allowed statuses:

```text
deploying
loading
running
stopped
failed
deleting
deleted
```

Mapping:

| Status | Meaning |
|---|---|
| `deploying` | Kubernetes resources are being created. |
| `loading` | Pod exists but vLLM/model is not ready yet. |
| `running` | Model is ready and serving traffic. |
| `stopped` | Deployment is scaled to zero. |
| `failed` | Deployment failed or pods are crashing. |
| `deleting` | Deletion has been requested. |
| `deleted` | Deployment has been deleted or soft-deleted. |

## Important Notes

- `name` is the API-facing model identifier.
- `model_id` is not unique.
- `UNIQUE(project_id, name)` is required.
- Autoscaling is supported in the MVP. If a deployment uses a shared PVC-backed Hugging Face cache with more than one replica, the cluster storage class must support a compatible shared access mode such as `ReadWriteMany`.
- The OpenAI-compatible request uses the deployment name:

```json
{
  "model": "qwen-small-prod",
  "messages": []
}
```

---

# 5. `api_keys`

## Purpose

Stores project-scoped API keys used to call deployed models.

This table answers:

> Which project is this API key allowed to access?

API keys are used by external applications calling the MiniTen inference API.

## Table Definition

```sql
CREATE TABLE api_keys (
  api_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  -- Human-readable key name shown in the dashboard
  -- Example: "local-dev", "production-app", "staging"
  name TEXT NOT NULL,

  -- Small visible prefix for identifying the key later
  -- Example: "mt_live_abcd"
  key_prefix TEXT NOT NULL,

  -- Hashed full API key. Never store the raw key.
  key_hash TEXT NOT NULL UNIQUE,

  created_by_user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMP,
  revoked_at TIMESTAMP,

  UNIQUE(project_id, name)
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `api_key_id` | `UUID` | Yes | Primary key for the API key. |
| `project_id` | `UUID` | Yes | Project this key grants access to. |
| `name` | `TEXT` | Yes | Human-readable key name. |
| `key_prefix` | `TEXT` | Yes | Visible prefix shown in dashboard. Not secret. |
| `key_hash` | `TEXT` | Yes | Hash of the full key. Raw key is never stored. |
| `created_by_user_id` | `UUID` | Yes | User who created the key. |
| `created_at` | `TIMESTAMP` | Yes | Time the key was created. |
| `last_used_at` | `TIMESTAMP` | No | Last time the key was used. |
| `revoked_at` | `TIMESTAMP` | No | Time the key was revoked. Null means active. |

## Important Notes

- API keys are project-scoped, not account-scoped.
- The raw API key is shown only once.
- Store a keyed hash of the full key, such as HMAC-SHA256 with a server-side secret.
- `key_prefix` is only for display and lookup hints.
- `key_prefix` is not a slug and is not a secret.
- `UNIQUE(project_id, name)` prevents duplicate key names inside the same project.

---

# 6. `inference_requests`

## Purpose

Stores lightweight metadata about inference API requests.

This supports request counts, average latency, error rate, recent request history, last activity timestamps, and dashboard metrics.

This table should not store prompts or model responses.

## Table Definition

```sql
CREATE TABLE inference_requests (
  inference_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  model_deployment_id UUID NOT NULL REFERENCES model_deployments(model_deployment_id) ON DELETE CASCADE,

  -- Nullable so old request records remain if an API key is revoked/deleted later
  api_key_id UUID REFERENCES api_keys(api_key_id) ON DELETE SET NULL,

  status_code INTEGER,
  latency_ms INTEGER,

  error_type TEXT,

  request_path TEXT,
  method TEXT,
  streamed BOOLEAN NOT NULL DEFAULT FALSE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `inference_request_id` | `UUID` | Yes | Primary key for the request log. |
| `project_id` | `UUID` | Yes | Project that owns the request. |
| `model_deployment_id` | `UUID` | Yes | Model deployment that received the request. |
| `api_key_id` | `UUID` | No | API key used for the request. Nullable if key is later deleted. |
| `status_code` | `INTEGER` | No | HTTP response status code. |
| `latency_ms` | `INTEGER` | No | End-to-end gateway latency in milliseconds. |
| `error_type` | `TEXT` | No | Error category, if the request failed. |
| `request_path` | `TEXT` | No | API path called. Example: `/v1/chat/completions`. |
| `method` | `TEXT` | No | HTTP method. Example: `POST`. |
| `streamed` | `BOOLEAN` | Yes | Whether the request used streaming. |
| `created_at` | `TIMESTAMP` | Yes | Time the request was recorded. |

## Important Notes

- Do not store full prompts or responses.
- This table is for metrics and operational visibility.
- `api_key_id` is nullable so historical request records remain even if an API key is revoked or deleted.

---

# 7. `model_events`

## Purpose

Stores structured lifecycle events for model deployments.

This table gives a history of what happened to a deployment over time.

`model_deployments.status` tells you the current state. `model_events` tells you how the deployment got there.

## Table Definition

```sql
CREATE TABLE model_events (
  model_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  model_deployment_id UUID NOT NULL
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  project_id UUID NOT NULL
    REFERENCES projects(project_id)
    ON DELETE CASCADE,

  event_type TEXT NOT NULL CHECK (event_type IN (
    'deploy_requested',
    'k8s_namespace_created',
    'k8s_deployment_created',
    'k8s_service_created',
    'hpa_created',
    'model_loading',
    'model_running',
    'model_stopped',
    'model_started',
    'model_scaled',
    'model_failed',
    'model_deleted'
  )),

  message TEXT,

  metadata JSONB,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `model_event_id` | `UUID` | Yes | Primary key for the event. |
| `model_deployment_id` | `UUID` | Yes | Deployment this event belongs to. |
| `project_id` | `UUID` | Yes | Project that owns the deployment. |
| `event_type` | `TEXT` | Yes | Structured event type. |
| `message` | `TEXT` | No | Human-readable event message. |
| `metadata` | `JSONB` | No | Extra structured data, such as Kubernetes error details. |
| `created_at` | `TIMESTAMP` | Yes | Time the event occurred. |

## Event Types

Allowed event types:

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

## Important Notes

- Use this table for deployment history and structured logs.
- Store Kubernetes error details in `metadata` when useful.
- This table should not replace application logs; it complements them.

---

# 8. `idempotency_keys`

## Purpose

Stores client-provided idempotency keys for control-plane requests.

This table prevents duplicate side effects when clients retry requests because of timeouts, network errors, refreshes, or double-clicks.

This is especially important for operations that create or modify infrastructure, such as:

```text
deploy model
start model
stop model
scale model
delete model
create API key
```

This table answers:

> Has this user already submitted this exact control-plane operation?

Normal inference requests are not idempotent in the MVP. A retry of `/v1/chat/completions` may produce a new model response, so idempotency is mainly for dashboard, CLI, and control-plane operations.

## Table Definition

```sql
CREATE TABLE idempotency_keys (
  idempotency_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  -- Client-provided key from the Idempotency-Key header
  idempotency_key TEXT NOT NULL,

  -- Hash of method + path + normalized body + project/user scope
  request_hash TEXT NOT NULL,

  -- Stored response from the first successful handling of this key
  response_status INTEGER,
  response_body JSONB,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,

  UNIQUE(project_id, user_id, idempotency_key)
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `idempotency_key_id` | `UUID` | Yes | Primary key for the idempotency record. |
| `project_id` | `UUID` | Yes | Project where the request was made. |
| `user_id` | `UUID` | Yes | User who made the request. |
| `idempotency_key` | `TEXT` | Yes | Client-provided key from the `Idempotency-Key` header. |
| `request_hash` | `TEXT` | Yes | Hash of the original request. Used to detect conflicting retries. |
| `response_status` | `INTEGER` | No | HTTP status code returned by the first handled request. |
| `response_body` | `JSONB` | No | Response body returned by the first handled request. |
| `created_at` | `TIMESTAMP` | Yes | Time the idempotency key was created. |
| `expires_at` | `TIMESTAMP` | Yes | Time after which the key can be deleted. |

## Unique Constraint

```sql
UNIQUE(project_id, user_id, idempotency_key)
```

This means a user cannot reuse the same idempotency key for two different operations inside the same project.

The same key string may appear in a different project or for a different user, but within a single project/user scope, it must uniquely identify one logical operation.

## Request Header

Clients provide idempotency keys with this header:

```http
Idempotency-Key: deploy-qwen-small-prod-001
```

Recommended client behavior:

```text
Use a unique key per logical operation.
Reuse the same key when retrying the same operation.
Do not reuse the same key for a different request body.
```

## Request Hash

The `request_hash` should be calculated from stable request data.

Recommended hash input:

```text
HTTP method
request path
normalized JSON body
project_id
user_id
```

Example conceptual input:

```text
POST:/projects/proj_123/models:{"name":"qwen-small-prod","model":"Qwen/Qwen2.5-0.5B-Instruct"}:proj_123:usr_123
```

Hash with SHA-256 or HMAC-SHA256:

```text
request_hash = sha256(method + path + normalized_body + project_id + user_id)
```

HMAC-SHA256 with a server-side secret is stronger, but plain SHA-256 is enough for detecting mismatched retries.

## Behavior

### First Request

When a request arrives with a new idempotency key:

```text
Request arrives
  ↓
No existing idempotency key
  ↓
Create idempotency_keys row
  ↓
Run operation
  ↓
Store response status/body
  ↓
Return response
```

### Retry With Same Request

When a request arrives with the same idempotency key and same request hash:

```text
Request arrives
  ↓
Existing idempotency key found
  ↓
Request hash matches
  ↓
Return stored response
  ↓
Do not run operation again
```

### Retry With Different Request Body

If the same key is reused with a different request hash, return a conflict error.

Recommended status code:

```text
409 Conflict
```

Example response:

```json
{
  "error": {
    "type": "idempotency_key_conflict",
    "message": "This Idempotency-Key was already used with a different request."
  }
}
```

## Example Usage

### Deploy Model

```http
POST /projects/proj_123/models
Idempotency-Key: deploy-qwen-small-prod-001
```

If the user retries this request, MiniTen should not create duplicate Kubernetes resources or duplicate deployment jobs.

### Stop Model

```http
POST /projects/proj_123/models/qwen-small-prod/stop
Idempotency-Key: stop-qwen-small-prod-001
```

If the user retries this request, MiniTen should not enqueue multiple stop jobs unnecessarily.

### Scale Model

```http
POST /projects/proj_123/models/qwen-small-prod/scale
Idempotency-Key: scale-qwen-small-prod-to-3-001
```

Body:

```json
{
  "replicas": 3
}
```

Retries should return the same response instead of repeatedly issuing scale operations.

### Create API Key

```http
POST /projects/proj_123/api-keys
Idempotency-Key: create-local-dev-key-001
```

API key creation has one caveat: the raw API key is usually shown only once.

For the MVP, the simplest behavior is to store and replay the original response during the idempotency window.

## Where This Table Is Used

This table is primarily used by control-plane services:

```text
Model Deployments Service
API Keys Service
Project Service, optional
Project Members Service, optional
```

It is not used for normal inference requests in the MVP.

## Expiration

Idempotency keys should not be stored forever.

MVP recommendation:

```text
Expire after 24 hours.
```

Cleanup query:

```sql
DELETE FROM idempotency_keys
WHERE expires_at < NOW();
```

## Important Notes

- Use idempotency for operations with side effects.
- Do not use idempotency for normal model inference requests in the MVP.
- Store the original response so retries can receive the same result.
- Return `409 Conflict` if the same key is reused with a different request.
- This table works with the real MVP `deployment_jobs` table to prevent duplicate async jobs.


---

# 9. `deployment_jobs`

## Purpose

Stores asynchronous jobs and command history for model lifecycle operations.

This table lets MiniTen return quickly from slow control-plane requests while a background Deployment Worker or Reconciler performs the actual Kubernetes work.

It is also the durable record of deployment commands that were requested, attempted, retried, completed, or failed.

This is useful because model operations can take a long time:

```text
deploy model
start model
stop model
scale model
delete model
sync status
```

This table answers:

> What model lifecycle work needs to be processed, retried, inspected, or reviewed later?

The `deployment_jobs` table is for control-plane operations only. Normal inference requests, such as `/v1/chat/completions`, should not use this queue in the MVP.

## Table Definition

```sql
CREATE TABLE deployment_jobs (
  deployment_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  model_deployment_id UUID
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  job_type TEXT NOT NULL CHECK (job_type IN (
    'deploy_model',
    'start_model',
    'stop_model',
    'scale_model',
    'delete_model',
    'sync_status'
  )),

  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued',
    'running',
    'succeeded',
    'failed',
    'retrying'
  )),

  payload JSONB NOT NULL DEFAULT '{}',

  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,

  locked_by TEXT,
  locked_at TIMESTAMP,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Columns

| Column | Type | Required | Purpose |
|---|---:|---:|---|
| `deployment_job_id` | `UUID` | Yes | Primary key for the deployment job. |
| `project_id` | `UUID` | Yes | Project that owns the job. |
| `model_deployment_id` | `UUID` | No | Model deployment this job targets. Nullable for jobs that may run before a deployment row fully exists. |
| `job_type` | `TEXT` | Yes | Type of lifecycle operation to perform. |
| `status` | `TEXT` | Yes | Current job status. |
| `payload` | `JSONB` | Yes | Operation-specific details needed by the worker. |
| `attempts` | `INTEGER` | Yes | Number of times the worker has attempted the job. |
| `max_attempts` | `INTEGER` | Yes | Maximum attempts before marking the job failed. |
| `last_error` | `TEXT` | No | Most recent error message if the job failed or is retrying. |
| `locked_by` | `TEXT` | No | Worker instance currently processing the job. |
| `locked_at` | `TIMESTAMP` | No | Time the job was locked by a worker. |
| `created_at` | `TIMESTAMP` | Yes | Time the job was created. |
| `updated_at` | `TIMESTAMP` | Yes | Last time the job was updated. |

## Job Types

Allowed job types:

```text
deploy_model
start_model
stop_model
scale_model
delete_model
sync_status
```

| Job Type | Meaning |
|---|---|
| `deploy_model` | Create Kubernetes resources for a model deployment. |
| `start_model` | Scale a stopped deployment back up. |
| `stop_model` | Scale a deployment down to zero. |
| `scale_model` | Change replica count or autoscaling settings. |
| `delete_model` | Delete Kubernetes resources and mark the deployment deleted. |
| `sync_status` | Reconcile MiniTen metadata with live Kubernetes state. |

## Status Values

Allowed statuses:

```text
queued
running
succeeded
failed
retrying
```

| Status | Meaning |
|---|---|
| `queued` | Job is waiting to be picked up by a worker. |
| `running` | A worker has locked and is processing the job. |
| `succeeded` | Job completed successfully. |
| `failed` | Job failed and has no attempts remaining. |
| `retrying` | Job failed but can be retried. |

## How It Works

### Create Job

When a user requests a lifecycle operation, the API writes metadata and inserts a job.

```text
User requests deploy/start/stop/scale/delete
  ↓
Model Deployments Service validates permissions
  ↓
Model Deployments Service writes or updates model_deployments
  ↓
Model Deployments Service inserts deployment_jobs row
  ↓
API returns quickly with queued/running status
```

### Process Job

A background Deployment Worker polls for queued jobs.

```text
Deployment Worker polls deployment_jobs
  ↓
Worker locks one queued job
  ↓
Worker marks job running
  ↓
Worker calls OKE / Kubernetes API
  ↓
Worker updates model_deployments status
  ↓
Worker writes model_events
  ↓
Worker marks job succeeded or failed
```

## Example Job Payloads

### Deploy Model

```json
{
  "deployment_name": "qwen-small-prod",
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "k8s_namespace": "miniten-personal",
  "k8s_deployment_name": "qwen-small-prod-v1",
  "k8s_service_name": "qwen-small-prod",
  "replicas": 1,
  "resources": {
    "cpu_request": "2",
    "cpu_limit": "4",
    "memory_request": "8Gi",
    "memory_limit": "16Gi",
    "gpu_count": 0
  },
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 3,
    "target_cpu_utilization": 70
  },
  "vllm": {
    "image": "vllm/vllm-openai:latest",
    "dtype": "auto",
    "max_model_len": 4096
  }
}
```

### Stop Model

```json
{
  "deployment_name": "qwen-small-prod",
  "k8s_namespace": "miniten-personal",
  "k8s_deployment_name": "qwen-small-prod-v1",
  "replicas": 0
}
```

### Scale Model

```json
{
  "deployment_name": "qwen-small-prod",
  "k8s_namespace": "miniten-personal",
  "k8s_deployment_name": "qwen-small-prod-v1",
  "replicas": 3
}
```

## Locking Pattern

Workers should lock jobs atomically so multiple workers do not process the same job.

Example query:

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

This allows multiple Deployment Worker instances to safely poll the same table.

## Retry Behavior

If a job fails:

```text
attempts < max_attempts
  → mark status = retrying
  → increment attempts
  → store last_error

attempts >= max_attempts
  → mark status = failed
  → store last_error
  → update model_deployments.status if needed
```

## Relationship to Idempotency

`deployment_jobs` should work together with `idempotency_keys`.

Recommended deploy flow:

```text
Client sends deploy request with Idempotency-Key
  ↓
Model Deployments Service checks idempotency_keys
  ↓
If new, creates model_deployments row
  ↓
Creates deployment_jobs row
  ↓
Stores response in idempotency_keys
  ↓
Deployment Worker processes the job asynchronously
```

This prevents duplicate jobs if the client retries the same request.

## Relationship to Model Events

`deployment_jobs` tracks work to be done.

`model_events` tracks what happened.

Example:

```text
deployment_jobs:
  deploy_model queued/running/succeeded

model_events:
  deploy_requested
  k8s_deployment_created
  k8s_service_created
  model_loading
  model_running
```

## Important Notes

- This is a durable Postgres-backed queue.
- It avoids needing Kafka or Redis for the MVP.
- It should only be used for control-plane operations.
- It should not be used for synchronous chat/inference requests.
- A Deployment Worker or Reconciler should be responsible for consuming this table.


---

# Indexes

Add indexes for common lookup paths.

```sql
CREATE INDEX idx_project_members_user_id
ON project_members(user_id);

CREATE INDEX idx_project_members_project_id
ON project_members(project_id);

CREATE INDEX idx_model_deployments_project_id
ON model_deployments(project_id);

CREATE INDEX idx_model_deployments_model_id
ON model_deployments(model_id);

CREATE INDEX idx_model_deployments_status
ON model_deployments(status);

CREATE INDEX idx_api_keys_project_id
ON api_keys(project_id);

CREATE INDEX idx_api_keys_key_prefix
ON api_keys(key_prefix);

CREATE INDEX idx_inference_requests_project_id
ON inference_requests(project_id);

CREATE INDEX idx_inference_requests_model_deployment_id
ON inference_requests(model_deployment_id);

CREATE INDEX idx_inference_requests_created_at
ON inference_requests(created_at);

CREATE INDEX idx_model_events_model_deployment_id
ON model_events(model_deployment_id);

CREATE INDEX idx_model_events_project_id
ON model_events(project_id);

CREATE INDEX idx_model_events_created_at
ON model_events(created_at);

CREATE INDEX idx_idempotency_keys_project_user
ON idempotency_keys(project_id, user_id);

CREATE INDEX idx_idempotency_keys_expires_at
ON idempotency_keys(expires_at);

CREATE INDEX idx_deployment_jobs_status
ON deployment_jobs(status);

CREATE INDEX idx_deployment_jobs_project_id
ON deployment_jobs(project_id);

CREATE INDEX idx_deployment_jobs_model_deployment_id
ON deployment_jobs(model_deployment_id);

CREATE INDEX idx_deployment_jobs_locked_at
ON deployment_jobs(locked_at);

CREATE INDEX idx_deployment_jobs_created_at
ON deployment_jobs(created_at);
```

---

# Key Query Patterns

## Find projects for a user

```sql
SELECT p.*
FROM projects p
JOIN project_members pm ON pm.project_id = p.project_id
WHERE pm.user_id = $1;
```

## Find users in a project

```sql
SELECT u.*, pm.role
FROM users u
JOIN project_members pm ON pm.user_id = u.user_id
WHERE pm.project_id = $1;
```

## List model deployments in a project

```sql
SELECT *
FROM model_deployments
WHERE project_id = $1
ORDER BY created_at DESC;
```

## Resolve an inference request

Given an API key and requested model name:

```text
API key -> project_id
request.body.model -> deployment name
project_id + deployment name -> model_deployments row
```

SQL after API key validation:

```sql
SELECT *
FROM model_deployments
WHERE project_id = $1
  AND name = $2;
```

## Get recent requests for a model

```sql
SELECT *
FROM inference_requests
WHERE model_deployment_id = $1
ORDER BY created_at DESC
LIMIT 100;
```

## Get model event history

```sql
SELECT *
FROM model_events
WHERE model_deployment_id = $1
ORDER BY created_at DESC;
```


## Check idempotency key

```sql
SELECT *
FROM idempotency_keys
WHERE project_id = $1
  AND user_id = $2
  AND idempotency_key = $3;
```

## Cleanup expired idempotency keys

```sql
DELETE FROM idempotency_keys
WHERE expires_at < NOW();
```



## Find queued deployment jobs

```sql
SELECT *
FROM deployment_jobs
WHERE status IN ('queued', 'retrying')
ORDER BY created_at ASC
LIMIT 100;
```

## Find jobs for a deployment

```sql
SELECT *
FROM deployment_jobs
WHERE model_deployment_id = $1
ORDER BY created_at DESC;
```

## Find failed jobs

```sql
SELECT *
FROM deployment_jobs
WHERE status = 'failed'
ORDER BY updated_at DESC;
```


---

# Full Schema

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  email TEXT NOT NULL UNIQUE,
  hashed_password TEXT NOT NULL,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP
);

CREATE TABLE projects (
  project_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  k8s_namespace TEXT NOT NULL UNIQUE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_members (
  project_member_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  role TEXT NOT NULL DEFAULT 'member'
    CHECK (role IN ('owner', 'member', 'viewer')),

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  UNIQUE(project_id, user_id)
);

CREATE TABLE model_deployments (
  model_deployment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  name TEXT NOT NULL,
  model_id TEXT NOT NULL,

  status TEXT NOT NULL DEFAULT 'deploying'
    CHECK (status IN (
      'deploying',
      'loading',
      'running',
      'stopped',
      'failed',
      'deleting',
      'deleted'
    )),

  k8s_namespace TEXT NOT NULL,
  k8s_deployment_name TEXT NOT NULL,
  k8s_service_name TEXT NOT NULL,
  k8s_hpa_name TEXT,

  replicas INTEGER NOT NULL DEFAULT 1,

  cpu_request TEXT,
  cpu_limit TEXT,
  memory_request TEXT,
  memory_limit TEXT,
  gpu_count INTEGER NOT NULL DEFAULT 0,

  vllm_image TEXT NOT NULL DEFAULT 'vllm/vllm-openai:latest',
  vllm_dtype TEXT NOT NULL DEFAULT 'auto',
  vllm_max_model_len INTEGER NOT NULL DEFAULT 4096,

  autoscaling_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  min_replicas INTEGER,
  max_replicas INTEGER,
  target_cpu_utilization INTEGER,

  created_by_user_id UUID NOT NULL REFERENCES users(user_id),

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at TIMESTAMP,

  UNIQUE(project_id, name)
);

CREATE TABLE api_keys (
  api_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,

  created_by_user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMP,
  revoked_at TIMESTAMP,

  UNIQUE(project_id, name)
);

CREATE TABLE inference_requests (
  inference_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  model_deployment_id UUID NOT NULL REFERENCES model_deployments(model_deployment_id) ON DELETE CASCADE,

  api_key_id UUID REFERENCES api_keys(api_key_id) ON DELETE SET NULL,

  status_code INTEGER,
  latency_ms INTEGER,

  error_type TEXT,

  request_path TEXT,
  method TEXT,
  streamed BOOLEAN NOT NULL DEFAULT FALSE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE model_events (
  model_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  model_deployment_id UUID NOT NULL
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  project_id UUID NOT NULL
    REFERENCES projects(project_id)
    ON DELETE CASCADE,

  event_type TEXT NOT NULL CHECK (event_type IN (
    'deploy_requested',
    'k8s_namespace_created',
    'k8s_deployment_created',
    'k8s_service_created',
    'hpa_created',
    'model_loading',
    'model_running',
    'model_stopped',
    'model_started',
    'model_scaled',
    'model_failed',
    'model_deleted'
  )),

  message TEXT,

  metadata JSONB,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE idempotency_keys (
  idempotency_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,

  response_status INTEGER,
  response_body JSONB,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,

  UNIQUE(project_id, user_id, idempotency_key)
);

CREATE TABLE deployment_jobs (
  deployment_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  model_deployment_id UUID
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  job_type TEXT NOT NULL CHECK (job_type IN (
    'deploy_model',
    'start_model',
    'stop_model',
    'scale_model',
    'delete_model',
    'sync_status'
  )),

  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued',
    'running',
    'succeeded',
    'failed',
    'retrying'
  )),

  payload JSONB NOT NULL DEFAULT '{}',

  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,

  locked_by TEXT,
  locked_at TIMESTAMP,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


```

---

## `deployment_versions`

Use later for real versioning, rollback, and promotion.

## `organizations`

Use later if projects need to belong to higher-level organizations or teams.

## `audit_logs`

Use later for security/compliance-grade audit history across all resources.

## `billing_usage`

Use later for quotas, metering, invoices, or usage-based pricing.

## `rate_limits`

Use later for per-project or per-key request limits.

## `model_cache_entries`

Use later if MiniTen caches model weights in OCI Object Storage or persistent volumes.

---

# Final Notes

This schema supports the MiniTen MVP features:

- multi-user authentication
- project membership
- project-scoped API keys
- named model deployments
- Hugging Face model IDs
- Kubernetes resource metadata
- start/stop/scale lifecycle state
- HPA autoscaling configuration
- inference request tracking
- deployment event history
- dashboard metrics
- idempotent control-plane operations
- asynchronous deployment jobs
- OpenAI-compatible request routing

The key product rule is:

> Users call models by their project-local deployment name. The Hugging Face model ID is stored as metadata and passed to vLLM at deployment time.
