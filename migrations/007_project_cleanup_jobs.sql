-- Queue project namespace cleanup independently from project metadata.
CREATE TABLE IF NOT EXISTS project_cleanup_jobs (
  project_cleanup_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  project_id UUID NOT NULL,
  k8s_namespace TEXT NOT NULL,

  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued',
    'running',
    'succeeded',
    'failed',
    'retrying'
  )),

  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,

  locked_by TEXT,
  locked_at TIMESTAMP,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_project_cleanup_jobs_status
ON project_cleanup_jobs(status);

CREATE INDEX IF NOT EXISTS idx_project_cleanup_jobs_project_id
ON project_cleanup_jobs(project_id);

CREATE INDEX IF NOT EXISTS idx_project_cleanup_jobs_locked_at
ON project_cleanup_jobs(locked_at);
