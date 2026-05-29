-- Project API key queries.
--
-- Expected scope:
-- - create project-scoped API key metadata
-- - list key metadata without exposing key_hash
-- - revoke keys
-- - find active keys by key_prefix during verification

-- name: create_api_key
-- Store project API key metadata. The raw key is returned to the caller once;
-- only key_prefix and the keyed hash are persisted.
INSERT INTO api_keys (
  project_id,
  name,
  key_prefix,
  key_hash,
  created_by_user_id
)
VALUES (
  %(project_id)s,
  %(name)s,
  %(key_prefix)s,
  %(key_hash)s,
  %(created_by_user_id)s
)
RETURNING api_key_id, project_id, name, key_prefix, created_at, last_used_at, revoked_at;

-- name: list_api_keys
-- List display-safe API key metadata for a project. key_hash is intentionally
-- omitted so read paths cannot expose credential material.
SELECT api_key_id, project_id, name, key_prefix, created_at, last_used_at, revoked_at
FROM api_keys
WHERE project_id = %(project_id)s
ORDER BY created_at DESC;

-- name: find_active_api_keys_by_prefix
-- Find active key candidates by visible prefix. The service still verifies the
-- full raw key against key_hash before authenticating the request.
SELECT api_key_id, project_id, key_prefix, key_hash, revoked_at
FROM api_keys
WHERE key_prefix = %(key_prefix)s
  AND revoked_at IS NULL;

-- name: update_api_key_last_used
-- Record successful API key authentication for dashboard visibility and basic
-- operational auditing.
UPDATE api_keys
SET last_used_at = CURRENT_TIMESTAMP
WHERE api_key_id = %(api_key_id)s
RETURNING api_key_id, last_used_at;

-- name: revoke_api_key
-- Soft-revoke a key so it can no longer authenticate while historical
-- inference rows can still reference its metadata.
UPDATE api_keys
SET revoked_at = CURRENT_TIMESTAMP
WHERE api_key_id = %(api_key_id)s
  AND project_id = %(project_id)s
  AND revoked_at IS NULL
RETURNING api_key_id, revoked_at;
