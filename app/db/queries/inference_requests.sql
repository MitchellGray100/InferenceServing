-- Inference request analytics queries.
--
-- Expected scope:
-- - insert lightweight request metadata
-- - aggregate request counts, error counts, and latency
-- - fetch recent request history without prompts or responses

-- name: create_inference_request
-- Insert lightweight request metadata for analytics. Prompts and generated
-- responses are intentionally not stored.
INSERT INTO inference_requests (
  project_id,
  model_deployment_id,
  api_key_id,
  status_code,
  latency_ms,
  error_type,
  request_path,
  method,
  streamed
)
VALUES (
  %(project_id)s,
  %(model_deployment_id)s,
  %(api_key_id)s,
  %(status_code)s,
  %(latency_ms)s,
  %(error_type)s,
  %(request_path)s,
  %(method)s,
  %(streamed)s
)
RETURNING *;

-- name: list_recent_inference_requests
-- Return recent request records for model request history views.
SELECT *
FROM inference_requests
WHERE model_deployment_id = %(model_deployment_id)s
  AND (%(status_code)s::integer IS NULL OR status_code = %(status_code)s::integer)
  AND (%(since)s::timestamp IS NULL OR created_at >= %(since)s::timestamp)
ORDER BY created_at DESC
LIMIT %(limit)s;

-- name: get_model_inference_metrics
-- Aggregate basic request counts and latency for one model. Optional `since`
-- bounds the time window when analytics callers provide it.
SELECT
  COUNT(*) AS request_count,
  COUNT(*) FILTER (WHERE status_code >= 200 AND status_code < 400) AS success_count,
  COUNT(*) FILTER (WHERE status_code >= 400) AS error_count,
  AVG(latency_ms)::INTEGER AS average_latency_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)::INTEGER AS p95_latency_ms,
  MAX(created_at) AS last_request_at
FROM inference_requests
WHERE model_deployment_id = %(model_deployment_id)s
  AND (%(since)s::timestamp IS NULL OR created_at >= %(since)s::timestamp);

-- name: get_project_analytics_overview
-- Return one row per non-deleted model plus request aggregates so the service
-- can derive project summary totals and dashboard model cards.
SELECT
  md.model_deployment_id,
  md.name,
  md.model_id,
  md.status,
  COUNT(ir.inference_request_id) AS request_count,
  COUNT(ir.inference_request_id) FILTER (WHERE ir.status_code >= 400) AS error_count,
  AVG(ir.latency_ms)::INTEGER AS average_latency_ms,
  MAX(ir.created_at) AS last_request_at
FROM model_deployments md
LEFT JOIN inference_requests ir
  ON ir.model_deployment_id = md.model_deployment_id
WHERE md.project_id = %(project_id)s
  AND md.deleted_at IS NULL
GROUP BY md.model_deployment_id
ORDER BY md.created_at DESC;
