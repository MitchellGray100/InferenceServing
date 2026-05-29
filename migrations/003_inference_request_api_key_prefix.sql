ALTER TABLE inference_requests
ADD COLUMN IF NOT EXISTS api_key_prefix TEXT;

UPDATE inference_requests ir
SET api_key_prefix = ak.key_prefix
FROM api_keys ak
WHERE ir.api_key_id = ak.api_key_id
  AND ir.api_key_prefix IS NULL;
