-- Account-scoped API keys for user-level automation such as Truss commands.
CREATE TABLE IF NOT EXISTS account_api_keys (
  account_api_key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMP,
  revoked_at TIMESTAMP,

  CONSTRAINT uq_account_api_keys_key_hash UNIQUE(key_hash)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_api_keys_user_active_name
ON account_api_keys(user_id, name)
WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_account_api_keys_user_id
ON account_api_keys(user_id);

CREATE INDEX IF NOT EXISTS idx_account_api_keys_key_prefix
ON account_api_keys(key_prefix);
