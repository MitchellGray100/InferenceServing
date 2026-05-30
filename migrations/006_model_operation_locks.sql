-- Add fenced per-model operation leases for horizontally scalable deployment workers.

CREATE TABLE IF NOT EXISTS model_operation_locks (
  model_deployment_id UUID PRIMARY KEY
    REFERENCES model_deployments(model_deployment_id)
    ON DELETE CASCADE,

  deployment_job_id UUID NOT NULL
    REFERENCES deployment_jobs(deployment_job_id)
    ON DELETE CASCADE,

  locked_by TEXT NOT NULL,
  lease_token UUID NOT NULL DEFAULT gen_random_uuid(),

  locked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_expires_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_operation_locks_deployment_job_id
ON model_operation_locks(deployment_job_id);

CREATE INDEX IF NOT EXISTS idx_model_operation_locks_lease_expires_at
ON model_operation_locks(lease_expires_at);
