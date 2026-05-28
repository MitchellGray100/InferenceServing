-- User account queries.
--
-- Expected scope:
-- - create users
-- - find users by email or user_id
-- - update last_login_at
-- - soft-delete or disable the authenticated user

-- name: create_user
INSERT INTO users (email, hashed_password)
VALUES (%(email)s, %(hashed_password)s)
RETURNING user_id, email, created_at, last_login_at;

-- name: get_user_by_id
SELECT user_id, email, created_at, last_login_at
FROM users
WHERE user_id = %(user_id)s;

-- name: get_user_auth_by_email
SELECT user_id, email, hashed_password, created_at, last_login_at
FROM users
WHERE email = %(email)s;

-- name: get_user_by_email
SELECT user_id, email, created_at, last_login_at
FROM users
WHERE email = %(email)s;

-- name: update_user_last_login
UPDATE users
SET last_login_at = CURRENT_TIMESTAMP
WHERE user_id = %(user_id)s
RETURNING user_id, email, created_at, last_login_at;

-- name: delete_user
DELETE FROM users
WHERE user_id = %(user_id)s
RETURNING user_id;
