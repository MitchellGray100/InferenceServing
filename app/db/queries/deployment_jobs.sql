-- Deployment job queries.
--
-- Expected scope:
-- - enqueue lifecycle jobs
-- - atomically claim queued/retrying jobs with FOR UPDATE SKIP LOCKED
-- - record attempts, errors, and final job status

-- name: create_deployment_job
INSERT INTO deployment_jobs (
  project_id,
  model_deployment_id,
  job_type,
  payload
)
VALUES (
  %(project_id)s,
  %(model_deployment_id)s,
  %(job_type)s,
  %(payload)s
)
RETURNING *;

-- name: claim_next_deployment_job
WITH next_job AS (
  SELECT deployment_job_id
  FROM deployment_jobs
  WHERE status IN ('queued', 'retrying')
    AND (locked_at IS NULL OR locked_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes')
  ORDER BY created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE deployment_jobs
SET
  status = 'running',
  locked_by = %(locked_by)s,
  locked_at = CURRENT_TIMESTAMP,
  updated_at = CURRENT_TIMESTAMP
WHERE deployment_job_id = (SELECT deployment_job_id FROM next_job)
RETURNING *;

-- name: mark_deployment_job_succeeded
UPDATE deployment_jobs
SET
  status = 'succeeded',
  last_error = NULL,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE deployment_job_id = %(deployment_job_id)s
RETURNING *;

-- name: mark_deployment_job_retrying
UPDATE deployment_jobs
SET
  status = 'retrying',
  attempts = attempts + 1,
  last_error = %(last_error)s,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE deployment_job_id = %(deployment_job_id)s
RETURNING *;

-- name: mark_deployment_job_failed
UPDATE deployment_jobs
SET
  status = 'failed',
  attempts = attempts + 1,
  last_error = %(last_error)s,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE deployment_job_id = %(deployment_job_id)s
RETURNING *;

-- name: list_deployment_jobs_for_model
SELECT *
FROM deployment_jobs
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC;
