-- Project API key queries.
--
-- Expected scope:
-- - create project-scoped API key metadata
-- - list key metadata without exposing key_hash
-- - revoke keys
-- - find active keys by key_prefix during verification

-- name: create_api_key
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
SELECT api_key_id, project_id, name, key_prefix, created_at, last_used_at, revoked_at
FROM api_keys
WHERE project_id = %(project_id)s
ORDER BY created_at DESC;

-- name: find_active_api_keys_by_prefix
SELECT api_key_id, project_id, key_hash, revoked_at
FROM api_keys
WHERE key_prefix = %(key_prefix)s
  AND revoked_at IS NULL;

-- name: update_api_key_last_used
UPDATE api_keys
SET last_used_at = CURRENT_TIMESTAMP
WHERE api_key_id = %(api_key_id)s
RETURNING api_key_id, last_used_at;

-- name: revoke_api_key
UPDATE api_keys
SET revoked_at = CURRENT_TIMESTAMP
WHERE api_key_id = %(api_key_id)s
  AND project_id = %(project_id)s
  AND revoked_at IS NULL
RETURNING api_key_id, revoked_at;
