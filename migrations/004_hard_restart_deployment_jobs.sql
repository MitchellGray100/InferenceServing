-- Add a force-recreate lifecycle job for model deployments.

ALTER TABLE deployment_jobs
DROP CONSTRAINT IF EXISTS deployment_jobs_job_type_check;

ALTER TABLE deployment_jobs
ADD CONSTRAINT deployment_jobs_job_type_check CHECK (job_type IN (
  'deploy_model',
  'update_model',
  'start_model',
  'stop_model',
  'hard_restart_model',
  'scale_model',
  'delete_model',
  'sync_status'
));
