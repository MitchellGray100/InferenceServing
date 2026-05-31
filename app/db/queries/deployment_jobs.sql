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
-- Atomically claim the oldest available job and acquire its per-model lease.
-- SKIP LOCKED lets multiple workers poll concurrently without claiming the
-- same row, while model_operation_locks serializes jobs for one deployment.
WITH expired_locks AS (
  DELETE FROM model_operation_locks
  WHERE lease_expires_at < CURRENT_TIMESTAMP
  RETURNING model_deployment_id
),
candidate AS (
  SELECT deployment_job_id, model_deployment_id
  FROM deployment_jobs AS job
  WHERE (
      status IN ('queued', 'retrying')
      OR (
        status = 'running'
        AND locked_at < CURRENT_TIMESTAMP - (%(lease_seconds)s * INTERVAL '1 second')
      )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM model_operation_locks AS operation_lock
      WHERE operation_lock.model_deployment_id = job.model_deployment_id
        AND operation_lock.lease_expires_at >= CURRENT_TIMESTAMP
    )
  ORDER BY created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
),
attempt_lock AS (
  INSERT INTO model_operation_locks (
    model_deployment_id,
    deployment_job_id,
    locked_by,
    locked_at,
    heartbeat_at,
    lease_expires_at
  )
  SELECT
    model_deployment_id,
    deployment_job_id,
    %(locked_by)s,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP + (%(lease_seconds)s * INTERVAL '1 second')
  FROM candidate
  WHERE model_deployment_id IS NOT NULL
  ON CONFLICT (model_deployment_id) DO NOTHING
  RETURNING deployment_job_id, lease_token
),
claimed AS (
  UPDATE deployment_jobs
  SET
    status = 'running',
    locked_by = %(locked_by)s,
    locked_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
  WHERE deployment_job_id = (SELECT deployment_job_id FROM candidate)
    AND (
      (SELECT model_deployment_id FROM candidate) IS NULL
      OR EXISTS (SELECT 1 FROM attempt_lock)
    )
  RETURNING *
)
SELECT
  claimed.*,
  attempt_lock.lease_token AS model_operation_lease_token
FROM claimed
LEFT JOIN attempt_lock
  ON attempt_lock.deployment_job_id = claimed.deployment_job_id;

-- name: heartbeat_model_operation_lease
-- Extend a worker's fenced model lease and refresh the job lock timestamp.
WITH refreshed_lock AS (
  UPDATE model_operation_locks
  SET
    heartbeat_at = CURRENT_TIMESTAMP,
    lease_expires_at = CURRENT_TIMESTAMP + (%(lease_seconds)s * INTERVAL '1 second')
  WHERE model_deployment_id = %(model_deployment_id)s
    AND deployment_job_id = %(deployment_job_id)s
    AND lease_token = %(lease_token)s
    AND lease_expires_at >= CURRENT_TIMESTAMP
  RETURNING *
),
refreshed_job AS (
  UPDATE deployment_jobs
  SET
    locked_by = %(locked_by)s,
    locked_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
  WHERE deployment_job_id = %(deployment_job_id)s
    AND EXISTS (SELECT 1 FROM refreshed_lock)
  RETURNING *
)
SELECT refreshed_lock.*
FROM refreshed_lock
WHERE EXISTS (SELECT 1 FROM refreshed_job);

-- name: verify_model_operation_lease
-- Confirm the worker still owns the current fenced lease before final writes.
SELECT *
FROM model_operation_locks
WHERE model_deployment_id = %(model_deployment_id)s
  AND deployment_job_id = %(deployment_job_id)s
  AND lease_token = %(lease_token)s
  AND lease_expires_at >= CURRENT_TIMESTAMP;

-- name: release_model_operation_lease
-- Release the per-model lease after the owning worker has recorded the result.
DELETE FROM model_operation_locks
WHERE model_deployment_id = %(model_deployment_id)s
  AND deployment_job_id = %(deployment_job_id)s
  AND lease_token = %(lease_token)s
RETURNING *;

-- name: release_expired_model_operation_locks
DELETE FROM model_operation_locks
WHERE lease_expires_at < CURRENT_TIMESTAMP
RETURNING *;

-- name: release_model_operation_locks_for_model
-- Force-release a model lease before a preemptive delete/hard-restart job.
-- The old worker will fail its next lease assertion and must not write final
-- state for the preempted job.
DELETE FROM model_operation_locks
WHERE model_deployment_id = %(model_deployment_id)s
RETURNING *;

-- name: reset_expired_running_deployment_jobs
-- Make abandoned running jobs eligible again when their worker lock has aged out.
UPDATE deployment_jobs
SET
  status = 'retrying',
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE status = 'running'
  AND locked_at < CURRENT_TIMESTAMP - (%(lease_seconds)s * INTERVAL '1 second')
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

-- name: preempt_deployment_jobs_for_model
-- Skip all unfinished jobs for a model so a destructive command can run next.
-- This includes the currently running job; force-releasing the model operation
-- lock prevents that worker from recording a stale success/failure later.
UPDATE deployment_jobs
SET
  status = 'skipped',
  last_error = %(last_error)s,
  locked_by = NULL,
  locked_at = NULL,
  updated_at = CURRENT_TIMESTAMP
WHERE model_deployment_id = %(model_deployment_id)s
  AND status IN ('queued', 'running', 'retrying')
RETURNING *;

-- name: list_deployment_jobs_for_model
-- Fetch command history for a model deployment, newest first.
SELECT *
FROM deployment_jobs
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC;

-- name: list_recent_deployment_jobs_for_model
-- Fetch the most recent command history rows for a compact status view.
SELECT *
FROM deployment_jobs
WHERE model_deployment_id = %(model_deployment_id)s
ORDER BY created_at DESC
LIMIT %(limit)s;
