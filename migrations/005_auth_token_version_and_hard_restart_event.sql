-- Add user token revocation state and a distinct hard-restart event type.

ALTER TABLE users
ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE model_events
DROP CONSTRAINT IF EXISTS model_events_event_type_check;

ALTER TABLE model_events
ADD CONSTRAINT model_events_event_type_check CHECK (event_type IN (
  'deploy_requested',
  'k8s_namespace_created',
  'k8s_deployment_created',
  'k8s_service_created',
  'hpa_created',
  'model_loading',
  'model_running',
  'model_stopped',
  'model_started',
  'model_hard_restarted',
  'model_updated',
  'model_scaled',
  'model_status_synced',
  'model_failed',
  'model_deleted'
));
