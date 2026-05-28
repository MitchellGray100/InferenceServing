-- Model deployment queries.
--
-- Expected scope:
-- - create named deployments
-- - resolve project_id + deployment name
-- - update desired lifecycle/configuration state
-- - list non-deleted deployments for dashboards and /v1/models

-- name: create_model_deployment
-- Insert desired deployment metadata. Kubernetes resources are created later
-- by a deployment_jobs worker, not by the API request transaction.
INSERT INTO model_deployments (
  project_id,
  name,
  model_id,
  status,
  k8s_namespace,
  k8s_deployment_name,
  k8s_service_name,
  k8s_hpa_name,
  replicas,
  cpu_request,
  cpu_limit,
  memory_request,
  memory_limit,
  gpu_count,
  vllm_image,
  vllm_dtype,
  vllm_max_model_len,
  autoscaling_enabled,
  min_replicas,
  max_replicas,
  target_cpu_utilization,
  created_by_user_id
)
VALUES (
  %(project_id)s,
  %(name)s,
  %(model_id)s,
  %(status)s,
  %(k8s_namespace)s,
  %(k8s_deployment_name)s,
  %(k8s_service_name)s,
  %(k8s_hpa_name)s,
  %(replicas)s,
  %(cpu_request)s,
  %(cpu_limit)s,
  %(memory_request)s,
  %(memory_limit)s,
  %(gpu_count)s,
  %(vllm_image)s,
  %(vllm_dtype)s,
  %(vllm_max_model_len)s,
  %(autoscaling_enabled)s,
  %(min_replicas)s,
  %(max_replicas)s,
  %(target_cpu_utilization)s,
  %(created_by_user_id)s
)
RETURNING *;

-- name: list_model_deployments
-- Dashboard/control-plane list view. Soft-deleted rows stay hidden while their
-- history remains available through deployment_jobs/model_events later.
SELECT *
FROM model_deployments
WHERE project_id = %(project_id)s
  AND deleted_at IS NULL
ORDER BY created_at DESC;

-- name: get_model_deployment_by_name
-- Name-based lookup is kept for inference routing, where OpenAI-compatible
-- requests send `model` as the project-local deployment name.
SELECT *
FROM model_deployments
WHERE project_id = %(project_id)s
  AND name = %(name)s
  AND deleted_at IS NULL;

-- name: list_running_model_deployments_for_project
-- OpenAI-compatible /v1/models should only expose deployments that can receive
-- inference traffic.
SELECT *
FROM model_deployments
WHERE project_id = %(project_id)s
  AND status = 'running'
  AND deleted_at IS NULL
ORDER BY name ASC;

-- name: get_model_deployment_by_id
-- Control-plane commands use immutable UUIDs so a future rename feature would
-- not accidentally target the wrong deployment.
SELECT *
FROM model_deployments
WHERE project_id = %(project_id)s
  AND model_deployment_id = %(model_deployment_id)s
  AND deleted_at IS NULL;

-- name: update_model_deployment_status
-- Status changes here represent requested/control-plane state. The worker will
-- later reconcile this with Kubernetes readiness and failures.
UPDATE model_deployments
SET
  status = %(status)s,
  updated_at = CURRENT_TIMESTAMP
WHERE model_deployment_id = %(model_deployment_id)s
RETURNING *;

-- name: update_model_deployment_replicas
-- Store desired fixed replica count before queueing scale work. The worker is
-- responsible for applying this to the Deployment/HPA.
UPDATE model_deployments
SET
  replicas = %(replicas)s,
  updated_at = CURRENT_TIMESTAMP
WHERE model_deployment_id = %(model_deployment_id)s
RETURNING *;

-- name: mark_model_deployment_deleted
-- Worker-facing hard completion marker after Kubernetes resources are removed.
UPDATE model_deployments
SET
  status = 'deleted',
  deleted_at = CURRENT_TIMESTAMP,
  updated_at = CURRENT_TIMESTAMP
WHERE model_deployment_id = %(model_deployment_id)s
RETURNING *;
