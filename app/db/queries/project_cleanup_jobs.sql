-- Project cleanup job queries.
--
-- Expected scope:
-- - queue namespace cleanup after project/account deletion
-- - atomically claim cleanup jobs with FOR UPDATE SKIP LOCKED
-- - record retries and terminal status

-- name: create_project_cleanup_job
INSERT INTO project_cleanup_jobs (
  project_id,
  k8s_namespace
)
VALUES (
  %(project_id)s,
  %(k8s_namespace)s
)
RETURNING *;

-- name: claim_next_project_cleanup_job
WITH candidate AS (
  SELECT project_cleanup_job_id
  FROM project_cleanup_jobs
  WHERE (
      status IN ('queued', 'retrying')
      OR (
        status = 'running'
        AND locked_at < CURRENT_TIMESTAMP - (%(lease_seconds)s * INTERVAL '1 second')
      )
    )
  ORDER BY created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
),
claimed AS (
  UPDATE project_cleanup_jobs
  SET
    status = 'running',
    locked_by = %(locked_by)s,
    locked_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
  WHERE project_cleanup_job_id = (SELECT project_cleanup_job_id FROM candidate)
  RETURNING *
)
SELECT *
FROM claimed;

-- name: mark_project_cleanup_job_succeeded
UPDATE project_cleanup_jobs
SET
  status = 'succeeded',
  last_error = NULL,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE project_cleanup_job_id = %(project_cleanup_job_id)s
RETURNING *;

-- name: mark_project_cleanup_job_retrying
UPDATE project_cleanup_jobs
SET
  status = 'retrying',
  attempts = attempts + 1,
  last_error = %(last_error)s,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE project_cleanup_job_id = %(project_cleanup_job_id)s
RETURNING *;

-- name: mark_project_cleanup_job_failed
UPDATE project_cleanup_jobs
SET
  status = 'failed',
  attempts = attempts + 1,
  last_error = %(last_error)s,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE project_cleanup_job_id = %(project_cleanup_job_id)s
RETURNING *;
