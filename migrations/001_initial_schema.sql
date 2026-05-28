-- Initial MiniTen database schema.
--
-- Product metadata lives in Postgres. Model weights live in Hugging Face and
-- Kubernetes PVC caches. Live infrastructure state remains owned by Kubernetes.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- User accounts. Project access is represented through project_members.
CREATE TABLE users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  email TEXT NOT NULL UNIQUE,
  hashed_password TEXT NOT NULL,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP
);

-- Projects are the main product isolation boundary and map to Kubernetes
-- namespaces.
CREATE TABLE projects (
  project_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  k8s_namespace TEXT NOT NULL UNIQUE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Many-to-many relationship between users and projects, including project role.
CREATE TABLE project_members (
  project_member_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  role TEXT NOT NULL DEFAULT 'member'
    CHECK (role IN ('owner', 'member', 'viewer')),

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  UNIQUE(project_id, user_id)
);

-- User-facing model deployments. The `name` column is the project-local model
-- identifier used in dashboard actions and OpenAI-compatible requests.
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
  desired_generation INTEGER NOT NULL DEFAULT 1,

  cpu_request TEXT,
  cpu_limit TEXT,
  memory_request TEXT,
  memory_limit TEXT,
  gpu_count INTEGER NOT NULL DEFAULT 0,

  vllm_image TEXT NOT NULL DEFAULT 'vllm/vllm-openai-cpu:latest-x86_64',
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

-- Project-scoped inference API keys. Raw key values are returned only once by
-- the API and only a keyed hash is stored here.
CREATE TABLE api_keys (
  api_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL,

  created_by_user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMP,
  revoked_at TIMESTAMP,

  CONSTRAINT uq_api_keys_key_hash UNIQUE(key_hash),
  CONSTRAINT uq_api_keys_project_name UNIQUE(project_id, name)
);

-- Lightweight inference request metadata for analytics and debugging. Prompts
-- and model responses are intentionally not stored.
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

-- Structured lifecycle history for model deployments.
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
    'model_updated',
    'model_scaled',
    'model_status_synced',
    'model_failed',
    'model_deleted'
  )),

  message TEXT,

  metadata JSONB,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Client-provided idempotency keys for retry-safe control-plane operations.
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

-- Durable queue and command history for model lifecycle work.
CREATE TABLE deployment_jobs (
  deployment_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,

  model_deployment_id UUID
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  job_type TEXT NOT NULL CHECK (job_type IN (
    'deploy_model',
    'update_model',
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
    'retrying',
    'skipped'
  )),

  desired_generation INTEGER NOT NULL DEFAULT 1,
  payload JSONB NOT NULL DEFAULT '{}',

  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,

  locked_by TEXT,
  locked_at TIMESTAMP,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common authorization, lookup, analytics, and worker polling paths.
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
