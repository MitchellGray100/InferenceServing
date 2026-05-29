-- Allow API key names to be reused after revocation while still preventing
-- duplicate active key names within one project.

ALTER TABLE api_keys
DROP CONSTRAINT IF EXISTS uq_api_keys_project_name;

CREATE UNIQUE INDEX uq_api_keys_project_active_name
ON api_keys(project_id, name)
WHERE revoked_at IS NULL;
