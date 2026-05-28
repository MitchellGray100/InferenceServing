-- Idempotency key queries.
--
-- Expected scope:
-- - find existing idempotency records by project/user/key
-- - store normalized request hashes and replayable responses
-- - clean up expired idempotency records

-- name: get_idempotency_key
-- Look up an existing idempotency record for the exact project/user/key scope.
-- Matching records let retrying clients receive the original response.
SELECT *
FROM idempotency_keys
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s
  AND idempotency_key = %(idempotency_key)s;

-- name: create_idempotency_key
-- Store the normalized request hash and optional replay response for a
-- control-plane operation.
INSERT INTO idempotency_keys (
  project_id,
  user_id,
  idempotency_key,
  request_hash,
  response_status,
  response_body,
  expires_at
)
VALUES (
  %(project_id)s,
  %(user_id)s,
  %(idempotency_key)s,
  %(request_hash)s,
  %(response_status)s,
  %(response_body)s,
  %(expires_at)s
)
RETURNING *;

-- name: update_idempotency_response
-- Attach the final response after the protected operation succeeds, allowing
-- future retries with the same key to replay it.
UPDATE idempotency_keys
SET
  response_status = %(response_status)s,
  response_body = %(response_body)s
WHERE idempotency_key_id = %(idempotency_key_id)s
RETURNING *;

-- name: delete_expired_idempotency_keys
-- Remove old idempotency records after their replay window has passed.
DELETE FROM idempotency_keys
WHERE expires_at < CURRENT_TIMESTAMP
RETURNING idempotency_key_id;
