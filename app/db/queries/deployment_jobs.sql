-- Deployment job queries.
--
-- Expected scope:
-- - enqueue lifecycle jobs
-- - atomically claim queued/retrying jobs with FOR UPDATE SKIP LOCKED
-- - record attempts, errors, and final job status

-- name: create_deployment_job
-- Enqueue a durable deployment command. This table is both the worker queue
-- and the historical record of requested lifecycle operations.
INSERT INTO deployment_jobs (
  project_id,
  model_deployment_id,
  job_type,
  desired_generation,
  payload
)
VALUES (
  %(project_id)s,
  %(model_deployment_id)s,
  %(job_type)s,
  %(desired_generation)s,
  %(payload)s
)
RETURNING *;

-- name: claim_next_deployment_job
-- Atomically claim the oldest queued/retrying job. SKIP LOCKED lets multiple
-- workers poll concurrently without claiming the same row.
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
-- Mark a job complete and clear lock/error fields after Kubernetes work
-- succeeds.
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
-- Record a failed attempt while leaving the job eligible for a future worker
-- retry.
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
-- Record a terminal failure once retry attempts are exhausted.
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

-- name: mark_deployment_job_skipped
-- Mark stale jobs as skipped when a newer desired_generation exists for the
-- same model deployment.
UPDATE deployment_jobs
SET
  status = 'skipped',
  last_error = NULL,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE deployment_job_id = %(deployment_job_id)s
RETURNING *;

-- name: list_deployment_jobs_for_model
-- Fetch command history for a model deployment, newest first.
SELECT *
FROM deployment_jobs
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC;
