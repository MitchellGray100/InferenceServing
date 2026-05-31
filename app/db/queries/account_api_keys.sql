-- Account API key queries.
--
-- Account keys authenticate user-level automation. They are deleted with the
-- owning user through the account_api_keys.user_id foreign key.

-- name: create_account_api_key
INSERT INTO account_api_keys (
  user_id,
  name,
  key_prefix,
  key_hash
)
VALUES (
  %(user_id)s,
  %(name)s,
  %(key_prefix)s,
  %(key_hash)s
)
RETURNING account_api_key_id, user_id, name, key_prefix, created_at, last_used_at, revoked_at;

-- name: list_account_api_keys
SELECT account_api_key_id, user_id, name, key_prefix, created_at, last_used_at, revoked_at
FROM account_api_keys
WHERE user_id = %(user_id)s
ORDER BY created_at DESC;

-- name: find_active_account_api_keys_by_prefix
SELECT account_api_key_id, user_id, key_prefix, key_hash, revoked_at
FROM account_api_keys
WHERE key_prefix = %(key_prefix)s
  AND revoked_at IS NULL;

-- name: update_account_api_key_last_used
UPDATE account_api_keys
SET last_used_at = CURRENT_TIMESTAMP
WHERE account_api_key_id = %(account_api_key_id)s
RETURNING account_api_key_id, last_used_at;

-- name: revoke_account_api_key
UPDATE account_api_keys
SET revoked_at = CURRENT_TIMESTAMP
WHERE account_api_key_id = %(account_api_key_id)s
  AND user_id = %(user_id)s
  AND revoked_at IS NULL
RETURNING account_api_key_id, revoked_at;
