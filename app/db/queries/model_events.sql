-- Model lifecycle event queries.
--
-- Expected scope:
-- - insert structured deployment lifecycle events
-- - fetch event history for model detail and analytics pages

-- name: create_model_event
-- Insert a structured lifecycle event for model deployment history and
-- debugging. Metadata is JSONB so workers can attach job-specific context.
INSERT INTO model_events (
  model_deployment_id,
  project_id,
  event_type,
  message,
  metadata
)
VALUES (
  %(model_deployment_id)s,
  %(project_id)s,
  %(event_type)s,
  %(message)s,
  %(metadata)s
)
RETURNING *;

-- name: list_model_events
-- Fetch model lifecycle history, newest first, for detail/analytics views.
SELECT *
FROM model_events
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC;
