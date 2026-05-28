-- Project and membership queries.
--
-- Expected scope:
-- - create/list/get/delete projects
-- - create owner membership during project creation
-- - check project role for authorization decisions

-- name: create_project
INSERT INTO projects (name, slug, k8s_namespace)
VALUES (%(name)s, %(slug)s, %(k8s_namespace)s)
RETURNING project_id, name, slug, k8s_namespace, created_at;

-- name: create_project_member
INSERT INTO project_members (project_id, user_id, role)
VALUES (%(project_id)s, %(user_id)s, %(role)s)
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: list_projects_for_user
SELECT
  p.project_id,
  p.name,
  p.slug,
  p.k8s_namespace,
  p.created_at,
  pm.role
FROM projects p
JOIN project_members pm ON pm.project_id = p.project_id
WHERE pm.user_id = %(user_id)s
ORDER BY p.created_at DESC;

-- name: get_project_for_user
SELECT
  p.project_id,
  p.name,
  p.slug,
  p.k8s_namespace,
  p.created_at,
  pm.role
FROM projects p
JOIN project_members pm ON pm.project_id = p.project_id
WHERE p.project_id = %(project_id)s
  AND pm.user_id = %(user_id)s;

-- name: get_project_member_role
SELECT role
FROM project_members
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s;

-- name: list_project_members
SELECT
  u.user_id,
  u.email,
  pm.role,
  pm.created_at
FROM project_members pm
JOIN users u ON u.user_id = pm.user_id
WHERE pm.project_id = %(project_id)s
ORDER BY pm.created_at ASC;

-- name: add_project_member_by_email
INSERT INTO project_members (project_id, user_id, role)
SELECT %(project_id)s, u.user_id, %(role)s
FROM users u
WHERE u.email = %(email)s
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: update_project_member_role
UPDATE project_members
SET role = %(role)s
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: remove_project_member
DELETE FROM project_members
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s
RETURNING project_member_id;

-- name: count_project_owners
SELECT COUNT(*) AS owner_count
FROM project_members
WHERE project_id = %(project_id)s
  AND role = 'owner';

-- name: delete_project
DELETE FROM projects
WHERE project_id = %(project_id)s
RETURNING project_id;
