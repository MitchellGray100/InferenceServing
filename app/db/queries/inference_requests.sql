-- Inference request analytics queries.
--
-- Expected scope:
-- - insert lightweight request metadata
-- - aggregate request counts, error counts, and latency
-- - fetch recent request history without prompts or responses

-- name: create_inference_request
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
SELECT *
FROM inference_requests
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC
LIMIT %(limit)s;

-- name: get_model_inference_metrics
SELECT
  COUNT(*) AS request_count,
  COUNT(*) FILTER (WHERE status_code >= 200 AND status_code < 400) AS success_count,
  COUNT(*) FILTER (WHERE status_code >= 400) AS error_count,
  AVG(latency_ms)::INTEGER AS average_latency_ms,
  MAX(created_at) AS last_request_at
FROM inference_requests
WHERE model_deployment_id = %(model_deployment_id)s
  AND (%(since)s::timestamp IS NULL OR created_at >= %(since)s::timestamp);
