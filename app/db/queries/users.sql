-- User account queries.
--
-- Expected scope:
-- - create users
-- - find users by email or user_id
-- - update last_login_at
-- - soft-delete or disable the authenticated user

-- name: create_user
-- Create a new user account. The service provides an already-hashed password;
-- raw plaintext passwords should never be inserted into this table.
INSERT INTO users (email, hashed_password)
VALUES (%(email)s, %(hashed_password)s)
RETURNING user_id, email, created_at, last_login_at;

-- name: get_user_by_id
-- Fetch public account fields for `/v1/users/me` and other user lookups.
-- The password hash is intentionally not selected here.
SELECT user_id, email, created_at, last_login_at
FROM users
WHERE user_id = %(user_id)s;

-- name: get_user_auth_by_email
-- Fetch login-only credential fields. This is the only user read path that
-- returns hashed_password so the auth service can verify a password.
SELECT user_id, email, hashed_password, created_at, last_login_at
FROM users
WHERE email = %(email)s;

-- name: get_user_by_email
-- Resolve an existing user by email for project membership management.
-- Membership creation does not implement invites in the MVP.
SELECT user_id, email, created_at, last_login_at
FROM users
WHERE email = %(email)s;

-- name: update_user_last_login
-- Record successful login time and return the updated public user fields.
UPDATE users
SET last_login_at = CURRENT_TIMESTAMP
WHERE user_id = %(user_id)s
RETURNING user_id, email, created_at, last_login_at;

-- name: delete_user
-- Delete the authenticated user account. Related rows rely on the foreign key
-- behavior defined in the schema.
DELETE FROM users
WHERE user_id = %(user_id)s
RETURNING user_id;
